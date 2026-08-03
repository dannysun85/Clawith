"""Volcengine Agent Plan visual-model protocol and routing contracts.

The Agent Plan gateway is intentionally represented as a distinct provider.
Using a normal Ark API key or the normal ``/api/v3`` gateway can bypass the
subscription allowance and create unexpected PAYG charges.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
import uuid


PROVIDER = "volcengine_agent_plan"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
DEFAULT_TEXT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan"
TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
TTS_MODEL = "doubao-seed-tts-2.0"
TTS_RESOURCE_ID = "seed-tts-2.0"
TTS_DEFAULT_SPEAKER = "zh_female_vv_uranus_bigtts"
ALLOWED_PLAN_TIERS = frozenset({"small", "medium", "large", "max"})
IMAGE_MODEL = "doubao-seedream-5.0-lite"
VIDEO_MODEL = "doubao-seedance-2.0"
VIDEO_MODEL_15_PRO = "doubao-seedance-1.5-pro"
VIDEO_MODEL_MINI = "doubao-seedance-2.0-mini"
VIDEO_CAPABLE_PLAN_TIERS = frozenset({"medium", "large", "max"})
# Single source of truth for the operator-reviewed Agent Plan video policy.
# Reviewed against the official 2026-06-07 Medium adjustment and 2026-07-24
# model retirement notices.  Medium no longer includes Seedance 2.0 / Fast,
# and Seedance 1.5 Pro is being retired in favour of Seedance 2.0 Mini.
VIDEO_PLAN_POLICY_REVIEWED_AT = "2026-07-24"
VIDEO_PLAN_POLICY_SOURCES = (
    "https://docs.volcengine.com/docs/82379/2525064?lang=zh",
    "https://docs.volcengine.com/docs/82379/2578673?lang=zh",
)
VIDEO_MODELS_BY_PLAN_TIER = {
    "medium": VIDEO_MODEL_MINI,
    "large": VIDEO_MODEL,
    "max": VIDEO_MODEL,
}
TEXT_MODELS_BY_SAAS_TIER = {
    "lite": "doubao-seed-2.0-mini",
    "pro": "doubao-seed-2.1-turbo",
    "ultra": "doubao-seed-evolving",
}
SEEDREAM_SKILL_VERSION = "3.0.0"
SEEDREAM_SKILL_LOCK_HASH = "4a150ace8b7d8ffa28e7fab87ec0398e5dff72221a032ee41a3013a617329798"
SEEDANCE_SKILL_VERSION = "4.0.0"
SEEDANCE_SKILL_LOCK_HASH = "cc4b905b8fbec7cc7c9fe94f16c94353a986001df03bebfcba38871b7c86b82d"
OFFICIAL_SKILL_SOURCE = "https://skills.volces.com/skills/volcengine/agentplan"

SUPPORTED_VIDEO_MODELS = frozenset(
    {
        VIDEO_MODEL_15_PRO,
        "doubao-seedance-2.0",
        "doubao-seedance-2.0-fast",
        "doubao-seedance-2.0-mini",
    }
)
RETIRING_VIDEO_MODELS = frozenset({"doubao-seedance-1.5-pro"})
VIDEO_MODEL_ALIASES = {
    VIDEO_MODEL_15_PRO: "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-2.0": "doubao-seedance-2-0-260128",
    "doubao-seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
    VIDEO_MODEL_MINI: "doubao-seedance-2-0-mini-260615",
}
# Keep the previously shipped provider ID readable for already persisted tasks
# and receipts.  New submissions must use the official Seedance 1.5 Pro ID
# above; the legacy ID is only an inbound compatibility alias.
VIDEO_MODEL_LEGACY_ALIASES = {
    "doubao-seedance-1-0-pro-250528": VIDEO_MODEL_15_PRO,
}
VIDEO_PROVIDER_MODELS = frozenset(
    (*VIDEO_MODEL_ALIASES.values(), *VIDEO_MODEL_LEGACY_ALIASES.keys())
)

# Agent Plan Seedream accepts either a quality preset (``2K``/``3K``/``4K``)
# or an explicit ``WIDTHxHEIGHT`` value in the same ``size`` field.  A bare
# quality preset does not carry the user's requested orientation, so the
# adapter must translate Astra's provider-neutral aspect-ratio contract into
# explicit pixels before submission.
IMAGE_DIMENSIONS_BY_QUALITY = {
    "2K": {
        "1:1": "2048x2048",
        "16:9": "2560x1440",
        "4:3": "2304x1728",
        "3:4": "1728x2304",
        "9:16": "1440x2560",
        "2:3": "1664x2496",
        "3:2": "2496x1664",
    },
    "3K": {
        "1:1": "3072x3072",
        "16:9": "3072x1728",
        "4:3": "3072x2304",
        "3:4": "2304x3072",
        "9:16": "1728x3072",
        "2:3": "2048x3072",
        "3:2": "3072x2048",
    },
    "4K": {
        "1:1": "4096x4096",
        "16:9": "4096x2304",
        "4:3": "4096x3072",
        "3:4": "3072x4096",
        "9:16": "2304x4096",
        "2:3": "2736x4096",
        "3:2": "4096x2736",
    },
}


class VolcengineAgentPlanError(RuntimeError):
    """Base error carrying structured provider evidence."""

    def __init__(
        self,
        message: str,
        *,
        provider_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_code = provider_code
        self.http_status = http_status


class VolcengineAgentPlanRejected(VolcengineAgentPlanError):
    """The provider returned a reviewed 4xx response before accepting work."""


@dataclass(frozen=True, slots=True)
class VolcengineVisualProfile:
    modality: str
    saas_tier: str
    model: str
    size: str | None = None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class SeedanceModelCapabilities:
    max_duration_seconds: int
    supported_resolutions: frozenset[str]
    supported_ratios: frozenset[str]
    supports_web_search: bool
    supports_draft: bool
    supports_flex_tier: bool
    supports_start_end_frame: bool = True
    supports_generate_audio: bool = True


_SEEDANCE_FIXED_RATIOS = frozenset({"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"})
VIDEO_MODEL_CAPABILITIES = {
    VIDEO_MODEL_15_PRO: SeedanceModelCapabilities(
        max_duration_seconds=12,
        supported_resolutions=frozenset({"480p", "720p", "1080p"}),
        supported_ratios=_SEEDANCE_FIXED_RATIOS,
        supports_web_search=False,
        supports_draft=True,
        supports_flex_tier=True,
    ),
    "doubao-seedance-2.0": SeedanceModelCapabilities(
        max_duration_seconds=15,
        supported_resolutions=frozenset({"480p", "720p", "1080p", "4k"}),
        supported_ratios=_SEEDANCE_FIXED_RATIOS,
        supports_web_search=True,
        supports_draft=False,
        supports_flex_tier=False,
    ),
    "doubao-seedance-2.0-fast": SeedanceModelCapabilities(
        max_duration_seconds=15,
        supported_resolutions=frozenset({"480p", "720p"}),
        supported_ratios=_SEEDANCE_FIXED_RATIOS,
        supports_web_search=True,
        supports_draft=False,
        supports_flex_tier=False,
    ),
    VIDEO_MODEL_MINI: SeedanceModelCapabilities(
        max_duration_seconds=15,
        supported_resolutions=frozenset({"480p", "720p"}),
        supported_ratios=_SEEDANCE_FIXED_RATIOS,
        supports_web_search=True,
        supports_draft=False,
        supports_flex_tier=False,
    ),
}


def normalize_base_url(value: str | None) -> str:
    """Return the only Agent Plan visual API base accepted by Astra."""

    normalized = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "ark.cn-beijing.volces.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Volcengine Agent Plan base_url must use the official HTTPS gateway")
    path = parsed.path.rstrip("/")
    if path not in {"/api/plan", "/api/plan/v3"}:
        raise ValueError("Volcengine Agent Plan base_url must end with /api/plan or /api/plan/v3")
    return DEFAULT_BASE_URL


def normalize_text_base_url(value: str | None) -> str:
    """Return the official Anthropic-compatible Agent Plan text gateway."""

    normalized = str(value or DEFAULT_TEXT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "ark.cn-beijing.volces.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Volcengine Agent Plan text base_url must use the official HTTPS gateway")
    if parsed.path.rstrip("/") not in {"/api/plan", "/api/plan/v3"}:
        raise ValueError("Volcengine Agent Plan text base_url must end with /api/plan or /api/plan/v3")
    return DEFAULT_TEXT_BASE_URL


def resolve_text_model(saas_tier: str) -> str:
    normalized_tier = str(saas_tier or "lite").strip().lower()
    return TEXT_MODELS_BY_SAAS_TIER.get(normalized_tier, TEXT_MODELS_BY_SAAS_TIER["lite"])


def resolve_video_model(plan_tier: str | None) -> str:
    """Choose the strongest video family authorized by an Agent Plan tier."""

    normalized_plan = str(plan_tier or "").strip().lower()
    model = VIDEO_MODELS_BY_PLAN_TIER.get(normalized_plan)
    if model is None:
        raise ValueError(
            "Agent Plan video requires Medium, Large, or Max and a current "
            "operator-reviewed model policy"
        )
    return model


def resolve_visual_profile(
    modality: str,
    saas_tier: str,
    *,
    plan_tier: str | None = None,
) -> VolcengineVisualProfile:
    normalized_modality = str(modality or "").strip().lower()
    normalized_tier = str(saas_tier or "lite").strip().lower()
    if normalized_tier not in {"lite", "pro", "ultra"}:
        normalized_tier = "lite"
    if normalized_modality == "image":
        size = {"lite": "2K", "pro": "3K", "ultra": "4K"}[normalized_tier]
        return VolcengineVisualProfile("image", normalized_tier, IMAGE_MODEL, size=size)
    if normalized_modality == "video":
        resolution = {"lite": "480p", "pro": "720p", "ultra": "1080p"}[normalized_tier]
        return VolcengineVisualProfile(
            "video",
            normalized_tier,
            resolve_video_model(plan_tier) if plan_tier is not None else VIDEO_MODEL,
            resolution=resolution,
        )
    raise ValueError(f"Unsupported Volcengine Agent Plan modality: {modality}")


def plan_tier_supports_modality(plan_tier: str | None, modality: str) -> bool:
    normalized_plan = str(plan_tier or "").strip().lower()
    normalized_modality = str(modality or "").strip().lower()
    if normalized_plan not in ALLOWED_PLAN_TIERS:
        return False
    if normalized_modality in {"text", "image", "audio"}:
        return True
    if normalized_modality == "video":
        return normalized_plan in VIDEO_CAPABLE_PLAN_TIERS
    return False


def tts_request_payload(
    *,
    text: str,
    speaker: str = TTS_DEFAULT_SPEAKER,
    audio_format: str = "mp3",
    sample_rate: int = 24000,
) -> dict[str, Any]:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        raise ValueError("Agent Plan TTS text must not be empty")
    normalized_format = str(audio_format or "").strip().lower()
    if normalized_format not in {"mp3", "wav", "pcm", "ogg_opus"}:
        raise ValueError("Agent Plan TTS format must be mp3, wav, pcm, or ogg_opus")
    normalized_rate = int(sample_rate)
    if normalized_rate <= 0:
        raise ValueError("Agent Plan TTS sample_rate must be positive")
    normalized_speaker = str(speaker or TTS_DEFAULT_SPEAKER).strip()
    if not normalized_speaker:
        raise ValueError("Agent Plan TTS speaker must not be empty")
    return {
        "req_params": {
            "text": normalized_text,
            "speaker": normalized_speaker,
            "audio_params": {
                "format": normalized_format,
                "sample_rate": normalized_rate,
            },
        }
    }


def _tts_error_from_payload(payload: object) -> VolcengineAgentPlanError:
    data = payload if isinstance(payload, dict) else {}
    code = data.get("code")
    message = str(data.get("message") or data.get("msg") or "TTS provider request failed")
    return VolcengineAgentPlanError(
        f"Volcengine Agent Plan TTS error ({code}): {message}",
        provider_code=str(code) if code is not None else None,
        http_status=200,
    )


async def generate_speech(
    *,
    api_key: str,
    text: str,
    speaker: str = TTS_DEFAULT_SPEAKER,
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    on_provider_request_started: Callable[[], None] | None = None,
    on_provider_accepted: Callable[[bytes | None], Awaitable[None]] | None = None,
) -> bytes:
    """Generate one bounded TTS asset through the official Agent Plan HTTP stream."""

    from app.services.agent_tools import (
        MAX_GENERATED_AUDIO_BYTES,
        _bounded_content_length,
        _public_only_async_client,
    )

    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": TTS_RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "X-Control-Require-Usage-Tokens-Return": "*",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    audio = bytearray()
    completed = False
    async with _public_only_async_client(
        TTS_ENDPOINT,
        timeout=120,
        on_request_started=on_provider_request_started,
    ) as client:
        async with client.stream(
            "POST",
            TTS_ENDPOINT,
            headers=headers,
            json=tts_request_payload(
                text=text,
                speaker=speaker,
                audio_format=audio_format,
                sample_rate=sample_rate,
            ),
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                body = (await response.aread())[:1000]
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError):
                    payload = {}
                raise provider_error_from_response(response.status_code, payload)
            _bounded_content_length(
                response,
                max_bytes=MAX_GENERATED_AUDIO_BYTES * 2,
                label="Volcengine Agent Plan TTS response",
            )
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise VolcengineAgentPlanError(
                        "Volcengine Agent Plan TTS returned invalid JSON"
                    ) from exc
                code = payload.get("code", 0) if isinstance(payload, dict) else 0
                if code == 20000000:
                    completed = True
                    break
                if code not in (0, "0", None):
                    raise _tts_error_from_payload(payload)
                encoded = payload.get("data") if isinstance(payload, dict) else None
                if not encoded:
                    continue
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (TypeError, ValueError) as exc:
                    raise VolcengineAgentPlanError(
                        "Volcengine Agent Plan TTS returned invalid audio data"
                    ) from exc
                if len(audio) + len(chunk) > MAX_GENERATED_AUDIO_BYTES:
                    raise VolcengineAgentPlanError(
                        "Volcengine Agent Plan TTS audio exceeds the safety limit"
                    )
                audio.extend(chunk)
    if not completed or not audio:
        raise VolcengineAgentPlanError(
            "Volcengine Agent Plan TTS did not return a complete audio asset"
        )
    result = bytes(audio)
    if on_provider_accepted:
        await on_provider_accepted(result)
    return result


def image_request_payload(
    *,
    prompt: str,
    model: str = IMAGE_MODEL,
    size: str = "2K",
    reference_image: str | list[str] | tuple[str, ...] | None = None,
    reference_strength: float | None = None,
    sequential: bool = False,
    enable_web_search: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "sequential_image_generation": "auto" if sequential else "disabled",
        "size": size,
        "output_format": "png",
        "response_format": "url",
        "watermark": False,
    }
    if reference_image:
        if isinstance(reference_image, str):
            references = [reference_image]
        else:
            references = [str(value).strip() for value in reference_image if str(value).strip()]
        if not 1 <= len(references) <= 14:
            raise ValueError("Agent Plan image generation accepts between 1 and 14 reference images")
        payload["image"] = references[0] if len(references) == 1 else references
    if reference_strength is not None:
        normalized_strength = float(reference_strength)
        if not 0 <= normalized_strength <= 1:
            raise ValueError("reference_strength must be between 0 and 1")
        payload["reference_strength"] = normalized_strength
    if enable_web_search:
        payload["tools"] = [{"type": "web_search"}]
    return payload


def image_size_for_aspect_ratio(size: str, aspect_ratio: str) -> str:
    """Preserve Astra's requested image shape on the Agent Plan route."""

    normalized_size = str(size or "").strip().upper()
    normalized_ratio = str(aspect_ratio or "1:1").strip()
    dimensions = IMAGE_DIMENSIONS_BY_QUALITY.get(normalized_size)
    if dimensions is None:
        raise ValueError("Agent Plan image quality must be 2K, 3K, or 4K")
    explicit_size = dimensions.get(normalized_ratio)
    if explicit_size is None:
        supported = ", ".join(dimensions)
        raise ValueError(f"Agent Plan image aspect_ratio must be one of: {supported}")
    return explicit_size


