"""CEO orchestrator P1 — provider-free contract tests.

Covers: rollout gate truth table, trigger triple-gate math, snapshot truncation
priority, budget-cap fail-closed math, meeting input validation, typed adapter
fail-closed branches, tool governance registration, and the SQLite migration
contract. DB integration (enable idempotency, seat exclusion, durable
registration, zero-Task) lives in scripts/ceo-orchestrator-postgres-smoke.py.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import database
from app.config import Settings
from app.services import agent_tools, ceo_briefing, ceo_orchestrator
from app.services.agent_template_contract import (
    load_agent_template_manifest,
    validate_template_capability_references,
)
from app.services.builtin_tool_definitions import (
    BUILTIN_TOOL_NAMES,
    builtin_policy,
)
from app.services.ceo_briefing import (
    BriefWorkItem,
    CompanyBriefSnapshot,
    ceo_orchestrator_allowed,
)
from app.services.template_seeder import _TEMPLATE_ROOT


# ─── Rollout gate (FR-CEO-5) ─────────────────────────────────────────


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://app:secret@db.example/clawith",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_ceo_rollout_gate_truth_table() -> None:
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    closed = _settings()
    assert not ceo_orchestrator_allowed(
        tenant_id=tenant, agent_id=agent, runtime_settings=closed
    )

    enabled_no_lists = _settings(CEO_ORCHESTRATOR_ENABLED=True)
    assert not ceo_orchestrator_allowed(
        tenant_id=tenant, agent_id=agent, runtime_settings=enabled_no_lists
    )

    tenant_hit = _settings(
        CEO_ORCHESTRATOR_ENABLED=True,
        CEO_ORCHESTRATOR_TENANT_IDS=str(tenant),
    )
    assert ceo_orchestrator_allowed(
        tenant_id=tenant, agent_id=None, runtime_settings=tenant_hit
    )

    agent_hit = _settings(
        CEO_ORCHESTRATOR_ENABLED=True,
        CEO_ORCHESTRATOR_AGENT_IDS=f" {agent} , other ",
    )
    assert ceo_orchestrator_allowed(
        tenant_id=uuid.uuid4(), agent_id=agent, runtime_settings=agent_hit
    )
    assert not ceo_orchestrator_allowed(
        tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), runtime_settings=agent_hit
    )


def test_ceo_rollout_defaults_closed() -> None:
    s = _settings()
    assert s.CEO_ORCHESTRATOR_ENABLED is False
    assert s.CEO_ORCHESTRATOR_TENANT_IDS == ""
    assert s.CEO_ORCHESTRATOR_AGENT_IDS == ""
    assert s.CEO_BRIEF_SNAPSHOT_MAX_CHARS == 4000


# ─── Trigger triple gate (FR-CEO-3) ──────────────────────────────────


def _settings_row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "ceo_agent_id": uuid.uuid4(),
        "enabled": True,
        "briefing_enabled": True,
        "morning_meeting_enabled": True,
        "enabled_by_user_id": None,
        "daily_credit_cap": 20,
        "monthly_credit_cap": 300,
        "meeting_member_agent_ids": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_trigger_triple_gate_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = {"value": True}
    monkeypatch.setattr(
        ceo_orchestrator,
        "ceo_orchestrator_allowed",
        lambda **_: allowed["value"],
    )

    row = _settings_row()
    assert ceo_orchestrator._trigger_gate_enabled(row, cadence="briefing") is True
    assert ceo_orchestrator._trigger_gate_enabled(row, cadence="meeting") is True

    # Gate 1: rollout closed
    allowed["value"] = False
    assert ceo_orchestrator._trigger_gate_enabled(row, cadence="briefing") is False
    allowed["value"] = True

    # Gate 2: tenant opt-in off
    disabled = _settings_row(enabled=False)
    assert ceo_orchestrator._trigger_gate_enabled(disabled, cadence="briefing") is False

    # Gate 3: cadence switches
    no_briefing = _settings_row(briefing_enabled=False)
    assert ceo_orchestrator._trigger_gate_enabled(no_briefing, cadence="briefing") is False
    assert ceo_orchestrator._trigger_gate_enabled(no_briefing, cadence="meeting") is True
    no_meeting = _settings_row(morning_meeting_enabled=False)
    assert ceo_orchestrator._trigger_gate_enabled(no_meeting, cadence="meeting") is False
    assert ceo_orchestrator._trigger_gate_enabled(no_meeting, cadence="briefing") is True


def test_ceo_trigger_names_never_collide_with_okr_names() -> None:
    okr_names = {
        "daily_okr_collection",
        "daily_okr_report",
        "weekly_okr_report",
        "biweekly_okr_checkin",
        "monthly_okr_report",
    }
    assert ceo_orchestrator.CEO_SYSTEM_TRIGGER_NAMES.isdisjoint(okr_names)
    assert ceo_orchestrator.CEO_BRIEFING_TRIGGER_NAMES <= {
        "ceo_daily_brief",
        "ceo_daily_collection",
        "ceo_weekly_brief",
    }
    assert "ceo_morning_meeting" in ceo_orchestrator.CEO_SYSTEM_TRIGGER_NAMES


@pytest.mark.asyncio
async def test_gate_ceo_trigger_disables_fire_when_rollout_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        is_system=True,
        name="ceo_daily_brief",
        is_enabled=True,
    )
    settings = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        ceo_agent_id=trigger.agent_id,
        enabled=True,
    )
    stored = SimpleNamespace(id=trigger.id, is_enabled=True)

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        def __init__(self):
            self.calls = 0
            self.committed = False

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _Result(settings)
            return _Result(stored)

        async def commit(self):
            self.committed = True

    db = _DB()

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(ceo_orchestrator, "async_session", lambda: _CM())
    monkeypatch.setattr(ceo_orchestrator, "ceo_orchestrator_allowed", lambda **_: False)

    skipped = await ceo_orchestrator.gate_ceo_trigger_automation(trigger, now)
    assert skipped is True
    assert stored.is_enabled is False
    assert db.committed is True


# ─── Snapshot truncation (FR-CEO-2) ──────────────────────────────────


def _snapshot(**overrides: object) -> CompanyBriefSnapshot:
    values: dict[str, object] = {
        "company_name": "Acme",
        "window_hours": 168,
        "generated_at": datetime(2026, 8, 19, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CompanyBriefSnapshot(**values)


def test_snapshot_render_keeps_everything_under_limit() -> None:
    snapshot = _snapshot(
        blocked_items=[BriefWorkItem(agent_name="A", title="Deploy", stage="blocked")],
        in_progress_items=[BriefWorkItem(agent_name="B", title="Spec", stage="executing")],
        recent_activities=["A: did something"],
    )
    rendered = snapshot.render_markdown(max_chars=4000)
    assert "## Blockers" in rendered
    assert "## In progress" in rendered
    assert "## Recent activity" in rendered
    assert snapshot.truncated is False


def test_snapshot_truncation_prefers_blockers_over_activity() -> None:
    snapshot = _snapshot(
        blocked_items=[
            BriefWorkItem(agent_name=f"B{i}", title="x" * 40, stage="blocked")
            for i in range(3)
        ],
        in_progress_items=[
            BriefWorkItem(agent_name=f"P{i}", title="y" * 40, stage="executing")
            for i in range(3)
        ],
        recent_activities=[f"act {i} " + "z" * 60 for i in range(10)],
    )
    # Header + OKR + blockers fit; later sections must be dropped in order.
    header_len = len(snapshot.render_markdown(max_chars=10_000))
    limit = header_len - 220
    rendered = snapshot.render_markdown(max_chars=limit)
    assert snapshot.truncated is True
    assert "truncated" in rendered
    assert "## Blockers" in rendered
    assert "act 0" not in rendered  # activity content dropped by priority trim


def test_snapshot_window_clamped_to_168() -> None:
    # The API query and typed adapter clamp; the model bound enforces it too.
    with pytest.raises(Exception):
        _snapshot(window_hours=169)


# ─── Budget cap math (FR-CEO-5) ──────────────────────────────────────


class _FakeScalars:
    def __init__(self, items=()) -> None:
        self._items = list(items)

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, *, scalar=None, items=()) -> None:
        self._scalar = scalar
        self._items = tuple(items)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _FakeScalars(self._items)


class _QueueSession:
    """Minimal async-session stand-in: each execute() pops the next canned result."""

    def __init__(self, results=()) -> None:
        self._results = list(results)
        self.added: list[object] = []
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        assert self._results, "unexpected extra query"
        return self._results.pop(0)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1


def _consume_rows(amounts: list[int]) -> _FakeResult:
    return _FakeResult(
        items=[SimpleNamespace(delta=-abs(amount)) for amount in amounts]
    )


@pytest.mark.asyncio
async def test_budget_cap_denies_when_daily_spent_reaches_cap() -> None:
    settings = _settings_row(daily_credit_cap=20, monthly_credit_cap=0)
    db = _QueueSession([_consume_rows([15, 5])])
    denial = await ceo_orchestrator.automation_budget_denial(db, settings=settings)
    assert denial == "daily_credit_cap_exceeded:20/20"


@pytest.mark.asyncio
async def test_budget_cap_denies_monthly_and_allows_under_cap() -> None:
    settings = _settings_row(daily_credit_cap=0, monthly_credit_cap=300)
    db = _QueueSession([_consume_rows([100, 250])])
    denial = await ceo_orchestrator.automation_budget_denial(db, settings=settings)
    assert denial == "monthly_credit_cap_exceeded:350/300"

    settings_ok = _settings_row(daily_credit_cap=20, monthly_credit_cap=300)
    db_ok = _QueueSession([_consume_rows([3]), _consume_rows([10, 20])])
    assert await ceo_orchestrator.automation_budget_denial(db_ok, settings=settings_ok) is None


@pytest.mark.asyncio
async def test_budget_cap_zero_means_unlimited() -> None:
    settings = _settings_row(daily_credit_cap=0, monthly_credit_cap=0)
    db = _QueueSession()
    assert await ceo_orchestrator.automation_budget_denial(db, settings=settings) is None


# ─── Meeting start validation (FR-CEO-4) ─────────────────────────────


@pytest.mark.asyncio
async def test_meeting_start_rejects_unknown_kind_before_any_query() -> None:
    db = _QueueSession()
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.start_ceo_meeting(
            db,
            settings=_settings_row(),
            actor=SimpleNamespace(id=uuid.uuid4()),
            kind="standup",
        )
    assert excinfo.value.code == "ceo_meeting_kind_invalid"


@pytest.mark.asyncio
async def test_meeting_start_fail_closed_when_disabled() -> None:
    db = _QueueSession()
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.start_ceo_meeting(
            db,
            settings=_settings_row(enabled=False),
            actor=SimpleNamespace(id=uuid.uuid4()),
            kind="morning",
        )
    assert excinfo.value.code == "ceo_orchestrator_disabled"


@pytest.mark.asyncio
async def test_meeting_start_fail_closed_when_rollout_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ceo_orchestrator,
        "ceo_orchestrator_allowed",
        lambda **_: False,
    )
    db = _QueueSession()
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.start_ceo_meeting(
            db,
            settings=_settings_row(),
            actor=SimpleNamespace(id=uuid.uuid4()),
            kind="weekly",
        )
    assert excinfo.value.code == "ceo_orchestrator_not_available"


@pytest.mark.asyncio
async def test_meeting_start_budget_block_persists_notify_and_audit_outside_request_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget-blocked meeting start: the API layer turns the error into an
    HTTPException and the request transaction rolls back, so the notification
    and audit rows must be written (and committed) in an independent session."""
    settings = _settings_row()
    monkeypatch.setattr(ceo_orchestrator, "ceo_orchestrator_allowed", lambda **_: True)

    async def _deny(db, *, settings, **kwargs):  # noqa: ARG001
        return "daily cap reached"

    monkeypatch.setattr(ceo_orchestrator, "automation_budget_denial", _deny)

    calls: dict[str, object] = {}

    async def _notify(db, *, settings, reason):  # noqa: ARG001
        calls["notify_db"] = db

    def _audit(db, *, settings, reason, source):  # noqa: ARG001
        calls["audit_db"] = db
        calls["source"] = source

    monkeypatch.setattr(ceo_orchestrator, "_notify_enabler_automation_blocked", _notify)
    monkeypatch.setattr(ceo_orchestrator, "_audit_ceo_automation_blocked", _audit)

    audit_db = _QueueSession([_FakeResult(scalar=settings)])
    monkeypatch.setattr(ceo_orchestrator, "async_session", lambda: audit_db)

    request_db = _QueueSession()
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.start_ceo_meeting(
            request_db,
            settings=settings,
            actor=SimpleNamespace(id=uuid.uuid4()),
            kind="morning",
        )
    assert excinfo.value.code == "ceo_budget_cap_exceeded"
    assert calls["notify_db"] is audit_db
    assert calls["audit_db"] is audit_db
    assert calls["source"] == "meeting_start"
    assert audit_db.committed == 1
    assert request_db.committed == 0
    assert request_db.added == []


