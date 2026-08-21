"""SaaS admin APIs (platform_admin only).

Provides platform-level management for subscriptions, model routes, billing rules,
credit packs, and tenant oversight.
"""

import uuid
import csv
from dataclasses import asdict
from io import StringIO

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_saas_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.llm import LLMModel
from app.models.subscription import (
    BillingRule,
    CreditBalance,
    CreditPack,
    CreditTransaction,
    ModelRoute,
    PaymentOrder,
    PaymentOrderOperatorDecision,
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
    LLMCreditHoldResolutionIn,
    MediaRouteOut,
    MediaRouteUpdateIn,
    MediaFailureRemediationIn,
    MediaProviderDebtResolutionIn,
    ManualOrderDecisionIn,
    ModelRouteCreateIn,
    ModelRouteOut,
    ModelRouteUpdateIn,
    SaasTenantOut,
)
from app.schemas.subscription import (
    CreditPackOut,
    CreditTransactionOut,
    ManualOrderDecisionResultOut,
    PaymentOrderOperatorDecisionOut,
    PaymentOrderOut,
)
from app.services.billing_reconciliation import (
    check_credit_ledger_integrity,
    expire_stale_credit_reservations,
    reconcile_pending_payment_orders,
)
from app.services.credit_service import (
    SUBSCRIPTION_PLAN_CHANGE_REF_TYPE,
    grant_credits_in_session,
    subscription_plan_change_grant_ref_id,
)
from app.services.media_incident_remediation import (
    remediate_media_tasks,
    resolve_media_provider_debt,
)
from app.services.llm_credit_reconciliation import resolve_llm_credit_holds
from app.services.manual_order_governance import (
    ManualOrderGovernanceError,
    apply_manual_order_decision_in_session,
)
from app.services.entitlements import get_active_subscription, get_tenant_entitlements
from app.services.agent_plan_selection import reconcile_tenant_agent_plan_selections
from app.services.subscription_lifecycle import (
    apply_paid_subscribe_effects,
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
from app.services.media_capabilities import (
    PlatformMediaProviderState,
    get_platform_media_generation_receipts,
    get_platform_media_provider_state,
    media_route_capability_status,
)
from app.services.media_provider_routing import (
    MINIMAX_PROVIDER,
    media_provider_order_for_image_strategy,
    media_provider_order_for_modality,
)
from app.services.modalities import canonicalize_modality, model_supports_modality
from app.services.provider_pricing import (
    VIDEO_PRICING_VERSION,
    minimax_image_credits,
    minimax_music_credits,
    minimax_tts_credits,
    minimax_video_credits,
    video_generation_quote,
)
from app.services.media_daily_allowance import minimax_video_allowance_summary
from app.services.volcengine_agent_plan import (
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    resolve_visual_profile,
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
    if not model_supports_modality(
        canonical,
        model_modality=model.modality,
        model_modalities=model.modalities,
        supports_vision=model.supports_vision,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model.label}' does not support the '{canonical}' route modality.",
        )
    return canonical


def _validate_platform_route_model(model: LLMModel) -> None:
    """Global Lite/Pro/Ultra routes may only use platform-owned models."""

    if getattr(model, "tenant_id", None) is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "SaaS model routes can only use platform-owned models; "
                "tenant API keys are never shared through global routes."
            ),
        )


async def _ensure_model_route_slot_available(
    db: AsyncSession,
    *,
    saas_tier: str,
    modality: str,
    priority: int,
    enabled: bool,
    exclude_route_id: uuid.UUID | None = None,
) -> None:
    """Reject ambiguous enabled routes before the DB uniqueness guard."""

    if not enabled:
        return
    conditions = [
        ModelRoute.saas_tier == saas_tier,
        ModelRoute.modality == modality,
        ModelRoute.priority == priority,
        ModelRoute.enabled == True,  # noqa: E712
    ]
    if exclude_route_id:
        conditions.append(ModelRoute.id != exclude_route_id)
    result = await db.execute(select(ModelRoute.id).where(*conditions).limit(1))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "An enabled model route already uses this SaaS tier, modality, "
                "and priority. Choose a different priority or disable the other route."
            ),
        )


