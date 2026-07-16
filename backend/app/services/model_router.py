"""SaaS tier + modality -> real LLMModel routing.

Resolves a user-facing tier (Lite/Pro/Ultra) and modality into a concrete
LLMModel using the platform-managed model_routes table, while enforcing the
tenant's subscription entitlements.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.database import async_session
from app.models.subscription import ModelRoute
from app.services.entitlements import get_tenant_entitlements
from app.services.modalities import canonicalize_modality, modality_match_values
from app.services.quota_guard import QuotaExceeded

if TYPE_CHECKING:
    from app.models.llm import LLMModel


# Internal tier mapping: user-facing SaaS tiers -> model tiers and quota weights
SAAS_TIER_ORDER = ("lite", "pro", "ultra")
SAAS_TO_MODEL_TIER = {
    "lite": "basic",
    "pro": "standard",
    "ultra": "premium",
}


@dataclass
class ResolvedRoute:
    """Result of resolving a SaaS tier + modality to a concrete model."""

    model: "LLMModel"
    fallback_model: "LLMModel | None"
    saas_tier: str
    modality: str
    provider: str
    model_name: str


async def resolve_route(
    tenant_id: uuid.UUID | None,
    saas_tier: str,
    modality: str | None,
    allow_fallback: bool = True,
) -> ResolvedRoute:
    """Resolve a SaaS tier + modality to a concrete LLMModel.

    Raises QuotaExceeded if the tier/modality is not allowed by the tenant's
    subscription or if no route is configured.
    """
    saas_tier = (saas_tier or "pro").lower()
    modality = canonicalize_modality(modality) or "text"

    await _check_tier_entitlement(tenant_id, saas_tier, modality)

    route = await _pick_route(saas_tier, modality)
    if not route:
        raise QuotaExceeded(
            f"No model route configured for {saas_tier}/{modality}.",
            quota_type="no_route",
        )

    model = await _load_model(route.llm_model_id, enabled_only=True)
    if not model:
        raise QuotaExceeded(
            f"Model route points to a missing or disabled model: {route.llm_model_id}.",
            quota_type="no_route",
        )

    fallback_model = None
    if allow_fallback and route.fallback_route_id:
        fallback_route = await _pick_route_by_id(route.fallback_route_id)
        if (
            fallback_route
            and fallback_route.saas_tier == saas_tier
            and fallback_route.modality == modality
        ):
            fallback_model = await _load_model(
                fallback_route.llm_model_id,
                enabled_only=True,
            )

    return ResolvedRoute(
        model=model,
        fallback_model=fallback_model,
        saas_tier=saas_tier,
        modality=modality,
        provider=model.provider,
        model_name=model.model,
    )


async def _check_tier_entitlement(
    tenant_id: uuid.UUID | None,
    saas_tier: str,
    modality: str,
) -> None:
    """Verify the tenant's subscription allows this tier/modality.

    No tenant / no subscription / empty allowed sets = no restriction
    (backward-compatible fallback).
    """
    if not tenant_id:
        return

    ent = await get_tenant_entitlements(tenant_id)
    if not ent:
        return

    allowed_modalities = ent.allowed_modalities or []
    allowed_tiers = ent.allowed_tiers or []

    # Empty allowed sets mean no restriction
    if not allowed_modalities and not allowed_tiers:
        return

    # Modality check (support canonical + aliases, e.g. vision/image)
    if allowed_modalities:
        match_values = modality_match_values(modality)
        if not any(v in allowed_modalities for v in match_values):
            raise QuotaExceeded(
                f"Modality '{modality}' is not included in your plan.",
                quota_type="model_modality",
            )

    # Tier check
    if allowed_tiers and saas_tier not in allowed_tiers:
        raise QuotaExceeded(
            f"Tier '{saas_tier}' is not included in your plan.",
            quota_type="model_tier",
        )


async def _pick_route(saas_tier: str, modality: str) -> ModelRoute | None:
    """Pick the highest-priority enabled route for a tier/modality."""
    async with async_session() as db:
        result = await db.execute(
            select(ModelRoute)
            .where(
                ModelRoute.saas_tier == saas_tier,
                ModelRoute.modality == modality,
                ModelRoute.enabled == True,  # noqa: E712
            )
            .order_by(
                ModelRoute.priority.desc(),
                ModelRoute.created_at.asc(),
                ModelRoute.id.asc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _pick_route_by_id(route_id: uuid.UUID) -> ModelRoute | None:
    """Load a route by ID."""
    async with async_session() as db:
        result = await db.execute(
            select(ModelRoute).where(
                ModelRoute.id == route_id,
                ModelRoute.enabled == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()


async def _load_model(
    model_id: uuid.UUID,
    *,
    enabled_only: bool = False,
) -> "LLMModel | None":
    """Load an LLMModel by ID."""
    from app.models.llm import LLMModel

    async with async_session() as db:
        conditions = [LLMModel.id == model_id]
        if enabled_only:
            conditions.append(LLMModel.enabled == True)  # noqa: E712
        result = await db.execute(select(LLMModel).where(*conditions))
        return result.scalar_one_or_none()


def saas_tier_to_model_tier(saas_tier: str) -> str:
    """Map user-facing SaaS tier to internal model tier for quota weighting."""
    return SAAS_TO_MODEL_TIER.get((saas_tier or "").lower(), "standard")
