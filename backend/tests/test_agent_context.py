import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.storage import StorageEntry


def _context_patches(*, soul: str = "", memory: str = "", skills: str = ""):
    agent_id_holder: dict[str, uuid.UUID] = {}

    async def fake_read_file(key, _max_chars=3000, **_kwargs):
        agent_id = agent_id_holder["agent_id"]
        if key == f"{agent_id}/soul.md":
            return soul
        if key in {f"{agent_id}/memory/memory.md", f"{agent_id}/memory.md"}:
            return memory
        return ""

    return agent_id_holder, (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch(
            "app.services.agent_context._load_skills_index",
            new_callable=AsyncMock,
            return_value=skills,
        ),
        patch(
            "app.services.agent_context._load_relationships_from_db",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.timezone_utils.get_agent_timezone",
            new_callable=AsyncMock,
            return_value="UTC",
        ),
    )


def test_memory_context_truncation_keeps_identity_and_latest_corrections():
    from app.services.agent_context import _truncate_context_text

    content = "# Memory\n" + ("old-history\n" * 400) + "LATEST CORRECTION: use the new policy"

    bounded = _truncate_context_text(content, 2000, preserve_tail=True)

    assert len(bounded) <= 2000
    assert bounded.startswith("# Memory")
    assert "older middle content omitted" in bounded
    assert bounded.endswith("LATEST CORRECTION: use the new policy")


def test_memory_context_truncation_preserves_middle_policy_sections():
    from app.services.agent_context import _truncate_context_text

    content = "\n".join(
        [
            "# Memory",
            "Company identity and operating context.",
            "## Old history",
            "old event " * 500,
            "## Security Policy",
            "Never reveal credentials. Require approval before an external write.",
            "policy details " * 500,
            "## Customer notes",
            "customer history " * 500,
            "## Latest correction",
            "LATEST CORRECTION: external publishing remains disabled",
        ]
    )

    bounded = _truncate_context_text(content, 2000, preserve_tail=True)

    assert len(bounded) <= 2000
    assert bounded.startswith("# Memory")
    assert "# Security Policy" in bounded
    assert "Never reveal credentials" in bounded
    assert "Section index:" in bounded
    assert bounded.endswith("LATEST CORRECTION: external publishing remains disabled")


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


def test_active_trigger_prompt_hides_platform_internal_triggers():
    from types import SimpleNamespace

    from app.services.agent_context import _render_active_trigger_lines

    lines = _render_active_trigger_lines(
        [
            SimpleNamespace(
                name="a2a_wake",
                type="a2a",
                config={"session_id": "private-session-id"},
                reason="Internal A2A routing",
                focus_ref=None,
            ),
            SimpleNamespace(
                name="__a2a_wake__",
                type="event",
                config={"event": "a2a"},
                reason="Internal A2A routing",
                focus_ref=None,
            ),
            SimpleNamespace(
                name="daily_report",
                type="cron",
                config={"cron": "0 9 * * *"},
                reason="Prepare the user's daily report",
                focus_ref=None,
            ),
            SimpleNamespace(
                name="__customer_defined__",
                type="event",
                config={"event": "customer_defined"},
                reason="A user-configured trigger may use a reserved-looking name",
                focus_ref=None,
            ),
        ]
    )

    rendered = "\n".join(lines)
    assert "daily_report" in rendered
    assert "__customer_defined__" in rendered
    assert "a2a_wake" not in rendered
    assert "__a2a_wake__" not in rendered
    assert "Internal A2A routing" not in rendered


def test_active_trigger_prompt_is_empty_when_only_internal_triggers_exist():
    from types import SimpleNamespace

    from app.services.agent_context import _render_active_trigger_lines

    assert (
        _render_active_trigger_lines(
            [
                SimpleNamespace(
                    name="a2a_wake",
                    type="a2a",
                    config={},
                    reason="Internal A2A routing",
                    focus_ref=None,
                ),
                SimpleNamespace(
                    name="__a2a_wake__",
                    type="event",
                    config={},
                    reason="Internal A2A routing",
                    focus_ref=None,
                ),
            ]
        )
        == []
    )


@pytest.mark.asyncio
async def test_base_prompt_starts_with_name_and_soul_and_never_injects_self_role():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        soul="# Soul\nBe precise and preserve evidence.",
        memory="# Memory\nThe release owner is Alice.",
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        static, dynamic = await build_agent_context(
            agent_id,
            "TestAgent",
            "THIS ROLE MUST NOT ENTER THE MODEL",
            allowed_tool_names={"wait"},
        )

    assert static.startswith("# Identity\n\nYou are TestAgent, a digital employee in Astra.")
    assert "<soul>\nBe precise and preserve evidence.\n</soul>" in static
    assert static.index("<soul>") < static.index("# Astra Environment")
    assert "The current product identity is Astra." in static
    assert "legacy project names" in static
    assert "Clawith" not in static
    assert "THIS ROLE MUST NOT ENTER THE MODEL" not in f"{static}\n{dynamic}"
    assert "# Memory" in static
    assert "The release owner is Alice." not in static
    assert "The release owner is Alice." in dynamic
    assert "## Current Time" in dynamic
    assert "## Current Time" not in static
    assert "## Role" not in static
    assert "call `finish`" not in static
    assert "return the exact final answer as normal Assistant content" in static