def test_ceo_orchestrator_never_writes_tasks() -> None:
    """FR-CEO-4: action items are advisory text; no Task model usage anywhere."""
    import inspect

    source = inspect.getsource(ceo_orchestrator)
    assert "models.task" not in source
    assert "Task(" not in source


# ─── Enable gate (FR-CEO-1/5) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_enable_fail_closed_when_rollout_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ceo_orchestrator,
        "ceo_orchestrator_allowed",
        lambda **_: False,
    )
    db = _QueueSession()
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.enable_ceo_orchestrator(
            db,
            tenant=SimpleNamespace(id=uuid.uuid4()),
            admin=SimpleNamespace(id=uuid.uuid4()),
            member_agent_ids=[],
        )
    assert excinfo.value.code == "ceo_orchestrator_not_available"


@pytest.mark.asyncio
async def test_enable_fail_closed_when_template_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ceo_orchestrator,
        "ceo_orchestrator_allowed",
        lambda **_: True,
    )
    db = _QueueSession([_FakeResult(scalar=None)])  # template lookup → missing
    with pytest.raises(ceo_orchestrator.CeoOrchestratorError) as excinfo:
        await ceo_orchestrator.enable_ceo_orchestrator(
            db,
            tenant=SimpleNamespace(id=uuid.uuid4()),
            admin=SimpleNamespace(id=uuid.uuid4()),
            member_agent_ids=[],
        )
    assert excinfo.value.code == "ceo_template_missing"


