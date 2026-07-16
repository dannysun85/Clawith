"""Regression tests for durable and isolated async A2A delivery."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.agent_tools import A2AContext


class DummyResult:
    def __init__(self, *, scalar_value=None, rows=None):
        self.scalar_value = scalar_value
        self.rows = list(rows or [])

    def scalar_one_or_none(self):
        return self.scalar_value

    def all(self):
        return self.rows


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.statements = []
        self.commit_count = 0
        self.flush_count = 0

    async def execute(self, statement, _params=None):
        self.statements.append(statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        pass

    async def get(self, _model, identity, **_kwargs):
        return SimpleNamespace(id=identity, user_id=uuid.uuid4())

    def get_bind(self):
        return None

    def begin_nested(self):
        @asynccontextmanager
        async def transaction():
            yield self

        return transaction()

    def expunge(self, _value):
        pass


@pytest.fixture(autouse=True)
def _authorized_a2a_behavior_contract(monkeypatch):
    """Exercise A2A behavior with the release gate explicitly enabled."""
    from app.services import a2a_authorization, agent_tools, trigger_daemon
    from app.services.trigger_runtime import config as trigger_config

    @asynccontextmanager
    async def noop_session():
        yield SimpleNamespace()

    monkeypatch.setattr(trigger_config, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    monkeypatch.setattr(trigger_daemon, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    monkeypatch.setattr(a2a_authorization, "validate_active_a2a_lane", AsyncMock())
    monkeypatch.setattr(agent_tools, "async_session", noop_session)


def _context(*, source_message_id=None, message="Prepare the report") -> A2AContext:
    source = MagicMock()
    source.id = uuid.uuid4()
    source.name = "Alice"
    target = MagicMock()
    target.id = uuid.uuid4()
    target.name = "Bob"
    return A2AContext(
        source_agent=source,
        target_agent=target,
        chat_session_id=str(uuid.uuid4()),
        source_message_id=source_message_id or uuid.uuid4(),
        session_agent_id=source.id,
        owner_id=uuid.uuid4(),
        src_participant_id=uuid.uuid4(),
        tgt_participant_id=uuid.uuid4(),
        msg_type="task_delegate",
        message_text=message,
        origin_source_channel="web",
        origin_session_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_wake_agent_with_context_commits_durable_a2a_execution():
    from app.services.trigger_daemon import wake_agent_with_context

    target_id = uuid.uuid4()
    source_id = uuid.uuid4()
    message_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    db = RecordingDB(
        responses=[
            DummyResult(scalar_value="Alice"),
            DummyResult(scalar_value=None),
        ]
    )

    with (
        patch("app.services.trigger_daemon.async_session") as session_factory,
        patch(
            "app.services.trigger_daemon.enqueue_trigger_execution",
            new_callable=AsyncMock,
            return_value=(MagicMock(), True),
        ) as enqueue,
    ):
        session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        accepted = await wake_agent_with_context(
            target_id,
            "[From Alice] Prepare the report",
            from_agent_id=source_id,
            a2a_session_id=session_id,
            message_kind="task_delegate",
            idempotency_key=f"a2a:{message_id}",
            source_message_id=message_id,
        )

    assert accepted is True
    trigger = db.added[0]
    assert isinstance(trigger, AgentTrigger)
    assert trigger.name == "__a2a_wake__"
    assert trigger.type == "a2a"
    assert trigger.is_system is True
    assert trigger.is_enabled is True
    assert db.commit_count == 1
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["source"] == "a2a"
    assert kwargs["idempotency_key"] == f"a2a:{message_id}"
    assert kwargs["payload_obj"]["_a2a_kind"] == "task_delegate"
    assert kwargs["payload_obj"]["_source_message_id"] == str(message_id)
    assert kwargs["payload_obj"]["_matched_from_agent_id"] == str(source_id)
    assert kwargs["payload_obj"]["_a2a_session_id"] == session_id


@pytest.mark.asyncio
async def test_wake_agent_with_context_treats_idempotent_retry_as_accepted():
    from app.services.trigger_daemon import wake_agent_with_context

    target_id = uuid.uuid4()
    existing = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=target_id,
        name="__a2a_wake__",
        type="a2a",
        config={},
        reason="existing",
        is_enabled=True,
        is_system=True,
    )
    db = RecordingDB(responses=[DummyResult(scalar_value=existing)])

    with (
        patch("app.services.trigger_daemon.async_session") as session_factory,
        patch(
            "app.services.trigger_daemon.enqueue_trigger_execution",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
    ):
        session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        accepted = await wake_agent_with_context(
            target_id,
            "retry",
            idempotency_key="a2a:stable",
        )

    assert accepted is True


def test_a2a_invocation_batches_never_merge_durable_messages():
    from app.services.trigger_daemon import _build_invocation_batches

    agent_id = uuid.uuid4()
    ordinary_one = AgentTrigger(
        id=uuid.uuid4(), agent_id=agent_id, name="cron", type="cron", config={}, reason=""
    )
    a2a_one = AgentTrigger(
        id=uuid.uuid4(), agent_id=agent_id, name="__a2a_wake__", type="a2a", config={}, reason=""
    )
    a2a_two = AgentTrigger(
        id=uuid.uuid4(), agent_id=agent_id, name="__a2a_wake__", type="a2a", config={}, reason=""
    )
    ordinary_two = AgentTrigger(
        id=uuid.uuid4(), agent_id=agent_id, name="poll", type="poll", config={}, reason=""
    )

    batches = _build_invocation_batches(
        [ordinary_one, a2a_one, a2a_two, ordinary_two]
    )

    assert [[trigger.type for trigger in batch] for batch in batches] == [
        ["cron"],
        ["a2a"],
        ["a2a"],
        ["poll"],
    ]


def test_ordinary_trigger_batches_are_isolated_by_delivery_principal():
    from app.services.trigger_daemon import _build_invocation_batches

    agent_id = uuid.uuid4()
    first_user_id = uuid.uuid4()
    second_user_id = uuid.uuid4()
    first_session_id = uuid.uuid4()
    second_session_id = uuid.uuid4()
    triggers = [
        AgentTrigger(
            id=uuid.uuid4(),
            agent_id=agent_id,
            name="first-a",
            type="cron",
            config={
                "_origin_user_id": str(first_user_id),
                "_origin_session_id": str(first_session_id),
                "_origin_source_channel": "web",
            },
            reason="first",
        ),
        AgentTrigger(
            id=uuid.uuid4(),
            agent_id=agent_id,
            name="first-b",
            type="interval",
            config={
                "_origin_user_id": str(first_user_id),
                "_origin_session_id": str(first_session_id),
                "_origin_source_channel": "web",
            },
            reason="first again",
        ),
        AgentTrigger(
            id=uuid.uuid4(),
            agent_id=agent_id,
            name="second",
            type="poll",
            config={
                "_origin_user_id": str(second_user_id),
                "_origin_session_id": str(second_session_id),
                "_origin_source_channel": "web",
            },
            reason="second",
        ),
    ]

    batches = _build_invocation_batches(triggers)

    assert [[trigger.name for trigger in batch] for batch in batches] == [
        ["first-a", "first-b"],
        ["second"],
    ]


@pytest.mark.asyncio
async def test_waiting_batch_claim_is_renewed_while_first_batch_runs(monkeypatch):
    from app.services import trigger_daemon

    agent_id = uuid.uuid4()
    first_execution_id = uuid.uuid4()
    second_execution_id = uuid.uuid4()
    first = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="first",
        type="cron",
        config={
            "_execution_id": str(first_execution_id),
            "_execution_lease_token": "worker:first",
        },
        reason="first",
    )
    second = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="second",
        type="cron",
        config={
            "_execution_id": str(second_execution_id),
            "_execution_lease_token": "worker:second",
        },
        reason="second",
    )
    waiting_claim_renewed = asyncio.Event()
    invocation_order: list[str] = []

    async def renew_waiting(claims):
        assert claims == [(second_execution_id, "worker:second")]
        waiting_claim_renewed.set()
        return len(claims)

    async def invoke(_agent_id, triggers):
        invocation_order.append(triggers[0].name)
        if triggers[0].name == "first":
            await asyncio.wait_for(waiting_claim_renewed.wait(), timeout=1)

    monkeypatch.setattr(
        trigger_daemon,
        "_WAITING_BATCH_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(trigger_daemon, "renew_trigger_execution_leases", renew_waiting)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)

    await trigger_daemon._invoke_agent_batches(agent_id, [[first], [second]])

    assert invocation_order == ["first", "second"]
    assert waiting_claim_renewed.is_set()


@pytest.mark.parametrize(
    "renew_outcome",
    [0, RuntimeError("temporary database failure")],
    ids=["short-count", "renew-error"],
)
@pytest.mark.asyncio
async def test_waiting_batch_renewal_failure_cancels_before_later_execution(
    monkeypatch,
    renew_outcome,
):
    from app.services import trigger_daemon

    agent_id = uuid.uuid4()
    first = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="first",
        type="cron",
        config={
            "_execution_id": str(uuid.uuid4()),
            "_execution_lease_token": "worker:first",
        },
        reason="first",
    )
    second = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="second",
        type="cron",
        config={
            "_execution_id": str(uuid.uuid4()),
            "_execution_lease_token": "worker:second",
        },
        reason="second",
    )
    invocation_order: list[str] = []
    active_batch_blocker = asyncio.Event()

    async def invoke(_agent_id, triggers):
        invocation_order.append(triggers[0].name)
        await active_batch_blocker.wait()

    async def renew_waiting(_claims):
        if isinstance(renew_outcome, Exception):
            raise renew_outcome
        return renew_outcome

    monkeypatch.setattr(
        trigger_daemon,
        "_WAITING_BATCH_LEASE_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        trigger_daemon,
        "renew_trigger_execution_leases",
        renew_waiting,
    )
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)

    task = asyncio.create_task(
        trigger_daemon._invoke_agent_batches(agent_id, [[first], [second]])
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert invocation_order == ["first"]


def test_a2a_claim_head_includes_live_processing_execution():
    from inspect import getsource

    from app.services.trigger_runtime.executions import claim_pending_trigger_executions

    source = getsource(claim_pending_trigger_executions)
    assert 'status.in_(("pending", "processing"))' in source
    assert "earlier_is_unfinished" in source
    assert "earlier_execution.source" not in source
    assert "uq_trigger_executions_processing_agent" in str(
        TriggerExecution.__table__.indexes
    )


@pytest.mark.asyncio
async def test_default_execution_claim_sources_include_a2a():
    from app.services.trigger_runtime.executions import claim_pending_trigger_executions

    db = RecordingDB(responses=[DummyResult(), DummyResult(rows=[])])
    with patch(
        "app.services.trigger_runtime.executions.async_session"
    ) as session_factory:
        session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await claim_pending_trigger_executions() == []

    compiled_params = db.statements[1].compile().params
    assert any(
        isinstance(value, list) and "a2a" in value
        for value in compiled_params.values()
    )


@pytest.mark.asyncio
async def test_notify_reports_queue_rejection_instead_of_false_success():
    from app.services.agent_tools import _a2a_handle_notify

    ctx = _context()
    ctx.msg_type = "notify"
    with patch(
        "app.services.agent_tools._wake_agent_async",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await _a2a_handle_notify(ctx)

    assert result.startswith("❌")
    assert "could not be queued" in result


@pytest.mark.asyncio
async def test_delegate_queue_rejection_cleans_callback_state():
    from app.services.agent_tools import _a2a_handle_task_delegate

    ctx = _context()
    with (
        patch(
            "app.services.agent_tools._create_on_message_trigger",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.agent_tools._wake_agent_async",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.agent_tools._cleanup_failed_delegate",
            new_callable=AsyncMock,
        ) as cleanup,
    ):
        result = await _a2a_handle_task_delegate(ctx)

    assert result.startswith("❌")
    cleanup.assert_awaited_once()
