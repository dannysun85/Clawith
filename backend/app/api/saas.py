"""SaaS admin APIs (platform_admin only).

Provides platform-level management for subscriptions, model routes, billing rules,
credit packs, and tenant oversight.
"""

import uuid
import csv
from dataclasses import asdict
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_saas_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.llm import LLMCredential, LLMModel
from app.models.subscription import (
    BillingRule,
    CreditBalance,
    CreditPack,
    CreditTransaction,
    ModelRoute,
    PaymentOrder,
    Plan,
    Subscription,
)
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import User
from app.schemas.saas import (
    AssignSubscriptionIn,
    BillingRuleCreateIn,
    BillingRuleOut,
    BillingRuleUpdateIn,
    CreditPackCreateIn,
    CreditPackUpdateIn,
    GrantCreditsIn,
    InitializeFreeSubscriptionsIn,
    InitializeFreeSubscriptionsOut,
    MediaRouteOut,
    MediaRouteUpdateIn,
    ModelRouteCreateIn,
    ModelRouteOut,
    ModelRouteUpdateIn,
    SaasTenantOut,
)
from app.schemas.subscription import CreditPackOut, CreditTransactionOut, PaymentOrderOut
from app.services.billing_events import finalize_order_in_session
from app.services.billing_reconciliation import (
    check_credit_ledger_integrity,
    expire_stale_credit_reservations,
    reconcile_pending_payment_orders,
)
from app.services.credit_service import grant_credits_in_session
from app.services.entitlements import get_active_subscription, get_tenant_entitlements
from app.services.agent_plan_selection import reconcile_tenant_agent_plan_selections
from app.services.subscription_lifecycle import (
    enforce_agent_limit,
    ensure_free_subscription_for_tenant,
    restore_stopped_agents,
)
from app.services.minimax_media_profiles import (
    MINIMAX_MEDIA_MODELS,
    MINIMAX_MEDIA_PROFILE_FIELDS,
    MINIMAX_MEDIA_TOOL_NAMES,
    MINIMAX_VIDEO_ALLOWED_QUALITY,
    minimax_media_override_snapshot,
    resolve_minimax_media_profile,
)
from app.services.modalities import canonicalize_modalities, canonicalize_modality
from app.services.provider_pricing import (
    minimax_image_credits,
    minimax_music_credits,
    minimax_tts_credits,
    minimax_video_credits,
)

router = APIRouter(prefix="/saas", tags=["saas"])

# All SaaS endpoints require the configured SaaS owner account.
get_platform_admin = get_saas_admin


def _admin_audit(
    *,
    action: str,
    actor: User,
    details: dict,
) -> AuditLog:
    return AuditLog(user_id=actor.id, agent_id=None, action=action, details=details)


def _jsonable(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _snapshot(obj: object, fields: list[str]) -> dict:
    out: dict[str, object] = {}
    for field in fields:
        value = getattr(obj, field, None)
        out[field] = _jsonable(value)
    return out


def _report_payload(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return _jsonable(value)


def _require_bulk_confirm(item_count: int, confirmed: bool, operation: str) -> None:
    if item_count > 1 and not confirmed:
        raise HTTPException(
            status_code=400,
            detail=f"Bulk {operation} requires confirmation.",
        )


# ── Model Routes ────────────────────────────────────────────────────


def _validate_model_route(model: LLMModel, modality: str) -> str:
    canonical = canonicalize_modality(modality) or ""
    if canonical in {"audio", "music"}:
        raise HTTPException(
            status_code=400,
            detail=f"{canonical} generation must be configured in SaaS media routes, not LLM model routes.",
        )
    model_modalities = set(canonicalize_modalities(model.modalities or [model.modality]))
    if model.supports_vision:
        model_modalities.add("image")
    if canonical == "multimodal":
        compatible = "multimodal" in model_modalities
    else:
        compatible = canonical in model_modalities or "multimodal" in model_modalities
    if not compatible:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model.label}' does not support the '{canonical}' route modality.",
        )
    return canonical


