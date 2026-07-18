"""Canonical media-artifact tool capabilities shared by runtime contracts."""

from __future__ import annotations


MEDIA_ARTIFACT_TOOL_MODALITIES: dict[str, str] = {
    "generate_image_siliconflow": "image",
    "generate_image_openai": "image",
    "generate_image_google": "image",
    "generate_image_custom": "image",
    "generate_image_minimax": "image",
    "generate_speech_minimax": "audio",
    "generate_music_minimax": "music",
    "generate_video_minimax": "video",
    "check_video_minimax": "video",
}
MEDIA_ARTIFACT_TOOL_NAMES = frozenset(MEDIA_ARTIFACT_TOOL_MODALITIES)

MINIMAX_COMPLETION_TOOLS: dict[str, str] = {
    "image": "generate_image_minimax",
    "audio": "generate_speech_minimax",
    "music": "generate_music_minimax",
    "video": "generate_video_minimax",
}


def is_media_artifact_tool(tool_name: str | None) -> bool:
    return str(tool_name or "") in MEDIA_ARTIFACT_TOOL_NAMES


def minimax_completion_tool(modality: str | None) -> str | None:
    return MINIMAX_COMPLETION_TOOLS.get(str(modality or "").strip().lower())
