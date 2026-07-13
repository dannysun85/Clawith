"""Product-facing MiniMax media profiles for Lite / Pro / Ultra.

These profiles select provider models and quality parameters. They are routing
policy, not authorization objects: subscription access is checked separately
against the user-facing generation capability and tier.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool
from app.services.modalities import canonicalize_modality


@dataclass(frozen=True)
class MiniMaxMediaProfile:
    modality: str
    tier: str
    model: str
    sample_rate: int | None = None
    bitrate: int | None = None
    duration: int | None = None
    resolution: str | None = None
    enabled: bool = True


MINIMAX_MEDIA_TOOL_NAMES: dict[str, str] = {
    "image": "generate_image_minimax",
    "audio": "generate_speech_minimax",
    "music": "generate_music_minimax",
    "video": "generate_video_minimax",
}

MINIMAX_MEDIA_MODELS: dict[str, tuple[str, ...]] = {
    "image": ("image-01",),
    "audio": ("speech-2.8-turbo", "speech-2.8-hd"),
    "music": ("music-2.6",),
    # 2.3-Fast is intentionally excluded here: MiniMax documents it as an
    # image-to-video-only model, while this route is also used for T2V.
    "video": ("MiniMax-Hailuo-02", "MiniMax-Hailuo-2.3"),
}

MINIMAX_MEDIA_PROFILE_FIELDS: dict[str, tuple[str, ...]] = {
    "image": ("model", "enabled"),
    "audio": ("model", "sample_rate", "bitrate", "enabled"),
    "music": ("model", "sample_rate", "bitrate", "enabled"),
    "video": ("model", "duration", "resolution", "enabled"),
}

MINIMAX_VIDEO_ALLOWED_QUALITY: dict[str, set[tuple[int, str]]] = {
    "lite": {(6, "768P")},
    "pro": {(6, "768P"), (10, "768P")},
    "ultra": {(6, "768P"), (10, "768P"), (6, "1080P")},
}


_PROFILES: dict[tuple[str, str], MiniMaxMediaProfile] = {
    ("image", "lite"): MiniMaxMediaProfile("image", "lite", "image-01"),
    ("image", "pro"): MiniMaxMediaProfile("image", "pro", "image-01"),
    # MiniMax currently exposes the same general text-to-image model for all
    # three product tiers. Do not invent a higher tier model that requires a
    # different input contract.
    ("image", "ultra"): MiniMaxMediaProfile("image", "ultra", "image-01"),
    ("audio", "lite"): MiniMaxMediaProfile(
        "audio", "lite", "speech-2.8-turbo", sample_rate=24000, bitrate=64000
    ),
    ("audio", "pro"): MiniMaxMediaProfile(
        "audio", "pro", "speech-2.8-turbo", sample_rate=32000, bitrate=128000
    ),
    ("audio", "ultra"): MiniMaxMediaProfile(
        "audio", "ultra", "speech-2.8-hd", sample_rate=44100, bitrate=256000
    ),
    ("music", "lite"): MiniMaxMediaProfile(
        "music", "lite", "music-2.6", sample_rate=44100, bitrate=128000
    ),
    ("music", "pro"): MiniMaxMediaProfile(
        "music", "pro", "music-2.6", sample_rate=44100, bitrate=256000
    ),
    ("music", "ultra"): MiniMaxMediaProfile(
        "music", "ultra", "music-2.6", sample_rate=44100, bitrate=256000
    ),
    ("video", "lite"): MiniMaxMediaProfile(
        "video", "lite", "MiniMax-Hailuo-02", duration=6, resolution="768P"
    ),
    ("video", "pro"): MiniMaxMediaProfile(
        "video", "pro", "MiniMax-Hailuo-2.3", duration=6, resolution="768P"
    ),
    ("video", "ultra"): MiniMaxMediaProfile(
        "video", "ultra", "MiniMax-Hailuo-2.3", duration=6, resolution="1080P"
    ),
}

_LEGACY_DEFAULTS: dict[str, dict[str, Any]] = {
    "image": {"model": "image-01"},
    "audio": {"model": "speech-2.8-turbo", "sample_rate": 32000, "bitrate": 128000},
    "music": {"model": "music-2.6", "sample_rate": 44100, "bitrate": 256000},
    "video": {
        "model": "MiniMax-Hailuo-2.3",
        "duration": 6,
        "resolution": "1080P",
    },
}


def _configured_value(
    config: dict[str, Any],
    tier: str,
    field: str,
    default: Any,
    legacy_default: Any,
) -> Any:
    tier_key = f"{tier}_{field}"
    if tier_key in config and config[tier_key] not in (None, ""):
        return config[tier_key]

    # Existing global Tool rows contain old one-size-fits-all defaults. Ignore
    # those exact values so they do not silently defeat the product tier. A
    # deliberately different generic value remains a backward-compatible
    # administrator override.
    generic = config.get(field)
    if generic not in (None, "") and generic != legacy_default:
        return generic
    return default


def resolve_minimax_media_profile(
    modality: str,
    tier: str,
    config: dict[str, Any] | None = None,
) -> MiniMaxMediaProfile:
    canonical = canonicalize_modality(modality) or ""
    normalized_tier = str(tier or "lite").strip().lower()
    if normalized_tier not in {"lite", "pro", "ultra"}:
        normalized_tier = "lite"
    try:
        profile = _PROFILES[(canonical, normalized_tier)]
    except KeyError as exc:
        raise ValueError(f"Unsupported MiniMax media modality: {modality}") from exc

    values = dict(config or {})
    legacy = _LEGACY_DEFAULTS[canonical]
    updates: dict[str, Any] = {}
    for field in ("model", "sample_rate", "bitrate", "duration", "resolution"):
        current = getattr(profile, field)
        if current is None:
            continue
        value = _configured_value(values, normalized_tier, field, current, legacy.get(field))
        if field in {"sample_rate", "bitrate", "duration"}:
            value = int(value)
        if field == "resolution":
            value = str(value).upper()
        updates[field] = value
    enabled_value = values.get(f"{normalized_tier}_enabled", True)
    if isinstance(enabled_value, str):
        enabled_value = enabled_value.strip().lower() not in {"0", "false", "no", "off"}
    updates["enabled"] = bool(enabled_value)
    return replace(profile, **updates)


async def load_platform_minimax_media_profile(
    modality: str,
    tier: str,
) -> MiniMaxMediaProfile:
    """Load platform-owned routing policy without tenant/agent overrides.

    Voice, format and other invocation preferences may still be configured per
    tenant or agent. Provider model selection and quality tiers are controlled
    centrally from the SaaS console.
    """
    canonical = canonicalize_modality(modality) or ""
    tool_name = MINIMAX_MEDIA_TOOL_NAMES.get(canonical)
    if not tool_name:
        raise ValueError(f"Unsupported MiniMax media modality: {modality}")
    async with async_session() as db:
        result = await db.execute(
            select(Tool.config).where(
                Tool.name == tool_name,
                Tool.tenant_id.is_(None),
            )
        )
        config = result.scalar_one_or_none() or {}
    return resolve_minimax_media_profile(canonical, tier, config)


def minimax_media_override_snapshot(
    modality: str,
    tier: str,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only one route's non-sensitive platform override fields."""
    canonical = canonicalize_modality(modality) or ""
    values = dict(config or {})
    return {
        field: values[f"{tier}_{field}"]
        for field in MINIMAX_MEDIA_PROFILE_FIELDS.get(canonical, ())
        if f"{tier}_{field}" in values
    }


def constrain_minimax_video_request(
    tier: str,
    profile: MiniMaxMediaProfile,
    requested_duration: int | str | None,
    requested_resolution: str | None,
) -> tuple[int, str]:
    """Return a provider-valid T2V quality pair within the selected tier."""
    normalized_tier = tier if tier in {"lite", "pro", "ultra"} else "lite"
    allowed = MINIMAX_VIDEO_ALLOWED_QUALITY[normalized_tier]
    try:
        duration = int(requested_duration) if requested_duration not in (None, "") else int(profile.duration or 6)
    except (TypeError, ValueError):
        duration = int(profile.duration or 6)
    resolution = str(requested_resolution or profile.resolution or "768P").upper()
    if (duration, resolution) in allowed:
        return duration, resolution
    if normalized_tier == "ultra" and duration == 10:
        return 10, "768P"
    safe_default = {
        "lite": (6, "768P"),
        "pro": (6, "768P"),
        "ultra": (6, "1080P"),
    }[normalized_tier]
    configured_default = (
        int(profile.duration or safe_default[0]),
        str(profile.resolution or safe_default[1]).upper(),
    )
    return configured_default if configured_default in allowed else safe_default
