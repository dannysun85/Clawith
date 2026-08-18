"""Negative and projection contracts for tenant-scoped OKR authority."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import okr as okr_api
from app.core.security import get_current_user
from app.models.okr import OKRObjective
from app.services import agent_tools, okr_reporting
from app.services.okr_access import OKRVisibility


def _user(role: str = "member", *, tenant_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=role,
        is_active=True,
        display_name="Current member",
        avatar_url=None,
        title="Product",
        identity=SimpleNamespace(is_platform_admin=False),
    )


def _app_for(user) -> FastAPI:
    app = FastAPI()
    app.include_router(okr_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    (
        ("GET", "/api/okr/evidence", None),
        ("PUT", "/api/okr/settings", {}),
        ("POST", "/api/okr/sync-relationships", None),
        (
            "POST",
            "/api/okr/objectives",
            {
                "title": "Company objective",
                "owner_type": "company",
                "period_start": "2026-07-01",
                "period_end": "2026-09-30",
            },
        ),
        ("GET", "/api/okr/company-reports", None),
        ("GET", "/api/okr/reports", None),
        ("GET", "/api/okr/members-without-okr", None),
        ("POST", "/api/okr/trigger-member-outreach", None),
        ("POST", "/api/okr/trigger-daily-collection", None),
        (
            "POST",
            "/api/okr/company-reports/regenerate",
            {"report_type": "daily", "period_start": "2026-08-18"},
        ),
    ),
)
def test_member_governance_routes_fail_before_database(
    monkeypatch,
    method,
    path,
    payload,
):
    calls = 0

    def forbidden_session():
        nonlocal calls
        calls += 1
        raise AssertionError("OKR database access must not start before authority")

    monkeypatch.setattr(okr_api, "async_session", forbidden_session)
    with TestClient(_app_for(_user())) as client:
        response = client.request(method, path, json=payload)

    assert response.status_code == 403
    assert calls == 0


def test_platform_operator_membership_does_not_inherit_tenant_okr_governance(monkeypatch):
    user = _user("member")
    user.identity.is_platform_admin = True
    calls = 0

    def forbidden_session():
        nonlocal calls
        calls += 1
        raise AssertionError("platform authority must not start tenant OKR reads")

    monkeypatch.setattr(okr_api, "async_session", forbidden_session)
    with TestClient(_app_for(user)) as client:
        response = client.get("/api/okr/company-reports")

    assert response.status_code == 403
    assert calls == 0


class _Scalars:
    def __init__(self, values=()):
        self.values = list(values)

    def all(self):
        return list(self.values)


class _Result:
    def __init__(self, *, scalar=None, values=(), rows=()):
        self.scalar = scalar
        self.values = tuple(values)
        self.rows = tuple(rows)

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return _Scalars(self.values)

    def fetchall(self):
        return list(self.rows)

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.commit_calls = 0
        self.flush_calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError(f"unexpected OKR query: {statement}")
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def refresh(self, _value):
        return None


class _Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _SessionFactory:
    def __init__(self, db):
        self.db = db
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return _Session(self.db)


def _objective(*, tenant_id, owner_type, owner_id, title):
    return OKRObjective(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=title,
        description=None,
        owner_type=owner_type,
        owner_id=owner_id,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        status="active",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_member_objective_projection_contains_company_and_self_only(monkeypatch):
    user = _user()
    company = _objective(
        tenant_id=user.tenant_id,
        owner_type="company",
        owner_id=None,
        title="Company visible",
    )
    own = _objective(
        tenant_id=user.tenant_id,
        owner_type="user",
        owner_id=user.id,
        title="Self visible",
    )
    other_user_id = uuid.uuid4()
    other = _objective(
        tenant_id=user.tenant_id,
        owner_type="user",
        owner_id=other_user_id,
        title="Other hidden",
    )
    db = _Db(
        _Result(values=(company, own, other)),
        _Result(values=()),
        _Result(rows=(SimpleNamespace(id=user.id, display_name=user.display_name),)),
    )
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    monkeypatch.setattr(
        okr_api,
        "resolve_okr_visibility",
        AsyncMock(
            return_value=OKRVisibility(
                user_owner_ids=frozenset({user.id}),
                agent_owner_ids=frozenset(),
                can_view_all_humans=False,
            )
        ),
    )

    result = await okr_api.list_objectives(
        period_start="2026-07-01",
        period_end="2026-09-30",
        user=user,
    )

    assert [item.title for item in result] == ["Company visible", "Self visible"]
    assert "Other hidden" not in str(result)


@pytest.mark.asyncio
async def test_invisible_objective_key_results_return_404(monkeypatch):
    user = _user()
    hidden = _objective(
        tenant_id=user.tenant_id,
        owner_type="user",
        owner_id=uuid.uuid4(),
        title="Hidden",
    )
    db = _Db(_Result(scalar=hidden))
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    monkeypatch.setattr(
        okr_api,
        "resolve_okr_visibility",
        AsyncMock(
            return_value=OKRVisibility(
                user_owner_ids=frozenset({user.id}),
                agent_owner_ids=frozenset(),
                can_view_all_humans=False,
            )
        ),
    )

    with pytest.raises(okr_api.HTTPException) as error:
        await okr_api.list_key_results(hidden.id, user=user)

    assert error.value.status_code == 404
    assert len(db.statements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", (okr_api.get_okr_settings, okr_api.list_periods))
async def test_okr_get_routes_do_not_insert_or_commit_defaults(monkeypatch, handler):
    user = _user()
    db = _Db(_Result(scalar=None))
    factory = _SessionFactory(db)
    monkeypatch.setattr(okr_api, "async_session", factory)

    await handler(user=user)

    assert factory.calls == 1
    assert db.added == []
    assert db.flush_calls == 0
    assert db.commit_calls == 0


@pytest.mark.asyncio
async def test_create_objective_rejects_foreign_human_owner_without_commit(monkeypatch):
    admin = _user("org_admin")
    db = _Db(_Result(scalar=None), _Result(rows=()))
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    body = okr_api.ObjectiveCreate(
        title="Foreign target",
        owner_type="user",
        owner_id=str(uuid.uuid4()),
        period_start="2026-07-01",
        period_end="2026-09-30",
    )

    with pytest.raises(okr_api.HTTPException) as error:
        await okr_api.create_objective(body, user=admin)

    assert error.value.status_code == 422
    assert db.added == []
    assert db.commit_calls == 0


@pytest.mark.asyncio
async def test_admin_daily_report_rejects_foreign_target_before_write(monkeypatch):
    admin = _user("org_admin")
    db = _Db(_Result(scalar=None), _Result(scalar=None))
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    write = AsyncMock()
    monkeypatch.setattr(okr_reporting, "upsert_member_daily_report", write)
    body = okr_api.MemberDailyReportUpsert(
        report_date="2026-08-18",
        content="Must not write",
        member_type="user",
        member_id=str(uuid.uuid4()),
    )

    with pytest.raises(okr_api.HTTPException) as error:
        await okr_api.upsert_member_daily_report(body, user=admin)

    assert error.value.status_code == 404
    write.assert_not_awaited()
    assert db.commit_calls == 0


@pytest.mark.asyncio
async def test_daily_report_source_is_server_owned(monkeypatch):
    user = _user()
    captured = {}
    now = datetime.now(timezone.utc)

    async def upsert(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            member_type="user",
            member_id=user.id,
            report_date=date(2026, 8, 18),
            content=kwargs["content"],
            status="submitted",
            submitted_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(okr_reporting, "upsert_member_daily_report", upsert)
    monkeypatch.setattr(
        okr_reporting,
        "list_tracked_okr_members",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    member_type="user",
                    member_id=user.id,
                    display_name=user.display_name,
                    avatar_url=None,
                    group_label="Product",
                )
            ]
        ),
    )
    body = okr_api.MemberDailyReportUpsert.model_validate(
        {
            "report_date": "2026-08-18",
            "content": "Done",
            "source": "forged_external_source",
        }
    )

    await okr_api.upsert_member_daily_report(body, user=user)

    assert captured["member_id"] == user.id
    assert captured["source"] == "manual"


@pytest.mark.asyncio
async def test_member_daily_projection_includes_only_self_and_usable_agent_reports(monkeypatch):
    user = _user()
    visible_agent_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    own_report = SimpleNamespace(
        member_type="user",
        member_id=user.id,
        status="submitted",
        content="Own summary",
        submitted_at=now,
        updated_at=now,
    )
    agent_report = SimpleNamespace(
        member_type="agent",
        member_id=visible_agent_id,
        status="submitted",
        content="Agent normalized summary",
        submitted_at=now,
        updated_at=now,
    )
    visible_agent = SimpleNamespace(
        id=visible_agent_id,
        name="Visible Agent",
        avatar_url=None,
    )
    db = _Db(
        _Result(values=(own_report, agent_report)),
        _Result(values=(visible_agent,)),
    )
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    monkeypatch.setattr(
        okr_api,
        "resolve_okr_visibility",
        AsyncMock(
            return_value=OKRVisibility(
                user_owner_ids=frozenset({user.id}),
                agent_owner_ids=frozenset({visible_agent_id}),
                can_view_all_humans=False,
            )
        ),
    )

    result = await okr_api.list_member_daily_reports(
        report_date="2026-08-18",
        user=user,
    )

    assert [(item.member_type, item.content) for item in result] == [
        ("user", "Own summary"),
        ("agent", "Agent normalized summary"),
    ]
    report_query = str(
        db.statements[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert user.id.hex in report_query
    assert visible_agent_id.hex in report_query


@pytest.mark.asyncio
async def test_concrete_agent_manager_can_submit_agent_daily_report(monkeypatch):
    user = _user()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=user.tenant_id,
        creator_id=user.id,
        access_mode="private",
        deleted_at=None,
        name="Private managed Agent",
        avatar_url=None,
    )
    db = _Db(_Result(scalar=agent))
    monkeypatch.setattr(okr_api, "async_session", _SessionFactory(db))
    now = datetime.now(timezone.utc)
    captured = {}

    async def upsert(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            member_type="agent",
            member_id=agent_id,
            report_date=date(2026, 8, 18),
            content=kwargs["content"],
            status="submitted",
            submitted_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(okr_reporting, "upsert_member_daily_report", upsert)
    monkeypatch.setattr(
        okr_reporting,
        "list_tracked_okr_members",
        AsyncMock(return_value=[]),
    )
    body = okr_api.MemberDailyReportUpsert(
        report_date="2026-08-18",
        content="Managed Agent update",
        member_type="agent",
        member_id=str(agent_id),
    )

    result = await okr_api.upsert_member_daily_report(body, user=user)

    assert captured["member_id"] == agent_id
    assert captured["source"] == "manual"
    assert result.display_name == "Private managed Agent"


@pytest.mark.asyncio
async def test_designated_okr_agent_non_admin_read_is_principal_scoped(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, is_system=True)
    settings = SimpleNamespace(
        enabled=True,
        period_frequency="quarterly",
        period_length_days=90,
        okr_agent_id=agent_id,
    )
    db = _Db(_Result(scalar=agent), _Result(scalar=settings), _Result(values=()))
    monkeypatch.setattr(agent_tools, "async_session", _SessionFactory(db))
    monkeypatch.setattr(
        agent_tools,
        "_load_okr_request_context",
        AsyncMock(
            return_value={
                "agent": agent,
                "agent_is_designated_okr_agent": True,
                "requester_user_id": user_id,
                "requester_is_admin": False,
            }
        ),
    )

    outcome = await agent_tools._get_okr_outcome(
        agent_id,
        {"period_start": "2026-07-01", "period_end": "2026-09-30"},
        user_id,
        own_only=False,
    )

    query = str(db.statements[2].compile(compile_kwargs={"literal_binds": True}))
    assert outcome.status == "succeeded"
    assert "okr_objectives.owner_type = 'company'" in query
    assert "okr_objectives.owner_type = 'user'" in query
    assert "okr_objectives.owner_type = 'agent'" in query
    assert "agent_permissions.access_level IN ('use', 'manage')" in query
    assert user_id.hex in query
    assert outcome.metadata["visibility_scope"] == "company_requester_and_usable_agents"


@pytest.mark.asyncio
async def test_designated_okr_agent_admin_read_preserves_private_agent_boundary(monkeypatch):
    agent_id = uuid.uuid4()
    requester = _user("org_admin")
    tenant_id = requester.tenant_id
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, is_system=True)
    settings = SimpleNamespace(
        enabled=True,
        period_frequency="quarterly",
        period_length_days=90,
        okr_agent_id=agent_id,
    )
    db = _Db(_Result(scalar=agent), _Result(scalar=settings), _Result(values=()))
    monkeypatch.setattr(agent_tools, "async_session", _SessionFactory(db))
    monkeypatch.setattr(
        agent_tools,
        "_load_okr_request_context",
        AsyncMock(
            return_value={
                "agent": agent,
                "agent_is_designated_okr_agent": True,
                "requester": requester,
                "requester_user_id": requester.id,
                "requester_is_admin": True,
            }
        ),
    )

    outcome = await agent_tools._get_okr_outcome(
        agent_id,
        {"period_start": "2026-07-01", "period_end": "2026-09-30"},
        requester.id,
        own_only=False,
    )

    query = str(db.statements[2].compile(compile_kwargs={"literal_binds": True}))
    assert outcome.status == "succeeded"
    assert "agents.creator_id" in query
    assert "agents.access_mode IN ('company', 'custom')" in query
    assert "agents.access_mode IN ('company', 'custom', 'private')" not in query
    assert outcome.metadata["visibility_scope"] == "tenant_governance_visible"
