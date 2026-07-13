"""Normalize Agent SaaS routing preferences against tenant entitlements.

These preferences select a platform-managed routing tier. They do not grant
access to concrete model objects; provider/model selection stays in the global
``model_routes`` pool.
"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.services.entitlements import Entitlements, get_tenant_entitlements
from app.services.modalities import canonicalize_modalities, canonicalize_modality


SAAS_AGENT_TIERS = ("lite", "pro", "ultra")


class InvalidAgentPlanSelection(ValueError):
    """A requested tier/modality is incompatible with the active plan."""

    def __init__(self, message: str, *, quota_type: str) -> None:
        super().__init__(message)
        self.quota_type = quota_type


def resolve_agent_plan_selection(
    entitlements: Entitlements | None,
    requested_tier: str | None,
    requested_modality: str | None,
    *,
    strict: bool = True,
) -> tuple[str, str]:
    """Return a supported SaaS tier/modality pair.

    Strict mode rejects user/API input. Non-strict mode repairs a legacy stored
    preference by falling back to the first capability in the active plan.
    """

    tier = (requested_tier or "").strip().lower() or None
    modality = canonicalize_modality(requested_modality) or "text"

    allowed_tiers = [
        str(value).strip().lower()
        for value in (entitlements.allowed_tiers if entitlements else [])
        if str(value).strip()
    ]
    supported_allowed_tiers = [value for value in allowed_tiers if value in SAAS_AGENT_TIERS]
    allowed_modalities = canonicalize_modalities(
        entitlements.allowed_modalities if entitlements else []
    )

    if allowed_tiers and not supported_allowed_tiers:
        raise InvalidAgentPlanSelection(
            "The active plan does not include a supported model tier.",
            quota_type="model_tier",
        )

    if tier and tier not in SAAS_AGENT_TIERS:
        if strict:
            raise InvalidAgentPlanSelection(
                f"Tier '{tier}' is not supported.",
                quota_type="model_tier",
            )
        tier = None

    if supported_allowed_tiers:
        if tier and tier not in supported_allowed_tiers:
            if strict:
                raise InvalidAgentPlanSelection(
                    f"Tier '{tier}' is not included in your plan.",
                    quota_type="model_tier",
                )
            tier = None
        tier = tier or supported_allowed_tiers[0]
    else:
        tier = tier or "lite"

    if allowed_modalities and modality not in allowed_modalities:
        if strict:
            raise InvalidAgentPlanSelection(
                f"Modality '{modality}' is not included in your plan.",
                quota_type="model_modality",
            )
        modality = allowed_modalities[0]

    return tier, modality


async def reconcile_tenant_agent_plan_selections(tenant_id: uuid.UUID) -> int:
    """Repair legacy Agent preferences after a tenant's plan changes."""

    from app.models.agent import Agent

    entitlements = await get_tenant_entitlements(tenant_id)
    if not entitlements:
        return 0

    async with async_session() as db:
        result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == "native",
            )
        )
        agents = result.scalars().all()
        changed = 0
        for agent in agents:
            try:
                tier, modality = resolve_agent_plan_selection(
                    entitlements,
                    agent.preferred_tier,
                    agent.preferred_modality,
                    strict=False,
                )
            except InvalidAgentPlanSelection as exc:
                logger.error(
                    "[agent_plan_selection] cannot reconcile tenant {}: {}",
                    tenant_id,
                    exc,
                )
                return 0
            if agent.preferred_tier != tier or agent.preferred_modality != modality:
                agent.preferred_tier = tier
                agent.preferred_modality = modality
                changed += 1
        if changed:
            await db.commit()
        return changed
