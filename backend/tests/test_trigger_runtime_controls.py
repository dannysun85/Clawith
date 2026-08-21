import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution


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


def test_stored_reserved_config_cannot_override_runtime_routing_or_execution_fence():
    from app.services.trigger_runtime.executions import build_execution_runtime_trigger

    agent_id = uuid.uuid4()
    trusted_user_id = uuid.uuid4()
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="webhook",
        type="webhook",
        config={"_origin_user_id": str(trusted_user_id)},
        reason="webhook",
        is_enabled=True,
        is_system=False,
    )
    execution = TriggerExecution(
        id=uuid.uuid4(),
        trigger_id=trigger.id,
        agent_id=agent_id,
        source="webhook",
        status="processing",
        idempotency_key="delivery-1",
        payload={
            "_origin_user_id": str(uuid.uuid4()),
            "_a2a_session_id": str(uuid.uuid4()),
            "_execution_id": None,
            "_execution_lease_token": "attacker-lease",
        },
        payload_text='{"_a2a_session_id":"external-data"}',
        lease_owner="worker:trusted-generation",
    )

    runtime = build_execution_runtime_trigger(trigger, execution)

    assert "_origin_user_id" not in runtime.config
    assert "_a2a_session_id" not in runtime.config
    assert runtime.config["_execution_id"] == str(execution.id)
    assert runtime.config["_execution_lease_token"] == execution.lease_owner
    assert runtime.config["_webhook_payload"] == execution.payload_text


def test_service_execution_payload_uses_an_explicit_allowlist():
    from app.services.trigger_runtime.executions import build_execution_runtime_trigger

    agent_id = uuid.uuid4()
    origin_user_id = uuid.uuid4()
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="scheduled",
        type="cron",
        config={},
        reason="scheduled",
        is_enabled=True,
        is_system=False,
    )
    execution = TriggerExecution(
        id=uuid.uuid4(),
        trigger_id=trigger.id,
        agent_id=agent_id,
        source="cron",
        status="processing",
        idempotency_key="scheduled-1",
        payload={
            "_origin_user_id": str(origin_user_id),
            "untrusted_extra": "must-not-enter-runtime-config",
            "_execution_lease_token": "must-not-override",
        },
        payload_text="",
        lease_owner="worker:trusted-generation",
    )

    runtime = build_execution_runtime_trigger(trigger, execution)

    assert runtime.config["_origin_user_id"] == str(origin_user_id)
    assert "untrusted_extra" not in runtime.config
    assert runtime.config["_execution_lease_token"] == execution.lease_owner


def test_unmarked_stored_message_context_never_enters_execution_payload():
    from app.services.trigger_runtime.dispatch import runtime_execution_payload

    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="legacy",
        type="on_message",
        config={
            "from_agent_name": "Ray",
            "_matched_message": "legacy injected prompt",
            "_matched_message_id": str(uuid.uuid4()),
            "_origin_user_id": str(uuid.uuid4()),
        },
        reason="legacy",
        is_enabled=True,
        is_system=False,
    )

    assert runtime_execution_payload(trigger) == {}


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

    assert handled is False
    session.assert_not_called()
    assert await evaluator.evaluate_trigger(trigger, datetime.now(timezone.utc)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "trigger_name"),
    [
        ("handle_okr_collection_trigger", "daily_okr_collection"),
        ("handle_okr_report_trigger", "daily_okr_report"),
    ],
)
async def test_user_trigger_name_collision_is_not_disabled_as_system_okr(
    monkeypatch,
    handler_name,
    trigger_name,
):
    from app.services.trigger_runtime import evaluator

    now = datetime.now(timezone.utc)
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name=trigger_name,
        type="interval",
        config={"minutes": 30},
        reason="user-authored automation with a colliding name",
        is_enabled=True,
        fire_count=0,
        cooldown_seconds=60,
        is_system=False,
        created_at=now - timedelta(hours=1),
    )
    monkeypatch.setattr(evaluator.runtime_settings, "OKR_AUTOMATION_ENABLED", False)
    handler = getattr(evaluator, handler_name)

    with patch("app.services.trigger_runtime.evaluator.async_session") as session:
        handled = await handler(trigger, now)

    assert handled is False
    session.assert_not_called()
    assert await evaluator.evaluate_trigger(trigger, now) is True


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


