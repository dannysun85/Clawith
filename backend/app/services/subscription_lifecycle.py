"""Subscription lifecycle enforcement (§3.6 state machine + 兜底).

- expire_subscriptions(): hourly safety-net job marking subscriptions past their
  cutoff as expired (active/trialing/canceled past period_end; past_due past the
  Stripe retry window). Webhooks are the primary path; this guarantees eventual
  consistency if a webhook is missed.
- enforce_agent_limit(tenant_id): stop excess agents when a tenant's effective
  max_agents drops (downgrade or expiry). Stopped agents keep their data/config;
  quota_guard rejects their LLM calls. Restored on renewal via
  restore_stopped_agents.
- start_subscription_lifecycle_daemon(): background loop registered in app lifespan.
  MiniMax Token Plan polling is a separate health-tracked worker task.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.subscription import CreditBalance, PaymentOrder, Plan, Subscription
from app.services.entitlements import STRIPE_RETRY_WINDOW, get_tenant_entitlements
from app.services.credit_service import (
    SUBSCRIPTION_PLAN_CHANGE_REF_TYPE,
    grant_credits_in_session,
    subscription_plan_change_grant_ref_id,
)
from app.services.llm.load_balancer import reset_daily_usage

# Hourly — cheap query, catches expiries within the hour. Webhooks handle the
# common case immediately; this is the safety net.
LIFECYCLE_INTERVAL_SECONDS = 3600
PAID_EFFECTS_LEASE_TIMEOUT = timedelta(minutes=10)


async def ensure_free_subscription_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    granted_by: uuid.UUID | None = None,
) -> Subscription | None:
    """Create the default free subscription and initial credit grant for a new tenant.

    This keeps registration/self-service tenant creation aligned with the admin
    subscription assignment path: a subscription row alone is not enough; the
    tenant also needs an auditable credit grant in credit_balances/transactions.
    """
    free_plan_result = await db.execute(
        select(Plan).where(
            Plan.code == "free",
            Plan.is_active == True,  # noqa: E712
        )
    )
    free_plan = free_plan_result.scalar_one_or_none()
    if not free_plan:
        balance_result = await db.execute(select(CreditBalance).where(CreditBalance.tenant_id == tenant_id))
        if not balance_result.scalar_one_or_none():
            db.add(CreditBalance(tenant_id=tenant_id, balance=0, reserved=0))
        return None

    existing_result = await db.execute(
        select(Subscription)
        .where(
            Subscription.tenant_id == tenant_id,
            Subscription.status.in_(("active", "trialing", "canceled", "past_due")),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        return existing

    subscription = Subscription(
        tenant_id=tenant_id,
        plan_id=free_plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        period_end=None,
    )
    db.add(subscription)
    await db.flush()

    if free_plan.credits_per_period > 0:
        await grant_credits_in_session(
            db,
            tenant_id=tenant_id,
            amount=free_plan.credits_per_period,
            reason="subscribe",
            granted_by=granted_by,
            ref_type="subscription",
            ref_id=subscription.id,
        )
    else:
        balance_result = await db.execute(select(CreditBalance).where(CreditBalance.tenant_id == tenant_id))
        if not balance_result.scalar_one_or_none():
            db.add(CreditBalance(tenant_id=tenant_id, balance=0, reserved=0))

    return subscription


async def _effective_max_agents(tenant_id: uuid.UUID) -> int:
    """Effective max_agents: active subscription entitlements → tenant default."""
    from app.models.tenant import Tenant

    ent = await get_tenant_entitlements(tenant_id)
    if ent:
        return ent.max_agents
    async with async_session() as db:
        r = await db.execute(select(Tenant.default_max_agents).where(Tenant.id == tenant_id))
        return r.scalar_one_or_none() or 2


async def expire_subscriptions() -> int:
    """Mark past-cutoff subscriptions expired + stop excess agents. Returns count expired."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        # active/trialing/canceled past period_end → expired
        r1 = await db.execute(
            select(Subscription).where(
                Subscription.status.in_(("active", "trialing", "canceled")),
                Subscription.period_end.isnot(None),
                Subscription.period_end < now,
            )
        )
        expired = list(r1.scalars().all())
        # past_due past Stripe retry window → expired
        pd_cutoff = now - STRIPE_RETRY_WINDOW
        r2 = await db.execute(
            select(Subscription).where(
                Subscription.status == "past_due",
                Subscription.period_end.isnot(None),
                Subscription.period_end < pd_cutoff,
            )
        )
        expired.extend(r2.scalars().all())

        applied = 0
        for s in expired:
            if s.scheduled_plan_id:
                # A paid downgrade was scheduled: apply it instead of expiring.
                scheduled_plan = await db.get(Plan, s.scheduled_plan_id)
                if scheduled_plan and scheduled_plan.is_active:
                    period_days = 365 if (s.scheduled_period or "monthly") == "yearly" else 30
                    previous_plan_id = s.plan_id
                    activation_at = s.period_end or now
                    base = s.period_end if s.period_end and s.period_end > now else now
                    s.plan_id = scheduled_plan.id
                    s.status = "active"
                    s.period_end = base + timedelta(days=period_days)
                    s.scheduled_plan_id = None
                    s.scheduled_period = None
                    if scheduled_plan.credits_per_period:
                        await grant_credits_in_session(
                            db,
                            tenant_id=s.tenant_id,
                            amount=scheduled_plan.credits_per_period,
                            reason="subscribe",
                            granted_by=None,
                            ref_type=SUBSCRIPTION_PLAN_CHANGE_REF_TYPE,
                            ref_id=subscription_plan_change_grant_ref_id(
                                s.id,
                                previous_plan_id,
                                scheduled_plan.id,
                                activation_at,
                            ),
                        )
                    applied += 1
                    continue
            s.status = "expired"
        if expired:
            await db.commit()
            logger.info(
                f"[subscription_lifecycle] expired {len(expired) - applied}, applied scheduled downgrade {applied}"
            )

    # max_agents drops to tenant default on expiry → stop excess agents
    for s in expired:
        await enforce_agent_limit(s.tenant_id)
    return len(expired)


