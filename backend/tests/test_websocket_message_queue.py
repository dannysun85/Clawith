import uuid
from types import SimpleNamespace

import pytest

from app.api.websocket import WebSocketChatHandler
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