def video_gateway_model_id(model: str) -> str:
    """Validate a public Seedance name and return the official Skill model ID.

    The Agent-facing contract keeps stable public names.  The reviewed
    Agent Plan Seedance Skill v4.0.0 maps those names to dated provider IDs
    immediately before submission.  ``UnsupportedModel`` from an account that
    lacks the relevant plan tier is entitlement evidence, not proof that the
    official alias itself is invalid.
    """
    normalized = str(model or "").strip()
    if not normalized:
        raise ValueError("A Seedance model is required")
    if normalized in VIDEO_MODEL_LEGACY_ALIASES:
        # Canonicalize legacy persisted IDs before a new submission so a
        # re-run cannot accidentally call the retired/wrong provider ID.
        return VIDEO_MODEL_ALIASES[VIDEO_MODEL_LEGACY_ALIASES[normalized]]
    if normalized in VIDEO_PROVIDER_MODELS:
        return normalized
    if normalized not in SUPPORTED_VIDEO_MODELS:
        raise ValueError(f"Unsupported Agent Plan video model: {normalized}")
    return VIDEO_MODEL_ALIASES[normalized]


def stable_video_model_name(model: str) -> str:
    """Return the public model name used by Astra capability policy."""

    normalized = str(model or "").strip()
    if normalized in SUPPORTED_VIDEO_MODELS:
        return normalized
    for public_name, provider_name in VIDEO_MODEL_ALIASES.items():
        if normalized == provider_name:
            return public_name
    if normalized in VIDEO_MODEL_LEGACY_ALIASES:
        return VIDEO_MODEL_LEGACY_ALIASES[normalized]
    raise ValueError(f"Unsupported Agent Plan video model: {normalized}")


