"""Provider-independent selection for platform-funded image/video generation."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from app.services.llm import load_balancer
from app.services.llm import utils as llm_utils
from app.services.llm.load_balancer import CredentialUnavailableReason, NoCredentialAvailable
from app.services.llm.load_balancer import credential_quota_is_blocked
from app.models.llm import LLMCredential
from app.services.media_daily_allowance import (
    DailyMediaAllowanceExhausted,
    claim_minimax_video_allowance,
)
from app.services.minimax_media_profiles import MINIMAX_VIDEO_ALLOWED_QUALITY
from app.services.volcengine_agent_plan import (
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    RETIRING_VIDEO_MODELS,
    TTS_MODEL,
    VIDEO_MODEL,
    VIDEO_MODELS_BY_PLAN_TIER,
    normalize_base_url as normalize_volcengine_agent_plan_base_url,
    plan_tier_supports_modality,
    resolve_visual_profile,
    video_model_capabilities,
)


MINIMAX_PROVIDER = "minimax"
DEFAULT_MEDIA_PROVIDER_ORDER = (
    VOLCENGINE_AGENT_PLAN_PROVIDER,
    MINIMAX_PROVIDER,
)
VIDEO_MEDIA_PROVIDER_ORDER = (
    MINIMAX_PROVIDER,
    VOLCENGINE_AGENT_PLAN_PROVIDER,
)
IMAGE_EXECUTION_STRATEGIES = frozenset(
    {"commercial_quality", "creative_exploration"}
)


def normalize_image_execution_strategy(value: object) -> str:
    """Resolve the provider-neutral image outcome policy.

    The strategy describes the customer's work contract, not a vendor choice.
    ``commercial_quality`` keeps the strongest verified commercial route first;
    ``creative_exploration`` prioritizes visual variation. The durable task
    receipt remains the only authority for the provider/model actually used.
    """

    normalized = str(value or "commercial_quality").strip().lower()
    if normalized not in IMAGE_EXECUTION_STRATEGIES:
        raise ValueError(
            "execution_strategy must be commercial_quality or creative_exploration"
        )
    return normalized


def media_provider_order_for_image_strategy(value: object) -> tuple[str, ...]:
    """Return the server-owned provider order for an image work contract."""

    strategy = normalize_image_execution_strategy(value)
    if strategy == "creative_exploration":
        return (MINIMAX_PROVIDER, VOLCENGINE_AGENT_PLAN_PROVIDER)
    return DEFAULT_MEDIA_PROVIDER_ORDER


def media_provider_order_for_modality(modality: str) -> tuple[str, ...]:
    """Return the providers implemented by the runtime for one modality."""

    normalized = str(modality or "").strip().lower()
    if normalized == "music":
        return (MINIMAX_PROVIDER,)
    if normalized == "video":
        return VIDEO_MEDIA_PROVIDER_ORDER
    if normalized in {"image", "audio"}:
        return DEFAULT_MEDIA_PROVIDER_ORDER
    return ()


def media_provider_order_for_voice_id(voice_id: str | None) -> tuple[str, ...]:
    """Keep an explicit provider voice identity stable across routing.

    Automatic/default speech may use the normal Agent Plan -> MiniMax route.
    Provider voice identifiers are not interchangeable, so an explicit
    identifier pins the request to the provider that owns its namespace instead
    of silently changing the customer's selected voice during fallback.
    """

    normalized = str(voice_id or "").strip()
    if not normalized or normalized.lower() == "auto":
        return DEFAULT_MEDIA_PROVIDER_ORDER
    if normalized.endswith("bigtts"):
        return (VOLCENGINE_AGENT_PLAN_PROVIDER,)
    return (MINIMAX_PROVIDER,)


def validate_media_route_policy() -> tuple[str, ...]:
    """Validate the provider/model policy without touching credentials.

    This is a static contract check for CI and local readiness diagnostics.  It
    deliberately does not probe a Provider, inspect a key, or infer an account
    entitlement.  Runtime selection must keep the quality-first order and the
    reviewed Agent Plan tier-to-Seedance mapping in sync.
    """

    errors: list[str] = []
    expected_order = {
        "image": (VOLCENGINE_AGENT_PLAN_PROVIDER, MINIMAX_PROVIDER),
        "audio": (VOLCENGINE_AGENT_PLAN_PROVIDER, MINIMAX_PROVIDER),
        "video": (MINIMAX_PROVIDER, VOLCENGINE_AGENT_PLAN_PROVIDER),
        "music": (MINIMAX_PROVIDER,),
    }
    for modality, expected in expected_order.items():
        actual = media_provider_order_for_modality(modality)
        if actual != expected:
            errors.append(
                f"{modality}: provider order {actual!r} does not match {expected!r}"
            )

    expected_image_strategies = {
        "commercial_quality": (
            VOLCENGINE_AGENT_PLAN_PROVIDER,
            MINIMAX_PROVIDER,
        ),
        "creative_exploration": (
            MINIMAX_PROVIDER,
            VOLCENGINE_AGENT_PLAN_PROVIDER,
        ),
    }
    for strategy, expected in expected_image_strategies.items():
        actual = media_provider_order_for_image_strategy(strategy)
        if actual != expected:
            errors.append(
                f"image strategy {strategy}: provider order {actual!r} does not match {expected!r}"
            )

    expected_video_models = {
        "large": VIDEO_MODEL,
        "max": VIDEO_MODEL,
    }
    for plan_tier, expected_model in expected_video_models.items():
        actual_model = VIDEO_MODELS_BY_PLAN_TIER.get(plan_tier)
        if actual_model != expected_model:
            errors.append(
                f"{plan_tier}: video model {actual_model!r} does not match "
                f"{expected_model!r}"
            )
        if not plan_tier_supports_modality(plan_tier, "video"):
            errors.append(f"{plan_tier}: video entitlement is unexpectedly disabled")

    for ineligible_tier in ("small", "medium"):
        if plan_tier_supports_modality(ineligible_tier, "video"):
            errors.append(f"{ineligible_tier}: video entitlement must remain disabled")
    if any(model in RETIRING_VIDEO_MODELS for model in expected_video_models.values()):
        errors.append("a new-tier video model is incorrectly marked retiring")
    if VIDEO_MODEL in RETIRING_VIDEO_MODELS:
        errors.append("Large/Max video model is incorrectly marked retiring")

    return tuple(errors)


def minimax_video_requires_first_frame(
    aspect_ratio: str,
    first_frame_image: object | None,
) -> bool:
    """Return whether MiniMax T2V cannot honor the requested delivery shape.

    The MiniMax text-to-video request contract has no aspect-ratio field. Its
    current T2V output is 16:9, so portrait and other non-landscape deliveries
    need a correctly shaped first frame and the image-to-video path.
    """

    return (
        str(aspect_ratio or "").strip() != "16:9"
        and first_frame_image is None
    )


def minimax_video_supported_durations(tier: str) -> frozenset[int]:
    """Return the exact delivery durations the MiniMax route can honor.

    MiniMax video is billed in fixed per-tier duration buckets.  A request
    outside the reviewed set cannot be honored exactly and must fail over to a
    capable route instead of being silently shortened.
    """

    allowed = MINIMAX_VIDEO_ALLOWED_QUALITY.get(str(tier or "").strip().lower())
    if not allowed:
        allowed = MINIMAX_VIDEO_ALLOWED_QUALITY["lite"]
    return frozenset(duration for duration, _resolution in allowed)


def video_route_max_duration_seconds(provider: str, model: str | None) -> int | None:
    """Return the reviewed duration ceiling of one prepared video route.

    The MiniMax ceiling is tier-scoped and therefore answered by
    ``minimax_video_supported_durations`` instead; this helper covers the
    model-scoped Agent Plan Seedance capability matrix.
    """

    normalized = str(provider or "").strip().lower()
    if normalized == VOLCENGINE_AGENT_PLAN_PROVIDER and model:
        return video_model_capabilities(model).max_duration_seconds
    return None


def volcengine_video_quota_model(model: str, resolution: str | None) -> str:
    """Return the variable-cost Agent Plan video allowance resource.

    Seedance video AFP consumption changes materially with output resolution.
    Provider evidence that an expensive 1080p request exceeds the remaining
    allowance therefore must not disable a cheaper 480p or 720p request for
    the same model.
    """

    normalized_model = str(model or "").strip()
    normalized_resolution = str(resolution or "").strip().lower()
    if not normalized_model or not normalized_resolution:
        return normalized_model
    return f"{normalized_model}@{normalized_resolution}"


@dataclass(frozen=True, slots=True)
class PreparedMediaProvider:
    provider: str
    credential_id: uuid.UUID
    api_key: str
    base_url: str
    model: str
    plan_tier: str | None = None
    size: str | None = None
    resolution: str | None = None
    daily_allowance_claim_id: uuid.UUID | None = None
    daily_allowance_quota: int | None = None
    daily_allowance_used: int | None = None
    daily_allowance_remaining: int | None = None

    @property
    def id(self) -> uuid.UUID:
        """Compatibility alias for existing durable media call sites."""

        return self.credential_id


def _minimax_base_url(value: str | None) -> str:
    normalized = str(value or "https://api.minimaxi.com").strip().rstrip("/")
    if normalized.lower().endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized or "https://api.minimaxi.com"


async def prepare_media_provider(
    provider: str,
    *,
    modality: str,
    saas_tier: str,
    minimax_model: str,
    reserve_daily_video_allowance: bool = False,
) -> PreparedMediaProvider:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in DEFAULT_MEDIA_PROVIDER_ORDER:
        raise ValueError(f"Unsupported media provider: {provider}")

    quota_model = minimax_model
    preselected_profile = None
    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        normalized_modality = str(modality or "").strip().lower()
        preselected_profile = (
            None
            if normalized_modality == "audio"
            else resolve_visual_profile(normalized_modality, saas_tier)
        )
        quota_model = (
            TTS_MODEL
            if normalized_modality == "audio"
            else volcengine_video_quota_model(
                preselected_profile.model,
                preselected_profile.resolution,
            )
            if normalized_modality == "video"
            else preselected_profile.model
        )

    # Resolve through the module so existing MiniMax call sites and tests can
    # continue to patch the canonical load-balancer boundary. Keeping a copied
    # function binding here bypassed those overrides and caused legacy paths to
    # open a real database connection during otherwise isolated executions.
    excluded_credentials: set[uuid.UUID] = set()
    allowance = None
    while True:
        pick_kwargs = {
            "modality": modality,
            "quota_modality": modality,
            "quota_model": quota_model,
        }
        if excluded_credentials:
            pick_kwargs["exclude_credential_ids"] = excluded_credentials
        credential = await load_balancer.pick_credential(
            normalized_provider,
            **pick_kwargs,
        )
        credential_provider = str(
            getattr(credential, "provider", "") or ""
        ).strip().lower()
        if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER and (
            credential_provider != VOLCENGINE_AGENT_PLAN_PROVIDER
        ):
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
                reason="credential is not an explicit Agent Plan account",
            )
        if credential_provider and credential_provider != normalized_provider:
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
                reason="credential provider does not match the selected route",
            )
        api_key = llm_utils.get_credential_api_key(credential)
        if not api_key:
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
                reason="credential has no usable API key",
            )
        if not (
            reserve_daily_video_allowance
            and normalized_provider == MINIMAX_PROVIDER
            and credential_provider == MINIMAX_PROVIDER
            and isinstance(credential, LLMCredential)
            and str(modality).strip().lower() == "video"
        ):
            break
        try:
            allowance = await claim_minimax_video_allowance(credential.id)
            break
        except DailyMediaAllowanceExhausted:
            excluded_credentials.add(credential.id)
            continue

    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        plan_tier = getattr(credential, "plan_tier", None)
        if not plan_tier_supports_modality(plan_tier, modality):
            # This should already be prevented at credential-write time. Keep
            # the runtime boundary defensive for legacy rows and direct SQL.
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
                reason_code=CredentialUnavailableReason.CAPABILITY_MISMATCH,
                reason="credential plan tier does not support this modality",
            )
        normalized_modality = str(modality or "").strip().lower()
        profile = (
            None
            if normalized_modality == "audio"
            else resolve_visual_profile(
                normalized_modality,
                saas_tier,
                plan_tier=plan_tier,
            )
        )
        quota_profile_model = (
            volcengine_video_quota_model(profile.model, profile.resolution)
            if profile and normalized_modality == "video"
            else profile.model
            if profile
            else None
        )
        if profile and credential_quota_is_blocked(
            credential,
            normalized_modality,
            quota_profile_model,
        ):
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
                reason="credential model allowance is blocked by provider evidence",
            )
        return PreparedMediaProvider(
            provider=normalized_provider,
            credential_id=credential.id,
            api_key=api_key,
            base_url=normalize_volcengine_agent_plan_base_url(credential.base_url),
            model=TTS_MODEL if profile is None else profile.model,
            plan_tier=plan_tier,
            size=profile.size if profile else None,
            resolution=profile.resolution if profile else None,
        )

    return PreparedMediaProvider(
        provider=normalized_provider,
        credential_id=credential.id,
        api_key=api_key,
        base_url=_minimax_base_url(credential.base_url),
        model=minimax_model,
        daily_allowance_claim_id=allowance.claim_id if allowance else None,
        daily_allowance_quota=allowance.quota if allowance else None,
        daily_allowance_used=allowance.used if allowance else None,
        daily_allowance_remaining=allowance.remaining if allowance else None,
    )


__all__ = [
    "DEFAULT_MEDIA_PROVIDER_ORDER",
    "IMAGE_EXECUTION_STRATEGIES",
    "MINIMAX_PROVIDER",
    "PreparedMediaProvider",
    "VIDEO_MEDIA_PROVIDER_ORDER",
    "media_provider_order_for_voice_id",
    "media_provider_order_for_image_strategy",
    "media_provider_order_for_modality",
    "minimax_video_requires_first_frame",
    "minimax_video_supported_durations",
    "normalize_image_execution_strategy",
    "prepare_media_provider",
    "validate_media_route_policy",
    "video_route_max_duration_seconds",
    "volcengine_video_quota_model",
]