def test_trigger_privacy_migration_uses_indexable_bounded_a2a_remapping():
    from pathlib import Path

    migration = (
        Path(__file__).parents[1]
        / "alembic/versions/099_trigger_privacy_serialization.py"
    ).read_text(encoding="utf-8")

    assert "CREATE TEMP TABLE _a2a_owner_sessions" in migration
    assert "CREATE UNIQUE INDEX _a2a_owner_sessions_lookup_idx" in migration
    assert "CREATE TEMP TABLE _a2a_source_message_payload_patches" in migration
    assert "jsonb_object_agg(" in migration
    assert "THEN (execution.payload ->> '_source_message_id')::uuid" in migration
    assert "THEN candidate.raw_session_id::uuid" in migration
    assert "source_message.id::text" not in migration


def test_execution_completion_cannot_overwrite_migration_or_operator_terminal_state():
    from inspect import getsource

    from app.services.trigger_runtime import executions, invoker

    completed = getsource(executions.mark_trigger_executions_completed)
    failed = getsource(executions.mark_trigger_executions_failed)
    renewed = getsource(executions.renew_trigger_execution_leases)
    assert 'TriggerExecution.status == "processing"' in completed
    assert 'TriggerExecution.status == "processing"' in failed
    assert 'TriggerExecution.status == "processing"' in renewed
    assert "TriggerExecution.lease_owner == lease_token" in getsource(
        executions._claim_fence
    )
    assert "update(TriggerExecution)" in completed
    assert "update(TriggerExecution)" in failed

    invocation = getsource(invoker.invoke_agent_for_triggers)
    assert invocation.index("await _stop_lease_renewal()\n            completed =") < invocation.index(
        "await mark_trigger_executions_completed(execution_claims)"
    )
    assert invocation.index("_successful_trigger_reply(reply, collected_content)") < invocation.index(
        'role="assistant"'
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "⚠️ 未配置 API key",
        "[LLM Error] provider unavailable",
        "[LLM call error] TimeoutError",
        "[Error] Tool execution failed",
        "⚠️ Credits 结算暂时不可用，本轮结果未执行，请稍后重试。",
    ],
)
def test_trigger_llm_error_outcomes_cannot_be_persisted_as_success(outcome):
    from app.services.trigger_runtime import invoker

    with pytest.raises(invoker.TriggerModelOutcomeError):
        invoker._successful_trigger_reply(outcome, ["partial provider output"])


def test_trigger_llm_content_remains_deliverable():
    from app.services.trigger_runtime import invoker

    assert invoker._successful_trigger_reply("完成", []) == "完成"
    assert invoker._successful_trigger_reply(None, ["分", "段"]) == "分段"


@pytest.mark.asyncio
async def test_execution_terminal_update_is_fenced_by_claim_generation(monkeypatch):
    from app.services.trigger_runtime import executions

    execution_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    lease_token = "worker-a:generation-2"

    class QueryResult:
        def __init__(self, rows):
            self.rows = list(rows)

        def scalars(self):
            return QueryResult(
                [row[0] if isinstance(row, tuple) else row for row in self.rows]
            )

        def all(self):
            return list(self.rows)

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            QueryResult([(trigger_id,)]),
            QueryResult(
                [
                    SimpleNamespace(
                        id=trigger_id,
                        type="cron",
                        max_fires=None,
                        fire_count=0,
                        is_enabled=True,
                    )
                ]
            ),
            QueryResult([(execution_id, trigger_id)]),
            QueryResult([]),
        ]
    )
    db.commit = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(executions, "async_session", lambda: session)

    updated = await executions.mark_trigger_executions_completed(
        [(execution_id, lease_token)]
    )

    statement = db.execute.await_args_list[2].args[0]
    params = statement.compile().params
    assert updated == 1
    assert execution_id in params.values()
    assert lease_token in params.values()
    db.commit.assert_awaited_once()


def test_runtime_trigger_carries_the_claim_generation_token():
    from app.models.trigger_execution import TriggerExecution
    from app.services.trigger_runtime.executions import build_execution_runtime_trigger

    agent_id = uuid.uuid4()
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="fenced",
        type="once",
        config={},
        reason="test",
        is_enabled=True,
        fire_count=0,
        cooldown_seconds=60,
        is_system=False,
    )
    execution = TriggerExecution(
        id=uuid.uuid4(),
        trigger_id=trigger.id,
        agent_id=agent_id,
        source="once",
        status="processing",
        idempotency_key="fenced-generation",
        lease_owner="worker-a:generation-7",
    )

    runtime_trigger = build_execution_runtime_trigger(trigger, execution)

    assert runtime_trigger.config["_execution_id"] == str(execution.id)
    assert runtime_trigger.config["_execution_lease_token"] == execution.lease_owner