def video_model_capabilities(model: str) -> SeedanceModelCapabilities:
    return VIDEO_MODEL_CAPABILITIES[stable_video_model_name(model)]


def video_request_payload(
    *,
    prompt: str,
    model: str = VIDEO_MODEL,
    duration: int,
    resolution: str,
    ratio: str,
    first_frame_image: str | None = None,
    last_frame_image: str | None = None,
    generate_audio: bool = False,
    seed: int | None = None,
    camera_fixed: bool | None = None,
    return_last_frame: bool | None = None,
    service_tier: str | None = None,
    draft: bool | None = None,
    enable_web_search: bool = False,
) -> dict[str, Any]:
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        raise ValueError("Agent Plan video prompt must not be empty")
    public_model = stable_video_model_name(model)
    capabilities = video_model_capabilities(public_model)
    normalized_duration = int(duration)
    if not 4 <= normalized_duration <= capabilities.max_duration_seconds:
        raise ValueError(
            f"{public_model} duration must be between 4 and "
            f"{capabilities.max_duration_seconds} seconds"
        )
    normalized_resolution = str(resolution or "").strip().lower()
    if normalized_resolution not in capabilities.supported_resolutions:
        supported = ", ".join(sorted(capabilities.supported_resolutions))
        raise ValueError(f"{public_model} resolution must be one of: {supported}")
    normalized_ratio = str(ratio or "").strip()
    if normalized_ratio not in capabilities.supported_ratios:
        supported = ", ".join(sorted(capabilities.supported_ratios))
        raise ValueError(f"{public_model} ratio must be one of: {supported}")
    if enable_web_search and not capabilities.supports_web_search:
        raise ValueError(f"{public_model} does not support web search")
    if draft is True and not capabilities.supports_draft:
        raise ValueError(f"{public_model} does not support draft mode")

    normalized_tier: str | None = None
    if service_tier is not None:
        normalized_tier = str(service_tier).strip().lower()
        if normalized_tier not in {"default", "flex"}:
            raise ValueError("service_tier must be default or flex")
        if normalized_tier == "flex" and not capabilities.supports_flex_tier:
            raise ValueError(f"{public_model} does not support flex service tier")

    content: list[dict[str, Any]] = [{"type": "text", "text": normalized_prompt}]
    if first_frame_image:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": first_frame_image},
                "role": "first_frame",
            }
        )
    if last_frame_image:
        if not first_frame_image:
            raise ValueError("last_frame_image requires first_frame_image")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": last_frame_image},
                "role": "last_frame",
            }
        )
    payload: dict[str, Any] = {
        "model": video_gateway_model_id(public_model),
        "content": content,
        "generate_audio": bool(generate_audio),
        "ratio": normalized_ratio,
        "duration": normalized_duration,
        "resolution": normalized_resolution,
        "watermark": False,
    }
    if seed is not None:
        payload["seed"] = int(seed)
    if camera_fixed is not None:
        payload["camera_fixed"] = bool(camera_fixed)
    if return_last_frame is not None:
        payload["return_last_frame"] = bool(return_last_frame)
    if normalized_tier is not None:
        payload["service_tier"] = normalized_tier
    if draft is not None:
        payload["draft"] = bool(draft)
    if enable_web_search:
        payload["tools"] = [{"type": "web_search"}]
    return payload


