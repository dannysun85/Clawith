import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import workforce_topology as workforce_topology_api
from app.services import workforce_topology


class _Result:
    def __init__(self, *, values=None, scalar=None, rows=None):
        self._values = list(values or [])
        self._scalar = scalar
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows or self._values)


class _RecordingDb:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.responses:
            raise AssertionError("unexpected database query")
        return self.responses.pop(0)


def _agent(*, tenant_id, creator_id, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Research Agent",
        "avatar_url": None,
        "role_description": "Researches customer needs",
        "tenant_id": tenant_id,
        "creator_id": creator_id,
        "status": "running",
        "last_active_at": datetime.now(UTC),
        "tokens_used_today": 120,
        "cache_read_tokens_today": 24,
        "max_tokens_per_day": 1000,
        "is_expired": False,
        "is_system": False,
        "access_mode": "company",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_workforce_topology_route_is_viewer_scoped_under_api_prefix():
    assert workforce_topology_api.router.prefix == "/workforce"
    assert "/workforce/topology" in {route.path for route in workforce_topology_api.router.routes}


def test_merge_activity_edges_is_undirected_and_rejects_unknown_agents():
    first = uuid.uuid4()
    second = uuid.uuid4()
    outsider = uuid.uuid4()
    now = datetime.now(UTC)

    edges = workforce_topology.merge_topology_activity_edges(
        [
            (first, second, 2, now - timedelta(minutes=5)),
            (second, first, 3, now),
            (first, outsider, 9, now),
            (first, first, 1, now),
        ],
        employee_ids={first, second},
    )

    assert len(edges) == 1
    assert {edges[0].agent_a_id, edges[0].agent_b_id} == {first, second}
    assert edges[0].interaction_count == 5
    assert edges[0].last_activity_at == now


def test_work_summary_prioritizes_blockers_and_ignores_cancelled_or_stale_work():
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    summaries = workforce_topology._project_topology_work_summaries(
        [
            workforce_topology._WorkCandidate(
                id=uuid.uuid4(),
                agent_id=agent_id,
                title="Blocked delivery",
                summary="Waiting for an approval dependency",
                user_stage="blocked",
                deep_link="/work/blocked",
                updated_at=now - timedelta(minutes=20),
            ),
            workforce_topology._WorkCandidate(
                id=uuid.uuid4(),
                agent_id=agent_id,
                title="Routine execution",
                summary="In progress",
                user_stage="execution",
                deep_link="/work/executing",
                updated_at=now,
            ),
            workforce_topology._WorkCandidate(
                id=uuid.uuid4(),
                agent_id=agent_id,
                title="Cancelled work",
                summary="Cancelled",
                user_stage="cancelled",
                deep_link="/work/cancelled",
                updated_at=now,
            ),
            workforce_topology._WorkCandidate(
                id=uuid.uuid4(),
                agent_id=agent_id,
                title="Old completed work",
                summary="Completed outside the window",
                user_stage="completed",
                deep_link="/work/old",
                updated_at=now - timedelta(days=2),
            ),
            workforce_topology._WorkCandidate(
                id=uuid.uuid4(),
                agent_id=agent_id,
                title="Recent completed work",
                summary="Completed recently",
                user_stage="completed",
                deep_link="/work/recent",
                updated_at=now - timedelta(minutes=30),
            ),
        ],
        since=now - timedelta(hours=24),
    )

    summary = summaries[agent_id]
    assert summary.title == "Blocked delivery"
    assert summary.stage == "blocked"
    assert summary.active_count == 2
    assert summary.recently_completed_count == 1


@pytest.mark.asyncio
async def test_work_summary_queries_are_viewer_scoped_and_not_globally_capped():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    db = _RecordingDb([_Result(values=[]), _Result(values=[])])

    result = await workforce_topology._load_topology_work_summaries(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        employee_ids={employee_id},
        since=datetime.now(UTC) - timedelta(hours=24),
    )

    assert result == {}
    assert len(db.statements) == 2
    task_query = str(db.statements[0])
    deliverable_query = str(db.statements[1])
    assert "deliverable_requests.created_by_user_id" in deliverable_query
    assert "deliverable_requests.updated_at" in deliverable_query
    assert "tasks.created_by" in task_query
    assert "tasks.updated_at" in task_query
    assert "LIMIT" not in deliverable_query
    assert "LIMIT" not in task_query


@pytest.mark.asyncio
async def test_topology_requires_company_context_without_querying_database():
    db = _RecordingDb([])
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=None, role="platform_admin")

    with pytest.raises(HTTPException) as exc:
        await workforce_topology.build_workforce_topology(
            db,
            user=user,
            window_hours=24,
        )

    assert exc.value.status_code == 403
    assert db.statements == []


