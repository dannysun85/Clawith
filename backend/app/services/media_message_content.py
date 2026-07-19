"""Safe persistence helpers for chat messages containing inline media.

Inline ``data:`` URLs are transport-only inputs for the current model call.
They must never be written to chat history or retained in the in-memory
conversation after that call, otherwise later turns repeatedly resend the
same binary payload.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_INLINE_MEDIA_MARKER_RE = re.compile(
    r"\[(?P<kind>image|video)_data:[^\]]*\]",
    flags=re.IGNORECASE,
)
_INLINE_MEDIA_DATA_URL_RE = re.compile(
    r"data:(?:image|video)/[^;,\s\"']+;base64,[A-Za-z0-9+/=]+",
    flags=re.IGNORECASE,
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def contains_inline_media(content: str | None) -> bool:
    """Return whether *content* contains an image/video transport marker."""

    return bool(_INLINE_MEDIA_MARKER_RE.search(content or ""))


def redact_inline_media_for_token_estimate(serialized: str) -> str:
    """Replace transport-only media bytes before estimating text tokens.

    Vision providers account for an image or video as a media input rather
    than one text token per few Base64 characters. Runtime context budgeting
    therefore keeps a stable typed placeholder while preserving the original
    payload for the actual provider request.
    """

    return _INLINE_MEDIA_DATA_URL_RE.sub(
        "data:media;base64,[omitted]",
        serialized,
    )


def _normalized_file_names(file_names: str | Iterable[str] | None) -> list[str]:
    if not file_names:
        return []

    raw_names = file_names.split(",") if isinstance(file_names, str) else file_names
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = str(raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        name = _CONTROL_CHARS_RE.sub("", name).replace("[", "(").replace("]", ")")
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def sanitize_inline_media_content(
    content: str | None,
    *,
    display_content: str | None = None,
    file_names: str | Iterable[str] | None = None,
) -> str:
    """Build a display-friendly, binary-free value for persistence/history.

    ``display_content`` is preferred when supplied because the web client sends
    it without transport markers. File markers retain a durable reference to
    workspace uploads. When a channel has no file reference, a typed
    ``[image]``/``[video]`` placeholder still records what the user sent.
    """

    transport_content = content or ""
    kinds = [match.group("kind").lower() for match in _INLINE_MEDIA_MARKER_RE.finditer(transport_content)]
    source = display_content if display_content else transport_content
    cleaned = _INLINE_MEDIA_MARKER_RE.sub("", source).strip()

    prefixes = [f"[file:{name}]" for name in _normalized_file_names(file_names)]
    if not prefixes and kinds:
        prefixes = [f"[{kind}]" for kind in dict.fromkeys(kinds)]

    existing_lines = set(cleaned.splitlines())
    prefixes = [prefix for prefix in prefixes if prefix not in existing_lines]
    parts = [*prefixes]
    if cleaned:
        parts.append(cleaned)
    return "\n".join(parts).strip()