def provider_error_from_response(status_code: int, payload: object) -> VolcengineAgentPlanError:
    data = payload if isinstance(payload, dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(
        error.get("code")
        or error.get("type")
        or data.get("code")
        or data.get("type")
        or status_code
    )
    message = str(error.get("message") or data.get("message") or "provider request failed")
    error_type = (
        VolcengineAgentPlanRejected
        if 400 <= int(status_code) < 500 and int(status_code) != 408
        else VolcengineAgentPlanError
    )
    return error_type(
        f"Volcengine Agent Plan error ({code}): {message}",
        provider_code=code,
        http_status=int(status_code),
    )


def image_url_from_response(payload: object) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else []
    for row in rows:
        if isinstance(row, dict) and str(row.get("url") or "").strip():
            return str(row["url"]).strip()
    raise VolcengineAgentPlanError("Volcengine Agent Plan image response has no URL")


def video_task_id_from_response(payload: object) -> str:
    if isinstance(payload, dict) and str(payload.get("id") or "").strip():
        return str(payload["id"]).strip()
    raise VolcengineAgentPlanError("Volcengine Agent Plan video response has no task id")


def normalized_video_status(payload: object) -> str:
    status = str(payload.get("status") if isinstance(payload, dict) else "").strip().lower()
    return {
        "queued": "Queueing",
        "running": "Processing",
        "succeeded": "Success",
        "failed": "Fail",
        "cancelled": "Fail",
        "expired": "Fail",
    }.get(status, "Unknown")


def video_url_from_response(payload: object) -> str | None:
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, dict):
        return None
    value = str(content.get("video_url") or "").strip()
    return value or None


