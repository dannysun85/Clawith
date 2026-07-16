import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value


class _DB:
    def __init__(self, values):
        self.values = iter(values)
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Result(next(self.values))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_schedule_worker_revalidates_durable_creator(monkeypatch):
    from app.services import scheduler

    agent_id = uuid.uuid4()
    schedule_creator = uuid.uuid4()
    actual_agent_creator = uuid.uuid4()
    schedule = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        created_by=schedule_creator,
        is_enabled=True,
        instruction="do work",
    )
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=actual_agent_creator,
        tenant_id=uuid.uuid4(),
        status="running",
        deletion_requested_at=None,
    )
    creator = SimpleNamespace(
        id=schedule_creator,
        tenant_id=agent.tenant_id,
        is_active=True,
        identity=SimpleNamespace(is_active=True),
    )
    db = _DB([schedule, agent, creator])
    monkeypatch.setattr(scheduler, "AUTOMATIC_SCHEDULE_EXECUTION_ENABLED", True)
    monkeypatch.setattr("app.database.async_session", lambda: db)

    @asynccontextmanager
    async def acquired(_schedule_id):
        yield True

    monkeypatch.setattr(scheduler, "_schedule_execution_lock", acquired)

    await scheduler._execute_schedule(schedule.id)

    assert db.commits == 0


@pytest.mark.asyncio
async def test_schedule_worker_does_not_overlap_an_active_cross_process_run(
    monkeypatch,
):
    from app.services import scheduler

    @asynccontextmanager
    async def busy(_schedule_id):
        yield False

    monkeypatch.setattr(scheduler, "AUTOMATIC_SCHEDULE_EXECUTION_ENABLED", True)
    monkeypatch.setattr(scheduler, "_schedule_execution_lock", busy)
    monkeypatch.setattr(
        scheduler,
        "_execute_schedule_claimed",
        AsyncMock(side_effect=AssertionError("busy schedule must not execute")),
    )

    await scheduler._execute_schedule(uuid.uuid4())

    scheduler._execute_schedule_claimed.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_daemon_kill_switch_stays_alive_without_ticking(monkeypatch):
    from app.services import scheduler

    class StopIdleLoop(Exception):
        pass

    tick = AsyncMock()

    async def stop_idle_loop(_seconds):
        raise StopIdleLoop

    monkeypatch.setattr(scheduler, "AUTOMATIC_SCHEDULE_EXECUTION_ENABLED", False)
    monkeypatch.setattr(scheduler, "_tick", tick)
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop_idle_loop)

    with pytest.raises(StopIdleLoop):
        await scheduler.start_scheduler()

    tick.assert_not_awaited()


def test_dedicated_worker_registers_user_schedule_as_critical_daemon():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "app/main.py").read_text(encoding="utf-8")

    assert 'from app.services.scheduler import start_scheduler' in source
    assert '("user_schedule", start_scheduler())' in source


def test_schedule_lock_uses_a_dedicated_postgresql_session():
    from inspect import getsource

    from app.services.scheduler import _schedule_execution_lock

    source = getsource(_schedule_execution_lock)
    assert "async with engine.connect() as connection" in source
    assert "pg_try_advisory_lock" in source
    assert "pg_advisory_unlock" in source


@pytest.mark.asyncio
async def test_task_worker_rejects_revoked_requester_before_claim(monkeypatch):
    from app.services import task_executor

    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        created_by=requester_id,
        type="todo",
        status="pending",
    )
    agent = SimpleNamespace(
        id=agent_id,
        creator_id=uuid.uuid4(),
        tenant_id=tenant_id,
        status="running",
        deletion_requested_at=None,
    )
    creator = SimpleNamespace(
        id=requester_id,
        tenant_id=tenant_id,
        is_active=True,
        identity=SimpleNamespace(is_active=True),
    )
    db = _DB([task, agent, creator])
    monkeypatch.setattr(task_executor, "AUTOMATIC_TASK_EXECUTION_ENABLED", True)
    monkeypatch.setattr(task_executor, "async_session", lambda: db)
    monkeypatch.setattr(
        "app.core.permissions.get_agent_access_level_for_user_id",
        AsyncMock(return_value=None),
    )

    await task_executor.execute_task(task.id, agent_id)

    assert task.status == "pending"
    assert db.commits == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_task_worker_never_replays_a_nonpending_task(monkeypatch):
    from app.services import task_executor

    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        agent_id=agent_id,
        created_by=uuid.uuid4(),
        type="todo",
        status="done",
    )
    db = _DB([task])
    monkeypatch.setattr(task_executor, "AUTOMATIC_TASK_EXECUTION_ENABLED", True)
    monkeypatch.setattr(task_executor, "async_session", lambda: db)

    await task_executor.execute_task(task_id, agent_id)

    assert task.status == "done"
    assert db.commits == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_agent_manager_cannot_rewrite_another_users_executable_task(
    monkeypatch,
):
    from app.api import tasks
    from app.schemas.schemas import TaskUpdate

    current_user = SimpleNamespace(id=uuid.uuid4())
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
    )
    db = _DB([task])
    monkeypatch.setattr(
        tasks,
        "check_agent_access",
        AsyncMock(return_value=(SimpleNamespace(), "manage")),
    )

    with pytest.raises(HTTPException) as captured:
        await tasks.update_task(
            task.agent_id,
            task.id,
            TaskUpdate(title="rewritten work"),
            current_user,
            db,
        )

    assert captured.value.status_code == 403
    assert db.commits == 0


