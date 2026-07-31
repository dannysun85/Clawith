"""Provider-independent selection for platform-funded image/video generation."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from app.services.llm import load_balancer
from app.services.llm import utils as llm_utils
from app.services.llm.load_balancer import NoCredentialAvailable
from app.services.llm.load_balancer import credential_quota_is_blocked
from app.services.volcengine_agent_plan import (
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    TTS_MODEL,
    normalize_base_url as normalize_volcengine_agent_plan_base_url,
    plan_tier_supports_modality,
    resolve_visual_profile,
)


MINIMAX_PROVIDER = "minimax"
DEFAULT_MEDIA_PROVIDER_ORDER = (
    VOLCENGINE_AGENT_PLAN_PROVIDER,
    MINIMAX_PROVIDER,
)


def media_provider_order_for_modality(modality: str) -> tuple[str, ...]:
    """Return the providers implemented by the runtime for one modality."""

    normalized = str(modality or "").strip().lower()
    if normalized == "music":
        return (MINIMAX_PROVIDER,)
    if normalized in {"image", "audio", "video"}:
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
) -> PreparedMediaProvider:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in DEFAULT_MEDIA_PROVIDER_ORDER:
        raise ValueError(f"Unsupported media provider: {provider}")

    quota_model = minimax_model
    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        normalized_modality = str(modality or "").strip().lower()
        quota_model = (
            TTS_MODEL
            if normalized_modality == "audio"
            else None
            if normalized_modality == "video"
            else resolve_visual_profile(normalized_modality, saas_tier).model
        )

    # Resolve through the module so existing MiniMax call sites and tests can
    # continue to patch the canonical load-balancer boundary. Keeping a copied
    # function binding here bypassed those overrides and caused legacy paths to
    # open a real database connection during otherwise isolated executions.
    credential = await load_balancer.pick_credential(
        normalized_provider,
        modality=modality,
        quota_modality=modality,
        quota_model=quota_model,
    )
    credential_provider = str(getattr(credential, "provider", "") or "").strip().lower()
    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER and (
        credential_provider != VOLCENGINE_AGENT_PLAN_PROVIDER
    ):
        # Agent Plan routing must never reinterpret a legacy/generic
        # credential as a plan account. Besides protecting old fixtures, this
        # prevents an Ark PAYG key or an incompletely migrated row from being
        # sent to the subscription gateway.
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
    # Use the canonical module boundary for the same reason as
    # ``pick_credential`` above: credential decryption is patched and audited
    # centrally by existing callers.
    api_key = llm_utils.get_credential_api_key(credential)
    if not api_key:
        raise NoCredentialAvailable(
            normalized_provider,
            modality,
            reason="credential has no usable API key",
        )

    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        plan_tier = getattr(credential, "plan_tier", None)
        if not plan_tier_supports_modality(plan_tier, modality):
            # This should already be prevented at credential-write time. Keep
            # the runtime boundary defensive for legacy rows and direct SQL.
            raise NoCredentialAvailable(
                normalized_provider,
                modality,
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
        if profile and credential_quota_is_blocked(
            credential,
            normalized_modality,
            profile.model,
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
    )


__all__ = [
    "DEFAULT_MEDIA_PROVIDER_ORDER",
    "MINIMAX_PROVIDER",
    "PreparedMediaProvider",
    "media_provider_order_for_voice_id",
    "media_provider_order_for_modality",
    "minimax_video_requires_first_frame",
    "prepare_media_provider",
]