async def generate_image(
    *,
    api_key: str,
    base_url: str | None,
    prompt: str,
    model: str,
    size: str,
    reference_image: str | None,
    on_provider_request_started: Callable[[], None] | None = None,
    on_provider_accepted: Callable[[str | None], Awaitable[None]] | None = None,
) -> bytes:
    """Generate and immediately capture one Agent Plan image."""

    from app.services.agent_tools import (
        MAX_GENERATED_IMAGE_BYTES,
        MAX_PROVIDER_IMAGE_JSON_BYTES,
        _bounded_json_request,
        _bounded_public_http_download,
        _public_only_async_client,
    )

    normalized_base = normalize_base_url(base_url)
    url = f"{normalized_base}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with _public_only_async_client(
        url,
        timeout=180,
        on_request_started=on_provider_request_started,
    ) as client:
        response = await _bounded_json_request(
            client,
            "POST",
            url,
            max_bytes=MAX_PROVIDER_IMAGE_JSON_BYTES,
            label="Volcengine Agent Plan image response",
            headers=headers,
            json=image_request_payload(
                prompt=prompt,
                model=model,
                size=size,
                reference_image=reference_image,
            ),
        )
    payload = response.json() if response.data is not None else {}
    if response.status_code < 200 or response.status_code >= 300:
        raise provider_error_from_response(response.status_code, payload)
    image_url = image_url_from_response(payload)
    if on_provider_accepted:
        await on_provider_accepted(image_url)
    return await _bounded_public_http_download(
        image_url,
        max_bytes=MAX_GENERATED_IMAGE_BYTES,
        label="Volcengine Agent Plan image download",
        timeout=90,
    )


