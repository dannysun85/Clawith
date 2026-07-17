import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import dingtalk


class _Response:
    def __init__(self, *, body=None, status_error: Exception | None = None):
        self.body = body
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.body


class _Client:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, json):
        return self.response


@pytest.mark.asyncio
async def test_dingtalk_webhook_propagates_http_failure(monkeypatch):
    response = _Response(status_error=RuntimeError("HTTP 500"))
    monkeypatch.setattr(
        dingtalk.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response),
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await dingtalk._post_dingtalk_session_webhook(
            "https://oapi.example/robot/send?access_token=secret",
            {"msgtype": "text"},
        )


@pytest.mark.asyncio
async def test_dingtalk_webhook_rejects_http_200_business_error(monkeypatch):
    response = _Response(body={"errcode": 310000, "errmsg": "invalid token"})
    monkeypatch.setattr(
        dingtalk.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response),
    )

    with pytest.raises(dingtalk.DingTalkWebhookDeliveryError):
        await dingtalk._post_dingtalk_session_webhook(
            "https://oapi.example/robot/send?access_token=secret",
            {"msgtype": "text"},
        )


@pytest.mark.asyncio
async def test_dingtalk_reply_falls_back_from_markdown_to_text(monkeypatch):
    deliver = AsyncMock(side_effect=[RuntimeError("markdown rejected"), None])
    monkeypatch.setattr(dingtalk, "_post_dingtalk_session_webhook", deliver)

    result = await dingtalk._deliver_dingtalk_session_reply(
        session_webhook="https://oapi.example/robot/send?access_token=secret",
        title="Assistant",
        reply_text="Delivered as text",
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert result is True
    assert [call.args[1]["msgtype"] for call in deliver.await_args_list] == [
        "markdown",
        "text",
    ]


@pytest.mark.asyncio
async def test_dingtalk_double_delivery_failure_is_redacted_and_reported(
    monkeypatch,
):
    secret = "sentinel-access-token"
    deliver = AsyncMock(
        side_effect=RuntimeError(
            f"https://oapi.example/robot/send?access_token={secret}"
        )
    )
    issue = AsyncMock()
    log_calls = []

    class _Logger:
        def error(self, message, *args):
            log_calls.append((message, args))

    monkeypatch.setattr(dingtalk, "_post_dingtalk_session_webhook", deliver)
    monkeypatch.setattr(dingtalk, "logger", _Logger())
    monkeypatch.setattr(
        "app.services.production_issue_monitor.record_production_issue",
        issue,
    )
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    result = await dingtalk._deliver_dingtalk_session_reply(
        session_webhook=f"https://oapi.example/robot/send?access_token={secret}",
        title="Assistant",
        reply_text="Not delivered",
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    assert result is False
    assert secret not in repr(log_calls)
    issue.assert_awaited_once_with(
        source="dingtalk",
        category="channel_delivery",
        summary="DingTalk reply delivery failed after text fallback",
        severity="error",
        error_code="RuntimeError",
        route="/dingtalk/session-webhook",
        operation="reply",
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        metadata={"fallback_attempted": True},
    )


class _QueryResult:
    def __init__(self, *, value=None, values=None):
        self.value = value
        self.values = list(values or [])

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _MessageDB:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.commits = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_failed_dingtalk_delivery_does_not_persist_false_assistant_history(
    monkeypatch,
):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=uuid.uuid4(),
        name="Delivery Truth Agent",
        context_window_size=20,
    )
    db = _MessageDB(
        [
            _QueryResult(value=agent),
            _QueryResult(values=[]),
            _QueryResult(value=None),
        ]
    )
    session_factory_calls = 0

    def session_factory():
        nonlocal session_factory_calls
        session_factory_calls += 1
        if session_factory_calls > 1:
            raise AssertionError("assistant persistence session must not open")
        return db

    monkeypatch.setattr("app.database.async_session", session_factory)
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.resolve_channel_user",
        AsyncMock(return_value=SimpleNamespace(id=user_id)),
    )
    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session",
        AsyncMock(return_value=SimpleNamespace(id=session_id, last_message_at=None)),
    )
    monkeypatch.setattr(
        "app.api.feishu._load_agent_and_model",
        AsyncMock(return_value=(object(), object(), None, {})),
    )
    monkeypatch.setattr(
        "app.api.feishu._call_llm_with_config",
        AsyncMock(return_value="Generated but never delivered"),
    )
    monkeypatch.setattr(
        dingtalk,
        "_deliver_dingtalk_session_reply",
        AsyncMock(return_value=False),
    )

    await dingtalk.process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id="staff-id",
        user_text="hello",
        conversation_id="conversation-id",
        conversation_type="1",
        session_webhook="https://oapi.example/robot/send?access_token=secret",
    )

    assert session_factory_calls == 1
    assert [message.role for message in db.added] == ["user"]
    assert db.commits == 1
    assert db.closed is True
