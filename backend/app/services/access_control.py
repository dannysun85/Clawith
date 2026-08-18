"""Authoritative identity, membership, product-surface, and capability resolution.

The product has three independent authorization axes:

* global Identity authority (for example ``platform_operator``);
* the active organization membership role;
* object grants such as ``AgentPermission(use|manage)``.

This module deliberately does not infer one axis from another.  In particular,
platform authority never turns into tenant administration and ``agent_admin``
is treated as a legacy membership value rather than a source of Agent access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentPermission
from app.models.identity_governance import (
    IdentityCapabilityGrant,
    OrganizationInvitation,
    PlatformSupportSession,
)
from app.models.user import User
from app.models.tenant import Tenant


GLOBAL_ROLE_PLATFORM_OPERATOR = "platform_operator"

SURFACE_WORK = "work"
SURFACE_COMPANY_ADMIN = "company_admin"
SURFACE_PLATFORM_ADMIN = "platform_admin"

MEMBERSHIP_ROLES = frozenset({"member", "org_admin", "org_owner"})
COMPANY_GOVERNANCE_ROLES = frozenset({"org_admin", "org_owner"})

# IdentityCapabilityGrant is account-global.  Only capabilities whose product
# meaning is explicitly global may be projected from it.  Tenant finance,
# analytics, OKR, settings, or other company authority must come from the
# active membership (or a future tenant-scoped grant model), never from a
# grant attached to the same Identity in another company.
GLOBAL_IDENTITY_CAPABILITIES = frozenset({"company.create"})

MEMBER_CAPABILITIES = frozenset(
    {
        "work.use",
        "company.view",
        "agent.use",
    }
)

COMPANY_ADMIN_CAPABILITIES = frozenset(
    {
        "company.members.view",
        "company.members.invite",
        "company.members.manage",
        "company.settings.manage",
        "company.audit.view",
        "company.billing.view",
        "company.analytics.view",
        "company.okr.view_all",
        "company.okr.reports.view_all",
        "company.okr.manage",
        "agent.create.company",
        "agent.manage.company",
    }
)

COMPANY_OWNER_CAPABILITIES = frozenset(
    {
        "company.admins.manage",
        "company.ownership.transfer",
        "company.delete",
        "company.billing.manage",
    }
)

PLATFORM_OPERATOR_CAPABILITIES = frozenset(
    {
        "platform.tenants.manage",
        "platform.registration.manage",
        "platform.billing.manage",
        "platform.providers.manage",
        "platform.support_session.create",
    }
)


@dataclass(frozen=True, slots=True)
class EffectiveAccess:
    """One response-safe authorization snapshot for the current request."""

    membership_id: uuid.UUID | None
    membership_role: str | None
    global_roles: tuple[str, ...]
    effective_capabilities: tuple[str, ...]
    available_surfaces: tuple[str, ...]
    pending_invitation_count: int
    current_support_session: dict[str, Any] | None


def normalized_membership_role(user: User | object | None) -> str | None:
    """Return only a real tenant membership role.

    ``platform_admin`` is a legacy anchor role and ``agent_admin`` is a legacy
    delegation role.  Neither may silently grant organization authority.
    """

    if user is None or getattr(user, "tenant_id", None) is None:
        return None
    role = str(getattr(user, "role", "") or "")
    return role if role in MEMBERSHIP_ROLES else "member"


def is_platform_operator(user: User | object | None) -> bool:
    identity = getattr(user, "identity", None)
    return bool(identity and getattr(identity, "is_platform_admin", False))


def is_company_governor(user: User | object | None) -> bool:
    return normalized_membership_role(user) in COMPANY_GOVERNANCE_ROLES


def is_company_owner(user: User | object | None) -> bool:
    return normalized_membership_role(user) == "org_owner"


async def _identity_capabilities(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID | None,
) -> set[str]:
    if identity_id is None:
        return set()
    result = await db.execute(
        select(IdentityCapabilityGrant.capability).where(
            IdentityCapabilityGrant.identity_id == identity_id,
            IdentityCapabilityGrant.revoked_at.is_(None),
        )
    )
    return {str(value) for value in result.scalars().all()}


async def _has_managed_agent(db: AsyncSession, user: User) -> bool:
    """Whether the membership owns or has an explicit manage grant.

    The boolean only drives the availability of the "managed Agents" surface;
    every Agent request still re-evaluates its concrete object permission.
    """

    if user.tenant_id is None:
        return False
    result = await db.execute(
        select(Agent.id)
        .outerjoin(
            AgentPermission,
            and_(
                AgentPermission.agent_id == Agent.id,
                AgentPermission.scope_type == "user",
                AgentPermission.scope_id == user.id,
                AgentPermission.access_level == "manage",
            ),
        )
        .where(
            Agent.tenant_id == user.tenant_id,
            Agent.deleted_at.is_(None),
            or_(Agent.creator_id == user.id, AgentPermission.id.is_not(None)),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _member_private_agent_creation_allowed(db: AsyncSession, user: User) -> bool:
    if user.tenant_id is None:
        return False
    result = await db.execute(
        select(Tenant.allow_member_private_agents).where(Tenant.id == user.tenant_id)
    )
    return bool(result.scalar_one_or_none())


async def _pending_invitation_count(
    db: AsyncSession,
    *,
    canonical_email: str | None,
) -> int:
    if not canonical_email:
        return 0
    now = datetime.now(UTC)
    result = await db.execute(
        select(func.count(OrganizationInvitation.id)).where(
            OrganizationInvitation.target_email == canonical_email.strip().lower(),
            OrganizationInvitation.status == "pending",
            OrganizationInvitation.expires_at > now,
        )
    )
    return int(result.scalar_one() or 0)


async def _active_support_session(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    if identity_id is None:
        return None
    now = datetime.now(UTC)
    result = await db.execute(
        select(PlatformSupportSession)
        .where(
            PlatformSupportSession.platform_identity_id == identity_id,
            PlatformSupportSession.ended_at.is_(None),
            PlatformSupportSession.expires_at > now,
        )
        .order_by(PlatformSupportSession.created_at.desc())
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    return {
        "id": str(session.id),
        "tenant_id": str(session.tenant_id),
        "scopes": list(session.scopes or []),
        "reason": session.reason,
        "expires_at": session.expires_at,
    }


async def resolve_effective_access(db: AsyncSession, user: User) -> EffectiveAccess:
    """Resolve the complete server-authoritative access response for ``user``."""

    identity = getattr(user, "identity", None)
    identity_id = getattr(user, "identity_id", None) or getattr(identity, "id", None)
    membership_role = normalized_membership_role(user)
    global_roles: set[str] = set()
    identity_capabilities = await _identity_capabilities(db, identity_id=identity_id)
    capabilities = identity_capabilities.intersection(GLOBAL_IDENTITY_CAPABILITIES)
    surfaces: set[str] = set()

    if membership_role is not None and getattr(user, "is_active", True):
        surfaces.add(SURFACE_WORK)
        capabilities.update(MEMBER_CAPABILITIES)
        if membership_role in COMPANY_GOVERNANCE_ROLES or await _member_private_agent_creation_allowed(db, user):
            capabilities.add("agent.create.private")
        if await _has_managed_agent(db, user):
            capabilities.add("agent.manage.assigned")
        if membership_role in COMPANY_GOVERNANCE_ROLES:
            surfaces.add(SURFACE_COMPANY_ADMIN)
            capabilities.update(COMPANY_ADMIN_CAPABILITIES)
        if membership_role == "org_owner":
            capabilities.update(COMPANY_OWNER_CAPABILITIES)

    platform_operator = is_platform_operator(user)
    if platform_operator:
        global_roles.add(GLOBAL_ROLE_PLATFORM_OPERATOR)
        surfaces.add(SURFACE_PLATFORM_ADMIN)
        capabilities.update(PLATFORM_OPERATOR_CAPABILITIES)

    email = getattr(identity, "email", None)
    pending_invitation_count = await _pending_invitation_count(
        db,
        canonical_email=email,
    )
    support_session = (
        await _active_support_session(db, identity_id=identity_id)
        if platform_operator
        else None
    )

    surface_order = (SURFACE_WORK, SURFACE_COMPANY_ADMIN, SURFACE_PLATFORM_ADMIN)
    return EffectiveAccess(
        membership_id=(user.id if membership_role is not None else None),
        membership_role=membership_role,
        global_roles=tuple(sorted(global_roles)),
        effective_capabilities=tuple(sorted(capabilities)),
        available_surfaces=tuple(surface for surface in surface_order if surface in surfaces),
        pending_invitation_count=pending_invitation_count,
        current_support_session=support_session,
    )
