import uuid
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
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" in dynamic
    assert "follow_up: Check the deployment" in dynamic
