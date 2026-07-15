import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.services.trigger_runtime import invoker


class FakeSession:
    def __init__(self, rows=None, results=None):
        self.rows = rows or {}
        self.results = list(results or [])
        self.committed = False
        self.added = []

    async def get(self, model, row_id):
        return self.rows.get((model, row_id))

    async def execute(self, _statement):
        if not self.results:
            raise AssertionError("unexpected execute() call")
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class FakeSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def trigger_with_config(config):
    return SimpleNamespace(config=config)


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_ordinary_delivery_ignores_legacy_a2a_session_config(monkeypatch):
    tenant_id = uuid.uuid4()
    current_agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
    )
    session_id = uuid.uuid4()
    unrelated_session = SimpleNamespace(
        id=session_id,
        agent_id=uuid.uuid4(),
        peer_agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_channel="agent",
    )
    db = FakeSession({(ChatSession, session_id): unrelated_session})
    access = AsyncMock(return_value="use")
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(invoker, "get_agent_access_level_for_user_id", access)

    target = await invoker.resolve_trigger_delivery_target(
        current_agent,
        [trigger_with_config({"_a2a_session_id": str(session_id)})],
    )

    assert target is None
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_origin_user_delivery_rejects_cross_tenant_or_revoked_access(monkeypatch):
    current_agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
    )
    target_user_id = uuid.uuid4()
    db = FakeSession()
    ensure_primary = AsyncMock()
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(
        invoker,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.chat_session_service.ensure_primary_platform_session",
        ensure_primary,
    )

    target = await invoker.resolve_trigger_delivery_target(
        current_agent,
        [trigger_with_config({"_origin_user_id": str(target_user_id)})],
    )

    assert target is None
    ensure_primary.assert_not_awaited()
    assert db.committed is False


@pytest.mark.asyncio
async def test_validated_session_owner_controls_notification_attribution(monkeypatch):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    session_owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=creator_id,
    )
    origin_session_id = uuid.uuid4()
    primary_session_id = uuid.uuid4()
    origin_session = SimpleNamespace(
        id=origin_session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=session_owner_id,
        source_channel="web",
    )
    primary_session = SimpleNamespace(
        id=primary_session_id,
        user_id=session_owner_id,
        source_channel="web",
    )
    db = FakeSession({(ChatSession, origin_session_id): origin_session})
    ensure_primary = AsyncMock(return_value=primary_session)
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(
        invoker,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value="use"),
    )
    monkeypatch.setattr(
        "app.services.chat_session_service.ensure_primary_platform_session",
        ensure_primary,
    )

    target = await invoker.resolve_trigger_delivery_target(
        agent,
        [
            trigger_with_config(
                {
                    "_origin_session_id": str(origin_session_id),
                    "_origin_user_id": str(session_owner_id),
                    "_origin_source_channel": "web",
                }
            )
        ],
    )
    message, payload = invoker._build_trigger_delivery_notification(
        agent_id=agent_id,
        delivery_target=target,
        content="scheduled result",
        triggers=["schedule"],
    )

    assert target == {
        "kind": "primary_user_session",
        "session_id": str(primary_session_id),
        "owner_user_id": str(session_owner_id),
        "source_channel": "web",
    }
    assert creator_id != session_owner_id
    assert message.user_id == session_owner_id
    assert payload["message_id"] == str(message.id)
    ensure_primary.assert_awaited_once_with(db, agent_id, session_owner_id)
    assert db.committed is True


@pytest.mark.asyncio
async def test_ordinary_delivery_never_uses_legacy_a2a_session_config(monkeypatch):
    tenant_id = uuid.uuid4()
    current_agent_id = uuid.uuid4()
    peer_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    current_agent = SimpleNamespace(
        id=current_agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
    )
    a2a_session = SimpleNamespace(
        id=session_id,
        agent_id=current_agent_id,
        peer_agent_id=peer_agent_id,
        user_id=owner_user_id,
        source_channel="agent",
    )
    cross_tenant_peer = SimpleNamespace(
        id=peer_agent_id,
        tenant_id=uuid.uuid4(),
    )
    db = FakeSession(
        {
            (ChatSession, session_id): a2a_session,
            (Agent, peer_agent_id): cross_tenant_peer,
        }
    )
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(
        invoker,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value="use"),
    )

    target = await invoker.resolve_trigger_delivery_target(
        current_agent,
        [trigger_with_config({"_a2a_session_id": str(session_id)})],
    )

    assert target is None


@pytest.mark.asyncio
async def test_invalid_a2a_production_path_stops_before_model_or_llm(monkeypatch):
    current_agent_id = uuid.uuid4()
    current_agent = SimpleNamespace(
        id=current_agent_id,
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        is_expired=False,
    )
    session_id = uuid.uuid4()
    unrelated_session = SimpleNamespace(
        id=session_id,
        agent_id=uuid.uuid4(),
        peer_agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_channel="agent",
    )
    db = FakeSession(
        {(ChatSession, session_id): unrelated_session},
        results=[ScalarResult(current_agent)],
    )
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        type="a2a",
        name="__a2a_wake__",
        is_system=True,
        config={"_a2a_session_id": str(session_id)},
        reason="Process a queued A2A message",
        focus_ref=None,
    )
    resolve_model = AsyncMock()
    call_llm = AsyncMock()
    capture_failure = AsyncMock()
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(invoker, "get_agent_access_level_for_user_id", AsyncMock())
    monkeypatch.setattr("app.services.llm.resolve_agent_model", resolve_model)
    monkeypatch.setattr("app.services.llm.call_llm", call_llm)
    monkeypatch.setattr(invoker, "_capture_invocation_failure", capture_failure)

    await invoker.invoke_agent_for_triggers(current_agent_id, [trigger])

    resolve_model.assert_not_awaited()
    call_llm.assert_not_awaited()
    assert db.added == []
    assert capture_failure.await_count == 1
    assert isinstance(capture_failure.await_args.args[1], PermissionError)


@pytest.mark.asyncio
async def test_a2a_reply_sink_revalidates_and_uses_session_owner(monkeypatch):
    tenant_id = uuid.uuid4()
    current_agent_id = uuid.uuid4()
    peer_agent_id = uuid.uuid4()
    session_owner_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    session_id = uuid.uuid4()
    participant = SimpleNamespace(id=uuid.uuid4())
    current_agent = SimpleNamespace(
        id=current_agent_id,
        tenant_id=tenant_id,
        creator_id=creator_id,
    )
    a2a_session = SimpleNamespace(
        id=session_id,
        agent_id=current_agent_id,
        peer_agent_id=peer_agent_id,
        user_id=session_owner_id,
        source_channel="agent",
        last_message_at=None,
    )
    peer_agent = SimpleNamespace(id=peer_agent_id, tenant_id=tenant_id)
    db = FakeSession(
        {
            (ChatSession, session_id): a2a_session,
            (Agent, peer_agent_id): peer_agent,
        },
        results=[ScalarResult(participant)],
    )
    monkeypatch.setattr(invoker, "async_session", FakeSessionFactory(db))
    monkeypatch.setattr(
        invoker,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value="use"),
    )

    await invoker._persist_validated_a2a_reply(
        agent=current_agent,
        target={
            "session_id": str(session_id),
            "owner_user_id": str(session_owner_id),
        },
        content="completed task",
    )

    assert creator_id != session_owner_id
    assert len(db.added) == 1
    assert db.added[0].conversation_id == str(session_id)
    assert db.added[0].user_id == session_owner_id
    assert db.added[0].content == "completed task"
    assert db.committed is True
    assert a2a_session.last_message_at is not None