@router.get("/model-routes", response_model=list[ModelRouteOut])
async def list_model_routes(
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all model routes."""
    result = await db.execute(select(ModelRoute).order_by(ModelRoute.saas_tier, ModelRoute.modality, ModelRoute.priority.desc()))
    return result.scalars().all()


@router.post("/model-routes", response_model=ModelRouteOut, status_code=status.HTTP_201_CREATED)
async def create_model_route(
    data: ModelRouteCreateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a model route."""
    model = await db.get(LLMModel, data.llm_model_id)
    if not model:
        raise HTTPException(status_code=404, detail="LLM model not found")
    data.modality = _validate_model_route(model, data.modality)
    if data.fallback_route_id:
        fallback = await db.get(ModelRoute, data.fallback_route_id)
        if not fallback:
            raise HTTPException(status_code=404, detail="Fallback route not found")

    route = ModelRoute(**data.model_dump())
    db.add(route)
    db.add(_admin_audit(
        action="saas_model_route_create",
        actor=current_user,
        details={"after": _jsonable(data.model_dump())},
    ))
    await db.commit()
    await db.refresh(route)
    return route


@router.patch("/model-routes/{route_id}", response_model=ModelRouteOut)
async def update_model_route(
    route_id: uuid.UUID,
    data: ModelRouteUpdateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a model route."""
    route = await db.get(ModelRoute, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    before = _snapshot(route, ["saas_tier", "modality", "llm_model_id", "priority", "fallback_route_id", "enabled"])

    update = data.model_dump(exclude_unset=True)
    if "llm_model_id" in update:
        model = await db.get(LLMModel, update["llm_model_id"])
        if not model:
            raise HTTPException(status_code=404, detail="LLM model not found")
    else:
        model = await db.get(LLMModel, route.llm_model_id)
    if model and ("modality" in update or "llm_model_id" in update):
        update["modality"] = _validate_model_route(
            model,
            update.get("modality", route.modality),
        )
    if "fallback_route_id" in update and update["fallback_route_id"]:
        fallback = await db.get(ModelRoute, update["fallback_route_id"])
        if not fallback:
            raise HTTPException(status_code=404, detail="Fallback route not found")

    for k, v in update.items():
        setattr(route, k, v)
    db.add(_admin_audit(
        action="saas_model_route_update",
        actor=current_user,
        details={
            "route_id": str(route_id),
            "before": before,
            "after": _snapshot(route, ["saas_tier", "modality", "llm_model_id", "priority", "fallback_route_id", "enabled"]),
        },
    ))
    await db.commit()
    await db.refresh(route)
    return route


# ── MiniMax Media Routes ───────────────────────────────────────────


async def _minimax_pool_modalities(db: AsyncSession) -> set[str]:
    from app.services.llm.load_balancer import credential_modality_is_blocked

    result = await db.execute(
        select(LLMCredential).where(
            LLMCredential.provider == "minimax",
            LLMCredential.tenant_id.is_(None),
            LLMCredential.enabled == True,  # noqa: E712
            LLMCredential.status == "healthy",
            or_(
                LLMCredential.daily_quota.is_(None),
                LLMCredential.used_today < LLMCredential.daily_quota,
            ),
        )
    )
    pool_modalities: set[str] = set()
    for credential in result.scalars().all():
        capabilities = canonicalize_modalities(credential.capabilities)
        if not capabilities or "multimodal" in capabilities:
            supported = set(MINIMAX_MEDIA_TOOL_NAMES)
        else:
            supported = set(capabilities)
        pool_modalities.update(
            modality
            for modality in supported
            if modality in MINIMAX_MEDIA_TOOL_NAMES
            and not credential_modality_is_blocked(credential, modality)
        )
    return pool_modalities


def _media_route_billing(profile) -> tuple[int | None, str]:
    if profile.modality == "image":
        return minimax_image_credits(profile.model, images=1), "张"
    if profile.modality == "audio":
        return minimax_tts_credits(profile.model, characters=1000), "千字符"
    if profile.modality == "music":
        return minimax_music_credits(profile.model), "首"
    if profile.modality == "video":
        try:
            return minimax_video_credits(
                profile.model,
                duration=int(profile.duration or 6),
                resolution=str(profile.resolution or "768P"),
            ), "条"
        except ValueError:
            return None, "条"
    return None, "次"


def _media_route_out(
    *,
    modality: str,
    tier: str,
    tool: Tool | None,
    pool_modalities: set[str],
) -> MediaRouteOut:
    config = dict(tool.config or {}) if tool else {}
    profile = resolve_minimax_media_profile(modality, tier, config)
    fields = MINIMAX_MEDIA_PROFILE_FIELDS[modality]
    settings = {
        field: getattr(profile, field)
        for field in fields
        if field not in {"model", "enabled"} and getattr(profile, field) is not None
    }
    overridden = bool(minimax_media_override_snapshot(modality, tier, config))
    pool_available = modality in pool_modalities
    tool_enabled = bool(tool and tool.enabled)
    estimated_credits, billing_unit = _media_route_billing(profile)
    return MediaRouteOut(
        modality=modality,
        tier=tier,
        provider="minimax",
        tool_name=MINIMAX_MEDIA_TOOL_NAMES[modality],
        model=profile.model,
        settings=settings,
        valid_models=list(MINIMAX_MEDIA_MODELS[modality]),
        enabled=profile.enabled,
        tool_enabled=tool_enabled,
        pool_available=pool_available,
        available=profile.enabled and tool_enabled and pool_available,
        source="override" if overridden else "default",
        billing_mode="provider_dynamic",
        estimated_credits=estimated_credits,
        billing_unit=billing_unit,
    )


async def _media_tool_map(db: AsyncSession) -> dict[str, Tool]:
    result = await db.execute(
        select(Tool).where(
            Tool.name.in_(MINIMAX_MEDIA_TOOL_NAMES.values()),
            Tool.tenant_id.is_(None),
        )
    )
    return {tool.name: tool for tool in result.scalars().all()}


@router.get("/media-routes", response_model=list[MediaRouteOut])
async def list_media_routes(
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List the 4 x 3 effective media routing matrix without credentials."""
    tools = await _media_tool_map(db)
    pool_modalities = await _minimax_pool_modalities(db)
    return [
        _media_route_out(
            modality=modality,
            tier=tier,
            tool=tools.get(tool_name),
            pool_modalities=pool_modalities,
        )
        for modality, tool_name in MINIMAX_MEDIA_TOOL_NAMES.items()
        for tier in ("lite", "pro", "ultra")
    ]


def _validate_media_route_profile(modality: str, tier: str, profile) -> None:
    if profile.model not in MINIMAX_MEDIA_MODELS[modality]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported MiniMax {modality} model: {profile.model}",
        )
    if modality == "audio":
        if profile.sample_rate not in {16000, 24000, 32000, 44100}:
            raise HTTPException(status_code=400, detail="Unsupported speech sample_rate")
        if profile.bitrate not in {32000, 64000, 128000, 256000}:
            raise HTTPException(status_code=400, detail="Unsupported speech bitrate")
    if modality == "music":
        if profile.sample_rate not in {32000, 44100}:
            raise HTTPException(status_code=400, detail="Unsupported music sample_rate")
        if profile.bitrate not in {128000, 256000}:
            raise HTTPException(status_code=400, detail="Unsupported music bitrate")
    if modality == "video" and (
        int(profile.duration or 0), str(profile.resolution or "").upper()
    ) not in MINIMAX_VIDEO_ALLOWED_QUALITY[tier]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported {tier} video duration/resolution combination",
        )


