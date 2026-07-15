import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.websocket import WebSocketChatHandler, generic_llm_failure_user_message
from app.services.llm import caller as llm_caller
from app.services.llm.caller import RouteMeta


class FakeWebSocket:
    def __init__(self):
        self.receive_calls = 0
        self.sent = []
        self.close_code = None

    async def receive_json(self):
        self.receive_calls += 1
        return {"content": "from-socket"}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=None):
        self.close_code = code


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class RecordingDB:
    def __init__(self, values):
        self.values = list(values)
        self.committed = False
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def execute(self, _statement):
        if not self.values:
            raise AssertionError("unexpected execute() call")
        return ScalarResult(self.values.pop(0))

    async def commit(self):
        self.committed = True


class RecordingDBContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_generic_llm_failure_does_not_expose_internal_exception_details():
    message = generic_llm_failure_user_message()

    assert message.startswith("[LLM call error]")
    assert "sqlalchemy" not in message.lower()
    assert "api_key" not in message.lower()


@pytest.mark.asyncio
async def test_messages_received_during_generation_are_not_dropped():
    websocket = FakeWebSocket()
    handler = WebSocketChatHandler(websocket, uuid.uuid4(), "token")
    handler.pending_messages.extend([
        {"content": "queued-first"},
        {"content": "queued-second"},
    ])

    assert await handler._receive_next_message() == {"content": "queued-first"}
    assert await handler._receive_next_message() == {"content": "queued-second"}
    assert websocket.receive_calls == 0
    assert await handler._receive_next_message() == {"content": "from-socket"}
    assert websocket.receive_calls == 1


@pytest.mark.asyncio
async def test_message_loop_uses_validated_client_id_and_emits_assistant_id():
    websocket = FakeWebSocket()
    client_message_id = uuid.uuid4()
    handler = WebSocketChatHandler(websocket, uuid.uuid4(), "token", str(uuid.uuid4()))
    handler.user = SimpleNamespace(id=uuid.uuid4())
    handler.agent_name = "Test Agent"
    handler.agent_type = ""
    handler.conv_id = handler.session_id_param
    handler._receive_next_message = AsyncMock(
        side_effect=[
            {
                "content": "hello",
                "display_content": "hello",
                "client_message_id": str(client_message_id),
            },
            StopAsyncIteration,
        ]
    )
    handler._resolve_route = AsyncMock(return_value=(None, None))
    handler._check_quotas = AsyncMock(return_value=True)
    handler._save_user_message = AsyncMock(return_value="user-message-1")
    handler._save_assistant_reply = AsyncMock(return_value="assistant-message-1")

    with pytest.raises(StopAsyncIteration):
        await handler.message_loop()

    handler._save_user_message.assert_awaited_once_with(
        "hello",
        "hello",
        "",
        False,
        message_id=client_message_id,
    )
    assert websocket.sent == [
        {
            "type": "done",
            "role": "assistant",
            "content": (
                "⚠️ Test Agent has no LLM model configured. "
                "Please select a tier in the agent's Settings tab or ask an admin to configure model routes."
            ),
            "message_id": "assistant-message-1",
        },
    ]


@pytest.mark.asyncio
async def test_saved_chat_messages_return_their_database_ids(monkeypatch):
    user_db = RecordingDB([None])
    assistant_db = RecordingDB([])
    dbs = iter([user_db, assistant_db])
    monkeypatch.setattr(
        "app.api.websocket.async_session",
        lambda: RecordingDBContext(next(dbs)),
    )
    monkeypatch.setattr(
        "app.api.websocket.maybe_mark_session_read_for_active_viewer",
        AsyncMock(),
    )

    handler = WebSocketChatHandler(FakeWebSocket(), uuid.uuid4(), "token", str(uuid.uuid4()))
    handler.user = SimpleNamespace(id=uuid.uuid4())
    handler.conv_id = handler.session_id_param

    requested_user_message_id = uuid.uuid4()
    user_message_id = await handler._save_user_message(
        "hello",
        "hello",
        "",
        False,
        message_id=requested_user_message_id,
    )
    assistant_message_id = await handler._save_assistant_reply("world", [])

    assert user_message_id == str(user_db.added[0].id)
    assert user_message_id == str(requested_user_message_id)
    assert assistant_message_id == str(assistant_db.added[0].id)
    assert user_db.committed is True
    assert assistant_db.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_channel", "is_group", "same_user", "same_agent"),
    [
        ("agent", False, True, True),
        ("web", True, True, True),
        ("web", False, False, True),
        ("web", False, True, False),
    ],
)
async def test_websocket_rejects_non_private_web_session(
    source_channel,
    is_group,
    same_user,
    same_agent,
):
    websocket = FakeWebSocket()
    requested_agent_id = uuid.uuid4()
    requested_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    handler = WebSocketChatHandler(
        websocket,
        requested_agent_id,
        "token",
        str(session_id),
    )
    session = SimpleNamespace(
        id=session_id,
        agent_id=requested_agent_id if same_agent else uuid.uuid4(),
        user_id=requested_user_id if same_user else uuid.uuid4(),
        source_channel=source_channel,
        is_group=is_group,
    )

    resolved = await handler._resolve_chat_session(RecordingDB([session]), requested_user_id)

    assert resolved is None
    assert websocket.sent == [{"type": "error", "content": "Not authorized for this session"}]
    assert websocket.close_code == 4003