async def create_video_task(
    *,
    api_key: str,
    base_url: str | None,
    prompt: str,
    model: str,
    duration: int,
    resolution: str,
    ratio: str,
    first_frame_image: str | None,
    last_frame_image: str | None = None,
    generate_audio: bool = False,
    on_provider_request_started: Callable[[], None] | None = None,
) -> str:
    from app.services.agent_tools import (
        MAX_PROVIDER_CONTROL_JSON_BYTES,
        _bounded_json_request,
        _public_only_async_client,
    )

    normalized_base = normalize_base_url(base_url)
    url = f"{normalized_base}/contents/generations/tasks"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with _public_only_async_client(
        url,
        timeout=180,
        on_request_started=on_provider_request_started,
    ) as client:
        response = await _bounded_json_request(
            client,
            "POST",
            url,
            max_bytes=MAX_PROVIDER_CONTROL_JSON_BYTES,
            label="Volcengine Agent Plan video creation response",
            headers=headers,
            json=video_request_payload(
                prompt=prompt,
                model=model,
                duration=duration,
                resolution=resolution,
                ratio=ratio,
                first_frame_image=first_frame_image,
                last_frame_image=last_frame_image,
                generate_audio=generate_audio,
            ),
        )
    payload = response.json() if response.data is not None else {}
    if response.status_code < 200 or response.status_code >= 300:
        raise provider_error_from_response(response.status_code, payload)
    return video_task_id_from_response(payload)


