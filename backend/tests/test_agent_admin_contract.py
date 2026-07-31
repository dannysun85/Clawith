"""Agent administrator assignment and per-Agent management contracts."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.schemas.schemas import AgentUpdate
from app.services.chat_session_access import can_audit_agent_chat_sessions


class _Result:
    def scalar_one_or_none(self):
        return None


class _DB:
    def __init__(self):
        self.execute = AsyncMock(return_value=_Result())
        self.flush = AsyncMock()


def _actor(role: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
    )


def _agent(actor):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=actor.tenant_id,
        creator_id=uuid.uuid4(),
        agent_type="native",
        name="Original",
    )


def test_agent_admin_audit_requires_manage_access_for_the_exact_agent():
    actor = _actor("agent_admin")
    agent = _agent(actor)

    assert not can_audit_agent_chat_sessions(
        actor,
        agent=agent,
        agent_access_level="use",
    )
    assert can_audit_agent_chat_sessions(
        actor,
        agent=agent,
        agent_access_level="manage",
    )


def test_agent_creator_and_company_admin_keep_permitted_audit_boundaries():
    creator = _actor("member")
    own_agent = _agent(creator)
    own_agent.creator_id = creator.id
    org_admin = _actor("org_admin")

    assert can_audit_agent_chat_sessions(
        creator,
        agent=own_agent,
        agent_access_level="manage",
    )
    assert can_audit_agent_chat_sessions(org_admin)


@pytest.mark.asyncio
async def test_agent_admin_can_update_an_agent_with_manage_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    db = _DB()
    output = SimpleNamespace(model_dump=lambda: {"id": str(agent.id), "name": agent.name})

    with (
        patch.object(
            agents_api,
            "check_agent_access",
            AsyncMock(return_value=(agent, "manage")),
        ),
        patch.object(
            agents_api,
            "_agent_to_out",
            AsyncMock(return_value=output),
        ),
    ):
        result = await agents_api.update_agent(
            agent.id,
            AgentUpdate(name="Managed"),
            current_user=actor,
            db=db,
        )

    assert agent.name == "Managed"
    assert result["name"] == "Managed"
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_agent_admin_cannot_update_an_agent_with_use_only_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "use")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.update_agent(
                agent.id,
                AgentUpdate(name="Forbidden"),
                current_user=actor,
                db=db,
            )

    assert exc.value.status_code == 403
    assert agent.name == "Original"
    db.flush.assert_not_awaited()
