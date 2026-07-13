"""Feishu group activation must avoid unsolicited processing and Credit spend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import feishu


def _config(mode=None):
    extra_config = {} if mode is None else {"activation_mode": mode}
    return SimpleNamespace(app_id="cli_test", app_secret="secret", extra_config=extra_config)


def _message(*, chat_type="group", mentions=None):
    return {"chat_type": chat_type, "mentions": mentions or []}


@pytest.mark.asyncio
async def test_private_messages_are_always_processed(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(feishu, "_get_feishu_bot_open_id", lookup)

    assert await feishu._should_process_feishu_message(_config("silent"), _message(chat_type="p2p"))
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_group_config_defaults_to_bot_mention_only(monkeypatch):
    monkeypatch.setattr(feishu, "_get_feishu_bot_open_id", AsyncMock(return_value="ou_bot"))

    assert not await feishu._should_process_feishu_message(_config(), _message())
    assert await feishu._should_process_feishu_message(
        _config(),
        _message(mentions=[{"id": {"open_id": "ou_other"}}, {"id": {"open_id": "ou_bot"}}]),
    )


@pytest.mark.asyncio
async def test_mention_mode_fails_closed_when_bot_identity_is_unavailable(monkeypatch):
    monkeypatch.setattr(feishu, "_get_feishu_bot_open_id", AsyncMock(return_value=None))

    assert not await feishu._should_process_feishu_message(
        _config("mention"),
        _message(mentions=[{"id": {"open_id": "ou_unknown"}}]),
    )


@pytest.mark.asyncio
async def test_explicit_always_and_silent_modes_do_not_lookup_identity(monkeypatch):
    lookup = AsyncMock(return_value="ou_bot")
    monkeypatch.setattr(feishu, "_get_feishu_bot_open_id", lookup)

    assert await feishu._should_process_feishu_message(_config("always"), _message())
    assert not await feishu._should_process_feishu_message(_config("silent"), _message())
    lookup.assert_not_awaited()


def test_unknown_activation_mode_falls_back_to_mention():
    assert feishu._feishu_group_activation_mode(_config("surprise")) == "mention"