@router.patch("/media-routes/{modality}/{tier}", response_model=MediaRouteOut)
async def update_media_route(
    modality: str,
    tier: str,
    data: MediaRouteUpdateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update one platform media route; credentials remain in the shared pool."""
    canonical = canonicalize_modality(modality) or ""
    normalized_tier = tier.strip().lower()
    if canonical not in MINIMAX_MEDIA_TOOL_NAMES or normalized_tier not in {"lite", "pro", "ultra"}:
        raise HTTPException(status_code=404, detail="Media route not found")

    result = await db.execute(
        select(Tool).where(
            Tool.name == MINIMAX_MEDIA_TOOL_NAMES[canonical],
            Tool.tenant_id.is_(None),
        )
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=503, detail="Media tool is not installed")

    before = minimax_media_override_snapshot(canonical, normalized_tier, tool.config)
    config = dict(tool.config or {})
    prefix = f"{normalized_tier}_"
    if data.reset_to_default:
        for field in MINIMAX_MEDIA_PROFILE_FIELDS[canonical]:
            config.pop(prefix + field, None)

    updates = data.model_dump(exclude_unset=True, exclude={"reset_to_default"})
    allowed_fields = set(MINIMAX_MEDIA_PROFILE_FIELDS[canonical])
    unsupported = set(updates) - allowed_fields
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported settings for {canonical}: {', '.join(sorted(unsupported))}",
        )
    for field, value in updates.items():
        config[prefix + field] = value

    profile = resolve_minimax_media_profile(canonical, normalized_tier, config)
    _validate_media_route_profile(canonical, normalized_tier, profile)
    tool.config = config
    after = minimax_media_override_snapshot(canonical, normalized_tier, config)
    db.add(_admin_audit(
        action="saas_media_route_update",
        actor=current_user,
        details={
            "modality": canonical,
            "tier": normalized_tier,
            "before": before,
            "after": after,
        },
    ))
    await db.commit()
    pool_modalities = await _minimax_pool_modalities(db)
    return _media_route_out(
        modality=canonical,
        tier=normalized_tier,
        tool=tool,
        pool_modalities=pool_modalities,
    )


@router.delete("/model-routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_route(
    route_id: uuid.UUID,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a model route."""
    route = await db.get(ModelRoute, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    before = _snapshot(route, ["saas_tier", "modality", "llm_model_id", "priority", "fallback_route_id", "enabled"])
    await db.delete(route)
    db.add(_admin_audit(
        action="saas_model_route_delete",
        actor=current_user,
        details={"route_id": str(route_id), "before": before},
    ))
    await db.commit()


# ── Billing Rules ───────────────────────────────────────────────────


@router.get("/billing-rules", response_model=list[BillingRuleOut])
async def list_billing_rules(
    action: str | None = None,
    modality: str | None = None,
    tier: str | None = None,
    page: int = 1,
    limit: int = 100,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all billing rules."""
    safe_page = max(page, 1)
    safe_limit = min(max(limit, 1), 500)
    stmt = select(BillingRule)
    if action:
        stmt = stmt.where(BillingRule.action == action)
    if modality:
        stmt = stmt.where(BillingRule.modality == modality)
    if tier:
        stmt = stmt.where(BillingRule.tier == tier)
    result = await db.execute(
        stmt.order_by(BillingRule.action, BillingRule.priority.desc())
        .offset((safe_page - 1) * safe_limit)
        .limit(safe_limit)
    )
    return result.scalars().all()


@router.post("/billing-rules", response_model=BillingRuleOut, status_code=status.HTTP_201_CREATED)
async def create_billing_rule(
    data: BillingRuleCreateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a billing rule."""
    rule = BillingRule(**data.model_dump())
    db.add(rule)
    db.add(_admin_audit(
        action="saas_billing_rule_create",
        actor=current_user,
        details={
            "after": data.model_dump(),
        },
    ))
    await db.commit()
    await db.refresh(rule)
    return rule


@router.patch("/billing-rules/{rule_id}", response_model=BillingRuleOut)
async def update_billing_rule(
    rule_id: uuid.UUID,
    data: BillingRuleUpdateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a billing rule."""
    rule = await db.get(BillingRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    before = _snapshot(rule, ["action", "modality", "tier", "unit", "credit_cost", "enabled", "priority"])
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(rule, k, v)
    db.add(_admin_audit(
        action="saas_billing_rule_update",
        actor=current_user,
        details={
            "rule_id": str(rule_id),
            "before": before,
            "after": _snapshot(rule, ["action", "modality", "tier", "unit", "credit_cost", "enabled", "priority"]),
        },
    ))
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/billing-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_billing_rule(
    rule_id: uuid.UUID,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a billing rule."""
    rule = await db.get(BillingRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    before = _snapshot(rule, ["action", "modality", "tier", "unit", "credit_cost", "enabled", "priority"])
    await db.delete(rule)
    db.add(_admin_audit(
        action="saas_billing_rule_delete",
        actor=current_user,
        details={"rule_id": str(rule_id), "before": before},
    ))
    await db.commit()


# ── Credit Packs ────────────────────────────────────────────────────


@router.get("/credit-packs", response_model=list[CreditPackOut])
async def list_credit_packs_admin(
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all credit packs (including inactive)."""
    result = await db.execute(select(CreditPack).order_by(CreditPack.sort_order))
    return result.scalars().all()


@router.post("/credit-packs", response_model=CreditPackOut, status_code=status.HTTP_201_CREATED)
async def create_credit_pack(
    data: CreditPackCreateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a credit pack."""
    existing = await db.execute(select(CreditPack).where(CreditPack.code == data.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Credit pack code '{data.code}' already exists")

    pack = CreditPack(**data.model_dump())
    db.add(pack)
    db.add(_admin_audit(
        action="saas_credit_pack_create",
        actor=current_user,
        details={"after": _jsonable(data.model_dump())},
    ))
    await db.commit()
    await db.refresh(pack)
    return pack


@router.patch("/credit-packs/{pack_id}", response_model=CreditPackOut)
async def update_credit_pack(
    pack_id: uuid.UUID,
    data: CreditPackUpdateIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a credit pack."""
    pack = await db.get(CreditPack, pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Credit pack not found")
    before = _snapshot(pack, ["code", "name", "credits", "price_cents", "currency", "applicable_plan_ids", "is_active", "sort_order"])
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(pack, k, v)
    db.add(_admin_audit(
        action="saas_credit_pack_update",
        actor=current_user,
        details={
            "pack_id": str(pack_id),
            "before": before,
            "after": _snapshot(pack, ["code", "name", "credits", "price_cents", "currency", "applicable_plan_ids", "is_active", "sort_order"]),
        },
    ))
    await db.commit()
    await db.refresh(pack)
    return pack


@router.delete("/credit-packs/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credit_pack(
    pack_id: uuid.UUID,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a credit pack."""
    pack = await db.get(CreditPack, pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Credit pack not found")
    before = _snapshot(pack, ["code", "name", "credits", "price_cents", "currency", "applicable_plan_ids", "is_active", "sort_order"])
    await db.delete(pack)
    db.add(_admin_audit(
        action="saas_credit_pack_delete",
        actor=current_user,
        details={"pack_id": str(pack_id), "before": before},
    ))
    await db.commit()


# ── Tenant Subscriptions ────────────────────────────────────────────


@router.get("/tenants", response_model=list[SaasTenantOut])
async def list_tenants(
    search: str | None = None,
    page: int = 1,
    limit: int = 100,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List tenants with subscription summary."""
    safe_page = max(page, 1)
    safe_limit = min(max(limit, 1), 500)
    stmt = select(Tenant)
    if search:
        stmt = stmt.where(Tenant.name.ilike(f"%{search}%"))
    result = await db.execute(
        stmt.order_by(Tenant.created_at.desc())
        .offset((safe_page - 1) * safe_limit)
        .limit(safe_limit)
    )
    tenants = result.scalars().all()
    out = []
    for tenant in tenants:
        sub = await get_active_subscription(tenant.id)
        plan = await db.get(Plan, sub.plan_id) if sub else None
        cb = await db.get(CreditBalance, tenant.id)
        ent = await get_tenant_entitlements(tenant.id)
        from app.services.quota_guard import _count_active_tenant_agents

        seats_total = ent.max_agents if ent else (plan.max_agents if plan else (sub.seats if sub else 1))
        seats_used = await _count_active_tenant_agents(tenant.id, db)

        out.append(SaasTenantOut(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            plan_code=plan.code if plan else None,
            subscription_status=sub.status if sub else None,
            period_end=sub.period_end if sub else None,
            seats_total=seats_total,
            seats_used=seats_used,
            credits_balance=cb.balance if cb else 0,
        ))
    return out


@router.post("/subscriptions/assign", status_code=status.HTTP_200_OK)
async def assign_subscriptions(
    data: AssignSubscriptionIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Assign a plan to multiple tenants (bulk, no payment)."""
    _require_bulk_confirm(len(data.tenant_ids), data.confirm, "subscription assignment")
    plan = await db.get(Plan, data.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(status_code=404, detail="Plan not found")

    from datetime import datetime, timezone, timedelta

    updated = 0
    for tenant_id in data.tenant_ids:
        existing = await db.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant_id,
                Subscription.status.in_(("active", "trialing")),
            )
        )
        existing_sub = existing.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=data.period_days) if data.period_days else None

        if existing_sub:
            plan_changed = existing_sub.plan_id != data.plan_id
            existing_sub.plan_id = data.plan_id
            existing_sub.status = "active"
            existing_sub.period_end = period_end
            existing_sub.cancel_at_period_end = False
            sub = existing_sub
        else:
            plan_changed = True
            sub = Subscription(
                tenant_id=tenant_id,
                plan_id=data.plan_id,
                status="active",
                period_start=now,
                period_end=period_end,
            )
            db.add(sub)
        await db.flush()
        if plan_changed and plan.credits_per_period > 0:
            await grant_credits_in_session(
                db,
                tenant_id=tenant_id,
                amount=plan.credits_per_period,
                reason="subscribe",
                granted_by=current_user.id,
                ref_type="subscription",
                ref_id=sub.id,
            )
        updated += 1

    db.add(_admin_audit(
        action="saas_subscription_assign",
        actor=current_user,
        details={
            "tenant_ids": [str(item) for item in data.tenant_ids],
            "plan_id": str(data.plan_id),
            "period_days": data.period_days,
            "audit_reason": data.audit_reason,
        },
    ))

    await db.commit()

    # Reconcile agent limits for each affected tenant
    for tenant_id in data.tenant_ids:
        await reconcile_tenant_agent_plan_selections(tenant_id)
        await restore_stopped_agents(tenant_id)
        await enforce_agent_limit(tenant_id)

    return {"updated": updated}


@router.post("/subscriptions/initialize-free", response_model=InitializeFreeSubscriptionsOut)
async def initialize_free_subscriptions(
    data: InitializeFreeSubscriptionsIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create Free subscriptions for existing tenants that do not have one.

    This is the production backfill path after enabling SaaS billing on an
    existing Astra deployment. It intentionally skips tenants with active,
    trialing, canceled, or past_due subscriptions so paid customers are never
    downgraded by a bulk Free initialization.
    """
    free_plan_result = await db.execute(
        select(Plan).where(
            Plan.code == "free",
            Plan.is_active == True,  # noqa: E712
        )
    )
    if not free_plan_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Free plan not found")

    tenant_stmt = select(Tenant)
    if data.tenant_ids:
        tenant_stmt = tenant_stmt.where(Tenant.id.in_(data.tenant_ids))
    if not data.include_inactive:
        tenant_stmt = tenant_stmt.where(Tenant.is_active == True)  # noqa: E712
    tenants_result = await db.execute(tenant_stmt.order_by(Tenant.created_at.asc()))
    tenants = list(tenants_result.scalars().all())
    _require_bulk_confirm(len(tenants), data.confirm, "Free subscription initialization")

    created_tenant_ids: list[uuid.UUID] = []
    skipped_existing = 0
    for tenant in tenants:
        existing_result = await db.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant.id,
                Subscription.status.in_(("active", "trialing", "canceled", "past_due")),
            )
            .limit(1)
        )
        if existing_result.scalar_one_or_none():
            skipped_existing += 1
            continue

        subscription = await ensure_free_subscription_for_tenant(
            db,
            tenant.id,
            granted_by=current_user.id,
        )
        if subscription:
            created_tenant_ids.append(tenant.id)

    db.add(_admin_audit(
        action="saas_subscription_initialize_free",
        actor=current_user,
        details={
            "tenant_ids": [str(item) for item in created_tenant_ids],
            "total_candidates": len(tenants),
            "created": len(created_tenant_ids),
            "skipped_existing": skipped_existing,
            "include_inactive": data.include_inactive,
            "audit_reason": data.audit_reason,
        },
    ))
    await db.commit()

    for tenant_id in created_tenant_ids:
        await reconcile_tenant_agent_plan_selections(tenant_id)
        await restore_stopped_agents(tenant_id)
        await enforce_agent_limit(tenant_id)

    return InitializeFreeSubscriptionsOut(
        total_candidates=len(tenants),
        created=len(created_tenant_ids),
        skipped_existing=skipped_existing,
        tenant_ids=created_tenant_ids,
    )


@router.post("/credits/grant", status_code=status.HTTP_200_OK)
async def grant_credits_bulk(
    data: GrantCreditsIn,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Grant credits to multiple tenants."""
    _require_bulk_confirm(len(data.tenant_ids), data.confirm, "credit grant")
    for tenant_id in data.tenant_ids:
        await grant_credits_in_session(
            db,
            tenant_id=tenant_id,
            amount=data.amount,
            reason=data.reason,
            granted_by=current_user.id,
        )
    db.add(_admin_audit(
        action="saas_credits_grant",
        actor=current_user,
        details={
            "tenant_ids": [str(item) for item in data.tenant_ids],
            "amount": data.amount,
            "reason": data.reason,
            "audit_reason": data.audit_reason,
        },
    ))
    await db.commit()
    return {"granted_to": len(data.tenant_ids), "amount": data.amount}


# ── Orders ──────────────────────────────────────────────────────────


@router.get("/orders", response_model=list[PaymentOrderOut])
async def list_orders(
    tenant_id: uuid.UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    order_type: str | None = Query(default=None, alias="type"),
    page: int = 1,
    limit: int = 100,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all payment orders."""
    result = await db.execute(
        _orders_query(tenant_id=tenant_id, status_filter=status_filter, order_type=order_type)
        .order_by(PaymentOrder.created_at.desc())
        .offset((max(page, 1) - 1) * min(max(limit, 1), 500))
        .limit(min(max(limit, 1), 500))
    )
    return result.scalars().all()


def _orders_query(
    *,
    tenant_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    order_type: str | None = None,
):
    stmt = select(PaymentOrder)
    if tenant_id:
        stmt = stmt.where(PaymentOrder.tenant_id == tenant_id)
    if status_filter:
        stmt = stmt.where(PaymentOrder.status == status_filter)
    if order_type:
        stmt = stmt.where(PaymentOrder.type == order_type)
    return stmt


@router.get("/credit-transactions", response_model=list[CreditTransactionOut])
async def list_credit_transactions_admin(
    tenant_id: uuid.UUID | None = None,
    reason: str | None = None,
    action: str | None = None,
    page: int = 1,
    limit: int = 100,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List credit transactions across tenants for audit and reconciliation."""
    result = await db.execute(
        _credit_transactions_query(tenant_id=tenant_id, reason=reason, action=action)
        .order_by(CreditTransaction.created_at.desc())
        .offset((max(page, 1) - 1) * min(max(limit, 1), 500))
        .limit(min(max(limit, 1), 500))
    )
    return result.scalars().all()


def _credit_transactions_query(
    *,
    tenant_id: uuid.UUID | None = None,
    reason: str | None = None,
    action: str | None = None,
):
    stmt = select(CreditTransaction)
    if tenant_id:
        stmt = stmt.where(CreditTransaction.tenant_id == tenant_id)
    if reason:
        stmt = stmt.where(CreditTransaction.reason == reason)
    if action:
        stmt = stmt.where(CreditTransaction.action == action)
    return stmt


@router.get("/orders/export.csv")
async def export_orders_csv(
    tenant_id: uuid.UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    order_type: str | None = Query(default=None, alias="type"),
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export orders for invoice preparation."""
    result = await db.execute(
        _orders_query(tenant_id=tenant_id, status_filter=status_filter, order_type=order_type)
        .order_by(PaymentOrder.created_at.desc())
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "tenant_id", "type", "plan_id", "credits", "amount_cents", "currency", "provider", "status", "created_at", "paid_at"])
    for order in result.scalars().all():
        writer.writerow([
            order.id,
            order.tenant_id,
            order.type,
            order.plan_id or "",
            order.credits or "",
            order.amount_cents,
            order.currency,
            order.provider,
            order.status,
            order.created_at,
            order.paid_at or "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payment_orders.csv"'},
    )


@router.get("/credit-transactions/export.csv")
async def export_credit_transactions_csv(
    tenant_id: uuid.UUID | None = None,
    reason: str | None = None,
    action: str | None = None,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export credit ledger rows for usage audit."""
    result = await db.execute(
        _credit_transactions_query(tenant_id=tenant_id, reason=reason, action=action)
        .order_by(CreditTransaction.created_at.desc())
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "tenant_id", "delta", "balance_after", "reason", "ref_type", "ref_id", "user_id", "agent_id", "action", "modality", "tier", "provider", "model", "created_at"])
    for tx in result.scalars().all():
        writer.writerow([
            tx.id,
            tx.tenant_id,
            tx.delta,
            tx.balance_after,
            tx.reason,
            tx.ref_type or "",
            tx.ref_id or "",
            tx.user_id or "",
            tx.agent_id or "",
            tx.action or "",
            tx.modality or "",
            tx.tier or "",
            tx.provider or "",
            tx.model or "",
            tx.created_at,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="credit_transactions.csv"'},
    )


@router.get("/reconciliation/ledger")
async def get_ledger_reconciliation(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run a read-only credit ledger integrity check."""
    report = await check_credit_ledger_integrity(db, tenant_id=tenant_id)
    return _report_payload(report)


@router.get("/reconciliation/payments")
async def get_payment_reconciliation(
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run a read-only pending payment order reconciliation check."""
    report = await reconcile_pending_payment_orders(db)
    return _report_payload(report)


@router.post("/reservations/expire-stale")
async def expire_stale_reservations(
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Release expired credit reservations and return the number affected."""
    expired = await expire_stale_credit_reservations(db)
    db.add(_admin_audit(
        action="saas_reservations_expire_stale",
        actor=current_user,
        details={"expired": expired},
    ))
    await db.commit()
    return {"expired": expired}


@router.post("/orders/{order_id}/mark-paid", response_model=PaymentOrderOut)
async def mark_order_paid(
    order_id: uuid.UUID,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Mark a pending manual order as paid and apply its effects idempotently."""

    order = await db.get(PaymentOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == "paid":
        return order

    before = _snapshot(order, ["status", "provider", "provider_session_id", "provider_payment_id", "paid_at"])
    await finalize_order_in_session(db, order, actor_user_id=current_user.id)
    db.add(_admin_audit(
        action="saas_order_mark_paid",
        actor=current_user,
        details={
            "order_id": str(order.id),
            "tenant_id": str(order.tenant_id),
            "before": before,
            "after": _snapshot(order, ["status", "provider", "provider_session_id", "provider_payment_id", "paid_at"]),
        },
    ))

    await db.commit()
    await db.refresh(order)
    if order.type == "subscribe":
        await reconcile_tenant_agent_plan_selections(order.tenant_id)
        await restore_stopped_agents(order.tenant_id)
        await enforce_agent_limit(order.tenant_id)
    return order
