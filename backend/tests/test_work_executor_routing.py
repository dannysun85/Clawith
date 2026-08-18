"""Deterministic and viewer-scoped Work executor routing contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest

from app.api.work import _confirmation_fingerprint
from app.schemas.work import WorkTaskPreflight
from app.services import work_executor_routing as routing
from app.services.agent_runtime.model_route import RuntimeModelRouteError


class _Result:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _RecordingDb:
    def __init__(self, agents):
        self.agents = list(agents)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.agents)


def _agent(*, name: str, role_description: str, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": name,
        "role_description": role_description,
        "tenant_id": uuid.uuid4(),
        "creator_id": uuid.uuid4(),
        "access_mode": "company",
        "status": "running",
        "deleted_at": None,
        "deletion_requested_at": None,
        "legacy_assistant_state": None,
        "is_expired": False,
        "expires_at": None,
        "is_system": False,
        "template_id": None,
        "template_sync_status": "current",
        "preferred_tier": None,
        "preferred_modality": "text",
        "primary_model_id": uuid.uuid4(),
        "fallback_model_id": None,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user(tenant_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        role="member",
        is_active=True,
    )


async def _roles(_db, *, agents, **_kwargs):
    return {
        agent.id: ("personal_assistant" if agent.name == "My Assistant" else "agent_employee")
        for agent in agents
    }


async def _available_route(_agent):
    return SimpleNamespace(model_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_auto_route_chooses_visible_role_match(monkeypatch):
    tenant_id = uuid.uuid4()
    assistant = _agent(
        name="My Assistant",
        role_description="General coordinator",
        tenant_id=tenant_id,
        is_system=True,
    )
    finance = _agent(
        name="Finance Partner",
        role_description="财务分析师，负责预算和经营分析",
        tenant_id=tenant_id,
    )
    db = _RecordingDb([assistant, finance])
    monkeypatch.setattr(routing, "resolve_agent_product_roles", _roles)
    monkeypatch.setattr(routing, "resolve_runtime_model_route", _available_route)

    decision = await routing.route_work_executor(
        db,
        user=_user(tenant_id),
        title="审核预算",
        intent="请让财务分析师审核下季度预算",
        work_type="general",
    )

    assert decision.agent.id == finance.id
    assert decision.chosen_executor_kind == "agent_employee"
    assert decision.confidence > 0.7
    assert "server_auto_route" in decision.reason_codes
    assert "agents.tenant_id" in str(db.statements[0])
    assert "agents.deleted_at IS NULL" in str(db.statements[0])


@pytest.mark.asyncio
async def test_low_confidence_route_falls_back_to_personal_assistant(monkeypatch):
    tenant_id = uuid.uuid4()
    assistant = _agent(
        name="My Assistant",
        role_description="General coordinator",
        tenant_id=tenant_id,
        is_system=True,
    )
    researcher = _agent(
        name="Researcher",
        role_description="Customer research",
        tenant_id=tenant_id,
    )
    monkeypatch.setattr(routing, "resolve_agent_product_roles", _roles)
    monkeypatch.setattr(routing, "resolve_runtime_model_route", _available_route)

    decision = await routing.route_work_executor(
        _RecordingDb([assistant, researcher]),
        user=_user(tenant_id),
        title="安排会议",
        intent="整理下周的例行事项",
        work_type="general",
    )

    assert decision.agent.id == assistant.id
    assert decision.chosen_executor_kind == "personal_assistant"
    assert decision.fallback["used"] is True
    assert "low_confidence_personal_assistant_fallback" in decision.reason_codes


@pytest.mark.asyncio
async def test_unavailable_matching_route_falls_back_without_selecting_arbitrary_agent(monkeypatch):
    tenant_id = uuid.uuid4()
    assistant = _agent(
        name="My Assistant",
        role_description="General coordinator",
        tenant_id=tenant_id,
        is_system=True,
    )
    finance = _agent(
        name="Finance Partner",
        role_description="Financial analyst",
        tenant_id=tenant_id,
    )
    monkeypatch.setattr(routing, "resolve_agent_product_roles", _roles)

    async def route(agent):
        if agent.id == finance.id:
            raise RuntimeModelRouteError("missing")
        return SimpleNamespace(model_id=uuid.uuid4())

    monkeypatch.setattr(routing, "resolve_runtime_model_route", route)
    decision = await routing.route_work_executor(
        _RecordingDb([assistant, finance]),
        user=_user(tenant_id),
        title="Financial review",
        intent="Ask the financial analyst to review the forecast",
        work_type="general",
    )

    assert decision.agent.id == assistant.id
    assert decision.fallback["attempts"] == [
        {"agent_id": str(finance.id), "reason_code": "text_route_unavailable"}
    ]


@pytest.mark.asyncio
async def test_stopped_or_system_employee_is_never_auto_selected(monkeypatch):
    tenant_id = uuid.uuid4()
    assistant = _agent(
        name="My Assistant",
        role_description="General coordinator",
        tenant_id=tenant_id,
        is_system=True,
    )
    stopped = _agent(
        name="Finance Stopped",
        role_description="Financial analyst",
        tenant_id=tenant_id,
        status="stopped",
    )
    system = _agent(
        name="Finance System",
        role_description="Financial analyst",
        tenant_id=tenant_id,
        is_system=True,
    )
    monkeypatch.setattr(routing, "resolve_agent_product_roles", _roles)
    monkeypatch.setattr(routing, "resolve_runtime_model_route", _available_route)

    decision = await routing.route_work_executor(
        _RecordingDb([assistant, stopped, system]),
        user=_user(tenant_id),
        title="Financial review",
        intent="Ask Finance Stopped and Finance System to review the forecast",
        work_type="general",
    )

    assert decision.agent.id == assistant.id
    assert decision.chosen_executor_kind == "personal_assistant"


def test_confirmation_binds_policy_choice_and_candidate_facts():
    draft = WorkTaskPreflight(title="Review launch", intent="Review the launch plan")
    agent_id = uuid.uuid4()

    baseline = _confirmation_fingerprint(
        draft,
        agent_id=agent_id,
        chosen_executor_kind="personal_assistant",
        candidate_facts_hash="a" * 64,
    )
    assert baseline != _confirmation_fingerprint(
        draft,
        agent_id=agent_id,
        chosen_executor_kind="agent_employee",
        candidate_facts_hash="a" * 64,
    )
    assert baseline != _confirmation_fingerprint(
        draft,
        agent_id=agent_id,
        chosen_executor_kind="personal_assistant",
        candidate_facts_hash="b" * 64,
    )
    assert baseline != _confirmation_fingerprint(
        draft,
        agent_id=agent_id,
        policy_version="work-router-v2",
        chosen_executor_kind="personal_assistant",
        candidate_facts_hash="a" * 64,
    )
