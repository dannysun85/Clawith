import json
import uuid
from types import SimpleNamespace

import pytest
from nacl.signing import SigningKey
from starlette.requests import Request

from app.api.discord_bot import discord_interaction_webhook


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


def _signed_request(signing_key: SigningKey, body: bytes, timestamp: str = "1234567890") -> Request:
    signature = signing_key.sign(timestamp.encode() + body).signature.hex()
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
            "path": "/api/channel/discord/test/webhook",
            "headers": [
                (b"x-signature-timestamp", timestamp.encode()),
                (b"x-signature-ed25519", signature.encode()),
            ],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_discord_signed_ping_returns_verification_pong():
    signing_key = SigningKey.generate()
    body = json.dumps({"type": 1}, separators=(",", ":")).encode()
    request = _signed_request(signing_key, body)
    config = SimpleNamespace(encrypt_key=signing_key.verify_key.encode().hex())

    result = await discord_interaction_webhook(uuid.uuid4(), request, _DB(config))

    assert result == {"type": 1}