async def enforce_agent_limit(tenant_id: uuid.UUID) -> int:
    """Stop excess agents when active count > effective max_agents (3.6 降级/失效).

    Keeps the oldest agents (by created_at); stops the newest excess. Stopped
    agents are not deleted — data/config preserved; quota_guard blocks their LLM
    calls. Returns count stopped.
    """
    from app.models.agent import Agent
    from app.models.onboarding import UserTenantOnboarding

    max_agents = await _effective_max_agents(tenant_id)
    personal_assistant_ids = select(UserTenantOnboarding.personal_assistant_agent_id).where(
        UserTenantOnboarding.tenant_id == tenant_id,
        UserTenantOnboarding.personal_assistant_agent_id.isnot(None),
    )
    async with async_session() as db:
        active = (
            (
                await db.execute(
                    select(Agent)
                    .where(
                        Agent.tenant_id == tenant_id,
                        Agent.status.notin_(("stopped", "error")),
                        Agent.is_expired == False,  # noqa: E712
                        Agent.is_system == False,  # noqa: E712
                        ~Agent.id.in_(personal_assistant_ids),
                    )
                    .order_by(Agent.created_at.asc(), Agent.id.asc())
                )
            )
            .scalars()
            .all()
        )
        excess = active[max_agents:]  # newest beyond the limit (ordered asc → kept oldest)
        for a in excess:
            a.status = "stopped"
        if excess:
            await db.commit()
            logger.info(f"[subscription_lifecycle] stopped {len(excess)} excess agent(s) in tenant {tenant_id}")
        return len(excess)