# ─── Typed adapter fail-closed (FR-CEO-2) ────────────────────────────


def _agent_stub(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "is_system": True,
        "template_id": uuid.uuid4(),
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_adapter_session(monkeypatch: pytest.MonkeyPatch, results: list[_FakeResult]) -> None:
    factory = lambda: _QueueSession(results)  # noqa: E731
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(agent_tools, "async_session", factory)


@pytest.mark.asyncio
async def test_snapshot_adapter_rejects_unknown_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_adapter_session(monkeypatch, [_FakeResult(scalar=None)])
    outcome = await agent_tools._company_brief_snapshot_outcome(uuid.uuid4(), {})
    assert outcome.status == "failed"
    assert outcome.error_code == "source_agent_not_found"


@pytest.mark.asyncio
async def test_snapshot_adapter_rejects_non_ceo_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent_stub(is_system=True)
    _patch_adapter_session(
        monkeypatch,
        [
            _FakeResult(scalar=agent),  # load agent
            _FakeResult(scalar="chief-of-staff"),  # template role_key lookup
        ],
    )
    outcome = await agent_tools._company_brief_snapshot_outcome(agent.id, {})
    assert outcome.status == "failed"
    assert outcome.error_code == "ceo_only"


@pytest.mark.asyncio
async def test_snapshot_adapter_rejects_when_settings_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent_stub()
    _patch_adapter_session(
        monkeypatch,
        [
            _FakeResult(scalar=agent),
            _FakeResult(scalar="ceo"),
            _FakeResult(scalar=None),  # no enabled settings row
        ],
    )
    outcome = await agent_tools._company_brief_snapshot_outcome(agent.id, {})
    assert outcome.status == "failed"
    assert outcome.error_code == "ceo_only"


@pytest.mark.asyncio
async def test_snapshot_adapter_rejects_when_rollout_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent_stub()
    settings_row = _settings_row(tenant_id=agent.tenant_id, ceo_agent_id=agent.id)
    _patch_adapter_session(
        monkeypatch,
        [
            _FakeResult(scalar=agent),
            _FakeResult(scalar="ceo"),
            _FakeResult(scalar=settings_row),  # is_enabled_ceo_agent → enabled row
        ],
    )
    monkeypatch.setattr(agent_tools, "ceo_orchestrator_allowed", lambda **_: False)
    outcome = await agent_tools._company_brief_snapshot_outcome(agent.id, {})
    assert outcome.status == "failed"
    assert outcome.error_code == "ceo_only"


@pytest.mark.asyncio
async def test_snapshot_adapter_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent_stub()
    settings_row = _settings_row(tenant_id=agent.tenant_id, ceo_agent_id=agent.id)
    _patch_adapter_session(
        monkeypatch,
        [
            _FakeResult(scalar=agent),
            _FakeResult(scalar="ceo"),
            _FakeResult(scalar=settings_row),
            _FakeResult(scalar=settings_row),  # get_enabled_ceo_settings_for_agent
        ],
    )
    monkeypatch.setattr(agent_tools, "ceo_orchestrator_allowed", lambda **_: True)

    class _StubSnapshot:
        def render_markdown(self, *, max_chars: int) -> str:
            return "snapshot-body"

    async def _noop(*a, **k):
        return _StubSnapshot()

    monkeypatch.setattr(agent_tools, "build_company_brief_snapshot", _noop)
    outcome = await agent_tools._company_brief_snapshot_outcome(agent.id, {"window_hours": 24})
    assert outcome.status == "succeeded", f"{outcome.error_code}: {outcome.result_summary}"
    assert outcome.result_summary == "snapshot-body"


def test_snapshot_adapter_window_clamped() -> None:
    assert ceo_briefing.DEFAULT_WINDOW_HOURS == 168
    assert ceo_briefing.MAX_WINDOW_HOURS == 168


# ─── Tool governance registration (FR-CEO-2) ─────────────────────────


def test_company_brief_snapshot_registered_read_safe_and_typed() -> None:
    assert "company_brief_snapshot" in BUILTIN_TOOL_NAMES
    policy = builtin_policy("company_brief_snapshot")
    assert policy == {"effect": "read", "retry_policy": "safe", "parallel_safe": True}
    assert "company_brief_snapshot" in agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


def test_ceo_template_manifest_passes_strict_validation() -> None:
    manifest = load_agent_template_manifest(_TEMPLATE_ROOT / "ceo")
    assert manifest.schema_version == 2
    assert manifest.role_key == "ceo"
    assert manifest.lifecycle_status == "enabled"
    assert "company_brief_snapshot" in manifest.default_tools
    assert manifest.default_skills == ["meeting-notes"]

    from app.services.skill_seeder import BUILTIN_SKILLS

    validate_template_capability_references(
        manifest,
        known_skill_folders={skill["folder_name"] for skill in BUILTIN_SKILLS},
        known_tool_names=set(BUILTIN_TOOL_NAMES),
        runtime_typed_tool_names=agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES,
    )


def test_ceo_template_minimal_tools_exclude_dispatch_and_external_send() -> None:
    manifest = load_agent_template_manifest(_TEMPLATE_ROOT / "ceo")
    forbidden = {
        "create_objective",
        "create_key_result",
        "update_any_kr_progress",
        "upsert_member_daily_report",
        "send_channel_message",
        "send_platform_message",
        "send_feishu_message",
        "execute_code",
        "execute_code_e2b",
        "import_mcp_server",
        "publish_page",
    }
    assert forbidden.isdisjoint(set(manifest.default_tools))


# ─── Migration contract (SQLite) ─────────────────────────────────────

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608192100_add_ceo_orchestrator_settings.py"
)

