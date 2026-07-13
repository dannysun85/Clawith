"""Server-authoritative delivery contract for generated workspace artifacts."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app.services.storage import agent_storage_key, get_storage_backend, normalize_storage_key


ARTIFACT_SUFFIXES = {
    ".aac", ".avi", ".bmp", ".csv", ".docx", ".flac", ".gif", ".jpeg",
    ".jpg", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".ogg",
    ".opus", ".pdf", ".png", ".pptx", ".svg", ".wav", ".webm", ".webp",
    ".xlsx",
}
MEDIA_SUFFIXES = {
    ".aac", ".avi", ".bmp", ".flac", ".gif", ".jpeg", ".jpg", ".m4a",
    ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".png",
    ".svg", ".wav", ".webm", ".webp",
}
TARGET_PATH_TOOLS = {
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
}

_WORKSPACE_ARTIFACT = re.compile(
    r"workspace/[^\n\r`<>]*?\.(?:aac|avi|bmp|csv|docx|flac|gif|jpe?g|m4a|m4v|mkv|mov|mp3|mp4|ogg|opus|pdf|png|pptx|svg|wav|webm|webp|xlsx)",
    re.IGNORECASE,
)
_MARKDOWN_DOWNLOAD = re.compile(
    r"!?\[[^\]]*\]\((?P<url>/api/agents/(?P<agent>[0-9a-fA-F-]+)/files/download\?[^)]+)\)",
)


def _normalize_artifact_path(raw_path: str) -> str | None:
    candidate = raw_path.strip().strip("'\"()[]{}.,;:")
    if not candidate.startswith("workspace/") or ".." in Path(candidate).parts:
        return None
    try:
        normalized = normalize_storage_key(candidate)
    except (TypeError, ValueError):
        return None
    if not normalized.startswith("workspace/") or Path(normalized).suffix.lower() not in ARTIFACT_SUFFIXES:
        return None
    return normalized


def artifact_candidates(tool_name: str, args: dict | None, result: str | None) -> list[str]:
    """Extract claimed artifact paths from a successful tool result."""
    result_text = str(result or "").strip()
    if not result_text or result_text.startswith(("❌", "⚠️", "⏳")):
        return []

    candidates: list[str] = []
    if tool_name in TARGET_PATH_TOOLS:
        target = str((args or {}).get("target_path") or "")
        normalized_target = _normalize_artifact_path(target)
        if normalized_target:
            candidates.append(normalized_target)

    for match in _WORKSPACE_ARTIFACT.finditer(result_text):
        normalized = _normalize_artifact_path(match.group(0))
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


async def verified_tool_artifacts(
    agent_id: uuid.UUID | str,
    tool_name: str,
    args: dict | None,
    result: str | None,
) -> list[str]:
    """Return only artifact paths that exist as files in authoritative storage."""
    storage = get_storage_backend()
    verified: list[str] = []
    for path in artifact_candidates(tool_name, args, result):
        key = agent_storage_key(agent_id, path)
        if await storage.exists(key) and await storage.is_file(key):
            verified.append(path)
    return verified


def artifact_download_url(agent_id: uuid.UUID | str, path: str) -> str:
    return f"/api/agents/{agent_id}/files/download?{urlencode({'path': path})}"


def append_authoritative_artifacts(
    response: str,
    agent_id: uuid.UUID | str,
    artifact_paths: list[str],
) -> str:
    """Remove unverified generated links and append verified canonical links."""
    verified = list(dict.fromkeys(path for path in artifact_paths if _normalize_artifact_path(path)))
    if not verified:
        return response
    verified_set = set(verified)

    def replace_unverified(match: re.Match) -> str:
        if match.group("agent") != str(agent_id):
            return match.group(0)
        query = parse_qs(urlsplit(match.group("url")).query)
        linked_path = _normalize_artifact_path((query.get("path") or [""])[0])
        if linked_path and linked_path in verified_set:
            return match.group(0)
        return "（未验证的产物链接已移除）"

    cleaned = _MARKDOWN_DOWNLOAD.sub(replace_unverified, response).rstrip()
    links: list[str] = []
    for path in verified:
        url = artifact_download_url(agent_id, path)
        label = Path(path).name
        if url in cleaned:
            continue
        if Path(path).suffix.lower() in MEDIA_SUFFIXES:
            links.append(f"![{label}]({url})")
        else:
            links.append(f"[{label}]({url})")
    if not links:
        return cleaned
    return f"{cleaned}\n\n系统已验证产物：\n" + "\n".join(links)
