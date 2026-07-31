"""Agent administrator assignment and per-Agent management contracts."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Response

from app.api import agents as agents_api
from app.api import enterprise as enterprise_api
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.user import User
from app.schemas.schemas import AgentUpdate, ApprovalAction
from app.services import autonomy_service as autonomy_module
from app.services.chat_session_access import can_audit_agent_chat_sessions


class _Result:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _DB:
    def __init__(self):
        self.execute = AsyncMock(return_value=_Result())
        self.flush = AsyncMock()
        self.commit = AsyncMock()


def _actor(role: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        is_active=True,
        identity=SimpleNamespace(is_platform_admin=False),
    )


def _agent(actor):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=actor.tenant_id,
        creator_id=uuid.uuid4(),
        agent_type="native",
        access_mode="custom",
        deleted_at=None,
        api_key_hash=None,
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


@pytest.mark.asyncio
async def test_agent_admin_can_list_approvals_with_manage_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    ):
        result = await agents_api.list_agent_approvals(
            agent.id,
            Response(),
            limit=100,
            current_user=actor,
            db=db,
        )

    assert result == []
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_admin_cannot_list_approvals_with_use_only_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "use")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.list_agent_approvals(
                agent.id,
                Response(),
                limit=100,
                current_user=actor,
                db=db,
            )

    assert exc.value.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_admin_cannot_resolve_approval_with_use_only_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "use")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.resolve_agent_approval(
                agent.id,
                uuid.uuid4(),
                ApprovalAction(action="reject"),
                current_user=actor,
                db=db,
            )

    assert exc.value.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_admin_can_reset_openclaw_key_with_manage_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    agent.agent_type = "openclaw"
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    ):
        result = await agents_api.generate_or_reset_api_key(
            agent.id,
            current_user=actor,
            db=db,
        )

    assert result["message"] == "Key configured successfully."
    assert agent.api_key_hash is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_admin_cannot_reset_openclaw_key_with_use_only_access():
    actor = _actor("agent_admin")
    agent = _agent(actor)
    agent.agent_type = "openclaw"
    db = _DB()

    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "use")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.generate_or_reset_api_key(
                agent.id,
                current_user=actor,
                db=db,
            )

    assert exc.value.status_code == 403
    assert agent.api_key_hash is None
    db.commit.assert_not_awaited()


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ApprovalDB:
    def __init__(self, approval, agent):
        self.results = iter((approval, agent))
        self.added = []
        self.flush = AsyncMock()
        self.commit = AsyncMock()

    async def execute(self, _statement):
        return _ScalarResult(next(self.results))

    def add(self, value):
        self.added.append(value)


class _CapturingDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result()


@pytest.mark.asyncio
async def test_enterprise_approval_queue_uses_object_manage_scope():
    actor = _actor("agent_admin")
    db = _CapturingDB()

    result = await enterprise_api.list_approvals(
        Response(),
        tenant_id=None,
        status_filter=None,
        limit=100,
        current_user=actor,
        db=db,
    )

    assert result == []
    assert len(db.statements) == 1
    compiled = db.statements[0].compile()
    sql = str(compiled)
    parameters = set(compiled.params.values())
    assert "agent_permissions" in sql
    assert "agents.deleted_at IS NULL" in sql
    assert "manage" in parameters
    assert "use" not in parameters


@pytest.mark.asyncio
async def test_agent_admin_manage_permission_can_resolve_approval(monkeypatch):
    tenant_id = uuid.uuid4()
    actor = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Agent manager",
        role="agent_admin",
        is_active=True,
    )
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Managed Agent",
        status="idle",
        access_mode="custom",
    )
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=agent.id,
        action_type="send_external_message",
        details={},
        status="pending",
        execution_status=None,
    )
    db = _ApprovalDB(approval, agent)
    can_manage = AsyncMock(return_value=True)
    notify = AsyncMock()
    monkeypatch.setattr(autonomy_module, "can_manage_agent", can_manage)
    monkeypatch.setattr(
        "app.services.notification_service.send_notification",
        notify,
    )

    resolved = await autonomy_module.AutonomyService().resolve_approval(
        db,
        approval.id,
        actor,
        "reject",
        expected_agent_id=agent.id,
    )

    assert resolved.status == "rejected"
    assert resolved.execution_status == "not_required"
    can_manage.assert_awaited_once_with(db, actor, agent)
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_admin_without_manage_permission_cannot_resolve_approval(monkeypatch):
    tenant_id = uuid.uuid4()
    actor = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Agent user",
        role="agent_admin",
        is_active=True,
    )
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Unmanaged Agent",
        status="idle",
        access_mode="custom",
    )
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=agent.id,
        action_type="send_external_message",
        details={},
        status="pending",
        execution_status=None,
    )
    db = _ApprovalDB(approval, agent)
    can_manage = AsyncMock(return_value=False)
    monkeypatch.setattr(autonomy_module, "can_manage_agent", can_manage)

    with pytest.raises(ValueError, match="Only an Agent manager"):
        await autonomy_module.AutonomyService().resolve_approval(
            db,
            approval.id,
            actor,
            "reject",
            expected_agent_id=agent.id,
        )

    can_manage.assert_awaited_once_with(db, actor, agent)
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