@pytest.mark.asyncio
async def test_member_topology_excludes_assistant_and_unmanageable_relationships(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    tenant = SimpleNamespace(id=tenant_id, name="Astra Labs")
    manageable = _agent(tenant_id=tenant_id, creator_id=user_id, name="Owned Agent")
    company_agent = _agent(
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Company Agent",
    )
    assistant = _agent(
        tenant_id=tenant_id,
        creator_id=user_id,
        name="Personal Assistant",
    )
    now = datetime.now(UTC)
    relationship = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=manageable.id,
        target_agent_id=company_agent.id,
        relation="collaborator",
        created_at=now,
        updated_at=None,
    )
    raw_owned_log = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=manageable.id,
        action_type="chat_reply",
        summary="Private creator-visible summary",
        created_at=now,
    )
    safe_company_log = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=company_agent.id,
        action_type="tool_call",
        summary="Sensitive tool details",
        created_at=now - timedelta(minutes=1),
    )
    unsafe_company_log = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=company_agent.id,
        action_type="chat_reply",
        summary="Must not be exposed",
        created_at=now - timedelta(minutes=2),
    )

    async def fake_product_roles(_db, *, agents, **_kwargs):
        return {agent.id: ("personal_assistant" if agent.id == assistant.id else "agent_employee") for agent in agents}

    monkeypatch.setattr(
        workforce_topology,
        "resolve_agent_product_roles",
        fake_product_roles,
    )

    async def fake_work_summaries(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        workforce_topology,
        "_load_topology_work_summaries",
        fake_work_summaries,
    )
    db = _RecordingDb(
        [
            _Result(scalar=tenant),
            _Result(values=[manageable, company_agent, assistant]),
            _Result(values=[manageable]),
            _Result(values=[raw_owned_log, safe_company_log, unsafe_company_log]),
            _Result(values=[relationship]),
            _Result(rows=[]),
            _Result(rows=[]),
        ]
    )

    result = await workforce_topology.build_workforce_topology(
        db,
        user=user,
        window_hours=24,
    )

    assert [node.name for node in result.nodes] == ["Owned Agent", "Company Agent"]
    assert [node.can_manage for node in result.nodes] == [True, False]
    assert [node.visibility for node in result.nodes] == ["company", "company"]
    assert result.relationship_edges == []
    assert [activity.summary for activity in result.recent_activities] == [
        "Private creator-visible summary",
        "Tool executed",
    ]
    assert len(db.statements) == 7
    assert "chat_sessions.user_id" in str(db.statements[5])
    assert "gateway_messages.sender_user_id" in str(db.statements[6])


@pytest.mark.asyncio
async def test_company_admin_receives_visible_relationships_and_merged_activity_edges(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="org_admin")
    tenant = SimpleNamespace(id=tenant_id, name="Astra Labs")
    first = _agent(tenant_id=tenant_id, creator_id=uuid.uuid4(), name="First")
    second = _agent(tenant_id=tenant_id, creator_id=uuid.uuid4(), name="Second")
    now = datetime.now(UTC)
    relationship = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=first.id,
        target_agent_id=second.id,
        relation="supervisor",
        created_at=now,
        updated_at=None,
    )

    async def fake_product_roles(_db, *, agents, **_kwargs):
        return {agent.id: "agent_employee" for agent in agents}

    monkeypatch.setattr(
        workforce_topology,
        "resolve_agent_product_roles",
        fake_product_roles,
    )

    async def fake_work_summaries(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        workforce_topology,
        "_load_topology_work_summaries",
        fake_work_summaries,
    )
    db = _RecordingDb(
        [
            _Result(scalar=tenant),
            _Result(values=[first, second]),
            _Result(values=[first, second]),
            _Result(values=[]),
            _Result(values=[relationship]),
            _Result(rows=[(first.id, second.id, 2, now)]),
            _Result(rows=[(second.id, first.id, 1, now - timedelta(minutes=1))]),
        ]
    )

    result = await workforce_topology.build_workforce_topology(
        db,
        user=user,
        window_hours=24,
    )

    assert len(result.relationship_edges) == 1
    assert all(node.can_manage for node in result.nodes)
    assert result.relationship_edges[0].relation == "supervisor"
    assert len(result.activity_edges) == 1
    assert result.activity_edges[0].interaction_count == 3
    assert result.activity_edges[0].last_activity_at == now
