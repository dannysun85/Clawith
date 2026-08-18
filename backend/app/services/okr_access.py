"""Server-authoritative visibility policy for tenant OKR data."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_use_agent
from app.models.agent import Agent
from app.models.okr import OKRObjective
from app.models.org import OrgMember
from app.models.user import User
from app.services.access_control import is_company_governor


@dataclass(frozen=True)
class OKRVisibility:
    """Concrete owner identifiers visible to one active tenant membership."""

    user_owner_ids: frozenset[uuid.UUID]
    agent_owner_ids: frozenset[uuid.UUID]
    can_view_all_humans: bool


async def resolve_okr_visibility(
    db: AsyncSession,
    user: User,
) -> OKRVisibility:
    """Resolve visibility without trusting account-global capability grants.

    Company objectives are public to the active tenant and therefore do not
    need an identifier in this result.  Human and Agent objectives do: human
    owner identifiers must belong to this tenant, and Agent identifiers must
    pass the existing Directory use policy (including the private-Agent rule).
    """

    tenant_id = user.tenant_id
    can_view_all_humans = is_company_governor(user)

    user_query = select(User.id).where(
        User.tenant_id == tenant_id,
        User.is_active.is_(True),
    )
    member_query = select(OrgMember.id).where(
        OrgMember.tenant_id == tenant_id,
        OrgMember.status == "active",
    )
    if not can_view_all_humans:
        user_query = user_query.where(User.id == user.id)
        member_query = member_query.where(OrgMember.user_id == user.id)

    user_ids = {row[0] for row in (await db.execute(user_query)).all()}
    member_ids = {row[0] for row in (await db.execute(member_query)).all()}

    agents = (
        await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    agent_ids: set[uuid.UUID] = set()
    for agent in agents:
        if await can_use_agent(db, user, agent):
            agent_ids.add(agent.id)

    return OKRVisibility(
        user_owner_ids=frozenset(user_ids | member_ids),
        agent_owner_ids=frozenset(agent_ids),
        can_view_all_humans=can_view_all_humans,
    )


def can_view_objective(
    objective: OKRObjective,
    visibility: OKRVisibility,
) -> bool:
    """Return whether an already tenant-scoped objective may be projected."""

    if objective.owner_type == "company":
        return True
    if objective.owner_id is None:
        return False
    if objective.owner_type == "user":
        return objective.owner_id in visibility.user_owner_ids
    if objective.owner_type == "agent":
        return objective.owner_id in visibility.agent_owner_ids
    return False