@pytest.mark.asyncio
async def test_agent_manager_cannot_run_a_task_as_its_original_creator(monkeypatch):
    from app.api import tasks

    current_user = SimpleNamespace(id=uuid.uuid4())
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        type="todo",
        status="pending",
    )
    db = _DB([task])
    monkeypatch.setattr(
        tasks,
        "check_agent_access",
        AsyncMock(return_value=(SimpleNamespace(), "manage")),
    )
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _agent: False)

    with pytest.raises(HTTPException) as captured:
        await tasks.trigger_task(
            task.agent_id,
            task.id,
            current_user,
            db,
        )

    assert captured.value.status_code == 403


@pytest.mark.asyncio
async def test_background_llm_caller_uses_explicit_requester_boundary(monkeypatch):
    from app.services.llm import call_agent_llm_with_tools

    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    requester = uuid.uuid4()
    db = _DB([agent])
    access = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.core.permissions.get_agent_access_level_for_user_id", access
    )

    result = await call_agent_llm_with_tools(
        db=db,
        agent_id=agent.id,
        system_prompt="system",
        user_prompt="work",
        requester_user_id=requester,
    )

    assert result == "⚠️ Automation requester no longer has access to this Agent"
    access.assert_awaited_once_with(db, requester, agent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "deletion_requested", "expired"),
    [
        ("stopped", False, False),
        ("error", False, False),
        ("running", True, False),
        ("idle", False, True),
    ],
)
async def test_approved_tool_rechecks_agent_lifecycle_before_dispatch(
    monkeypatch,
    status,
    deletion_requested,
    expired,
):
    from app.services import autonomy_service

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status=status,
        deletion_requested_at=(object() if deletion_requested else None),
    )
    db = _DB([agent])
    monkeypatch.setattr(autonomy_service, "async_session", lambda: db)
    monkeypatch.setattr(
        "app.core.permissions.is_agent_expired",
        lambda _agent: expired,
    )

    with pytest.raises(ValueError, match="no longer executable"):
        await autonomy_service.AutonomyService()._assert_execution_permission(
            agent.id,
            "write_file",
        )


@pytest.mark.asyncio
async def test_approved_tool_allows_current_idle_agent_and_tool_grant(monkeypatch):
    from app.services import autonomy_service

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="idle",
        deletion_requested_at=None,
    )
    tool = SimpleNamespace(
        enabled=True,
        is_default=True,
        tenant_id=None,
        source="builtin",
    )
    db = _DB([agent, (tool, None)])
    monkeypatch.setattr(autonomy_service, "async_session", lambda: db)
    monkeypatch.setattr(
        "app.core.permissions.is_agent_expired",
        lambda _agent: False,
    )
    monkeypatch.setattr(
        "app.services.agent_tools._code_tool_denial_reason",
        AsyncMock(return_value=None),
    )

    await autonomy_service.AutonomyService()._assert_execution_permission(
        agent.id,
        "write_file",
    )


@pytest.mark.asyncio
async def test_approved_tool_rejects_disabled_requester_identity_before_dispatch(
    monkeypatch,
):
    from app.services import autonomy_service

    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    requester = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        identity=SimpleNamespace(is_active=False),
    )
    db = _DB([agent, requester])
    access = AsyncMock(return_value="use")
    monkeypatch.setattr(autonomy_service, "async_session", lambda: db)
    monkeypatch.setattr(
        "app.core.permissions.get_agent_access_level_for_user_id",
        access,
    )

    with pytest.raises(ValueError, match="no longer active"):
        await autonomy_service.AutonomyService()._assert_requester_execution_scope(
            agent.id,
            requester.id,
            None,
        )

    access.assert_not_awaited()
