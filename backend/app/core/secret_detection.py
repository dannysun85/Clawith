"""Conservative detection for credentials pasted into public display fields."""

import math
import re
from collections import Counter


_EXPLICIT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?key(?:[_ -]?id)?|"
        r"secret[_ -]?access[_ -]?key|secret[_ -]?key)\s*[:=]\s*\S+"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:ark|sk|ak)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAK[A-Z0-9]{14,}\b"),
    re.compile(r"\bSK[A-Za-z0-9]{14,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:sk|key|token)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_LONG_OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9_./+=-]{24,}$")
_EMBEDDED_OPAQUE_TOKEN = re.compile(r"(?<![A-Za-z0-9_./+=~-])[A-Za-z0-9_./+=~-]{32,}(?![A-Za-z0-9_./+=~-])")


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in Counter(value).values())


def looks_like_secret(value: str | None) -> bool:
    """Return True for explicit credentials or high-entropy opaque tokens.

    This intentionally targets fields such as company names where a long,
    machine-generated token is never a useful label. It should not be applied
    to URLs, encrypted columns, or legitimate credential inputs.
    """
    candidate = (value or "").strip()
    if not candidate:
        return False
    if any(pattern.search(candidate) for pattern in _EXPLICIT_SECRET_PATTERNS):
        return True
    if not _LONG_OPAQUE_TOKEN.fullmatch(candidate):
        return False
    character_groups = sum(
        bool(pattern.search(candidate))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"), re.compile(r"[_./+=-]"))
    )
    return character_groups >= 3 and _shannon_entropy(candidate) >= 3.6


def contains_secret_like_material(value: str | None) -> bool:
    """Return True when narrative text contains credential-like material.

    Unlike :func:`looks_like_secret`, this scans opaque fragments embedded in
    otherwise human-readable notes.  It is intended for audit/comment fields,
    where retaining a false-positive-free copy of a token is less important
    than preventing accidental long-term credential storage.
    """

    candidate = (value or "").strip()
    if not candidate:
        return False
    if any(pattern.search(candidate) for pattern in _EXPLICIT_SECRET_PATTERNS):
        return True
    for match in _EMBEDDED_OPAQUE_TOKEN.finditer(candidate):
        token = match.group(0)
        character_groups = sum(
            bool(pattern.search(token))
            for pattern in (
                re.compile(r"[a-z]"),
                re.compile(r"[A-Z]"),
                re.compile(r"[0-9]"),
                re.compile(r"[_./+=~-]"),
            )
        )
        if character_groups >= 3 and _shannon_entropy(token) >= 3.6:
            return True
    return False
