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
MEDIA_GENERATION_TOOLS = {
    "generate_image_minimax",
    "generate_music_minimax",
    "generate_speech_minimax",
    "generate_video_minimax",
}

_WORKSPACE_ARTIFACT = re.compile(
    r"workspace/[^\n\r`<>]*?\.(?:aac|avi|bmp|csv|docx|flac|gif|jpe?g|m4a|m4v|mkv|mov|mp3|mp4|ogg|opus|pdf|png|pptx|svg|wav|webm|webp|xlsx)",
    re.IGNORECASE,
)
_MARKDOWN_DOWNLOAD = re.compile(
    r"!?\[[^\]]*\]\((?P<url>/api/agents/(?P<agent>[0-9a-fA-F-]+)/files/download\?[^)]+)\)",
)
_MEDIA_TOOL_EXECUTION_CLAIM = re.compile(
    r"(?:调用次数\s*[:：]?\s*[1-9]|已(?:经)?调用|调用了|调用成功|仅调用|只调用|"
    r"工具(?:真实)?(?:返回|回包)|\b(?:called|invoked|ran)\b|\btool\s+(?:returned|response)\b)",
    re.IGNORECASE,
)
_NEGATED_MEDIA_TOOL_EXECUTION = re.compile(
    r"(?:未|没有|并未|不能|无法)\s*(?:实际|真正)?\s*调用|"
    r"\b(?:did\s+not|was\s+not|not)\s+(?:actually\s+)?(?:call|invoke|run)",
    re.IGNORECASE,
)
_MEDIA_SUCCESS_CLAIM = re.compile(
    r"(?:视频|图片|图像|音频|语音|音乐|媒体).{0,32}"
    r"(?:已生成|生成成功|已完成|已保存|成功完成|真实返回成功|验证可用)|"
    r"(?:已生成|生成成功|成功完成|真实返回成功|验证可用).{0,32}"
    r"(?:视频|图片|图像|音频|语音|音乐|媒体)|"
    r"\b(?:video|image|audio|speech|music|media).{0,48}"
    r"(?:success|saved\s+to|is\s+ready|generated\s+successfully)\b|"
    r"\b(?:success|saved\s+to|is\s+ready|generated\s+successfully).{0,48}"
    r"(?:video|image|audio|speech|music|media)\b",
    re.IGNORECASE,
)
_NEGATED_MEDIA_SUCCESS = re.compile(
    r"(?:未|没有|并未|不能|无法).{0,12}(?:成功|生成|完成|保存|验证)|"
    r"\b(?:not|did\s+not|was\s+not|cannot|could\s+not).{0,20}"
    r"(?:succeed|generate|complete|save|verify|ready)",
    re.IGNORECASE,
)
_UNVERIFIED_TASK_ID = re.compile(
    r"(?P<label>(?:task[_ ]?id|任务\s*ID|任务号)\s*[:：=]\s*)"
    r"(?P<value>[0-9a-f]{12,}(?:_[^\s，。；;)]*)?)",
    re.IGNORECASE,
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


async def verified_response_artifacts(
    agent_id: uuid.UUID | str,
    response: str | None,
) -> list[str]:
    """Return response-linked artifacts that exist in authoritative storage.

    Models can emit plausible-looking workspace paths without calling a tool.  Treat
    those paths as presentation hints only; the storage backend remains the source
    of truth before a download or media link is allowed into the final response.
    """
    response_text = str(response or "")
    if not response_text:
        return []

    candidates: list[str] = []
    for match in _WORKSPACE_ARTIFACT.finditer(response_text):
        normalized = _normalize_artifact_path(match.group(0))
        if normalized and normalized not in candidates:
            candidates.append(normalized)
        if len(candidates) >= 32:
            break

    storage = get_storage_backend()
    verified: list[str] = []
    for path in candidates:
        key = agent_storage_key(agent_id, path)
        if await storage.exists(key) and await storage.is_file(key):
            verified.append(path)
    return verified


async def sanitize_response_artifacts(
    agent_id: uuid.UUID | str,
    response: str | None,
    artifact_paths: list[str] | None = None,
    *,
    allow_stored_response_artifacts: bool = True,
    completed_tool_names: set[str] | None = None,
    generated_media_artifact_paths: list[str] | None = None,
) -> str:
    """Apply the authoritative artifact contract to live or persisted replies."""
    response_text = str(response or "")
    response_paths: list[str] = []
    if allow_stored_response_artifacts:
        try:
            response_paths = await verified_response_artifacts(agent_id, response_text)
        except Exception:
            # Storage availability must fail closed for model-authored links without
            # discarding the surrounding chat response.
            response_paths = []
    authoritative_paths = list(
        dict.fromkeys([*(artifact_paths or []), *response_paths])
    )
    cleaned = append_authoritative_artifacts(
        response_text,
        agent_id,
        authoritative_paths,
    )
    if completed_tool_names is None:
        return cleaned
    return annotate_unverified_media_claims(
        cleaned,
        completed_tool_names=completed_tool_names,
        generated_media_artifact_paths=generated_media_artifact_paths or [],
    )


def _claimed_media_tools(response: str) -> list[str]:
    claimed: list[str] = []
    for line in response.splitlines():
        if not _MEDIA_TOOL_EXECUTION_CLAIM.search(line):
            continue
        if _NEGATED_MEDIA_TOOL_EXECUTION.search(line):
            continue
        for tool_name in MEDIA_GENERATION_TOOLS:
            if tool_name in line and tool_name not in claimed:
                claimed.append(tool_name)
    return claimed


def _has_unnegated_media_success_claim(response: str) -> bool:
    return any(
        _MEDIA_SUCCESS_CLAIM.search(line)
        and not _NEGATED_MEDIA_SUCCESS.search(line)
        for line in response.splitlines()
    )


def annotate_unverified_media_claims(
    response: str,
    *,
    completed_tool_names: set[str],
    generated_media_artifact_paths: list[str],
) -> str:
    """Mark media execution claims that lack evidence from the current turn."""
    missing_tools = [
        name
        for name in _claimed_media_tools(response)
        if name not in completed_tool_names
    ]
    warnings: list[str] = []
    cleaned = response
    if missing_tools:
        cleaned = _UNVERIFIED_TASK_ID.sub(
            lambda match: f"{match.group('label')}[未验证任务号已移除]",
            cleaned,
        )
        warnings.append(
            "⚠️ 系统未检测到本轮 "
            + "、".join(missing_tools)
            + " 的完成事件；关于已调用、工具回包或任务号的描述不可信，"
            "本轮不能计入生成调用次数。"
        )

    completed_media_tools = MEDIA_GENERATION_TOOLS.intersection(completed_tool_names)
    verified_generated_media_paths = [
        path
        for path in generated_media_artifact_paths
        if Path(path).suffix.lower() in MEDIA_SUFFIXES
    ]
    success_claimed = _has_unnegated_media_success_claim(cleaned)
    if completed_media_tools and not verified_generated_media_paths and success_claimed:
        warnings.append(
            "⚠️ 系统没有验证到本轮媒体生成工具产物；"
            "工具已执行但不能视为生成成功。"
        )
    elif success_claimed and not verified_generated_media_paths and not missing_tools:
        cleaned = _UNVERIFIED_TASK_ID.sub(
            lambda match: f"{match.group('label')}[未验证任务号已移除]",
            cleaned,
        )
        warnings.append(
            "⚠️ 系统未检测到本轮媒体工具完成事件或可验证产物；"
            "下述媒体生成成功描述未经验证。"
        )

    if not warnings:
        return cleaned
    return "\n\n".join([*warnings, cleaned]).rstrip()


def artifact_download_url(agent_id: uuid.UUID | str, path: str) -> str:
    return f"/api/agents/{agent_id}/files/download?{urlencode({'path': path})}"


def append_authoritative_artifacts(
    response: str,
    agent_id: uuid.UUID | str,
    artifact_paths: list[str],
) -> str:
    """Remove unverified generated links and append verified canonical links."""
    verified = list(dict.fromkeys(path for path in artifact_paths if _normalize_artifact_path(path)))
    verified_set = set(verified)
    unverified_paths: list[str] = []

    def replace_unverified(match: re.Match) -> str:
        query = parse_qs(urlsplit(match.group("url")).query)
        linked_path = _normalize_artifact_path((query.get("path") or [""])[0])
        if match.group("agent") == str(agent_id) and linked_path in verified_set:
            return match.group(0)
        if linked_path and linked_path not in unverified_paths:
            unverified_paths.append(linked_path)
        return "（未验证的产物链接已移除）"

    cleaned = _MARKDOWN_DOWNLOAD.sub(replace_unverified, response).rstrip()
    if unverified_paths:
        warning = "⚠️ 系统未验证到模型声明的产物，不能视为生成成功。"
        cleaned = f"{warning}\n\n{cleaned}"

    if not verified:
        return cleaned

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
