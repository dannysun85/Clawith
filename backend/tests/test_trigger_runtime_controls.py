import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.models.trigger import AgentTrigger


@pytest.mark.asyncio
async def test_dispatch_forwards_bounded_claim_limit(monkeypatch):
    from app.services.trigger_runtime import dispatch

    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(dispatch, "claim_pending_trigger_executions", claim)

    fired, forced = await dispatch.claim_ready_trigger_invocations(
        datetime.now(timezone.utc),
        limit=8,
    )

    claim.assert_awaited_once_with(limit=8)
    assert fired == {}
    assert forced == set()


@pytest.mark.asyncio
async def test_150_agent_backlog_cannot_exceed_worker_concurrency(monkeypatch):
    from app.services import trigger_daemon
    from app.services.trigger_runtime import dispatch

    agents = [uuid.uuid4() for _ in range(150)]
    pairs = [
        (
            object(),
            AgentTrigger(
                id=uuid.uuid4(),
                agent_id=agent_id,
                name=f"load-{index}",
                type="cron",
                config={},
                reason="load test",
                is_enabled=True,
                fire_count=0,
                cooldown_seconds=60,
                is_system=False,
            ),
        )
        for index, agent_id in enumerate(agents)
    ]

    async def bounded_claim(*, limit):
        return pairs[:limit]

    monkeypatch.setattr(dispatch, "claim_pending_trigger_executions", bounded_claim)
    monkeypatch.setattr(dispatch, "mark_base_triggers_fired", AsyncMock())
    monkeypatch.setattr(
        dispatch,
        "build_execution_runtime_trigger",
        lambda trigger, _execution: trigger,
    )
    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_MAX_CONCURRENCY", 8)
    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_CLAIM_BATCH_SIZE", 16)
    trigger_daemon._invocation_tasks.clear()

    fired, _forced = await dispatch.claim_ready_trigger_invocations(
        datetime.now(timezone.utc),
        limit=min(
            trigger_daemon._claim_batch_size(),
            trigger_daemon._available_invocation_slots(),
        ),
    )

    assert len(fired) == 8
    assert set(fired).issubset(set(agents))


@pytest.mark.asyncio
async def test_active_invocations_reduce_claim_capacity(monkeypatch):
    from app.services import trigger_daemon

    blocker = asyncio.Event()
    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_MAX_CONCURRENCY", 8)
    trigger_daemon._invocation_tasks.clear()
    tasks = {
        asyncio.create_task(blocker.wait())
        for _ in range(5)
    }
    trigger_daemon._invocation_tasks.update(tasks)

    try:
        assert trigger_daemon._active_invocation_count() == 5
        assert trigger_daemon._available_invocation_slots() == 3
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        trigger_daemon._invocation_tasks.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "trigger_name"),
    [
        ("handle_okr_collection_trigger", "daily_okr_collection"),
        ("handle_okr_report_trigger", "daily_okr_report"),
    ],
)
async def test_okr_automation_kill_switch_avoids_database_and_llm_work(
    monkeypatch,
    handler_name,
    trigger_name,
):
    from app.services.trigger_runtime import evaluator

    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name=trigger_name,
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="system automation",
        is_enabled=True,
        fire_count=0,
        cooldown_seconds=60,
        is_system=True,
    )
    monkeypatch.setattr(evaluator.runtime_settings, "OKR_AUTOMATION_ENABLED", False)
    handler = getattr(evaluator, handler_name)

    with patch("app.services.trigger_runtime.evaluator.async_session") as session:
        handled = await handler(trigger, datetime.now(timezone.utc))

    assert handled is True
    session.assert_not_called()
    assert await evaluator.evaluate_trigger(trigger, datetime.now(timezone.utc)) is False


@pytest.mark.asyncio
async def test_trigger_database_failure_enters_privacy_safe_issue_ledger(monkeypatch):
    from app.services.trigger_runtime import invoker

    capture = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(
        "app.services.production_issue_monitor.record_production_issue",
        capture,
    )
    agent_id = uuid.uuid4()

    await invoker._capture_invocation_failure(
        agent_id,
        SQLAlchemyTimeoutError("provider response and prompt must stay private"),
    )

    kwargs = capture.await_args.kwargs
    assert kwargs["category"] == "database"
    assert kwargs["error_code"] == "TimeoutError"
    assert kwargs["agent_id"] == agent_id
    assert "provider response" not in str(kwargs)


def test_okr_shutdown_migration_preserves_tenant_settings_and_never_auto_restarts():
    from pathlib import Path

    migration = (
        Path(__file__).parents[1]
        / "alembic/versions/094_disable_system_okr_automation.py"
    ).read_text(encoding="utf-8")

    assert "UPDATE agent_triggers" in migration
    assert "UPDATE trigger_executions" in migration
    assert "UPDATE okr_settings" not in migration
    assert "Never restart token-consuming automation" in migration
