"""Create and remove a bounded local browser-acceptance fixture.

The state file contains short-lived local credentials and must be kept outside
the repository. ``summary`` never emits credentials; browser automation reads
the mode-0600 state file directly so credentials never need to be printed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import uuid

from sqlalchemy import delete, or_, select, update

from app.core.security import hash_password_async
from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.audit import AuditLog
from app.models.identity_mfa import IdentityMfaChallenge, IdentityMfaRecoveryCode
from app.models.onboarding import UserTenantOnboarding
from app.models.tenant import Tenant
from app.models.tenant_deletion import (
    TenantDeletionHold,
    TenantDeletionJob,
    TenantDeletionTombstone,
)
from app.models.user import Identity, User
from app.services.mfa_service import generate_totp_secret, seal_mfa_secret


ROLES = (
    "owner",
    "admin",
    "member",
    "agent_manager",
    "second_owner",
    "platform",
)


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("state_file_already_exists")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported_state_file")
    return payload


async def _seed(path: Path) -> dict[str, object]:
    if path.exists():
        raise RuntimeError("state_file_already_exists")

    run_tag = uuid.uuid4().hex[:12]
    password = f"{secrets.token_urlsafe(24)}Aa1!"
    password_hash = await hash_password_async(password)
    now = datetime.now(UTC)

    identities: dict[str, Identity] = {}
    secrets_by_role: dict[str, str] = {}
    for role in ROLES:
        secret = generate_totp_secret()
        secrets_by_role[role] = secret
        identities[role] = Identity(
            email=f"browser-{role}-{run_tag}@local.clawith.test",
            username=f"browser{role.replace('_', '')}{run_tag}",
            password_hash=password_hash,
            password_login_enabled=True,
            email_verified=True,
            is_active=True,
            is_platform_admin=role == "platform",
            mfa_secret_envelope=seal_mfa_secret(secret),
            mfa_enabled=True,
            mfa_confirmed_at=now,
        )

    async with async_session() as db:
        db.add_all(identities.values())
        await db.flush()

        primary = Tenant(
            name=f"Browser Primary {run_tag}",
            slug=f"browser-primary-{run_tag}",
            im_provider="web_only",
            is_active=True,
            initialization_completed_at=now,
        )
        secondary = Tenant(
            name=f"Browser Secondary {run_tag}",
            slug=f"browser-secondary-{run_tag}",
            im_provider="web_only",
            is_active=True,
            initialization_completed_at=now,
        )
        purge = Tenant(
            name=f"Browser Purge Candidate {run_tag}",
            slug=f"g11-purge-browser-{run_tag}",
            im_provider="web_only",
            is_active=False,
            deletion_requested_at=now - timedelta(days=31),
            deletion_scheduled_for=now - timedelta(days=1),
        )
        db.add_all([primary, secondary, purge])
        await db.flush()

        users = {
            "owner": User(
                identity_id=identities["owner"].id,
                tenant_id=primary.id,
                display_name="Browser Owner",
                role="org_owner",
                is_active=True,
            ),
            "admin": User(
                identity_id=identities["admin"].id,
                tenant_id=primary.id,
                display_name="Browser Admin",
                role="org_admin",
                is_active=True,
            ),
            "member": User(
                identity_id=identities["member"].id,
                tenant_id=primary.id,
                display_name="Browser Member",
                role="member",
                is_active=True,
            ),
            "agent_manager": User(
                identity_id=identities["agent_manager"].id,
                tenant_id=primary.id,
                display_name="Browser Agent Manager",
                role="agent_admin",
                is_active=True,
            ),
            "second_owner": User(
                identity_id=identities["second_owner"].id,
                tenant_id=secondary.id,
                display_name="Browser Second Owner",
                role="org_owner",
                is_active=True,
            ),
            "platform": User(
                identity_id=identities["platform"].id,
                tenant_id=None,
                display_name="Browser Platform Operator",
                role="platform_admin",
                is_active=True,
            ),
        }
        db.add_all(users.values())
        await db.flush()
        primary.owner_user_id = users["owner"].id
        primary.initialized_by_user_id = users["owner"].id
        secondary.owner_user_id = users["second_owner"].id
        secondary.initialized_by_user_id = users["second_owner"].id

        assistant_template_result = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.role_key == "private-assistant",
                AgentTemplate.is_builtin.is_(True),
            )
        )
        assistant_template = assistant_template_result.scalar_one()
        agents = {
            "current_assistant": Agent(
                name="Browser Current Assistant",
                role_description="Private assistant acceptance fixture",
                creator_id=users["owner"].id,
                tenant_id=primary.id,
                status="idle",
                access_mode="private",
                company_access_level="use",
                template_id=assistant_template.id,
            ),
            "retained_assistant": Agent(
                name="Browser Previous Assistant",
                role_description="Retained assistant acceptance fixture",
                creator_id=users["owner"].id,
                tenant_id=primary.id,
                status="idle",
                access_mode="private",
                company_access_level="use",
                template_id=assistant_template.id,
            ),
            "managed_employee": Agent(
                name="Browser Managed Employee",
                role_description="Object-level management acceptance fixture",
                creator_id=users["owner"].id,
                tenant_id=primary.id,
                status="idle",
                access_mode="custom",
                company_access_level="use",
            ),
        }
        db.add_all(agents.values())
        await db.flush()
        db.add_all(
            [
                UserTenantOnboarding(
                    user_id=users["owner"].id,
                    tenant_id=primary.id,
                    status="completed",
                    current_step="complete",
                    entry_mode="create",
                    personal_assistant_agent_id=agents["current_assistant"].id,
                    completed_at=now,
                ),
                AgentPermission(
                    agent_id=agents["managed_employee"].id,
                    scope_type="user",
                    scope_id=users["agent_manager"].id,
                    access_level="manage",
                ),
            ]
        )
        db.add(
            TenantDeletionJob(
                tenant_id=purge.id,
                status="scheduled",
                eligible_at=purge.deletion_scheduled_for,
            )
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "run_tag": run_tag,
            "password": password,
            "identities": {
                role: {
                    "id": str(identity.id),
                    "email": identity.email,
                    "secret": secrets_by_role[role],
                    "user_id": str(users[role].id),
                }
                for role, identity in identities.items()
            },
            "tenants": {
                "primary": str(primary.id),
                "secondary": str(secondary.id),
                "purge": str(purge.id),
            },
            "agents": {name: str(agent.id) for name, agent in agents.items()},
        }
        # Persist the cleanup receipt before committing the fixture. If the
        # commit outcome is uncertain, the known IDs remain available to the
        # idempotent cleanup command instead of becoming orphaned test data.
        _write_state(path, payload)
        await db.commit()
    return {
        "status": "seeded",
        "run_tag": run_tag,
        "roles": list(ROLES),
        "tenant_count": 3,
    }


async def _cleanup(path: Path) -> dict[str, object]:
    payload = _read_state(path)
    identities = payload["identities"]
    tenants = payload["tenants"]
    agents = payload.get("agents", {})
    identity_ids = [uuid.UUID(value["id"]) for value in identities.values()]
    user_ids = [uuid.UUID(value["user_id"]) for value in identities.values()]
    tenant_ids = [uuid.UUID(value) for value in tenants.values()]
    agent_ids = [uuid.UUID(value) for value in agents.values()]

    async with async_session() as db:
        await db.execute(
            delete(AuditLog).where(
                or_(AuditLog.user_id.in_(user_ids), AuditLog.tenant_id.in_(tenant_ids))
            )
        )
        await db.execute(
            delete(IdentityMfaChallenge).where(
                IdentityMfaChallenge.identity_id.in_(identity_ids)
            )
        )
        await db.execute(
            delete(IdentityMfaRecoveryCode).where(
                IdentityMfaRecoveryCode.identity_id.in_(identity_ids)
            )
        )
        await db.execute(
            delete(TenantDeletionHold).where(TenantDeletionHold.tenant_id.in_(tenant_ids))
        )
        await db.execute(
            delete(TenantDeletionJob).where(TenantDeletionJob.tenant_id.in_(tenant_ids))
        )
        await db.execute(
            delete(TenantDeletionTombstone).where(
                TenantDeletionTombstone.tenant_id.in_(tenant_ids)
            )
        )
        await db.execute(
            update(Tenant)
            .where(Tenant.id.in_(tenant_ids))
            .values(owner_user_id=None, initialized_by_user_id=None, deletion_requested_by_user_id=None)
        )
        await db.execute(
            delete(UserTenantOnboarding).where(
                or_(
                    UserTenantOnboarding.user_id.in_(user_ids),
                    UserTenantOnboarding.tenant_id.in_(tenant_ids),
                )
            )
        )
        if agent_ids:
            await db.execute(
                delete(AgentPermission).where(AgentPermission.agent_id.in_(agent_ids))
            )
            await db.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        await db.execute(delete(Identity).where(Identity.id.in_(identity_ids)))
        await db.commit()

    path.unlink(missing_ok=True)
    return {
        "status": "cleaned",
        "identities": len(identity_ids),
        "tenants": len(tenant_ids),
        "agents": len(agent_ids),
    }


def _summary(path: Path) -> dict[str, object]:
    payload = _read_state(path)
    return {
        "status": "present",
        "run_tag": payload["run_tag"],
        "roles": list(payload["identities"].keys()),
        "tenant_count": len(payload["tenants"]),
        "agent_count": len(payload.get("agents", {})),
    }


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "cleanup", "summary"))
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "seed":
            result = await _seed(args.state_file)
        elif args.command == "cleanup":
            result = await _cleanup(args.state_file)
        elif args.command == "summary":
            result = _summary(args.state_file)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
