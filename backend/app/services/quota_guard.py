"""Usage quota guard — check and enforce usage limits.

Subscription-aware (3.6/3.7):
- Limits come from tenant entitlements (active subscription) with fallback to
  tenant.default_* (backward compatible — no subscription = existing behavior).
- Usage is tracked at tenant level (tenant_usage table) with atomic UPDATE to
  prevent over-consumption under concurrency.
- LLM calls are tier-weighted (premium=5, standard=1, basic=1).
- Daily reset by tenant-local timezone (tenant.timezone).

Guards operate on tenant/product capabilities rather than concrete provider credentials.
"""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.database import async_session
from app.services.entitlements import get_tenant_entitlements
from app.services.modalities import canonicalize_modalities, canonicalize_modality


QUOTA_ACTIONS = {
    "insufficient_credits": "buy_credits",
    "max_agents": "upgrade",
    "model_tier": "upgrade",
    "model_modality": "upgrade",
    "generation_tier": "upgrade",
    "generation_modality": "upgrade",
    "tenant_llm": "upgrade",
    "conversation": "upgrade",
    "max_triggers": "upgrade",
    "agent_stopped": "manage_subscription",
    "no_route": "contact_admin",
    "media_payload": "reduce_attachments",
}


class QuotaExceeded(Exception):
    """Raised when a quota limit is reached."""

    def __init__(
        self,
        message: str,
        quota_type: str = "generic",
        *,
        action: str | None = None,
        details: dict | None = None,
    ):
        self.message = message
        self.quota_type = quota_type
        self.action = action or QUOTA_ACTIONS.get(quota_type, "upgrade")
        self.details = details or {}
        super().__init__(message)


class AgentExpired(Exception):
    """Raised when an agent has expired."""

    def __init__(self, agent_name: str = ""):
        self.message = f"Agent '{agent_name}' has expired and is no longer available."
        super().__init__(self.message)


# Tier weighting for LLM call quota (3.7): premium models consume more quota
TIER_WEIGHTS = {"premium": 5, "standard": 1, "basic": 1}

# Historical compatibility constant for token-usage displays only. Billable
# Credits are now wallet units granted by plans/topups and consumed from
# credit_transactions; they are not a hidden token budget.
CREDITS_PER_1K_TOKENS = 1.0


# ── Helpers ─────────────────────────────────────────────────────────


def subscription_action_message(message: str) -> str:
    """Append the client-side next step used by chat/tool quota errors."""
    return f"{message} 请前往「套餐详情」升级套餐或购买 Boost 额度后重试。"


def quota_error_payload(exc: QuotaExceeded) -> dict:
    """Return the single quota error envelope used by REST and WebSocket."""
    action = exc.action or QUOTA_ACTIONS.get(exc.quota_type, "upgrade")
    details = {
        "upgrade_url": "/account/subscription",
        **(exc.details or {}),
    }
    return {
        "error": "QUOTA_EXCEEDED",
        "quota_type": exc.quota_type,
        "action": action,
        "message": exc.message,
        "details": details,
    }


def _get_period_duration(period: str) -> timedelta:
    """Convert period string to timedelta."""
    mapping = {
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
        "monthly": timedelta(days=30),
    }
    return mapping.get(period, timedelta(days=36500))  # permanent = ~100 years


async def _today_in_tenant_tz(tenant_id: uuid.UUID) -> datetime.date:
    """Today's date in the tenant's timezone (3.7)."""
    from app.models.tenant import Tenant

    async with async_session() as db:
        result = await db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))
        tz_name = result.scalar_one_or_none() or "UTC"
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()


async def _tenant_llm_limit(tenant_id: uuid.UUID) -> int:
    """LLM calls/day limit: entitlements → fallback tenant default."""
    from app.models.tenant import Tenant

    ent = await get_tenant_entitlements(tenant_id)
    if ent:
        return ent.max_llm_calls_per_day
    async with async_session() as db:
        result = await db.execute(
            select(Tenant.default_max_llm_calls_per_day).where(Tenant.id == tenant_id)
        )
        return result.scalar_one_or_none() or 1000


async def _tenant_message_limit(tenant_id: uuid.UUID) -> tuple[int, str]:
    """Message limit + period: entitlements → fallback tenant default."""
    from app.models.tenant import Tenant

    ent = await get_tenant_entitlements(tenant_id)
    if ent:
        return ent.message_limit, ent.message_period
    async with async_session() as db:
        result = await db.execute(
            select(Tenant.default_message_limit, Tenant.default_message_period).where(
                Tenant.id == tenant_id
            )
        )
        row = result.one_or_none()
        return (row[0] if row else 50, row[1] if row else "permanent")


