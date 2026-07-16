import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import discord_bot, feishu, slack, teams, whatsapp


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, config):
        self.config = config

    async def execute(self, _statement):
        return _ScalarResult(self.config)


def _request(body: bytes = b"{}", headers: list[tuple[bytes, bytes]] | None = None):
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/channel/test/webhook",
            "headers": headers or [],
        },
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "setting_name", "handler"),
    [
        (feishu, "FEISHU_WEBHOOK_ENABLED", feishu.feishu_event_webhook),
        (teams, "TEAMS_WEBHOOK_ENABLED", teams.teams_event_webhook),
    ],
)
async def test_unverified_webhook_transports_are_disabled_before_body_processing(
    monkeypatch,
    module,
    setting_name,
    handler,
):
    monkeypatch.setattr(module.settings, setting_name, False)
    request = _request(b"not-json")

    with pytest.raises(HTTPException) as exc:
        if module is teams:
            await handler(uuid.uuid4(), request, _DB(None))
        else:
            await handler(uuid.uuid4(), request)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "config"),
    [
        (slack.slack_event_webhook, SimpleNamespace(encrypt_key="")),
        (discord_bot.discord_interaction_webhook, SimpleNamespace(encrypt_key="")),
        (whatsapp.whatsapp_event_webhook, SimpleNamespace(encrypt_key="")),
    ],
)
async def test_signed_webhooks_fail_closed_when_verification_secret_is_missing(
    handler,
    config,
):
    response = await handler(uuid.uuid4(), _request(), _DB(config))

    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "config"),
    [
        (slack.slack_event_webhook, SimpleNamespace(encrypt_key="signing-secret")),
        (discord_bot.discord_interaction_webhook, SimpleNamespace(encrypt_key="00" * 32)),
        (whatsapp.whatsapp_event_webhook, SimpleNamespace(encrypt_key="app-secret")),
    ],
)
async def test_signed_webhooks_reject_invalid_or_missing_signatures(handler, config):
    response = await handler(uuid.uuid4(), _request(), _DB(config))

    assert response.status_code == 401
