"""Tenant subscription entitlements (地基 3.6).

Derives effective entitlements from a tenant's active subscription, applying the
state machine:
- active / trialing / canceled → entitlements until period_end
- past_due → entitlements during Stripe retry window
- expired / no subscription / past cutoff → None (caller falls back to tenant defaults)

None return = backward-compatible fallback (3.3): quota_guard uses tenant.default_*.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.subscription import Plan, Subscription
from app.services.modalities import canonicalize_modalities

# past_due grace window ≈ Stripe automatic retry window (3-5 days)
STRIPE_RETRY_WINDOW = timedelta(days=5)


@dataclass
class Entitlements:
    """Effective entitlements derived from a tenant's active subscription."""

    plan_id: uuid.UUID
    plan_code: str
    max_agents: int
    max_llm_calls_per_day: int
    message_limit: int
    message_period: str
    max_triggers: int
    credits_per_period: int
    allowed_modalities: list = field(default_factory=list)
    allowed_tiers: list = field(default_factory=list)
    generation_modalities: list = field(default_factory=list)
    generation_tiers: list = field(default_factory=list)


async def get_active_subscription(tenant_id: uuid.UUID) -> Subscription | None:
    """Get the tenant's most recent non-expired subscription record."""
    async with async_session() as db:
        result = await db.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status.in_(("active", "trialing", "canceled", "past_due")),
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_tenant_entitlements(tenant_id: uuid.UUID) -> Entitlements | None:
    """Return current effective entitlements, or None if no active subscription.

    None → caller falls back to tenant.default_* (向后兼容, 3.3).
    """
    sub = await get_active_subscription(tenant_id)
    if not sub:
        return None

    now = datetime.now(timezone.utc)
    # State machine cutoff (3.6)
    if sub.status in ("active", "trialing", "canceled"):
        # canceled retains until period_end (cancel at period end).
        # period_end=None means permanent (e.g. free plan) → never expires.
        if sub.period_end and now >= sub.period_end:
            return None  # past period_end → expired
    elif sub.status == "past_due":
        cutoff = (sub.period_end or now) + STRIPE_RETRY_WINDOW
        if now >= cutoff:
            return None  # past retry window → expired
    else:
        return None

    # Load plan
    async with async_session() as db:
        plan_result = await db.execute(select(Plan).where(Plan.id == sub.plan_id))
        plan = plan_result.scalar_one_or_none()
    if not plan:
        return None

    features = plan.features if isinstance(plan.features, dict) else {}
    generation_modalities = features.get("generation_modalities")
    if not isinstance(generation_modalities, list):
        generation_modalities = [
            modality
            for modality in canonicalize_modalities(plan.allowed_modalities or [])
            if modality in {"image", "audio", "music", "video"}
        ]
    generation_tiers = features.get("generation_tiers")
    if not isinstance(generation_tiers, list):
        generation_tiers = list(plan.allowed_tiers or [])

    return Entitlements(
        plan_id=plan.id,
        plan_code=plan.code,
        max_agents=plan.max_agents,
        max_llm_calls_per_day=plan.max_llm_calls_per_day,
        message_limit=plan.message_limit,
        message_period=plan.message_period,
        max_triggers=plan.max_triggers,
        credits_per_period=plan.credits_per_period,
        allowed_modalities=list(plan.allowed_modalities or []),
        allowed_tiers=list(plan.allowed_tiers or []),
        generation_modalities=canonicalize_modalities(generation_modalities),
        generation_tiers=[
            str(value).strip().lower()
            for value in generation_tiers
            if str(value).strip()
        ],
    )