def test_send_channel_file_contract_matches_runtime_channel_support():
    from app.services.agent_tools import AGENT_TOOLS
    from app.services.tool_seeder import BUILTIN_TOOLS

    runtime_tool = next(tool["function"] for tool in AGENT_TOOLS if tool["function"]["name"] == "send_channel_file")
    seeded_tool = next(tool for tool in BUILTIN_TOOLS if tool["name"] == "send_channel_file")

    for schema in (
        runtime_tool["parameters"],
        seeded_tool["parameters_schema"],
    ):
        assert "target_member_id" in schema["properties"]
        assert "member_name" not in schema["properties"]
        assert schema["properties"]["channel"]["enum"] == ["feishu", "slack"]

    assert "query_directory" in runtime_tool["description"]
    assert "query_directory" in seeded_tool["description"]


def test_text_only_channels_do_not_register_file_delivery_callbacks():
    api_root = Path(__file__).resolve().parents[1] / "app" / "api"

    # v1.11 channels all enqueue Durable Runtime commands. Attachment
    # delivery is resolved from the persisted delivery target, never from a
    # request-local callback that disappears once the webhook returns.
    for filename in ("teams.py", "slack.py", "dingtalk.py", "feishu.py"):
        source = (api_root / filename).read_text(encoding="utf-8")
        assert "channel_file_sender" not in source

    dingtalk_source = (api_root / "dingtalk.py").read_text(encoding="utf-8")
    assert "enqueue_channel_chat_runtime" in dingtalk_source


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


@pytest.mark.asyncio
async def test_channel_file_delivery_rejects_all_workspace_escape_forms(tmp_path):
    from app.services import agent_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    prefix_collision = tmp_path / "workspace-escape"
    prefix_collision.mkdir()
    (prefix_collision / "secret.txt").write_text("private", encoding="utf-8")
    (workspace / "symlink.txt").symlink_to(outside)

    sender = AsyncMock(return_value=True)
    token = agent_tools.channel_file_sender.set(sender)
    try:
        for hostile_path in (
            str(outside),
            "../outside.txt",
            "../workspace-escape/secret.txt",
            "symlink.txt",
        ):
            result = await agent_tools._send_channel_file(
                uuid.uuid4(),
                workspace,
                {"file_path": hostile_path},
            )
            assert result == "Error: file_path must stay within the Agent workspace"
    finally:
        agent_tools.channel_file_sender.reset(token)

    sender.assert_not_awaited()


@pytest.mark.parametrize(
    "hostile_path",
    [".", "/etc/passwd", "../secret.txt", "nested/../secret.txt", "..\\secret.txt", "C:/secret.txt"],
)
def test_channel_file_path_syntax_is_rejected_before_materialization(hostile_path):
    from app.services import agent_tools
    from app.services.workspace_paths import WorkspacePathError

    with pytest.raises(WorkspacePathError):
        agent_tools._validate_channel_file_path_syntax(hostile_path)


@pytest.mark.asyncio
async def test_focus_mechanism_is_constant_but_tool_policy_follows_effective_tools():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        without_tools, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait"},
        )
        with_focus_tools, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "list_focus_items",
                "upsert_focus_item",
                "complete_focus_item",
            },
        )

    assert "## Focus" in without_tools
    assert "Focus is your structured persistent working state" in without_tools
    assert "list_focus_items" not in without_tools
    assert "list_focus_items" in with_focus_tools
    assert "Do not read or write `focus.md`" in with_focus_tools


@pytest.mark.asyncio
async def test_skill_catalog_requires_read_file_and_prompt_has_no_hardcoded_channel_manuals():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        skills="| Risk Review | Check release risks | skills/risk/SKILL.md |",
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        without_loader, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait"},
        )
        with_loader, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "list_files"},
        )

    assert "Risk Review" not in without_loader
    assert "# Available Skills" in with_loader
    assert "skills/risk/SKILL.md" in with_loader
    assert "MCP Import Rules" not in with_loader
    assert "atlassian_jira_search_issues" not in with_loader
    assert "Pre-installed Feishu Tools" not in with_loader


@pytest.mark.asyncio
async def test_lowercase_skill_entry_advertises_the_actual_readable_path(monkeypatch):
    from app.services import agent_context

    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/skills"
    folder_key = f"{prefix}/risk-review"
    lowercase_key = f"{folder_key}/skill.md"

    class _Storage:
        async def exists(self, key):
            return key in {prefix, folder_key, lowercase_key}

        async def is_dir(self, key):
            return key in {prefix, folder_key}

        async def list_dir(self, key):
            assert key == prefix
            return [StorageEntry(name="risk-review", key=folder_key, is_dir=True)]

        async def read_text(self, key, **_kwargs):
            assert key == lowercase_key
            return "---\nname: Risk Review\ndescription: Check release risks\n---\n"

    monkeypatch.setattr(agent_context, "get_storage_backend", lambda: _Storage())

    catalog = await agent_context._load_skills_index(agent_id)

    assert "skills/risk-review/skill.md" in catalog
    assert "skills/risk-review/SKILL.md" not in catalog


@pytest.mark.asyncio
async def test_directory_and_human_send_policies_only_name_enabled_tools():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        static, dynamic = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "query_directory",
                "send_platform_message",
                "send_channel_message",
            },
        )

    prompt = f"{static}\n{dynamic}"
    assert "send_feishu_message" not in prompt
    assert "query_directory" in prompt
    assert "send_platform_message" in prompt
    assert "send_channel_message" in prompt


@pytest.mark.asyncio
async def test_experience_policy_is_short_and_only_names_enabled_operations():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        read_only, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "search_experience",
                "read_experience",
            },
        )
        with_draft, _ = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "search_experience",
                "read_experience",
                "propose_experience_draft",
            },
        )

    assert "search_experience" in read_only
    assert "read_experience" in read_only
    assert "propose_experience_draft" not in read_only
    assert "现有标签" not in read_only
    assert "propose_experience_draft" in with_draft
