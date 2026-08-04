"""Server-owned product-role classification for visible Agents.

The Agent table remains the execution identity.  Product roles are derived
from durable relations so the UI never guesses from a display name or an
editable role description.
"""

import uuid
from collections.abc import Iterable
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate
from app.models.onboarding import UserTenantOnboarding


AgentProductRole = Literal[
    "personal_assistant",
    "legacy_personal_assistant",
    "agent_employee",
]

PRIVATE_ASSISTANT_ROLE_KEY = "private-assistant"
PRIVATE_ASSISTANT_TEMPLATE_NAME = "Private Assistant"


def classify_agent_product_roles(
    agents: Iterable[Agent],
    *,
    personal_assistant_agent_id: uuid.UUID | None,
    private_assistant_template_ids: set[uuid.UUID],
) -> dict[uuid.UUID, AgentProductRole]:
    """Classify Agents without inferring identity from user-editable text."""

    roles: dict[uuid.UUID, AgentProductRole] = {}
    for agent in agents:
        if agent.id == personal_assistant_agent_id:
            roles[agent.id] = "personal_assistant"
        elif (
            not getattr(agent, "is_system", False)
            and getattr(agent, "template_id", None) in private_assistant_template_ids
        ):
            # Product-managed assistants created before the onboarding relation
            # are retained as history.  They are not long-term employee seats.
            roles[agent.id] = "legacy_personal_assistant"
        else:
            roles[agent.id] = "agent_employee"
    return roles


async def resolve_agent_product_roles(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    agents: Iterable[Agent],
) -> dict[uuid.UUID, AgentProductRole]:
    """Resolve viewer-specific product roles for a bounded Agent collection."""

    agent_list = list(agents)
    if not agent_list:
        return {}
    if tenant_id is None:
        return {agent.id: "agent_employee" for agent in agent_list}

    onboarding_result = await db.execute(
        select(UserTenantOnboarding.personal_assistant_agent_id).where(
            UserTenantOnboarding.user_id == viewer_id,
            UserTenantOnboarding.tenant_id == tenant_id,
        )
    )
    personal_assistant_agent_id = onboarding_result.scalar_one_or_none()

    template_result = await db.execute(
        select(AgentTemplate.id).where(
            AgentTemplate.is_builtin.is_(True),
            or_(
                AgentTemplate.role_key == PRIVATE_ASSISTANT_ROLE_KEY,
                AgentTemplate.name == PRIVATE_ASSISTANT_TEMPLATE_NAME,
            ),
        )
    )
    private_assistant_template_ids = set(template_result.scalars().all())

    return classify_agent_product_roles(
        agent_list,
        personal_assistant_agent_id=personal_assistant_agent_id,
        private_assistant_template_ids=private_assistant_template_ids,
    )


__all__ = [
    "AgentProductRole",
    "PRIVATE_ASSISTANT_ROLE_KEY",
    "classify_agent_product_roles",
    "resolve_agent_product_roles",
]