async def restore_stopped_agents(tenant_id: uuid.UUID) -> int:
    """Restore stopped agents to idle when the limit allows (after upgrade/renewal)."""
    from app.models.agent import Agent
    from app.models.onboarding import UserTenantOnboarding

    max_agents = await _effective_max_agents(tenant_id)
    personal_assistant_ids = select(UserTenantOnboarding.personal_assistant_agent_id).where(
        UserTenantOnboarding.tenant_id == tenant_id,
        UserTenantOnboarding.personal_assistant_agent_id.isnot(None),
    )
    async with async_session() as db:
        active = (
            (
                await db.execute(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id,
                        Agent.status.notin_(("stopped", "error")),
                        Agent.is_expired == False,  # noqa: E712
                        Agent.is_system == False,  # noqa: E712
                        ~Agent.id.in_(personal_assistant_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        slots = max_agents - len(active)
        if slots <= 0:
            return 0
        stopped = (
            (
                await db.execute(
                    select(Agent)
                    .where(
                        Agent.tenant_id == tenant_id,
                        Agent.status == "stopped",
                        Agent.is_expired == False,  # noqa: E712
                        Agent.is_system == False,  # noqa: E712
                        ~Agent.id.in_(personal_assistant_ids),
                    )
                    .order_by(Agent.created_at.asc(), Agent.id.asc())
                )
            )
            .scalars()
            .all()
        )
        to_restore = stopped[:slots]
        for a in to_restore:
            a.status = "idle"
        if to_restore:
            await db.commit()
            logger.info(f"[subscription_lifecycle] restored {len(to_restore)} agent(s) in tenant {tenant_id}")
        return len(to_restore)


async def _claim_paid_subscribe_effects(
    order_id: uuid.UUID,
) -> tuple[uuid.UUID, int] | None:
    """Claim one retryable effects receipt without holding a cross-service lock."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update())
        order = result.scalar_one_or_none()
        if order is None or order.type != "subscribe" or order.status not in {"paid", "partially_refunded"}:
            return None
        effects_status = order.paid_effects_status or "pending"
        if effects_status == "applied":
            return None
        if (
            effects_status == "processing"
            and order.paid_effects_started_at is not None
            and order.paid_effects_started_at > now - PAID_EFFECTS_LEASE_TIMEOUT
        ):
            return None
        order.paid_effects_status = "processing"
        order.paid_effects_attempts = max(int(order.paid_effects_attempts or 0), 0) + 1
        order.paid_effects_started_at = now
        order.paid_effects_error = None
        attempt = order.paid_effects_attempts
        tenant_id = order.tenant_id
        await db.commit()
    return tenant_id, attempt


async def _finish_paid_subscribe_effects(
    order_id: uuid.UUID,
    *,
    attempt: int,
    applied: bool,
    error: Exception | None = None,
) -> bool:
    """Finish only the lease attempt that performed the side effects."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            select(PaymentOrder)
            .where(
                PaymentOrder.id == order_id,
                PaymentOrder.paid_effects_status == "processing",
                PaymentOrder.paid_effects_attempts == attempt,
            )
            .with_for_update()
        )
        order = result.scalar_one_or_none()
        if order is None:
            return False
        order.paid_effects_status = "applied" if applied else "failed"
        order.paid_effects_error = None if error is None else f"{type(error).__name__}: {str(error)}"[:500]
        order.paid_effects_applied_at = now if applied else None
        await db.commit()
    return True


async def apply_paid_subscribe_effects(order: PaymentOrder) -> bool:
    """Restore routing and agent slots after a subscribe order is marked paid.

    Shared by the webhook, QR poll, and missed-webhook reconciler. Safe to
    call more than once for the same paid order.
    """
    if order.type != "subscribe" or order.status not in {"paid", "partially_refunded"}:
        return False
    claim = await _claim_paid_subscribe_effects(order.id)
    if claim is None:
        return False
    tenant_id, attempt = claim
    from app.services.agent_plan_selection import reconcile_tenant_agent_plan_selections

    try:
        await reconcile_tenant_agent_plan_selections(tenant_id)
        await restore_stopped_agents(tenant_id)
        await enforce_agent_limit(tenant_id)
    except Exception as exc:
        receipt_updated = await _finish_paid_subscribe_effects(
            order.id,
            attempt=attempt,
            applied=False,
            error=exc,
        )
        from app.services.production_issue_monitor import record_production_issue

        if receipt_updated:
            await record_production_issue(
                source="billing_reconciliation",
                category="paid_subscribe_effects",
                summary="Paid subscription effects require retry",
                severity="critical",
                error_code="paid_effects_incomplete",
                operation="apply_paid_subscribe_effects",
                tenant_id=tenant_id,
                metadata={
                    "order_id": str(order.id),
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                },
            )
        raise
    return await _finish_paid_subscribe_effects(
        order.id,
        attempt=attempt,
        applied=True,
    )


async def start_subscription_lifecycle_daemon() -> None:
    """Expire past-due subscriptions and reset daily credential usage hourly."""
    logger.info("[subscription_lifecycle] daemon started")

    while True:
        try:
            await expire_subscriptions()
            await reset_daily_usage()  # idempotent within a day (date guard in reset_daily_usage)
        except Exception as exc:
            logger.error(
                "[subscription_lifecycle] job failed error_type={}",
                type(exc).__name__,
            )
        await asyncio.sleep(LIFECYCLE_INTERVAL_SECONDS)
