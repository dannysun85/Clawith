import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api import wecom as wecom_api
from app.models.channel_config import ChannelConfig
from app.models.user import User


class DummyResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.deleted = []
        self.flushed = False

    async def execute(self, statement):
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, _obj):
        return None

    async def flush(self):
        self.flushed = True

    async def delete(self, obj):
        self.deleted.append(obj)


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "old-hash",
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return User(**values)


def make_channel(agent_id: uuid.UUID, *, connection_mode: str = "websocket") -> ChannelConfig:
    return ChannelConfig(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="wecom",
        app_id="corp_id",
        app_secret="secret",
        is_configured=True,
        is_connected=False,
        extra_config={"connection_mode": connection_mode, "bot_id": "bot_123", "bot_secret": "secret_123"},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_get_wecom_channel_reports_runtime_websocket_status(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id, connection_mode="websocket")
    db = RecordingDB([DummyResult(config)])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return object(), None

    class FakeManager:
        def status(self):
            return {str(agent_id): True}

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr("app.services.wecom_stream.wecom_stream_manager", FakeManager())

    result = await wecom_api.get_wecom_channel(
        agent_id=agent_id,
        current_user=make_user(),
        db=db,
    )

    assert result.is_connected is True


@pytest.mark.asyncio
async def test_get_wecom_channel_marks_webhook_mode_disconnected(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id, connection_mode="webhook")
    db = RecordingDB([DummyResult(config)])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return object(), None

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)

    result = await wecom_api.get_wecom_channel(
        agent_id=agent_id,
        current_user=make_user(),
        db=db,
    )

    assert result.is_connected is False


@pytest.mark.asyncio
async def test_delete_wecom_channel_stops_runtime_client(monkeypatch):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id)
    db = RecordingDB([DummyResult(config)])
    stop_calls = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(creator_id=creator.id), None

    async def fake_stop_client(aid):
        stop_calls.append(aid)

    creator = make_user()
    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr("app.services.wecom_stream.wecom_stream_manager.stop_client", fake_stop_client)

    await wecom_api.delete_wecom_channel(
        agent_id=agent_id,
        current_user=creator,
        db=db,
    )

    assert stop_calls == [agent_id]
    assert db.deleted == [config]


@pytest.mark.asyncio
async def test_configure_wecom_explicit_webhook_mode_clears_stale_websocket_credentials(monkeypatch):
    agent_id = uuid.uuid4()
    creator = make_user()
    config = make_channel(agent_id, connection_mode="websocket")
    db = RecordingDB([DummyResult(config)])
    scheduled = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(creator_id=creator.id), None

    def fake_create_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(wecom_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(wecom_api.asyncio, "create_task", fake_create_task)

    await wecom_api.configure_wecom_channel(
        agent_id,
        {
            "connection_mode": "webhook",
            "bot_id": "stale-bot",
            "bot_secret": "stale-bot-secret",
            "corp_id": "corp-current",
            "wecom_agent_id": "00123",
            "secret": "app-secret-current",
            "token": "token-current",
            "encoding_aes_key": "aes-current",
        },
        current_user=creator,
        db=db,
    )

    assert config.app_id == "corp-current"
    assert config.app_secret == "app-secret-current"
    assert config.verification_token == "token-current"
    assert config.encrypt_key == "aes-current"
    assert config.extra_config == {
        "wecom_agent_id": "123",
        "bot_id": "",
        "bot_secret": "",
        "connection_mode": "webhook",
    }
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_configure_wecom_explicit_websocket_mode_clears_stale_webhook_credentials(monkeypatch):
    agent_id = uuid.uuid4()
    creator = make_user()
    config = make_channel(agent_id, connection_mode="webhook")
    db = RecordingDB([DummyResult(config)])
    scheduled = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(creator_id=creator.id), None

    def fake_create_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(wecom_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(wecom_api, "is_agent_creator", lambda _user, _agent: True)
    monkeypatch.setattr(wecom_api.asyncio, "create_task", fake_create_task)

    await wecom_api.configure_wecom_channel(
        agent_id,
        {
            "connection_mode": "websocket",
            "bot_id": "bot-current",
            "bot_secret": "bot-secret-current",
            "corp_id": "stale-corp",
            "wecom_agent_id": "not-required-for-websocket",
            "secret": "stale-app-secret",
            "token": "stale-token",
            "encoding_aes_key": "stale-aes",
        },
        current_user=creator,
        db=db,
    )

    assert config.app_id == ""
    assert config.app_secret == ""
    assert config.verification_token == ""
    assert config.encrypt_key == ""
    assert config.extra_config == {
        "wecom_agent_id": "",
        "bot_id": "bot-current",
        "bot_secret": "bot-secret-current",
        "connection_mode": "websocket",
    }
    assert len(scheduled) == 1
