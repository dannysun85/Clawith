import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import wecom
from app.services import agent_tools, wecom_service


class _SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100001", 100001),
        (100001, 100001),
        (" 100001 ", 100001),
        (None, None),
        ("", None),
        ("0", None),
        ("-1", None),
        ("not-a-number", None),
        ("１００００１", None),
    ],
)
def test_normalize_wecom_agent_id(value, expected):
    assert wecom_service.normalize_wecom_agent_id(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id_value", [None, "", "0", "-1", "abc", "１００００１"])
async def test_wecom_webhook_configuration_rejects_invalid_application_agent_id(
    monkeypatch,
    agent_id_value,
):
    agent = SimpleNamespace()
    monkeypatch.setattr(
        wecom,
        "check_agent_access",
        AsyncMock(return_value=(agent, None)),
    )
    monkeypatch.setattr(wecom, "is_agent_creator", lambda _user, _agent: True)
    db = AsyncMock()
    payload = {
        "corp_id": "corp-id",
        "secret": "corp-secret",
        "token": "verification-token",
        "encoding_aes_key": "encoding-key",
        "wecom_agent_id": agent_id_value,
    }

    with pytest.raises(HTTPException) as exc_info:
        await wecom.configure_wecom_channel(
            uuid.uuid4(),
            payload,
            current_user=SimpleNamespace(),
            db=db,
        )

    assert exc_info.value.status_code == 422
    assert "wecom_agent_id" in exc_info.value.detail
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_wecom_websocket_configuration_rejects_explicit_invalid_agent_id(
    monkeypatch,
):
    agent = SimpleNamespace()
    monkeypatch.setattr(
        wecom,
        "check_agent_access",
        AsyncMock(return_value=(agent, None)),
    )
    monkeypatch.setattr(wecom, "is_agent_creator", lambda _user, _agent: True)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await wecom.configure_wecom_channel(
            uuid.uuid4(),
            {
                "bot_id": "bot-id",
                "bot_secret": "bot-secret",
                "wecom_agent_id": "not-a-number",
            },
            current_user=SimpleNamespace(),
            db=db,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "wecom_agent_id must be a positive ASCII numeric value"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_wecom_standard_webhook_reply_rejects_legacy_invalid_agent_id_before_llm(
    monkeypatch,
):
    config = SimpleNamespace(extra_config={"wecom_agent_id": "invalid"})
    send = AsyncMock()
    monkeypatch.setattr(wecom, "send_wecom_message", send)
    monkeypatch.setattr(
        wecom,
        "async_session",
        lambda: (_ for _ in ()).throw(AssertionError("database session must not open")),
    )

    await wecom._process_wecom_text(
        uuid.uuid4(),
        config,
        "wecom-user",
        "hello",
    )

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_wecom_delivery_forwards_configured_application_agent_id(
    monkeypatch,
):
    config = SimpleNamespace(
        app_id="corp-id",
        app_secret="corp-secret",
        extra_config={"wecom_agent_id": "100001"},
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: config)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(agent_tools, "async_session", lambda: _SessionContext(db))

    send = AsyncMock(return_value={"errcode": 40013, "errmsg": "invalid recipient"})
    monkeypatch.setattr(wecom_service, "send_wecom_message", send)
    target = SimpleNamespace(
        external_id="wecom-user",
        open_id=None,
        id=uuid.uuid4(),
    )

    response = await agent_tools._send_wecom_message(
        uuid.uuid4(),
        "Recipient",
        "hello",
        target,
    )

    send.assert_awaited_once_with(
        "corp-id",
        "corp-secret",
        "wecom-user",
        "hello",
        agent_id=100001,
    )
    assert response.startswith("❌ WeCom send failed")


@pytest.mark.asyncio
async def test_proactive_wecom_delivery_fails_closed_without_application_agent_id(
    monkeypatch,
):
    config = SimpleNamespace(
        app_id="corp-id",
        app_secret="corp-secret",
        extra_config={},
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: config)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(agent_tools, "async_session", lambda: _SessionContext(db))

    send = AsyncMock()
    monkeypatch.setattr(wecom_service, "send_wecom_message", send)
    target = SimpleNamespace(
        external_id="wecom-user",
        open_id=None,
        id=uuid.uuid4(),
    )

    response = await agent_tools._send_wecom_message(
        uuid.uuid4(),
        "Recipient",
        "hello",
        target,
    )

    assert response == "❌ WeCom channel is missing the application agent ID"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_wecom_delivery_rejects_non_numeric_application_agent_id(
    monkeypatch,
):
    config = SimpleNamespace(
        app_id="corp-id",
        app_secret="corp-secret",
        extra_config={"wecom_agent_id": "not-a-number"},
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: config)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(agent_tools, "async_session", lambda: _SessionContext(db))

    send = AsyncMock()
    monkeypatch.setattr(wecom_service, "send_wecom_message", send)
    target = SimpleNamespace(
        external_id="wecom-user",
        open_id=None,
        id=uuid.uuid4(),
    )

    response = await agent_tools._send_wecom_message(
        uuid.uuid4(),
        "Recipient",
        "hello",
        target,
    )

    assert response == "❌ WeCom application agent ID must be a positive numeric value"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_wecom_service_rejects_invalid_agent_id_before_requesting_token(
    monkeypatch,
):
    get_token = AsyncMock()
    monkeypatch.setattr(wecom_service, "get_wecom_access_token", get_token)

    response = await wecom_service.send_wecom_message(
        "corp-id",
        "corp-secret",
        "wecom-user",
        "hello",
        agent_id="１００００１",
    )

    assert response == {
        "errcode": -1,
        "errmsg": "a positive numeric agent_id is required for WeCom messages",
    }
    get_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_wecom_service_serializes_agent_id_as_integer(monkeypatch):
    monkeypatch.setattr(
        wecom_service,
        "get_wecom_access_token",
        AsyncMock(return_value={"access_token": "token"}),
    )

    class _Response:
        @staticmethod
        def json():
            return {"errcode": 0}

    class _Client:
        def __init__(self):
            self.payload = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, _url, *, params, json):
            assert params == {"access_token": "token"}
            self.payload = json
            return _Response()

    client = _Client()
    monkeypatch.setattr(wecom_service.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await wecom_service.send_wecom_message(
        "corp-id",
        "corp-secret",
        "wecom-user",
        "hello",
        agent_id="100001",
    )

    assert response == {"errcode": 0}
    assert client.payload["agentid"] == 100001
    assert isinstance(client.payload["agentid"], int)
