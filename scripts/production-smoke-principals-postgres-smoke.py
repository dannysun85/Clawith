#!/usr/bin/env python3
"""PostgreSQL integration smoke for the production smoke-principal lifecycle."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import uuid

from sqlalchemy import delete, select, update

from app.core.security import verify_password_async
from app.database import async_session
from app.models.audit import AuditLog
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User


REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = REPO_ROOT / "backend/scripts/manage_production_smoke_principals.py"


def _load_manager():
    spec = importlib.util.spec_from_file_location(
        "production_smoke_principal_manager_integration",
        MANAGER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("principal_manager_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _seed(manager, run_tag: str) -> tuple[object, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    owner_identity_id = uuid.uuid4()
    credentials = manager.parse_credentials(
        {
            "SMOKE_TENANT_EMAIL": f"release-owner-{run_tag}@example.invalid",
            "SMOKE_TENANT_PASSWORD": f"Owner-{run_tag}-Release-Smoke-Password-001!",
            "SMOKE_TENANT_ID": str(tenant_id),
            "SMOKE_PLATFORM_ADMIN_EMAIL": (
                f"release-smoke-platform-{run_tag}@release-smoke.invalid"
            ),
            "SMOKE_PLATFORM_ADMIN_PASSWORD": (
                f"Platform-{run_tag}-Release-Smoke-Password-002!"
            ),
            "SMOKE_MEMBER_EMAIL": f"release-smoke-member-{run_tag}@release-smoke.invalid",
            "SMOKE_MEMBER_PASSWORD": f"Member-{run_tag}-Release-Smoke-Password-003!",
        }
    )
    async with async_session() as db:
        async with db.begin():
            owner_identity = Identity(
                id=owner_identity_id,
                email=credentials.tenant_email,
                username=f"release_smoke_owner_{run_tag}",
                password_hash="retired-password-hash",
                password_login_enabled=False,
                auth_version=7,
                email_verified=True,
                is_active=True,
                is_platform_admin=False,
                mfa_enabled=False,
            )
            db.add(owner_identity)
            await db.flush()
            tenant = Tenant(
                id=tenant_id,
                name=f"Release QA Integration {run_tag}",
                slug=f"release-qa-integration-{run_tag}",
                im_provider="web_only",
                is_active=True,
            )
            db.add(tenant)
            await db.flush()
            owner = User(
                identity_id=owner_identity.id,
                tenant_id=tenant.id,
                display_name="Release QA Integration Owner",
                role="org_owner",
                is_active=True,
                registration_source=manager.RELEASE_SMOKE_SOURCE,
            )
            db.add(owner)
            await db.flush()
            tenant.owner_user_id = owner.id
    return credentials, tenant_id, owner_identity_id


async def _assert_ready(manager, credentials, *, platform_ready: bool) -> None:
    state = await manager.inventory(credentials)
    principals = state["principals"]
    if not principals["owner"]["login_ready"]:
        raise RuntimeError("release_smoke_owner_not_ready")
    if not principals["member"]["login_ready"] or not principals["member"]["ordinary_member"]:
        raise RuntimeError("release_smoke_member_not_ready")
    if principals["platform"]["login_ready"] is not platform_ready:
        raise RuntimeError("release_smoke_platform_state_mismatch")
    if principals["platform"]["authority_active"] is not platform_ready:
        raise RuntimeError("release_smoke_platform_authority_state_mismatch")
    if principals["platform"]["present"] and not principals["platform"]["tenantless"]:
        raise RuntimeError("release_smoke_platform_not_tenantless")


async def _principal_versions(credentials) -> dict[str, int]:
    async with async_session() as db:
        identities = list(
            (
                await db.execute(
                    select(Identity).where(
                        Identity.email.in_(
                            {
                                credentials.tenant_email,
                                credentials.member_email,
                                credentials.platform_email,
                            }
                        )
                    )
                )
            ).scalars().all()
        )
        return {identity.email or "": int(identity.auth_version or 0) for identity in identities}


async def _assert_database_contract(manager, credentials, tenant_id: uuid.UUID) -> None:
    async with async_session() as db:
        identities = list(
            (
                await db.execute(
                    select(Identity).where(
                        Identity.email.in_(
                            {
                                credentials.tenant_email,
                                credentials.member_email,
                                credentials.platform_email,
                            }
                        )
                    )
                )
            ).scalars().all()
        )
        by_email = {identity.email: identity for identity in identities}
        if set(by_email) != {
            credentials.tenant_email,
            credentials.member_email,
            credentials.platform_email,
        }:
            raise RuntimeError("release_smoke_identity_set_mismatch")
        for email, password in (
            (credentials.tenant_email, credentials.tenant_password),
            (credentials.member_email, credentials.member_password),
            (credentials.platform_email, credentials.platform_password),
        ):
            identity = by_email[email]
            if not identity.password_hash or not await verify_password_async(
                password,
                identity.password_hash,
            ):
                raise RuntimeError("release_smoke_password_rotation_failed")

        platform_identity = by_email[credentials.platform_email]
        platform_users = list(
            (
                await db.execute(
                    select(User).where(User.identity_id == platform_identity.id)
                )
            ).scalars().all()
        )
        if (
            len(platform_users) != 1
            or platform_users[0].tenant_id is not None
            or platform_users[0].role != "platform_admin"
            or platform_users[0].registration_source != manager.RELEASE_SMOKE_SOURCE
        ):
            raise RuntimeError("release_smoke_platform_boundary_failed")

        tenant_users = list(
            (
                await db.execute(select(User).where(User.tenant_id == tenant_id))
            ).scalars().all()
        )
        roles = sorted(user.role for user in tenant_users)
        if roles != ["member", "org_owner"]:
            raise RuntimeError("release_smoke_tenant_role_matrix_failed")

        receipts = list(
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.tenant_id == tenant_id)
                )
            ).scalars().all()
        )
        serialized_receipts = json.dumps(
            [receipt.details for receipt in receipts],
            sort_keys=True,
        )
        forbidden_values = {
            credentials.tenant_email,
            credentials.member_email,
            credentials.platform_email,
            credentials.tenant_password,
            credentials.member_password,
            credentials.platform_password,
        }
        if any(value in serialized_receipts for value in forbidden_values):
            raise RuntimeError("release_smoke_receipt_leaked_credentials")


async def _cleanup(credentials, tenant_id: uuid.UUID, owner_identity_id: uuid.UUID) -> None:
    async with async_session() as db:
        async with db.begin():
            identity_ids = list(
                (
                    await db.execute(
                        select(Identity.id).where(
                            Identity.email.in_(
                                {
                                    credentials.tenant_email,
                                    credentials.member_email,
                                    credentials.platform_email,
                                }
                            )
                        )
                    )
                ).scalars().all()
            )
            all_identity_ids = set(identity_ids + [owner_identity_id])
            user_ids = list(
                (
                    await db.execute(
                        select(User.id).where(User.identity_id.in_(all_identity_ids))
                    )
                ).scalars().all()
            )
            await db.execute(
                update(Tenant).where(Tenant.id == tenant_id).values(owner_user_id=None)
            )
            if user_ids:
                await db.execute(
                    delete(Participant).where(
                        Participant.type == "user",
                        Participant.ref_id.in_(user_ids),
                    )
                )
            await db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
            if all_identity_ids:
                await db.execute(delete(User).where(User.identity_id.in_(all_identity_ids)))
                await db.execute(delete(Identity).where(Identity.id.in_(all_identity_ids)))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def main() -> None:
    manager = _load_manager()
    run_tag = uuid.uuid4().hex[:10]
    credentials, tenant_id, owner_identity_id = await _seed(manager, run_tag)
    provision_operation = uuid.uuid4()
    deactivate_operation = uuid.uuid4()
    reprovision_operation = uuid.uuid4()
    try:
        initial = await manager.inventory(credentials)
        if initial["principals"]["owner"]["login_ready"]:
            raise RuntimeError("release_smoke_owner_unexpectedly_ready")
        if initial["principals"]["member"]["present"]:
            raise RuntimeError("release_smoke_member_unexpectedly_present")
        if initial["principals"]["platform"]["present"]:
            raise RuntimeError("release_smoke_platform_unexpectedly_present")

        concurrent = await asyncio.gather(
            manager.provision(
                credentials,
                operation_id=provision_operation,
                release_version="1.12.0",
            ),
            manager.provision(
                credentials,
                operation_id=provision_operation,
                release_version="1.12.0",
            ),
        )
        if sorted(result["status"] for result in concurrent) != [
            "already_applied",
            "applied",
        ]:
            raise RuntimeError("release_smoke_operation_not_exactly_once")
        await _assert_ready(manager, credentials, platform_ready=True)
        await _assert_database_contract(manager, credentials, tenant_id)

        versions_before_replay = await _principal_versions(credentials)
        replay = await manager.provision(
            credentials,
            operation_id=provision_operation,
            release_version="1.12.0",
        )
        if replay["status"] != "already_applied":
            raise RuntimeError("release_smoke_replay_not_detected")
        if await _principal_versions(credentials) != versions_before_replay:
            raise RuntimeError("release_smoke_replay_rotated_credentials")

        deactivated = await manager.deactivate_platform(
            credentials,
            operation_id=deactivate_operation,
            release_version="1.12.0",
        )
        if deactivated["status"] != "applied":
            raise RuntimeError("release_smoke_platform_deactivation_failed")
        await _assert_ready(manager, credentials, platform_ready=False)

        reprovisioned = await manager.provision(
            credentials,
            operation_id=reprovision_operation,
            release_version="1.12.0",
        )
        if reprovisioned["status"] != "applied":
            raise RuntimeError("release_smoke_platform_reprovision_failed")
        await _assert_ready(manager, credentials, platform_ready=True)
    finally:
        await _cleanup(credentials, tenant_id, owner_identity_id)

    print(
        json.dumps(
            {
                "ok": True,
                "checks": [
                    "release_qa_boundary",
                    "concurrent_exactly_once",
                    "owner_rotation",
                    "ordinary_member_scope",
                    "tenantless_platform_scope",
                    "receipt_redaction",
                    "platform_deactivation",
                    "reprovision",
                    "cleanup",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