async def query_video_task(
    *,
    api_key: str,
    base_url: str | None,
    task_id: str,
) -> dict[str, Any]:
    from app.services.agent_tools import (
        MAX_PROVIDER_CONTROL_JSON_BYTES,
        _bounded_json_request,
        _public_only_async_client,
    )

    normalized_base = normalize_base_url(base_url)
    url = f"{normalized_base}/contents/generations/tasks/{str(task_id).strip()}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with _public_only_async_client(url, timeout=60) as client:
        response = await _bounded_json_request(
            client,
            "GET",
            url,
            max_bytes=MAX_PROVIDER_CONTROL_JSON_BYTES,
            label="Volcengine Agent Plan video status response",
            headers=headers,
        )
    payload = response.json() if response.data is not None else {}
    if response.status_code < 200 or response.status_code >= 300:
        raise provider_error_from_response(response.status_code, payload)
    if not isinstance(payload, dict):
        raise VolcengineAgentPlanError("Volcengine Agent Plan video status response is invalid")
    return payload


async def download_video(video_url: str) -> bytes:
    from app.services.agent_tools import (
        MAX_MINIMAX_VIDEO_DOWNLOAD_BYTES,
        _bounded_public_http_download,
    )

    return await _bounded_public_http_download(
        video_url,
        max_bytes=MAX_MINIMAX_VIDEO_DOWNLOAD_BYTES,
        label="Volcengine Agent Plan video download",
        timeout=180,
    )


__all__ = [
    "ALLOWED_PLAN_TIERS",
    "DEFAULT_BASE_URL",
    "DEFAULT_TEXT_BASE_URL",
    "IMAGE_MODEL",
    "OFFICIAL_SKILL_SOURCE",
    "PROVIDER",
    "RETIRING_VIDEO_MODELS",
    "SEEDANCE_SKILL_LOCK_HASH",
    "SEEDANCE_SKILL_VERSION",
    "SeedanceModelCapabilities",
    "SEEDREAM_SKILL_LOCK_HASH",
    "SEEDREAM_SKILL_VERSION",
    "VIDEO_CAPABLE_PLAN_TIERS",
    "VIDEO_MODEL",
    "VIDEO_MODEL_15_PRO",
    "VIDEO_MODEL_MINI",
    "VIDEO_MODEL_CAPABILITIES",
    "VIDEO_MODEL_ALIASES",
    "VIDEO_MODEL_LEGACY_ALIASES",
    "VIDEO_MODELS_BY_PLAN_TIER",
    "VIDEO_PLAN_POLICY_REVIEWED_AT",
    "VIDEO_PLAN_POLICY_SOURCES",
    "VIDEO_PROVIDER_MODELS",
    "SUPPORTED_VIDEO_MODELS",
    "TEXT_MODELS_BY_SAAS_TIER",
    "TTS_DEFAULT_SPEAKER",
    "TTS_ENDPOINT",
    "TTS_MODEL",
    "TTS_RESOURCE_ID",
    "VolcengineAgentPlanError",
    "VolcengineAgentPlanRejected",
    "VolcengineVisualProfile",
    "create_video_task",
    "download_video",
    "generate_image",
    "generate_speech",
    "image_request_payload",
    "image_size_for_aspect_ratio",
    "image_url_from_response",
    "normalize_base_url",
    "normalize_text_base_url",
    "normalized_video_status",
    "plan_tier_supports_modality",
    "provider_error_from_response",
    "query_video_task",
    "resolve_visual_profile",
    "resolve_video_model",
    "stable_video_model_name",
    "resolve_text_model",
    "tts_request_payload",
    "video_request_payload",
    "video_gateway_model_id",
    "video_model_capabilities",
    "video_task_id_from_response",
    "video_url_from_response",
]
