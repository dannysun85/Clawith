import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import atlassian, dingtalk, discord_bot, feishu, slack, teams, wechat, wecom


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
