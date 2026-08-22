"""Create and remove a bounded local browser-acceptance fixture.

The state file contains short-lived local credentials and must be kept outside
the repository. ``summary`` never emits credentials; browser automation reads
the mode-0600 state file directly so credentials never need to be printed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.subscription_lifecycle import ensure_free_subscription_for_tenant
from app.services.tenant_purge import TenantRowPlanner


ROLES = (
    "owner",
    "admin",
    "member",
    "agent_manager",
    "second_owner",
    "platform",
)
_RUN_TAG_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_TENANT_SLUG_PREFIXES = {
    "primary": "browser-primary",
    "secondary": "browser-secondary",
    "purge": "g11-purge-browser",
}
_USER_DISPLAY_NAMES = {
    "owner": "Browser Owner",
    "admin": "Browser Admin",
    "member": "Browser Member",
    "agent_manager": "Browser Agent Manager",
    "second_owner": "Browser Second Owner",
    "platform": "Browser Platform Operator",
}
_USER_ROLES = {
    "owner": "org_owner",
    "admin": "org_admin",
    "member": "member",
    "agent_manager": "agent_admin",
    "second_owner": "org_owner",
    "platform": "platform_admin",
}
_AGENT_KEYS = frozenset(
    {"current_assistant", "retained_assistant", "managed_employee"}
)


@dataclass(frozen=True, slots=True)
class _CleanupScope:
    run_tag: str
    identity_emails: dict[uuid.UUID, str]
    users: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID | None, str, str]]
    tenant_slugs: dict[uuid.UUID, str]
    agent_ids: tuple[uuid.UUID, ...]
    fixture_template_id: uuid.UUID | None


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


def _fixture_uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid_fixture_state:{field}") from exc


def _cleanup_scope(payload: Mapping[str, object]) -> _CleanupScope:
    """Parse the cleanup receipt and bind every destructive ID to fixture names."""

    run_tag = payload.get("run_tag")
    if not isinstance(run_tag, str) or _RUN_TAG_PATTERN.fullmatch(run_tag) is None:
        raise RuntimeError("invalid_fixture_state:run_tag")

    identities = payload.get("identities")
    if not isinstance(identities, Mapping) or set(identities) != set(ROLES):
        raise RuntimeError("invalid_fixture_state:identities")
    identity_emails: dict[uuid.UUID, str] = {}
    user_ids_by_role: dict[str, uuid.UUID] = {}
    identity_ids_by_role: dict[str, uuid.UUID] = {}
    for role in ROLES:
        details = identities[role]
        if not isinstance(details, Mapping):
            raise RuntimeError(f"invalid_fixture_state:identity:{role}")
        expected_email = f"browser-{role}-{run_tag}@local.clawith.test"
        if details.get("email") != expected_email:
            raise RuntimeError(f"fixture_ownership_mismatch:identity:{role}")
        identity_id = _fixture_uuid(details.get("id"), f"identity:{role}:id")
        user_id = _fixture_uuid(details.get("user_id"), f"identity:{role}:user_id")
        identity_emails[identity_id] = expected_email
        identity_ids_by_role[role] = identity_id
        user_ids_by_role[role] = user_id
    if len(identity_emails) != len(ROLES) or len(set(user_ids_by_role.values())) != len(
        ROLES
    ):
        raise RuntimeError("invalid_fixture_state:duplicate_identity_or_user")

    tenants = payload.get("tenants")
    if not isinstance(tenants, Mapping) or set(tenants) != set(
        _TENANT_SLUG_PREFIXES
    ):
        raise RuntimeError("invalid_fixture_state:tenants")
    tenant_ids_by_name = {
        name: _fixture_uuid(tenants[name], f"tenant:{name}")
        for name in _TENANT_SLUG_PREFIXES
    }
    if len(set(tenant_ids_by_name.values())) != len(_TENANT_SLUG_PREFIXES):
        raise RuntimeError("invalid_fixture_state:duplicate_tenant")
    tenant_slugs = {
        tenant_ids_by_name[name]: f"{prefix}-{run_tag}"
        for name, prefix in _TENANT_SLUG_PREFIXES.items()
    }

    tenant_for_role = {
        "owner": tenant_ids_by_name["primary"],
        "admin": tenant_ids_by_name["primary"],
        "member": tenant_ids_by_name["primary"],
        "agent_manager": tenant_ids_by_name["primary"],
        "second_owner": tenant_ids_by_name["secondary"],
        "platform": None,
    }
    users = {
        user_ids_by_role[role]: (
            identity_ids_by_role[role],
            tenant_for_role[role],
            _USER_DISPLAY_NAMES[role],
            _USER_ROLES[role],
        )
        for role in ROLES
    }
    additional_memberships = payload.get("additional_memberships")
    if not isinstance(additional_memberships, Mapping) or set(
        additional_memberships
    ) != {"owner_secondary"}:
        raise RuntimeError("invalid_fixture_state:additional_memberships")
    owner_secondary_id = _fixture_uuid(
        additional_memberships["owner_secondary"],
        "additional_memberships:owner_secondary",
    )
    if owner_secondary_id in users:
        raise RuntimeError("invalid_fixture_state:duplicate_user")
    users[owner_secondary_id] = (
        identity_ids_by_role["owner"],
        tenant_ids_by_name["secondary"],
        "Browser Owner (Secondary Member)",
        "member",
    )

    agents = payload.get("agents")
    if not isinstance(agents, Mapping) or set(agents) != _AGENT_KEYS:
        raise RuntimeError("invalid_fixture_state:agents")
    agent_ids = tuple(
        _fixture_uuid(agents[name], f"agent:{name}") for name in sorted(_AGENT_KEYS)
    )
    if len(set(agent_ids)) != len(_AGENT_KEYS):
        raise RuntimeError("invalid_fixture_state:duplicate_agent")

    global_rows = payload.get("fixture_global_rows")
    if not isinstance(global_rows, Mapping) or set(global_rows) != {
        "assistant_template"
    }:
        raise RuntimeError("invalid_fixture_state:fixture_global_rows")
    fixture_template_value = global_rows.get("assistant_template")
    fixture_template_id = (
        _fixture_uuid(fixture_template_value, "assistant_template")
        if fixture_template_value is not None
        else None
    )
    return _CleanupScope(
        run_tag=run_tag,
        identity_emails=identity_emails,
        users=users,
        tenant_slugs=tenant_slugs,
        agent_ids=agent_ids,
        fixture_template_id=fixture_template_id,
    )


def _assert_fixture_rows_owned(
    kind: str,
    *,
    expected: Mapping[uuid.UUID, object],
    actual: Mapping[uuid.UUID, object],
) -> None:
    # Missing rows are safe and support an idempotent retry after external test
    # activity. Any present row must still carry the exact fixture provenance.
    if any(expected.get(row_id) != value for row_id, value in actual.items()):
        raise RuntimeError(f"fixture_ownership_mismatch:{kind}")


async def _validate_cleanup_ownership(
    db: AsyncSession,
    scope: _CleanupScope,
) -> None:
    identity_result = await db.execute(
        select(Identity.id, Identity.email).where(
            Identity.id.in_(scope.identity_emails)
        )
    )
    _assert_fixture_rows_owned(
        "identities",
        expected=scope.identity_emails,
        actual={row.id: row.email for row in identity_result.all()},
    )

    user_result = await db.execute(
        select(
            User.id,
            User.identity_id,
            User.tenant_id,
            User.display_name,
            User.role,
        ).where(User.id.in_(scope.users))
    )
    _assert_fixture_rows_owned(
        "users",
        expected=scope.users,
        actual={
            row.id: (row.identity_id, row.tenant_id, row.display_name, row.role)
            for row in user_result.all()
        },
    )

    tenant_result = await db.execute(
        select(Tenant.id, Tenant.slug).where(Tenant.id.in_(scope.tenant_slugs))
    )
    _assert_fixture_rows_owned(
        "tenants",
        expected=scope.tenant_slugs,
        actual={row.id: row.slug for row in tenant_result.all()},
    )

    if scope.fixture_template_id is not None:
        template_result = await db.execute(
            select(AgentTemplate).where(
                AgentTemplate.id == scope.fixture_template_id
            )
        )
        template = template_result.scalar_one_or_none()
        if template is not None and (
            template.name != f"Browser Private Assistant {scope.run_tag}"
            or not isinstance(template.source_provenance, Mapping)
            or template.source_provenance.get("source")
            != "browser_acceptance_fixture"
        ):
            raise RuntimeError("fixture_ownership_mismatch:assistant_template")


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

        # Keep browser acceptance tenants on the same commercial initialization
        # path as self-service and operator-created companies.  Without a real
        # subscription + credit grant the Work preflight quite correctly sees
        # no entitled model route, so an identity-only fixture cannot prove the
        # business workflow it is meant to exercise.
        await ensure_free_subscription_for_tenant(db, primary.id)
        await ensure_free_subscription_for_tenant(db, secondary.id)

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
        owner_secondary_membership = User(
            identity_id=identities["owner"].id,
            tenant_id=secondary.id,
            display_name="Browser Owner (Secondary Member)",
            role="member",
            is_active=True,
        )
        db.add(owner_secondary_membership)
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
        assistant_template = assistant_template_result.scalar_one_or_none()
        fixture_template_id: uuid.UUID | None = None
        if assistant_template is None:
            # A production-like database is expected to have the built-in
            # private-assistant template, while migration smoke starts from an
            # intentionally empty schema. Create one bounded global dependency
            # for that case and record it in the cleanup receipt; never mutate
            # or delete an existing product template.
            assistant_template = AgentTemplate(
                name=f"Browser Private Assistant {run_tag}",
                description="Private assistant browser acceptance dependency",
                icon="assistant",
                category="assistant",
                soul_template="Browser acceptance fixture only.",
                default_skills=[],
                default_tools=[],
                default_mcp_servers=[],
                default_autonomy_policy={},
                capability_bullets=[],
                role_key="private-assistant",
                role_revision=1,
                responsibilities=[],
                non_responsibilities=[],
                limitations=[],
                workflows=[],
                deliverables=[],
                evaluation_criteria=[],
                source_provenance={"source": "browser_acceptance_fixture"},
                lifecycle_status="enabled",
                is_builtin=True,
            )
            db.add(assistant_template)
            await db.flush()
            fixture_template_id = assistant_template.id
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
            "additional_memberships": {
                "owner_secondary": str(owner_secondary_membership.id),
            },
            "fixture_global_rows": {
                "assistant_template": (
                    str(fixture_template_id) if fixture_template_id is not None else None
                ),
            },
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
    scope = _cleanup_scope(payload)
    identity_ids = list(scope.identity_emails)
    user_ids = list(scope.users)
    tenant_ids = list(scope.tenant_slugs)

    async with async_session() as db:
        # Fail closed before the first DELETE. A valid mode-0600 state file is
        # necessary but not sufficient: each present target row must still carry
        # the exact fixture email/slug/profile/provenance written by ``seed``.
        await _validate_cleanup_ownership(db, scope)

        # Identity-scoped rows are intentionally outside the tenant purge graph.
        # Remove only rows owned by this bounded fixture before deleting the
        # fixture identities after all tenant rows are gone.
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

        # Browser acceptance can create far more than the seed rows (Groups,
        # sessions, tasks, subscriptions, Credits ledgers, and future
        # tenant-owned tables). Reuse the production tenant ownership planner
        # instead of maintaining a second, inevitably incomplete delete list.
        # The surrounding transaction makes cleanup all-or-nothing; the state
        # file remains available for an idempotent retry after any failure.
        for tenant_id in tenant_ids:
            planner = TenantRowPlanner(db, tenant_id)
            await planner.build()
            await planner.delete_planned_rows()

        if scope.fixture_template_id is not None:
            await db.execute(
                delete(AgentTemplate).where(
                    AgentTemplate.id == scope.fixture_template_id
                )
            )

        # The platform fixture user is deliberately tenantless and therefore
        # not part of any tenant plan. Tenant-scoped fixture users have already
        # been deleted, so this statement normally removes only that user.
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.execute(delete(Identity).where(Identity.id.in_(identity_ids)))
        await db.commit()

    path.unlink(missing_ok=True)
    return {
        "status": "cleaned",
        "identities": len(identity_ids),
        "tenants": len(tenant_ids),
        "agents": len(scope.agent_ids),
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
