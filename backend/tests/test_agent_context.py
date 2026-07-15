import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def test_active_trigger_prompt_omits_internal_routing_and_webhook_secrets():
    from types import SimpleNamespace

    from app.services.agent_context import _render_active_trigger_lines

    lines = _render_active_trigger_lines(
        [
            SimpleNamespace(
                name="webhook",
                type="webhook",
                config={
                    "event": "push",
                    "token": "private-url-token",
                    "secret": "private-hmac-secret",
                    "_origin_session_id": "private-session-id",
                    "_origin_user_id": "private-user-id",
                },
                reason="Process a push event",
                focus_ref=None,
            )
        ]
    )

    rendered = "\n".join(lines)
    assert "event" in rendered
    assert "private-url-token" not in rendered
    assert "private-hmac-secret" not in rendered
    assert "private-session-id" not in rendered
    assert "private-user-id" not in rendered


@pytest.mark.asyncio
async def test_build_agent_context_reads_focus_from_storage_key():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()

    async def fake_read_file(key, _max_chars=3000):
        if key == f"{agent_id}/focus.md":
            return "# Focus\n\n- [ ] follow_up: Check the deployment"
        return ""

    with (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" in dynamic
    assert "follow_up: Check the deployment" in dynamic
    assert "Named-recipient file delivery is currently supported only on Feishu and Slack" in static
    assert "automatically resolves the recipient across all connected channels" not in static
    assert "WeCom, DingTalk, Teams, and other external connectors are text-only" in static


def test_send_channel_file_contract_matches_runtime_channel_support():
    from app.services.agent_tools import AGENT_TOOLS
    from app.services.tool_seeder import BUILTIN_TOOLS

    runtime_tool = next(
        tool["function"]
        for tool in AGENT_TOOLS
        if tool["function"]["name"] == "send_channel_file"
    )
    seeded_tool = next(
        tool for tool in BUILTIN_TOOLS if tool["name"] == "send_channel_file"
    )

    for description in (
        runtime_tool["description"],
        runtime_tool["parameters"]["properties"]["member_name"]["description"],
        seeded_tool["description"],
        seeded_tool["parameters_schema"]["properties"]["member_name"]["description"],
    ):
        assert "Feishu or Slack" in description
        assert "across all" not in description

    assert "other external connectors are text-only" in runtime_tool["description"]
    assert "other external connectors are text-only" in seeded_tool["description"]


def test_text_only_channels_do_not_register_file_delivery_callbacks():
    api_root = Path(__file__).resolve().parents[1] / "app" / "api"

    for filename in ("teams.py",):
        source = (api_root / filename).read_text(encoding="utf-8")
        assert "channel_file_sender" not in source

    for filename in ("feishu.py", "slack.py", "dingtalk.py"):
        source = (api_root / filename).read_text(encoding="utf-8")
        assert "channel_file_sender" in source

    dingtalk_source = (api_root / "dingtalk.py").read_text(encoding="utf-8")
    assert "_dingtalk_file_sender(file_path: str, msg: str = \"\") -> bool" in dingtalk_source
    assert "A text-only filename fallback is not attachment delivery" in dingtalk_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_result", "expected_text"),
    [
        (True, "sent to user via channel"),
        (None, "Direct channel attachment was not confirmed"),
    ],
)
async def test_channel_file_delivery_requires_explicit_provider_confirmation(
    tmp_path,
    provider_result,
    expected_text,
):
    from app.services import agent_tools

    file_path = tmp_path / "report.txt"
    file_path.write_text("release evidence", encoding="utf-8")

    async def sender(_file_path, _message):
        return provider_result

    token = agent_tools.channel_file_sender.set(sender)
    try:
        result = await agent_tools._send_channel_file(
            uuid.uuid4(),
            tmp_path,
            {"file_path": "report.txt"},
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert expected_text in result
    if provider_result is not True:
        assert "sent to user via channel" not in result
        assert "File ready:" in result
