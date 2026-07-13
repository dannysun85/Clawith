"""Canonical modality names shared by plan gating and credential routing."""

from __future__ import annotations

CANONICAL_MODALITIES = ("text", "image", "audio", "music", "video", "multimodal")

_ALIASES = {
    "vision": "image",
    "voice": "audio",
    "tts": "audio",
}

_REVERSE_ALIASES: dict[str, tuple[str, ...]] = {
    "image": ("vision",),
    "audio": ("voice", "tts"),
}


def canonicalize_modality(value: str | None) -> str | None:
    """Return the canonical modality value while preserving unknown values."""
    if value is None:
        return None
    normalized = value.strip().lower()
    return _ALIASES.get(normalized, normalized)


def canonicalize_modalities(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Canonicalize a modality list and preserve stable order."""
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = canonicalize_modality(value)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def modality_match_values(value: str | None) -> list[str]:
    """Values that should match existing persisted capability tags."""
    canonical = canonicalize_modality(value)
    if not canonical:
        return []
    values = [canonical, *_REVERSE_ALIASES.get(canonical, ())]
    if canonical != "multimodal":
        values.append("multimodal")
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