async def _tenant_max_agents(tenant_id: uuid.UUID) -> int:
    """Max agents: entitlements → fallback tenant default."""
    from app.models.tenant import Tenant

    ent = await get_tenant_entitlements(tenant_id)
    if ent:
        return ent.max_agents
    async with async_session() as db:
        result = await db.execute(select(Tenant.default_max_agents).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none() or 2


async def _get_model_tier(primary_model_id: uuid.UUID | None) -> str:
    """Get tier of the agent's primary model (for quota weighting)."""
    from app.models.llm import LLMModel

    if not primary_model_id:
        return "standard"
    async with async_session() as db:
        result = await db.execute(select(LLMModel.tier).where(LLMModel.id == primary_model_id))
        return result.scalar_one_or_none() or "standard"


async def _get_tenant_usage(tenant_id: uuid.UUID, today) -> tuple[int, int]:
    """Return (llm_calls_used, messages_used) for tenant today (0 if no row)."""
    from app.models.subscription import TenantUsage

    async with async_session() as db:
        result = await db.execute(
            select(TenantUsage.llm_calls_used, TenantUsage.messages_used).where(
                TenantUsage.tenant_id == tenant_id, TenantUsage.period_date == today
            )
        )
        row = result.one_or_none()
        return (row[0] if row else 0, row[1] if row else 0)


# ── Conversation quota (tenant-level shared, 3.7) ───────────────────


async def check_conversation_quota(user_id: uuid.UUID) -> None:
    """Pre-check: reject if tenant message quota already exceeded."""
    from app.models.user import User

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return
        # Admin users are exempt
        if user.role in ("platform_admin", "org_admin"):
            return
        if not user.tenant_id:
            return

    tenant_id = user.tenant_id
    today = await _today_in_tenant_tz(tenant_id)
    limit, _ = await _tenant_message_limit(tenant_id)
    _, used = await _get_tenant_usage(tenant_id, today)
    if used >= limit:
        raise QuotaExceeded(
            f"Message quota exceeded ({used}/{limit}).",
            quota_type="conversation",
        )


async def consume_conversation_quota(user_id: uuid.UUID) -> None:
    """Atomically consume one tenant message quota unit (3.7)."""
    from app.models.user import User

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user or user.role in ("platform_admin", "org_admin") or not user.tenant_id:
            return
        tenant_id = user.tenant_id

    today = await _today_in_tenant_tz(tenant_id)
    limit, _ = await _tenant_message_limit(tenant_id)
    # Atomic upsert + conditional increment (prevents over-consumption)
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO tenant_usage (tenant_id, period_date, messages_used, messages_limit,
                                          llm_calls_used, llm_calls_limit, tokens_used)
                VALUES (:tid, :d, 1, :lim, 0, 0, 0)
                ON CONFLICT (tenant_id, period_date) DO UPDATE
                SET messages_used = tenant_usage.messages_used + 1,
                    messages_limit = :lim
                WHERE tenant_usage.messages_used < :lim
                RETURNING messages_used
                """
            ),
            {"tid": tenant_id, "d": today, "lim": limit},
        )
        consumed = result.scalar_one_or_none()
        if consumed is None:
            raise QuotaExceeded(
                f"Message quota exceeded ({limit}/{limit}).",
                quota_type="conversation",
            )
        await db.commit()


async def increment_conversation_usage(user_id: uuid.UUID) -> None:
    """Backward-compatible alias for consuming tenant message usage."""
    await consume_conversation_quota(user_id)


# ── Agent expiry (unchanged) ────────────────────────────────────────


async def check_agent_expired(agent_id: uuid.UUID) -> None:
    """Check if agent has expired. If so, mark it and raise AgentExpired."""
    from app.models.agent import Agent

    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return

        if agent.is_expired:
            raise AgentExpired(agent.name)

        now = datetime.now(timezone.utc)
        if agent.expires_at and now >= agent.expires_at:
            agent.is_expired = True
            agent.status = "stopped"
            agent.heartbeat_enabled = False
            await db.commit()
            raise AgentExpired(agent.name)


async def get_agent_expiry_reply(agent_name: str) -> str:
    """Return a message for when an expired agent is contacted."""
    return (
        f"I'm sorry, but I ({agent_name}) am currently unavailable. "
        "My service period has ended. Please contact the platform administrator for assistance."
    )


# ── Agent LLM call quota (tenant-level shared, tier-weighted, 3.7) ──


def _tier_weight(tier: str | None) -> int:
    """Quota weight for a tier. Handles old model tiers and SaaS tiers."""
    if not tier:
        return 1
    tier = tier.lower()
    if tier in TIER_WEIGHTS:
        return TIER_WEIGHTS[tier]
    saas_map = {"lite": "basic", "pro": "standard", "ultra": "premium"}
    return TIER_WEIGHTS.get(saas_map.get(tier, tier), 1)


async def check_agent_llm_quota(agent_id: uuid.UUID, model_tier: str | None = None) -> None:
    """Pre-check: reject if tenant LLM quota would be exceeded (tier-weighted).

    Args:
        agent_id: The agent about to call LLM.
        model_tier: Optional tier override (e.g. SaaS tier from route resolution).
                    Falls back to the agent's primary_model_id tier if not given.
    """
    from app.models.agent import Agent

    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent or not agent.tenant_id:
            return
        tenant_id = agent.tenant_id

    if model_tier:
        tier = model_tier
    else:
        tier = await _get_model_tier(agent.primary_model_id)
    weight = _tier_weight(tier)
    today = await _today_in_tenant_tz(tenant_id)
    limit = await _tenant_llm_limit(tenant_id)
    used, _ = await _get_tenant_usage(tenant_id, today)
    if used + weight > limit:
        raise QuotaExceeded(
            f"Tenant LLM quota exceeded ({used}/{limit}, tier={tier} weight={weight}).",
            quota_type="tenant_llm",
        )


async def consume_agent_llm_quota(agent_id: uuid.UUID, model_tier: str | None = None) -> None:
    """Atomically consume tenant LLM usage, tier-weighted (3.7).

    Args:
        agent_id: The agent that just called LLM.
        model_tier: Optional tier override (e.g. SaaS tier from route resolution).
                    Falls back to the agent's primary_model_id tier if not given.

    Also updates agent.llm_calls_today for backward compatibility/display.
    """
    from app.models.agent import Agent

    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent or not agent.tenant_id:
            return
        tenant_id = agent.tenant_id

        if model_tier:
            tier = model_tier
        else:
            tier = await _get_model_tier(agent.primary_model_id)
        weight = _tier_weight(tier)
        today = await _today_in_tenant_tz(tenant_id)
        limit = await _tenant_llm_limit(tenant_id)

        # Atomic upsert + conditional tier-weighted increment
        result = await db.execute(
            text(
                """
                INSERT INTO tenant_usage (tenant_id, period_date, llm_calls_used, llm_calls_limit,
                                          messages_used, messages_limit, tokens_used)
                VALUES (:tid, :d, :w, :lim, 0, 0, 0)
                ON CONFLICT (tenant_id, period_date) DO UPDATE
                SET llm_calls_used = tenant_usage.llm_calls_used + :w,
                    llm_calls_limit = :lim
                WHERE tenant_usage.llm_calls_used + :w <= :lim
                RETURNING llm_calls_used
                """
            ),
            {"tid": tenant_id, "d": today, "w": weight, "lim": limit},
        )
        consumed = result.scalar_one_or_none()
        if consumed is None:
            raise QuotaExceeded(
                f"Tenant LLM quota exceeded ({limit}/{limit}, tier={tier} weight={weight}).",
                quota_type="tenant_llm",
            )

        # Backward-compat: also update agent.llm_calls_today (display/fallback)
        now = datetime.now(timezone.utc)
        if not agent.llm_calls_reset_at or now.date() > agent.llm_calls_reset_at.date():
            agent.llm_calls_today = weight
            agent.llm_calls_reset_at = now
        else:
            agent.llm_calls_today += weight
        await db.commit()


async def increment_agent_llm_usage(agent_id: uuid.UUID, model_tier: str | None = None) -> None:
    """Backward-compatible alias for consuming tenant LLM usage."""
    await consume_agent_llm_quota(agent_id, model_tier=model_tier)


# ── Inference entitlement (plan-gated modality/tier, 模块四 7.4) ──


async def check_plan_inference_entitlement(
    agent_id: uuid.UUID | None,
    *,
    modality: str | None,
    saas_tier: str | None,
) -> None:
    """Reject if the plan disallows an inference capability.

    Authorization is based only on the user-facing SaaS tier and modality.
    Concrete ``LLMModel`` rows and provider credentials belong to the shared
    platform routing pool and are intentionally not authorization objects.
    No agent_id / no tenant / no active subscription means no added
    restriction (backward-compatible fallback to tenant defaults).

    Raises QuotaExceeded(quota_type="model_modality" | "model_tier") on denial.
    """
    from app.models.agent import Agent

    if not agent_id:
        return

    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id, Agent.status).where(Agent.id == agent_id))
        row = result.one_or_none()
    if not row:
        return
    tenant_id, status = row
    # Stopped agents (subscription downgrade/expiry) must not call LLM (3.6).
    if status == "stopped":
        raise QuotaExceeded(
            subscription_action_message("该 Agent 已停止（订阅降级或套餐数量超限），无法继续执行。"),
            quota_type="agent_stopped",
        )
    if not tenant_id:
        return

    ent = await get_tenant_entitlements(tenant_id)
    if not ent:
        return  # no active subscription → fallback, no model restriction

    modality = canonicalize_modality(modality)
    allowed_modalities = canonicalize_modalities(ent.allowed_modalities)
    if allowed_modalities and modality and modality not in allowed_modalities:
        raise QuotaExceeded(
            f"当前订阅套餐不允许使用该模型类型（modality={modality}），请升级套餐或联系管理员。",
            quota_type="model_modality",
        )

    tier = (saas_tier or "").strip().lower() or None
    allowed_tiers = {
        str(value).strip().lower()
        for value in (ent.allowed_tiers or [])
        if str(value).strip()
    }
    if allowed_tiers and tier and tier not in allowed_tiers:
        raise QuotaExceeded(
            f"当前订阅套餐不允许使用该模型等级（tier={tier}），请升级套餐或联系管理员。",
            quota_type="model_tier",
        )


async def check_plan_generation_entitlement(
    agent_id: uuid.UUID | None,
    *,
    modality: str | None,
    saas_tier: str | None,
) -> None:
    """Reject media generation outside the plan's product capability matrix.

    This deliberately does not inspect an ``LLMModel`` row. Provider models
    and pooled credentials are routing infrastructure; authorization remains
    attached to the SaaS generation capability and Lite / Pro / Ultra tier.
    """
    from app.models.agent import Agent

    if not agent_id:
        return
    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id, Agent.status).where(Agent.id == agent_id))
        row = result.one_or_none()
    if not row:
        return
    tenant_id, status = row
    if status == "stopped":
        raise QuotaExceeded(
            subscription_action_message("该 Agent 已停止（订阅降级或套餐数量超限），无法继续执行。"),
            quota_type="agent_stopped",
        )
    if not tenant_id:
        return

    ent = await get_tenant_entitlements(tenant_id)
    if not ent:
        return

    canonical = canonicalize_modality(modality)
    allowed_modalities = set(canonicalize_modalities(ent.generation_modalities))
    if not canonical or canonical not in allowed_modalities:
        raise QuotaExceeded(
            f"当前订阅套餐不允许使用该生成能力（modality={canonical or modality}），请升级套餐或联系管理员。",
            quota_type="generation_modality",
        )

    tier = (saas_tier or "").strip().lower() or None
    allowed_tiers = {
        str(value).strip().lower()
        for value in (ent.generation_tiers or [])
        if str(value).strip()
    }
    if not tier or tier not in allowed_tiers:
        raise QuotaExceeded(
            f"当前订阅套餐不允许使用该生成等级（tier={tier or '(unset)'}），请升级套餐或联系管理员。",
            quota_type="generation_tier",
        )


# ── Agent creation quota (entitlements-driven, 3.6) ─────────────────


async def _count_active_tenant_agents(tenant_id: uuid.UUID, db=None) -> int:
    """Count active, non-expired user Agents that consume max_agents quota.

    System agents such as OKR Agent are platform infrastructure. They must not
    consume a tenant's purchased Agent seats.
    """
    from app.models.agent import Agent

    async def _count(session) -> int:
        result = await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.status.notin_(("stopped", "error")),
                Agent.is_expired == False,  # noqa: E712
                Agent.is_system == False,  # noqa: E712
            )
        )
        return len(result.scalars().all())

    if db is not None:
        return await _count(db)

    async with async_session() as session:
        return await _count(session)


async def check_agent_creation_quota(
    user_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    db=None,
) -> None:
    """Check if user can create more agents (tenant max_agents from entitlements)."""
    from app.models.user import User

    if tenant_id is None:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user or not user.tenant_id:
                return
            tenant_id = user.tenant_id
            current_count = await _count_active_tenant_agents(tenant_id, session)
    else:
        current_count = await _count_active_tenant_agents(tenant_id, db)

    max_agents = await _tenant_max_agents(tenant_id)
    if current_count >= max_agents:
        raise QuotaExceeded(
            f"Agent 创建数量已达当前套餐上限（{current_count}/{max_agents}）。请前往「套餐详情」升级套餐后继续。",
            quota_type="max_agents",
        )


# ── Heartbeat floor enforcement (unchanged) ─────────────────────────


async def enforce_heartbeat_floor(tenant_id: uuid.UUID, floor: int | None = None, db=None) -> int:
    """Enforce heartbeat floor on all agents in the tenant.

    Args:
        tenant_id: The tenant to enforce for.
        floor: The minimum interval in minutes. If None, reads from tenant.
        db: Optional existing database session to reuse (avoids session isolation bugs).

    Returns number of agents adjusted.
    """
    from app.models.agent import Agent
    from app.models.tenant import Tenant

    async def _enforce(session, floor_val):
        # If floor not provided, read from tenant
        if floor_val is None:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            if not tenant:
                return 0
            floor_val = tenant.min_heartbeat_interval_minutes

        # Find agents with interval below floor
        agents_result = await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.heartbeat_interval_minutes < floor_val,
            )
        )
        agents = agents_result.scalars().all()
        for agent in agents:
            agent.heartbeat_interval_minutes = floor_val

        if agents:
            await session.commit()
        return len(agents)

    if db is not None:
        return await _enforce(db, floor)
    else:
        async with async_session() as new_db:
            return await _enforce(new_db, floor)


# ── Tenant token / credits enforcement (3.6/3.7) ───────────────────


async def record_tenant_tokens(tenant_id: uuid.UUID | None, tokens: int) -> None:
    """Atomically add tokens to tenant_usage.tokens_used for today.

    Called after each LLM invocation with the actual total_tokens consumed.
    No-op for null tenant (platform agents) or zero tokens.
    """
    if not tenant_id or tokens <= 0:
        return
    today = await _today_in_tenant_tz(tenant_id)
    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO tenant_usage (tenant_id, period_date, tokens_used,
                                          llm_calls_used, llm_calls_limit,
                                          messages_used, messages_limit)
                VALUES (:tid, :d, :tok, 0, 0, 0, 0)
                ON CONFLICT (tenant_id, period_date) DO UPDATE
                SET tokens_used = tenant_usage.tokens_used + :tok
                """
            ),
            {"tid": tenant_id, "d": today, "tok": tokens},
        )
        await db.commit()


