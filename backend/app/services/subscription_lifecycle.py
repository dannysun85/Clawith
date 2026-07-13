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
"""

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.subscription import CreditBalance, Plan, Subscription
from app.services.entitlements import STRIPE_RETRY_WINDOW, get_tenant_entitlements
from app.services.credit_service import grant_credits_in_session
from app.services.llm.load_balancer import reset_daily_usage

# Hourly — cheap query, catches expiries within the hour. Webhooks handle the
# common case immediately; this is the safety net.
LIFECYCLE_INTERVAL_SECONDS = 3600


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

        for s in expired:
            s.status = "expired"
        if expired:
            await db.commit()
            logger.info(f"[subscription_lifecycle] expired {len(expired)} subscription(s)")

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
    from app.models.agent import Agent, AgentTemplate

    max_agents = await _effective_max_agents(tenant_id)
    assistant_priority = case(
        (Agent.access_mode == "private", 0),
        (Agent.role_description == "Private Assistant", 0),
        (AgentTemplate.name == "Private Assistant", 0),
        else_=1,
    )
    async with async_session() as db:
        active = (
            await db.execute(
                select(Agent)
                .outerjoin(AgentTemplate, Agent.template_id == AgentTemplate.id)
                .where(
                    Agent.tenant_id == tenant_id,
                    Agent.status.notin_(("stopped", "error")),
                    Agent.is_expired == False,  # noqa: E712
                    Agent.is_system == False,  # noqa: E712
                )
                .order_by(assistant_priority.asc(), Agent.created_at.asc(), Agent.id.asc())
            )
        ).scalars().all()
        excess = active[max_agents:]  # newest beyond the limit (ordered asc → kept oldest)
        for a in excess:
            a.status = "stopped"
        if excess:
            await db.commit()
            logger.info(
                f"[subscription_lifecycle] stopped {len(excess)} excess agent(s) in tenant {tenant_id}"
            )
        return len(excess)


async def restore_stopped_agents(tenant_id: uuid.UUID) -> int:
    """Restore stopped agents to idle when the limit allows (after upgrade/renewal)."""
    from app.models.agent import Agent, AgentTemplate

    max_agents = await _effective_max_agents(tenant_id)
    assistant_priority = case(
        (Agent.access_mode == "private", 0),
        (Agent.role_description == "Private Assistant", 0),
        (AgentTemplate.name == "Private Assistant", 0),
        else_=1,
    )
    async with async_session() as db:
        active = (
            await db.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.status.notin_(("stopped", "error")),
                    Agent.is_expired == False,  # noqa: E712
                    Agent.is_system == False,  # noqa: E712
                )
            )
        ).scalars().all()
        slots = max_agents - len(active)
        if slots <= 0:
            return 0
        stopped = (
            await db.execute(
                select(Agent)
                .outerjoin(AgentTemplate, Agent.template_id == AgentTemplate.id)
                .where(
                    Agent.tenant_id == tenant_id,
                    Agent.status == "stopped",
                    Agent.is_expired == False,  # noqa: E712
                    Agent.is_system == False,  # noqa: E712
                )
                .order_by(assistant_priority.asc(), Agent.created_at.asc(), Agent.id.asc())
            )
        ).scalars().all()
        to_restore = stopped[:slots]
        for a in to_restore:
            a.status = "idle"
        if to_restore:
            await db.commit()
            logger.info(
                f"[subscription_lifecycle] restored {len(to_restore)} agent(s) in tenant {tenant_id}"
            )
        return len(to_restore)


async def start_subscription_lifecycle_daemon() -> None:
    """Background loop: expire past-due subscriptions + daily-reset the credential pool hourly (3.6 兜底).

    Also runs a concurrent MiniMax Token Plan quota poller every 5 minutes that
    actively marks depleted subscription keys as quota_exceeded.
    """
    logger.info("[subscription_lifecycle] daemon started")

    async def _minimax_quota_loop():
        from app.services.llm.minimax_quota import poll_minimax_quota
        while True:
            try:
                await poll_minimax_quota()
            except Exception as e:
                logger.error(f"[subscription_lifecycle] minimax quota poll failed: {e}")
            await asyncio.sleep(300)  # 5 minutes

    # Run the quota poller concurrently alongside the main lifecycle loop.
    asyncio.create_task(_minimax_quota_loop())

    while True:
        try:
            await expire_subscriptions()
            await reset_daily_usage()  # idempotent within a day (date guard in reset_daily_usage)
        except Exception as e:
            logger.error(f"[subscription_lifecycle] job failed: {e}")
        await asyncio.sleep(LIFECYCLE_INTERVAL_SECONDS)