async def _validate_fallback_route(
    db: AsyncSession,
    *,
    fallback_route_id: uuid.UUID | None,
    route_id: uuid.UUID | None,
    saas_tier: str,
    modality: str,
) -> None:
    if not fallback_route_id:
        return
    if route_id and fallback_route_id == route_id:
        raise HTTPException(status_code=400, detail="A model route cannot fall back to itself")

    fallback = await db.get(ModelRoute, fallback_route_id)
    if not fallback:
        raise HTTPException(status_code=404, detail="Fallback route not found")
    if not fallback.enabled:
        raise HTTPException(status_code=400, detail="Fallback route must be enabled")
    if fallback.saas_tier != saas_tier or fallback.modality != modality:
        raise HTTPException(
            status_code=400,
            detail="Fallback route must use the same SaaS tier and modality",
        )
    fallback_model = await db.get(LLMModel, fallback.llm_model_id)
    if not fallback_model or not fallback_model.enabled:
        raise HTTPException(status_code=400, detail="Fallback model must be enabled")
    _validate_platform_route_model(fallback_model)
    _validate_model_route(fallback_model, modality)

    visited: set[uuid.UUID] = set()
    cursor = fallback
    while cursor.fallback_route_id:
        if cursor.id in visited:
            raise HTTPException(status_code=400, detail="Fallback route cycle detected")
        visited.add(cursor.id)
        if route_id and cursor.fallback_route_id == route_id:
            raise HTTPException(status_code=400, detail="Fallback route cycle detected")
        cursor = await db.get(ModelRoute, cursor.fallback_route_id)
        if not cursor:
            break


async def _inbound_fallback_routes(
    db: AsyncSession,
    route_id: uuid.UUID,
    *,
    enabled_only: bool,
) -> list[ModelRoute]:
    """Lock routes that depend on ``route_id`` as their fallback target."""

    query = select(ModelRoute).where(ModelRoute.fallback_route_id == route_id)
    if enabled_only:
        query = query.where(ModelRoute.enabled == True)  # noqa: E712
    result = await db.execute(query.order_by(ModelRoute.id).with_for_update())
    return list(result.scalars().all())


def _validate_inbound_fallback_continuity(
    inbound_routes: list[ModelRoute],
    *,
    enabled: bool,
    saas_tier: str,
    modality: str,
) -> None:
    """Prevent an edit from silently invalidating active fallback users."""

    incompatible = [
        route
        for route in inbound_routes
        if not enabled
        or route.saas_tier != saas_tier
        or route.modality != modality
    ]
    if not incompatible:
        return
    dependants = ", ".join(str(route.id) for route in incompatible[:5])
    raise HTTPException(
        status_code=409,
        detail=(
            "This route is an active fallback target. Remove the inbound fallback "
            f"references before disabling it or changing its slot: {dependants}"
        ),
    )


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
    _validate_platform_route_model(model)
    if data.enabled and not model.enabled:
        raise HTTPException(status_code=400, detail="Enabled routes require an enabled LLM model")
    data.modality = _validate_model_route(model, data.modality)
    await _ensure_model_route_slot_available(
        db,
        saas_tier=data.saas_tier,
        modality=data.modality,
        priority=data.priority,
        enabled=data.enabled,
    )
    await _validate_fallback_route(
        db,
        fallback_route_id=data.fallback_route_id,
        route_id=None,
        saas_tier=data.saas_tier,
        modality=data.modality,
    )

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
    result = await db.execute(
        select(ModelRoute).where(ModelRoute.id == route_id).with_for_update()
    )
    route = result.scalar_one_or_none()
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
    if not model:
        raise HTTPException(status_code=404, detail="LLM model not found")
    _validate_platform_route_model(model)
    prospective_tier = update.get("saas_tier", route.saas_tier)
    prospective_modality = _validate_model_route(
        model,
        update.get("modality", route.modality),
    )
    prospective_enabled = update.get("enabled", route.enabled)
    prospective_priority = update.get("priority", route.priority)
    prospective_fallback_id = update.get("fallback_route_id", route.fallback_route_id)
    update["modality"] = prospective_modality
    if prospective_enabled and not model.enabled:
        raise HTTPException(status_code=400, detail="Enabled routes require an enabled LLM model")
    inbound_routes = await _inbound_fallback_routes(
        db,
        route_id,
        enabled_only=True,
    )
    _validate_inbound_fallback_continuity(
        inbound_routes,
        enabled=prospective_enabled,
        saas_tier=prospective_tier,
        modality=prospective_modality,
    )
    await _ensure_model_route_slot_available(
        db,
        saas_tier=prospective_tier,
        modality=prospective_modality,
        priority=prospective_priority,
        enabled=prospective_enabled,
        exclude_route_id=route_id,
    )
    await _validate_fallback_route(
        db,
        fallback_route_id=prospective_fallback_id,
        route_id=route_id,
        saas_tier=prospective_tier,
        modality=prospective_modality,
    )

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


