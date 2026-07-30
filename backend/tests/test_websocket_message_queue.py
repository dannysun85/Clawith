import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.websocket import (
    WebSocketChatHandler,
    _client_file_names,
    generic_llm_failure_user_message,
)
from app.services.chat_session_access import ChatSessionAuthorizationError
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


def test_structured_file_names_are_authoritative_and_preserve_commas():
    assert _client_file_names({
        "file_names": ["report,final_4875d85abdb4.png"],
        "file_name": "report,final_4875d85abdb4.png",
    }) == ["report,final_4875d85abdb4.png"]
    assert _client_file_names({"file_name": "legacy.png, demo.mp4"}) == (
        "legacy.png, demo.mp4"
    )
    assert _client_file_names({
        "file_names": ["valid.png", 7],
        "file_name": "legacy.png",
    }) == "legacy.png"


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
async def test_runtime_intake_uses_validated_client_message_id():
    websocket = FakeWebSocket()
    client_message_id = uuid.uuid4()
    handler = WebSocketChatHandler(websocket, uuid.uuid4(), "token", str(uuid.uuid4()))
    handler.user = SimpleNamespace(id=uuid.uuid4())
    handler.agent_name = "Test Agent"
    handler.agent_type = ""
    handler.conv_id = handler.session_id_param
    model = SimpleNamespace(id=uuid.uuid4())
    handler._resolve_effective_model = AsyncMock(return_value=model)
    handler._check_quotas = AsyncMock(return_value=True)
    handler._enqueue_runtime_chat = AsyncMock(return_value=None)
    handler._save_user_message = AsyncMock()
    handler._save_assistant_reply = AsyncMock()

    accepted = await handler._accept_client_message(
        {
            "content": "hello",
            "display_content": "hello",
            "client_message_id": str(client_message_id),
        }
    )

    assert accepted is None
    assert handler._enqueue_runtime_chat.await_args.kwargs["message_id"] == client_message_id
    handler._save_user_message.assert_not_awaited()
    handler._save_assistant_reply.assert_not_awaited()
    packet = websocket.sent[-1]
    assert packet["type"] == "error"
    assert packet["content"] == "Durable Runtime is not enabled for native Web Chat."
    assert packet["code"] == "runtime_disabled"
    assert packet["stage"] == "intake"
    assert packet["error"]["trace_id"] == packet["trace_id"]


@pytest.mark.asyncio
async def test_runtime_intake_preserves_selected_tier_and_ephemeral_media_route():
    handler = WebSocketChatHandler(FakeWebSocket(), uuid.uuid4(), "token", str(uuid.uuid4()))
    handler.user = SimpleNamespace(id=uuid.uuid4(), preferred_chat_tier="lite")
    handler.agent_name = "Test Agent"
    handler.agent_type = ""
    handler.conv_id = handler.session_id_param
    handler.session_model_tier = "lite"
    handler.session_model_modality = "text"
    model = SimpleNamespace(id=uuid.uuid4())

    async def resolve_model(_override, *, tier, modality):
        assert tier == "ultra"
        assert modality == "video"
        handler.current_route_meta = RouteMeta(saas_tier="ultra", modality="video")
        return model

    handler._resolve_effective_model = AsyncMock(side_effect=resolve_model)
    handler._persist_session_model_selection = AsyncMock()
    handler._check_quotas = AsyncMock(return_value=True)
    handler._enqueue_runtime_chat = AsyncMock(return_value=None)

    await handler._accept_client_message(
        {
            "content": "[video_data:data:video/mp4;base64,abc] Analyze this",
            "display_content": "Analyze this",
            "tier": "ultra",
            "modality": "video",
            "ephemeral_modality": True,
        }
    )

    handler._persist_session_model_selection.assert_awaited_once_with("ultra", None)
    assert handler._enqueue_runtime_chat.await_args.kwargs["saas_tier"] == "ultra"
    assert handler._enqueue_runtime_chat.await_args.kwargs["model_modality"] == "video"


@pytest.mark.asyncio
async def test_saved_chat_messages_return_their_database_ids(monkeypatch):
    user_db = RecordingDB([])
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

    session = SimpleNamespace(
        title="Session 1",
        last_message_at=None,
    )
    validate_lane = AsyncMock(return_value=SimpleNamespace(session=session))
    monkeypatch.setattr(
        "app.api.websocket.validate_active_user_chat_lane",
        validate_lane,
    )

    handler = WebSocketChatHandler(FakeWebSocket(), uuid.uuid4(), "token", str(uuid.uuid4()))
    handler.user = SimpleNamespace(id=uuid.uuid4())
    handler.auth_version = 0
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
    assert validate_lane.await_count == 2
    assert all(
        call.kwargs["expected_auth_version"] == 0
        for call in validate_lane.await_args_list
    )


@pytest.mark.asyncio
async def test_websocket_closes_revoked_identity_before_processing_next_message(
    monkeypatch,
):
    websocket = FakeWebSocket()
    db = RecordingDB([])
    validate_lane = AsyncMock(
        side_effect=ChatSessionAuthorizationError("Chat credential has been revoked")
    )
    monkeypatch.setattr(
        "app.api.websocket.async_session",
        lambda: RecordingDBContext(db),
    )
    monkeypatch.setattr(
        "app.api.websocket.validate_active_user_chat_lane",
        validate_lane,
    )

    handler = WebSocketChatHandler(
        websocket,
        uuid.uuid4(),
        "token",
        str(uuid.uuid4()),
    )
    handler.user = SimpleNamespace(id=uuid.uuid4())
    handler.auth_version = 7
    handler.conv_id = handler.session_id_param

    assert await handler._ensure_access_token_current() is False
    validate_lane.assert_awaited_once_with(
        db,
        agent_id=handler.agent_id,
        owner_user_id=handler.user.id,
        session_id=handler.conv_id,
        lock_authority=True,
        expected_auth_version=7,
    )
    assert websocket.sent == [
        {"type": "error", "content": "Session expired. Please sign in again."}
    ]
    assert websocket.close_code == 4001


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
    tenant_id = uuid.uuid4()
    handler.agent = SimpleNamespace(id=requested_agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        tenant_id=tenant_id,
        agent_id=requested_agent_id if same_agent else uuid.uuid4(),
        user_id=requested_user_id if same_user else uuid.uuid4(),
        session_type="direct",
        group_id=None,
        source_channel=source_channel,
        is_group=is_group,
        deleted_at=None,
    )

    resolved = await handler._resolve_chat_session(RecordingDB([session]), requested_user_id)

    assert resolved is None
    packet = websocket.sent[-1]
    assert packet["type"] == "error"
    assert packet["content"] == "Not authorized for this session"
    assert packet["code"] == "chat_session_scope_mismatch"
    assert packet["stage"] == "request"
    assert packet["error"]["trace_id"] == packet["trace_id"]
    assert websocket.close_code == 4002


@pytest.mark.asyncio
async def test_websocket_accepts_owners_private_web_session():
    websocket = FakeWebSocket()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    handler = WebSocketChatHandler(websocket, agent_id, "token", str(session_id))
    tenant_id = uuid.uuid4()
    handler.agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_type="direct",
        group_id=None,
        source_channel="web",
        is_group=False,
        deleted_at=None,
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