async def check_tenant_token_credits(tenant_id: uuid.UUID | None) -> None:
    """Compatibility hook for the removed plan-credit token budget.

    Plan.credits_per_period is a periodic grant into the tenant's spendable
    credit balance. Runtime enforcement is handled by check_credit_balance() and
    charge_credits(); this hook intentionally does not interpret plan credits as
    a daily token budget.
    """
    return


async def check_trigger_quota(tenant_id: uuid.UUID | None) -> None:
    """Pre-check: reject if tenant has reached max_triggers (active triggers).

    Counts non-expired triggers owned by agents in the tenant. No subscription
    or max_triggers=0 means unlimited.
    """
    if not tenant_id:
        return
    ent = await get_tenant_entitlements(tenant_id)
    if not ent or ent.max_triggers <= 0:
        return
    from app.models.agent import Agent
    from app.models.trigger import AgentTrigger

    async with async_session() as db:
        result = await db.execute(
            select(func.count()).select_from(AgentTrigger).join(
                Agent,
                AgentTrigger.agent_id == Agent.id,
            ).where(
                Agent.tenant_id == tenant_id,
                AgentTrigger.is_enabled.is_(True),
                AgentTrigger.is_system.is_(False),
            )
        )
        current = result.scalar_one()
    if current >= ent.max_triggers:
        raise QuotaExceeded(
            f"Trigger 数量已达上限（{current}/{ent.max_triggers}）。",
            quota_type="max_triggers",
        )