# ── Automatic Media Routes ────────────────────────────────────────


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
    provider_state: PlatformMediaProviderState,
    generation_receipts: dict[tuple[str, str], dict[str, object]],
    minimax_allowance: dict[str, object] | None = None,
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
    provider_order = media_provider_order_for_modality(modality)
    account_ready_providers = [
        provider
        for provider in provider_order
        if modality in provider_state.verified_modalities.get(provider, set())
    ]
    minimax_allowance_exhausted = bool(
        modality == "video"
        and minimax_allowance is not None
        and int(minimax_allowance.get("quota", 0) or 0) > 0
        and int(minimax_allowance.get("remaining", 0) or 0) <= 0
    )
    available_providers = [
        provider
        for provider in account_ready_providers
        if not (minimax_allowance_exhausted and provider == MINIMAX_PROVIDER)
    ]
    capability_status, reason_code, recommended_action = (
        media_route_capability_status(
            modality,
            available_providers,
            provider_plan_tiers=provider_state.provider_plan_tiers,
        )
    )
    if minimax_allowance_exhausted:
        if VOLCENGINE_AGENT_PLAN_PROVIDER in available_providers:
            reason_code = "minimax_daily_allowance_exhausted_volcengine_active"
            recommended_action = (
                "MiniMax 今日免费额度已用尽，当前按策略由火山 Agent Plan 接管；"
                "仅在供应商明确拒绝且尚未接受任务时才会切换线路。"
            )
        else:
            reason_code = "minimax_daily_allowance_exhausted"
            recommended_action = (
                "MiniMax 今日免费额度已用尽，且火山视频线路当前不可用；"
                "等待次日额度重置或恢复火山账号后再提交。"
            )
    # Preserve the legacy route-baseline fields during rolling deployment.
    # Strategy-specific availability lives in execution_strategies; actual
    # provider/model remains authoritative only in task receipts.
    primary_provider = (
        MINIMAX_PROVIDER
        if modality == "music"
        else str(provider_order[0])
        if provider_order
        else ""
    )
    fallback_provider = str(provider_order[1]) if len(provider_order) > 1 else ""
    degraded_providers = (
        [MINIMAX_PROVIDER]
        if modality == "image"
        else []
    )
    strategy_orders = (
        (
            ("commercial_quality", media_provider_order_for_image_strategy("commercial_quality")),
            ("creative_exploration", media_provider_order_for_image_strategy("creative_exploration")),
        )
        if modality == "image"
        else (("default", provider_order),)
    )
    execution_strategies = []
    for strategy, strategy_order in strategy_orders:
        strategy_available = [
            provider for provider in strategy_order if provider in available_providers
        ]
        preferred_provider = strategy_order[0] if strategy_order else ""
        preferred_ready = bool(
            preferred_provider and preferred_provider in strategy_available
        )
        alternate_provider = next(
            (
                provider
                for provider in strategy_order[1:]
                if provider in strategy_available
            ),
            "",
        )
        execution_strategies.append(
            {
                "strategy": strategy,
                "provider_order": list(strategy_order),
                "available_providers": strategy_available,
                "preferred_provider": preferred_provider,
                "alternate_provider": alternate_provider,
                "preferred_ready": preferred_ready,
                "executable_without_alternate_confirmation": bool(
                    preferred_ready
                    or (alternate_provider and modality != "image")
                ),
                "alternate_confirmation_required": bool(
                    alternate_provider and modality == "image"
                ),
            }
        )
    pool_available = bool(available_providers)
    tool_enabled = bool(tool and tool.enabled)
    provider_readiness = []
    for candidate_provider in provider_order:
        key = (candidate_provider, modality)
        provider_readiness.append(
            {
                "provider": candidate_provider,
                "configured": modality
                in provider_state.configured_modalities.get(candidate_provider, set()),
                "account_verified": modality
                in provider_state.verified_modalities.get(candidate_provider, set()),
                "generation_observed": key in generation_receipts,
                "plan_tiers": sorted(provider_state.plan_tiers.get(key, set())),
                "account_receipt": provider_state.account_receipts.get(key),
                "generation_receipt": generation_receipts.get(key),
            }
        )
    any_configured = any(item["configured"] for item in provider_readiness)
    any_account_verified = any(item["account_verified"] for item in provider_readiness)
    any_generation_observed = any(item["generation_observed"] for item in provider_readiness)
    if not any_configured:
        readiness_status = "unconfigured"
    elif not any_account_verified:
        readiness_status = "account_verification_required"
    elif not any_generation_observed:
        readiness_status = "generation_unverified"
    else:
        readiness_status = "generation_observed"
    evidence_action = ""
    if readiness_status == "account_verification_required":
        evidence_action = (
            "账号已配置但没有当前配置对应的鉴权 receipt；请在账号池执行只读验证。"
        )
    elif readiness_status == "generation_unverified":
        evidence_action = (
            "账号鉴权已通过，但尚无当前配置下的真实生成 receipt；正式开放前需经授权执行受控生成。"
        )
    elif readiness_status == "generation_observed":
        evidence_action = (
            "已有真实生成成功 receipt，但尚未证明人工质量或商用门槛；仍需质量评审。"
        )
    if evidence_action:
        recommended_action = f"{recommended_action} {evidence_action}"
    estimated_credits, billing_unit = _media_route_billing(profile)
    volcengine_profile = None
    provider_quotes: dict[str, dict[str, object]] = {}
    if modality == "video":
        fire_profile = resolve_visual_profile("video", tier)
        volcengine_profile = {
            "model": fire_profile.model,
            "resolution": str(fire_profile.resolution or ""),
        }
        for quote_provider, quote_model, quote_resolution in (
            ("minimax", profile.model, str(profile.resolution or "768P")),
            (
                "volcengine_agent_plan",
                fire_profile.model,
                str(fire_profile.resolution or "720p"),
            ),
        ):
            try:
                quote = video_generation_quote(
                    quote_provider,
                    quote_model,
                    duration=int(profile.duration or 6),
                    resolution=quote_resolution,
                )
            except ValueError:
                continue
            provider_quotes[quote_provider] = {
                "model": quote.model,
                "resolution": quote.resolution,
                "duration_seconds": quote.duration_seconds,
                "credits": quote.credits,
                "billing_basis": quote.billing_basis,
                "pricing_version": quote.pricing_version,
            }
    return MediaRouteOut(
        modality=modality,
        tier=tier,
        provider="automatic",
        routing_mode="automatic_failover",
        route_semantics="account_pool_readiness_only",
        provider_order=list(provider_order),
        available_providers=available_providers,
        execution_strategies=execution_strategies,
        primary_provider=primary_provider,
        degraded_providers=degraded_providers,
        capability_status=capability_status,
        reason_code=reason_code,
        recommended_action=recommended_action,
        evaluation_source="persisted_account_and_generation_receipts",
        readiness_status=readiness_status,
        quality_evidence_status="not_reviewed",
        provider_readiness=provider_readiness,
        fallback_provider=fallback_provider,
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
        volcengine_profile=volcengine_profile,
        minimax_allowance=minimax_allowance if modality == "video" else None,
        provider_quotes=provider_quotes,
        pricing_version=VIDEO_PRICING_VERSION if modality == "video" else None,
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
    """List the 4 x 3 automatic media routing matrix without credentials."""
    tools = await _media_tool_map(db)
    provider_state = await get_platform_media_provider_state(db)
    generation_receipts = await get_platform_media_generation_receipts(
        db, provider_state
    )
    minimax_allowance = await minimax_video_allowance_summary(
        db,
        credentials=tuple(provider_state.verified_credentials.values()),
    )
    return [
        _media_route_out(
            modality=modality,
            tier=tier,
            tool=tools.get(tool_name),
            provider_state=provider_state,
            generation_receipts=generation_receipts,
            minimax_allowance=minimax_allowance,
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
    """Update one MiniMax fallback profile; provider failover stays automatic."""
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
    provider_state = await get_platform_media_provider_state(db)
    generation_receipts = await get_platform_media_generation_receipts(
        db, provider_state
    )
    minimax_allowance = await minimax_video_allowance_summary(
        db,
        credentials=tuple(provider_state.verified_credentials.values()),
    )
    return _media_route_out(
        modality=canonical,
        tier=normalized_tier,
        tool=tool,
        provider_state=provider_state,
        generation_receipts=generation_receipts,
        minimax_allowance=minimax_allowance,
    )


@router.delete("/model-routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_route(
    route_id: uuid.UUID,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a model route."""
    result = await db.execute(
        select(ModelRoute).where(ModelRoute.id == route_id).with_for_update()
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    inbound_routes = await _inbound_fallback_routes(
        db,
        route_id,
        enabled_only=False,
    )
    if inbound_routes:
        dependants = ", ".join(str(item.id) for item in inbound_routes[:5])
        raise HTTPException(
            status_code=409,
            detail=(
                "Remove every inbound fallback reference before deleting this route: "
                f"{dependants}"
            ),
        )
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
            ).with_for_update()
        )
        existing_sub = existing.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=data.period_days) if data.period_days else None
        grant_ref_type = "subscription"
        grant_ref_id: uuid.UUID | None = None

        if existing_sub:
            previous_plan_id = existing_sub.plan_id
            plan_changed = existing_sub.plan_id != data.plan_id
            if plan_changed:
                grant_ref_type = SUBSCRIPTION_PLAN_CHANGE_REF_TYPE
                grant_ref_id = subscription_plan_change_grant_ref_id(
                    existing_sub.id,
                    previous_plan_id,
                    data.plan_id,
                    now,
                )
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
        grant_ref_id = grant_ref_id or sub.id
        if plan_changed and plan.credits_per_period > 0:
            await grant_credits_in_session(
                db,
                tenant_id=tenant_id,
                amount=plan.credits_per_period,
                reason="subscribe",
                granted_by=current_user.id,
                ref_type=grant_ref_type,
                ref_id=grant_ref_id,
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


@router.get(
    "/order-decisions",
    response_model=list[PaymentOrderOperatorDecisionOut],
)
async def list_manual_order_decisions(
    order_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    limit: int = 500,
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """List bounded, platform-admin-only receipts for manual-order decisions."""

    stmt = select(PaymentOrderOperatorDecision)
    if order_id is not None:
        stmt = stmt.where(PaymentOrderOperatorDecision.order_id == order_id)
    if tenant_id is not None:
        stmt = stmt.where(PaymentOrderOperatorDecision.tenant_id == tenant_id)
    result = await db.execute(
        stmt.order_by(
            PaymentOrderOperatorDecision.created_at.desc(),
            PaymentOrderOperatorDecision.id.desc(),
        ).limit(min(max(limit, 1), 1000))
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


@router.post("/media/remediate-failures")
async def remediate_media_failures(
    data: MediaFailureRemediationIn,
    current_user: User = Depends(get_platform_admin),
):
    """Preview/apply refundable failure handling for exact tenant-fenced tasks."""
    try:
        result = await remediate_media_tasks(
            task_ids=tuple(data.task_ids),
            incident_key=data.incident_key,
            expected_tenant_id=data.expected_tenant_id,
            apply=data.apply,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/media/resolve-provider-debt")
async def resolve_provider_media_debt(
    data: MediaProviderDebtResolutionIn,
    current_user: User = Depends(get_platform_admin),
):
    """Preview/apply evidence-backed settlement or release for exact media debt."""
    try:
        result = await resolve_media_provider_debt(
            task_ids=tuple(data.task_ids),
            expected_tenant_id=data.expected_tenant_id,
            incident_key=data.incident_key,
            evidence_ref=data.evidence_ref,
            resolution=data.resolution,
            actor_user_id=current_user.id,
            apply=data.apply,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/credits/resolve-llm-holds")
async def resolve_ambiguous_llm_credit_holds(
    data: LLMCreditHoldResolutionIn,
    current_user: User = Depends(get_platform_admin),
):
    """Preview/apply an evidence-backed resolution for exact LLM holds."""
    try:
        result = await resolve_llm_credit_holds(
            reservation_ids=tuple(data.reservation_ids),
            expected_tenant_id=data.expected_tenant_id,
            incident_key=data.incident_key,
            evidence_ref=data.evidence_ref,
            resolution=data.resolution,
            settlement_amount=data.settlement_amount,
            actor_user_id=current_user.id,
            apply=data.apply,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_dict()


async def _apply_manual_order_decision(
    *,
    order_id: uuid.UUID,
    data: ManualOrderDecisionIn,
    idempotency_key: str | None,
    current_user: User,
    db: AsyncSession,
) -> ManualOrderDecisionResultOut:
    try:
        result = await apply_manual_order_decision_in_session(
            db,
            order_id=order_id,
            expected_tenant_id=data.expected_tenant_id,
            expected_status=data.expected_status,
            disposition=data.disposition,
            evidence_ref=data.evidence_ref,
            reason=data.reason,
            rollback_of_decision_id=data.rollback_of_decision_id,
            actor_user_id=current_user.id,
            idempotency_key=idempotency_key,
        )
    except ManualOrderGovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    if not result.replayed:
        db.add(
            AuditLog(
                tenant_id=result.order.tenant_id,
                user_id=current_user.id,
                agent_id=None,
                action="saas_manual_order_decision",
                details={
                    "decision_id": str(result.decision.id),
                    "order_id": str(result.order.id),
                    "tenant_id": str(result.order.tenant_id),
                    "disposition": result.decision.disposition,
                    "evidence_ref": result.decision.evidence_ref,
                    "reason": result.decision.reason,
                    "before": {"status": result.decision.previous_status},
                    "after": {"status": result.decision.resulting_status},
                    "idempotency_key_hash": result.decision.idempotency_key_hash,
                    "rollback_of_decision_id": (
                        str(result.decision.rollback_of_decision_id)
                        if result.decision.rollback_of_decision_id
                        else None
                    ),
                    "non_targets": [
                        "provider-backed orders",
                        "other tenants",
                        "unreferenced payment orders",
                    ],
                },
            )
        )

    await db.commit()
    await db.refresh(result.order)
    await db.refresh(result.decision)
    if result.order.type == "subscribe" and result.order.status == "paid":
        await apply_paid_subscribe_effects(result.order)
    return ManualOrderDecisionResultOut(
        order=PaymentOrderOut.model_validate(result.order),
        decision=PaymentOrderOperatorDecisionOut.model_validate(result.decision),
        replayed=result.replayed,
    )


@router.post(
    "/orders/{order_id}/operator-decisions",
    response_model=ManualOrderDecisionResultOut,
)
async def decide_manual_order(
    order_id: uuid.UUID,
    data: ManualOrderDecisionIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Keep, pay, cancel, or restore one manual order with a durable receipt."""

    return await _apply_manual_order_decision(
        order_id=order_id,
        data=data,
        idempotency_key=idempotency_key,
        current_user=current_user,
        db=db,
    )


@router.post(
    "/orders/{order_id}/mark-paid",
    response_model=ManualOrderDecisionResultOut,
)
async def mark_order_paid(
    order_id: uuid.UUID,
    data: ManualOrderDecisionIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Compatibility route for an evidence-backed mark-paid decision."""

    if data.disposition != "mark_paid":
        raise HTTPException(
            status_code=422,
            detail="mark-paid requires disposition=mark_paid",
        )
    return await _apply_manual_order_decision(
        order_id=order_id,
        data=data,
        idempotency_key=idempotency_key,
        current_user=current_user,
        db=db,
    )
