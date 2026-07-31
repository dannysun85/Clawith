"""Safe product capability view for platform media generation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMCredential
from app.models.media_generation import MediaGenerationTask
from app.models.tool import AgentTool, Tool
from app.services.credential_readiness import (
    credential_verification_receipt,
    current_credential_verification_receipt,
)
from app.services.entitlements import Entitlements
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.llm.load_balancer import credential_modality_is_blocked
from app.services.modalities import canonicalize_modalities
from app.services.tool_visibility import tool_enabled_for_agent
from app.services.volcengine_agent_plan import (
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    plan_tier_supports_modality,
)


MEDIA_TOOL_NAMES: dict[str, str] = {
    "image": "generate_image_minimax",
    "audio": "generate_speech_minimax",
    "music": "generate_music_minimax",
    "video": "generate_video_minimax",
}
MEDIA_MODALITIES = tuple(MEDIA_TOOL_NAMES)
SAAS_TIERS = ("lite", "pro", "ultra")
MEDIA_PROVIDERS = ("volcengine_agent_plan", "minimax")


@dataclass(frozen=True)
class PlatformMediaProviderState:
    """Secret-free account readiness used by runtime and SaaS control plane."""

    configured_modalities: dict[str, set[str]]
    verified_modalities: dict[str, set[str]]
    plan_tiers: dict[tuple[str, str], set[str]]
    account_receipts: dict[tuple[str, str], dict[str, object]]
    verified_credentials: dict[uuid.UUID, LLMCredential]


def media_route_capability_status(
    modality: str,
    available_providers: set[str] | list[str] | tuple[str, ...],
) -> tuple[str, str | None, str]:
    """Classify a provider pool without treating known quality loss as equivalent.

    Image and video use Agent Plan as their commercial-quality primary route.
    MiniMax remains an implemented emergency route, but when it is the only
    available provider the product contract is ``degraded`` and requires an
    explicit business-level quality decision before a formal delivery starts.
    Speech may fail over between the two managed providers, while music is a
    MiniMax-only capability by design.
    """

    normalized_modality = str(modality or "").strip().lower()
    providers = {
        str(provider or "").strip().lower()
        for provider in available_providers
        if str(provider or "").strip()
    }
    if not providers:
        return (
            "unavailable",
            "provider_pool_unavailable",
            "保留工作说明并等待可用的生成线路，未提交供应商任务。",
        )
    if (
        normalized_modality in {"image", "video"}
        and VOLCENGINE_AGENT_PLAN_PROVIDER not in providers
        and "minimax" in providers
    ):
        return (
            "degraded",
            "commercial_primary_unavailable",
            "正式质量优先时等待主线路恢复；如接受应急质量，再明确确认降级执行。",
        )
    return (
        "available",
        None,
        "按当前工作合同执行；供应商选择由平台托管。",
    )


def _credential_media_modalities(credential: LLMCredential) -> set[str]:
    """Mirror load-balancer semantics for None versus an explicit empty list."""

    if credential.capabilities is None:
        return set(MEDIA_MODALITIES)
    capabilities = set(canonicalize_modalities(credential.capabilities))
    if "multimodal" in capabilities:
        return set(MEDIA_MODALITIES)
    return capabilities.intersection(MEDIA_MODALITIES)


def _provider_supported_media_modalities(credential: LLMCredential) -> set[str]:
    supported = _credential_media_modalities(credential)
    if str(getattr(credential, "provider", "") or "") == VOLCENGINE_AGENT_PLAN_PROVIDER:
        supported = {
            modality
            for modality in supported
            if plan_tier_supports_modality(
                getattr(credential, "plan_tier", None),
                modality,
            )
        }
    return supported


async def get_platform_media_provider_state(
    db: AsyncSession,
) -> PlatformMediaProviderState:
    """Return configured and explicitly account-verified provider state.

    A configured key is not verification evidence.  Only a healthy credential
    with a current persisted authentication receipt contributes to the
    verified/routable view.  Media generation and commercial quality remain
    separate evidence levels.
    """

    credentials_result = await db.execute(
        select(LLMCredential).where(
            LLMCredential.provider.in_(MEDIA_PROVIDERS),
            LLMCredential.tenant_id.is_(None),
        )
    )
    configured_modalities = {provider: set() for provider in MEDIA_PROVIDERS}
    verified_modalities = {provider: set() for provider in MEDIA_PROVIDERS}
    plan_tiers: dict[tuple[str, str], set[str]] = {}
    account_receipts: dict[tuple[str, str], dict[str, object]] = {}
    account_receipt_times: dict[tuple[str, str], datetime] = {}
    verified_credentials: dict[uuid.UUID, LLMCredential] = {}
    for credential in credentials_result.scalars().all():
        provider = str(getattr(credential, "provider", "") or "").strip().lower()
        if provider not in configured_modalities:
            continue
        supported = _provider_supported_media_modalities(credential)
        configured_modalities[provider].update(supported)
        receipt = credential_verification_receipt(credential)
        verified_at = getattr(credential, "last_verification_at", None)
        for modality in supported:
            key = (provider, modality)
            plan_tier = str(getattr(credential, "plan_tier", "") or "").strip().lower()
            if plan_tier:
                plan_tiers.setdefault(key, set()).add(plan_tier)
            if (
                receipt is not None
                and isinstance(verified_at, datetime)
                and (
                    key not in account_receipt_times
                    or verified_at > account_receipt_times[key]
                )
            ):
                account_receipts[key] = receipt
                account_receipt_times[key] = verified_at

        current_receipt = current_credential_verification_receipt(credential)
        daily_quota_available = (
            getattr(credential, "daily_quota", None) is None
            or int(getattr(credential, "used_today", 0) or 0)
            < int(getattr(credential, "daily_quota", 0) or 0)
        )
        if (
            bool(getattr(credential, "enabled", False))
            and str(getattr(credential, "status", "") or "") == "healthy"
            and daily_quota_available
            and current_receipt is not None
        ):
            verified_credentials[credential.id] = credential
            verified_modalities[provider].update(
                modality
                for modality in supported
                if modality in MEDIA_MODALITIES
                and not credential_modality_is_blocked(credential, modality)
            )
    return PlatformMediaProviderState(
        configured_modalities=configured_modalities,
        verified_modalities=verified_modalities,
        plan_tiers=plan_tiers,
        account_receipts=account_receipts,
        verified_credentials=verified_credentials,
    )


async def get_platform_media_provider_modalities(
    db: AsyncSession,
) -> dict[str, set[str]]:
    """Return explicitly account-verified platform media modalities."""

    state = await get_platform_media_provider_state(db)
    return state.verified_modalities


async def get_platform_media_generation_receipts(
    db: AsyncSession,
    state: PlatformMediaProviderState,
) -> dict[tuple[str, str], dict[str, object]]:
    """Return latest successful generation evidence under current credentials.

    These receipts prove that a provider returned a non-empty media result
    after the current account verification.  They deliberately do not claim
    that a human approved quality or that the output is commercially usable.
    """

    credential_ids = list(state.verified_credentials)
    if not credential_ids:
        return {}
    result = await db.execute(
        select(MediaGenerationTask)
        .where(
            MediaGenerationTask.credential_id.in_(credential_ids),
            MediaGenerationTask.provider.in_(MEDIA_PROVIDERS),
            MediaGenerationTask.modality.in_(MEDIA_MODALITIES),
            MediaGenerationTask.status == "succeeded",
            MediaGenerationTask.provider_task_id.is_not(None),
            MediaGenerationTask.output_size > 0,
            MediaGenerationTask.completed_at.is_not(None),
        )
        .order_by(MediaGenerationTask.completed_at.desc())
        .limit(200)
    )
    receipts: dict[tuple[str, str], dict[str, object]] = {}
    for task in result.scalars().all():
        provider = str(task.provider or "").strip().lower()
        modality = str(task.modality or "").strip().lower()
        key = (provider, modality)
        if key in receipts or modality not in state.verified_modalities.get(provider, set()):
            continue
        credential = state.verified_credentials.get(task.credential_id)
        verified_at = getattr(credential, "last_verification_at", None)
        completed_at = getattr(task, "completed_at", None)
        if (
            credential is None
            or not isinstance(verified_at, datetime)
            or not isinstance(completed_at, datetime)
            or completed_at < verified_at
        ):
            continue
        account_receipt = current_credential_verification_receipt(credential) or {}
        receipts[key] = {
            "receipt_ref": f"media-generation:{task.id}",
            "kind": "media_generation_success",
            "evidence_level": "generation_observed",
            "provider": provider,
            "modality": modality,
            "model": task.model,
            "completed_at": completed_at.isoformat(),
            "output_size": int(task.output_size),
            "provider_task_recorded": True,
            "account_verification_ref": account_receipt.get("receipt_ref"),
            "quality_reviewed": False,
        }
    return receipts


def evaluate_media_capabilities(
    entitlements: Entitlements | None,
    *,
    tier: str,
    enabled_tools: set[str],
    pool_modalities: set[str],
) -> list[dict[str, Any]]:
    normalized_tier = str(tier or "lite").strip().lower()
    if normalized_tier not in SAAS_TIERS:
        normalized_tier = "lite"

    if entitlements is None:
        plan_modalities = set(MEDIA_MODALITIES)
        plan_tiers = set(SAAS_TIERS)
    else:
        plan_modalities = set(canonicalize_modalities(entitlements.generation_modalities))
        plan_tiers = {
            str(value).strip().lower()
            for value in entitlements.generation_tiers
            if str(value).strip()
        }
    canonical_pool = set(canonicalize_modalities(list(pool_modalities)))

    rows: list[dict[str, Any]] = []
    for modality, tool_name in MEDIA_TOOL_NAMES.items():
        allowed_by_plan = modality in plan_modalities and normalized_tier in plan_tiers
        tool_enabled = tool_name in enabled_tools
        pool_available = modality in canonical_pool
        available = allowed_by_plan and tool_enabled and pool_available
        reason = None
        if not allowed_by_plan:
            reason = "plan_denied"
        elif not tool_enabled:
            reason = "agent_tool_disabled"
        elif not pool_available:
            reason = "pool_unavailable"
        rows.append(
            {
                "modality": modality,
                "tool_name": tool_name,
                "available": available,
                "allowed_by_plan": allowed_by_plan,
                "pool_available": pool_available,
                "tool_enabled": tool_enabled,
                "reason": reason,
                "allowed_tiers": sorted(plan_tiers, key=lambda value: SAAS_TIERS.index(value) if value in SAAS_TIERS else 99),
            }
        )
    return rows


async def get_agent_media_capabilities(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    entitlements: Entitlements | None,
    tier: str,
) -> list[dict[str, Any]]:
    tools_result = await db.execute(
        select(Tool).where(
            Tool.name.in_(MEDIA_TOOL_NAMES.values()),
            Tool.enabled == True,  # noqa: E712
        )
    )
    tools = tools_result.scalars().all()
    tool_ids = [tool.id for tool in tools]
    assignments: dict[uuid.UUID, AgentTool] = {}
    if tool_ids:
        assignments_result = await db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id.in_(tool_ids),
            )
        )
        assignments = {assignment.tool_id: assignment for assignment in assignments_result.scalars().all()}

    enabled_tools: set[str] = set()
    for tool in tools:
        assignment = assignments.get(tool.id)
        enabled = tool_enabled_for_agent(tool, assignment)
        modality = next(
            (key for key, name in MEDIA_TOOL_NAMES.items() if name == tool.name),
            None,
        )
        route_enabled = bool(
            modality
            and resolve_minimax_media_profile(modality, tier, tool.config or {}).enabled
        )
        if enabled and route_enabled:
            enabled_tools.add(tool.name)

    provider_modalities = await get_platform_media_provider_modalities(db)
    pool_modalities = set().union(*provider_modalities.values())

    rows = evaluate_media_capabilities(
        entitlements,
        tier=tier,
        enabled_tools=enabled_tools,
        pool_modalities=pool_modalities,
    )
    for row in rows:
        modality = str(row["modality"])
        available_providers = {
            provider
            for provider, modalities in provider_modalities.items()
            if modality in modalities
        }
        if row["available"]:
            capability_status, reason_code, next_action = media_route_capability_status(
                modality,
                available_providers,
            )
        else:
            capability_status = "unavailable"
            reason_code = str(row.get("reason") or "media_capability_unavailable")
            next_action = "保留工作说明并修复套餐、工具或账号池配置后重试。"
        row.update(
            {
                "capability_status": capability_status,
                "available_providers": sorted(available_providers),
                "route_reason": reason_code,
                "next_action": next_action,
            }
        )
    return rows
