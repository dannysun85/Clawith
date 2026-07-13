"""Safe product capability view for MiniMax media generation."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMCredential
from app.models.tool import AgentTool, Tool
from app.services.entitlements import Entitlements
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.llm.load_balancer import credential_modality_is_blocked
from app.services.modalities import canonicalize_modalities


MEDIA_TOOL_NAMES: dict[str, str] = {
    "image": "generate_image_minimax",
    "audio": "generate_speech_minimax",
    "music": "generate_music_minimax",
    "video": "generate_video_minimax",
}
MEDIA_MODALITIES = tuple(MEDIA_TOOL_NAMES)
SAAS_TIERS = ("lite", "pro", "ultra")


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
        enabled = bool(assignment.enabled) if assignment is not None else bool(tool.is_default)
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

    credentials_result = await db.execute(
        select(LLMCredential).where(
            LLMCredential.provider == "minimax",
            LLMCredential.enabled == True,  # noqa: E712
            LLMCredential.status == "healthy",
            or_(
                LLMCredential.daily_quota.is_(None),
                LLMCredential.used_today < LLMCredential.daily_quota,
            ),
        )
    )
    pool_modalities: set[str] = set()
    for credential in credentials_result.scalars().all():
        capabilities = canonicalize_modalities(credential.capabilities)
        supported = set(MEDIA_MODALITIES) if not capabilities or "multimodal" in capabilities else set(capabilities)
        pool_modalities.update(
            modality
            for modality in supported
            if modality in MEDIA_MODALITIES and not credential_modality_is_blocked(credential, modality)
        )

    return evaluate_media_capabilities(
        entitlements,
        tier=tier,
        enabled_tools=enabled_tools,
        pool_modalities=pool_modalities,
    )
