import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import (
    atlassian,
    dingtalk,
    discord_bot,
    feishu,
    slack,
    teams,
    tools,
    wechat,
    wecom,
)


class MissingResult:
    def scalar_one_or_none(self):
        return None


class MissingConfigDB:
    async def execute(self, _statement):
        return MissingResult()


CHANNEL_GETTERS = [
    (atlassian, atlassian.get_atlassian_channel),
    (dingtalk, dingtalk.get_dingtalk_channel),
    (discord_bot, discord_bot.get_discord_channel),
    (feishu, feishu.get_channel_config),
    (slack, slack.get_slack_channel),
    (teams, teams.get_teams_channel),
    (wechat, wechat.get_wechat_channel),
    (wecom, wecom.get_wecom_channel),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "getter"), CHANNEL_GETTERS)
async def test_missing_channel_config_can_be_read_as_null(module, getter):
    with patch.object(module, "check_agent_access", AsyncMock()):
        result = await getter(
            uuid.uuid4(),
            missing_ok=True,
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=MissingConfigDB(),
        )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "getter"), CHANNEL_GETTERS)
async def test_missing_channel_config_still_defaults_to_not_found(module, getter):
    with patch.object(module, "check_agent_access", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await getter(
                uuid.uuid4(),
                current_user=SimpleNamespace(id=uuid.uuid4()),
                db=MissingConfigDB(),
            )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_atlassian_credential_comparison_fails_closed_on_decrypt_error():
    agent_id = uuid.uuid4()
    config = SimpleNamespace(app_secret="legacy-plaintext-secret")

    class Result:
        def __init__(self, value=None):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.results = iter((Result(), Result(config)))

        async def execute(self, _statement):
            return next(self.results)

    with patch("app.core.security.decrypt_data", side_effect=ValueError("bad ciphertext")):
        matched = await atlassian._atlassian_credential_matches(
            agent_id,
            "legacy-plaintext-secret",
            DB(),
        )

    assert matched is False


@pytest.mark.asyncio
async def test_atlassian_sync_failure_is_durable_and_not_fire_and_forget(monkeypatch):
    agent_id = uuid.uuid4()
    persist_failure = AsyncMock()

    async def fail_sync(_agent_id, _api_key):
        raise atlassian.AtlassianToolSyncError("atlassian_discovery_failed")

    monkeypatch.setattr(atlassian, "_sync_atlassian_tools_for_agent", fail_sync)
    monkeypatch.setattr(atlassian, "_mark_atlassian_sync_failed", persist_failure)

    with pytest.raises(HTTPException) as exc:
        await atlassian._complete_atlassian_tool_sync(agent_id, "secret")

    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "atlassian_discovery_failed"
    persist_failure.assert_awaited_once_with(
        agent_id,
        "secret",
        "atlassian_discovery_failed",
    )


def test_atlassian_configure_does_not_launch_untracked_background_sync():
    source = Path(atlassian.__file__).read_text(encoding="utf-8")

    assert "asyncio.create_task(_sync_atlassian_tools_for_agent" not in source
    assert '"tool_sync_status": "syncing"' in source
    assert '"tool_sync_status": "ready"' in source
    assert '"tool_sync_status": "failed"' in source


@pytest.mark.asyncio
async def test_tools_category_atlassian_path_awaits_canonical_sync_after_commit(
    monkeypatch,
):
    events: list[str] = []
    agent_id = uuid.uuid4()
    config = SimpleNamespace(
        app_secret="encrypted-old",
        extra_config={"cloud_id": "old-cloud"},
        is_configured=True,
    )

    class Result:
        def scalar_one_or_none(self):
            return config

    class DB:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            events.append("commit")

    async def allow_access(*_args, **_kwargs):
        return None

    async def no_lock(*_args, **_kwargs):
        return None

    async def revoke(*_args, **_kwargs):
        events.append("revoke")

    async def complete_sync(selected_agent_id, api_key):
        assert selected_agent_id == agent_id
        assert api_key == "new-secret"
        events.append("sync")

    monkeypatch.setattr(tools, "_require_agent_tool_access", allow_access)
    monkeypatch.setattr(atlassian, "lock_atlassian_agent", no_lock)
    monkeypatch.setattr(atlassian, "revoke_atlassian_tool_grants", revoke)
    monkeypatch.setattr(atlassian, "_complete_atlassian_tool_sync", complete_sync)
    monkeypatch.setattr(
        tools,
        "_decrypt_sensitive_fields",
        lambda _config: {"api_key": "old-secret", "cloud_id": "old-cloud"},
    )
    monkeypatch.setattr(
        tools,
        "_encrypt_sensitive_fields",
        lambda value: {**value, "api_key": "encrypted-new"},
    )

    result = await tools.update_category_config(
        agent_id,
        "atlassian",
        tools.CategoryConfigUpdate(
            config={"api_key": "new-secret", "cloud_id": "new-cloud"}
        ),
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=DB(),
    )

    assert result == {"ok": True}
    assert events == ["revoke", "commit", "sync"]
    assert config.extra_config["tool_sync_status"] == "syncing"
    assert config.extra_config["tool_count"] == 0
    assert config.extra_config["tool_sync_error_code"] is None
