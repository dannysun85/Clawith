"""Canonical grant policy shared by builtin definitions and persistence."""

from __future__ import annotations

from app.services.code_execution_policy import CODE_EXECUTION_TOOL_NAMES


# These capabilities are role-specific, provider-paid, or install/publish new
# executable reach. They may be assigned by a reviewed AgentTemplate or an
# Agent manager, but an absent AgentTool row never grants them implicitly.
#
# Ordinary workspace, reminder, directory, and communication tools are not in
# this set merely because they can mutate state. Their risk is governed by the
# Agent autonomy policy. Conflating a role grant with an action approval would
# make core product flows disappear and leave role instructions inconsistent
# with the runtime workset.
EXPLICIT_GRANT_TOOL_NAMES = frozenset(
    {
        "upload_image",
        "generate_image_minimax",
        "generate_speech_minimax",
        "generate_music_minimax",
        "generate_video_minimax",
        "check_video_minimax",
        "import_mcp_server",
        "install_skill",
        "publish_page",
        "update_kr_content",
        "update_kr_progress",
    }
) | CODE_EXECUTION_TOOL_NAMES


# These media tools are funded and authenticated by the platform-level
# ``LLMCredential`` pool.  A Tool/AgentTool assignment grants the ability to
# request the capability; it must never become an object-level or tenant BYOK
# credential binding for MiniMax.
CENTRAL_CREDENTIAL_POOL_TOOL_NAMES = frozenset(
    {
        "generate_image_minimax",
        "generate_speech_minimax",
        "generate_music_minimax",
        "generate_video_minimax",
        "check_video_minimax",
    }
)


# v1.11 replaced the AI-posting Plaza.  Keep the names registered for old
# audit/assignment rows, but never make them ambient capabilities again.  This
# set is shared by the legacy persistence seeder and the canonical runtime
# catalog so import order cannot change the grant policy.
LEGACY_NON_DEFAULT_TOOL_NAMES = frozenset(
    {
        "plaza_get_new_posts",
        "plaza_create_post",
        "plaza_add_comment",
    }
)


PERSISTED_NON_DEFAULT_TOOL_NAMES = (
    EXPLICIT_GRANT_TOOL_NAMES | LEGACY_NON_DEFAULT_TOOL_NAMES
)


__all__ = [
    "CENTRAL_CREDENTIAL_POOL_TOOL_NAMES",
    "EXPLICIT_GRANT_TOOL_NAMES",
    "LEGACY_NON_DEFAULT_TOOL_NAMES",
    "PERSISTED_NON_DEFAULT_TOOL_NAMES",
]