_EXPECTED_COLUMNS = {
    "tenant_id",
    "ceo_agent_id",
    "enabled",
    "enabled_by_user_id",
    "enabled_at",
    "briefing_enabled",
    "morning_meeting_enabled",
    "meeting_group_id",
    "daily_credit_cap",
    "monthly_credit_cap",
    "meeting_member_agent_ids",
    "created_at",
    "updated_at",
}

_PARENT_TABLE_DDL = (
    "CREATE TABLE tenants (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE agents (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE users (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE groups (id CHAR(36) PRIMARY KEY)",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "ceo_orchestrator_settings_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(migration, connection, operation: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        if operation == "upgrade":
            migration.upgrade()
        else:
            migration.downgrade()


def _table_names(connection) -> set[str]:
    return set(sa.inspect(connection).get_table_names())


def test_ceo_settings_migration_upgrade_downgrade_on_sqlite() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for ddl in _PARENT_TABLE_DDL:
            connection.exec_driver_sql(ddl)

        _run(migration, connection, "upgrade")
        assert "ceo_orchestrator_settings" in _table_names(connection)
        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("ceo_orchestrator_settings")
        }
        assert columns == _EXPECTED_COLUMNS

        # Idempotent re-run (existing-table guard).
        _run(migration, connection, "upgrade")
        assert "ceo_orchestrator_settings" in _table_names(connection)

        _run(migration, connection, "downgrade")
        assert "ceo_orchestrator_settings" not in _table_names(connection)


def test_ceo_settings_migration_chains_single_head() -> None:
    migration = _load_migration()
    assert migration.revision == "ceo_orchestrator_settings"
    assert migration.down_revision == "subscription_change_kind"