@pytest.mark.asyncio
async def test_websocket_accepts_owners_private_web_session():
    websocket = FakeWebSocket()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    handler = WebSocketChatHandler(websocket, agent_id, "token", str(session_id))
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        is_group=False,
        model_tier="ultra",
        model_modality="image",
    )

    resolved = await handler._resolve_chat_session(RecordingDB([session]), user_id)

    assert resolved == str(session_id)
    assert websocket.sent == []
    assert websocket.close_code is None
    assert handler.session_model_tier == "ultra"
    assert handler.session_model_modality == "image"


@pytest.mark.asyncio
async def test_websocket_persists_session_route_without_overwriting_user_preference(monkeypatch):
    websocket = FakeWebSocket()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        is_group=False,
        model_tier=None,
        model_modality=None,
    )
    db = RecordingDB([session])
    monkeypatch.setattr("app.api.websocket.async_session", lambda: RecordingDBContext(db))

    handler = WebSocketChatHandler(websocket, agent_id, "token", str(session_id))
    handler.user = SimpleNamespace(id=user_id, preferred_chat_tier=None)
    handler.conv_id = str(session_id)

    await handler._persist_session_model_selection("pro", "text")

    assert session.model_tier == "pro"
    assert session.model_modality == "text"
    assert handler.session_model_tier == "pro"
    assert handler.session_model_modality == "text"
    assert handler.user.preferred_chat_tier is None
    assert db.committed is True


@pytest.mark.asyncio
async def test_attachment_route_persists_tier_but_not_ephemeral_media_modality(monkeypatch):
    websocket = FakeWebSocket()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        is_group=False,
        model_tier="pro",
        model_modality="text",
    )
    db = RecordingDB([session])
    monkeypatch.setattr("app.api.websocket.async_session", lambda: RecordingDBContext(db))

    handler = WebSocketChatHandler(websocket, agent_id, "token", str(session_id))
    handler.user = SimpleNamespace(id=user_id, preferred_chat_tier=None)
    handler.conv_id = str(session_id)
    handler.session_model_tier = "lite"
    handler.session_model_modality = "text"

    await handler._persist_session_model_selection("pro", None)

    assert session.model_tier == "pro"
    assert session.model_modality == "text"
    assert handler.session_model_tier == "pro"
    assert handler.session_model_modality == "text"
    assert db.committed is True


@pytest.mark.asyncio
async def test_route_resolution_reloads_current_agent_for_existing_session(monkeypatch):
    websocket = FakeWebSocket()
    agent_id = uuid.uuid4()
    first_agent = SimpleNamespace(id=agent_id, preferred_model_tier="lite")
    updated_agent = SimpleNamespace(id=agent_id, preferred_model_tier="pro")
    first_model = SimpleNamespace(model="first")
    updated_model = SimpleNamespace(model="updated")
    dbs = iter([RecordingDB([first_agent]), RecordingDB([updated_agent])])
    resolved_agents = []

    monkeypatch.setattr(
        "app.api.websocket.async_session",
        lambda: RecordingDBContext(next(dbs)),
    )

    async def fake_resolve_agent_model(agent, *, tier, modality):
        resolved_agents.append((agent, tier, modality))
        model = first_model if agent is first_agent else updated_model
        return model, None, RouteMeta(saas_tier="pro", modality="text")

    monkeypatch.setattr(llm_caller, "resolve_agent_model", fake_resolve_agent_model)

    handler = WebSocketChatHandler(websocket, agent_id, "token")
    first, _ = await handler._resolve_route(tier=None, modality="text")
    second, _ = await handler._resolve_route(tier=None, modality="text")

    assert first is first_model
    assert second is updated_model
    assert [entry[0] for entry in resolved_agents] == [first_agent, updated_agent]
    assert handler.llm_model is updated_model
    assert handler.current_route_meta == RouteMeta(saas_tier="pro", modality="text")
