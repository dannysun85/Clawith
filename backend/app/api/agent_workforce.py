"""Platform-admin APIs for governed Agent workforce rollout."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.agent import AgentTemplate, AgentTemplateEvaluation
from app.models.audit import AuditLog
from app.models.user import User
from app.services.agent_capability_readiness import template_capability_contract
from app.services.agent_role_evaluation import (
    EVALUATOR_VERSION,
    activation_gate_reasons,
    evaluate_candidate_metrics,
    expected_role_family,
    load_evaluation_fixtures,
    validate_fixture_results,
)
from app.services.agent_template_contract import TEMPLATE_LIFECYCLE_ENABLED
from app.services.agent_workforce_catalog import (
    WorkforceDecision,
    load_agent_workforce_catalog,
)
from app.services.agent_workforce_packs import load_workforce_conditional_registry
from app.services.skill_seeder import push_default_skills_to_existing_agents
from app.services.template_capabilities import reconcile_template_tool_grants
from app.services.template_revision_sync import finalize_template_revision_sync


router = APIRouter(prefix="/agents", tags=["agent-workforce"])


class AgentRoleMetricsIn(BaseModel):
    task_success_rate: float
    first_effective_output_seconds: float
    clarification_turns: float
    tool_success_rate: float
    human_edit_ratio: float
    elapsed_seconds: float
    tokens_used: float


class AgentRoleFixtureResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str = Field(min_length=1, max_length=100)
    status: Literal["completed"]
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class AgentRoleEvaluationIn(BaseModel):
    role_revision: int = Field(ge=1)
    role_family: str = Field(min_length=1, max_length=40)
    fixture_set_version: str = Field(min_length=1, max_length=50)
    baseline_metrics: AgentRoleMetricsIn
    candidate_metrics: AgentRoleMetricsIn
    fixture_results: list[AgentRoleFixtureResultIn]
    safety_pass: bool
    capability_pass: bool


class AgentTemplateEnableBatchIn(BaseModel):
    template_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


class AgentTemplateRollbackIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _require_platform_admin(current_user: User) -> None:
    identity = getattr(current_user, "identity", None)
    if current_user.role != "platform_admin" and not bool(
        getattr(identity, "is_platform_admin", False)
    ):
        raise HTTPException(status_code=403, detail="Platform admin required")


@router.get("/workforce-catalog")
async def get_workforce_catalog(
    decision: WorkforceDecision | None = Query(default=None),
    pack: str | None = Query(default=None, min_length=1, max_length=100),
    current_user: User = Depends(get_current_admin),
):
    """Return the pinned 268-role decision catalog to administrators."""
    del current_user
    catalog = load_agent_workforce_catalog()
    conditional_registry = load_workforce_conditional_registry()
    records = [
        record
        for record in catalog.records
        if (decision is None or record.decision == decision)
        and (pack is None or record.pack == pack)
    ]
    return {
        "schema_version": catalog.schema_version,
        "source": catalog.source.model_dump(),
        "local_baseline": catalog.local_baseline,
        "summary": catalog.summary.model_dump(),
        "filters": {"decision": decision, "pack": pack},
        "count": len(records),
        "records": [record.model_dump() for record in records],
        "conditional_packs": [
            contract.model_dump() for contract in conditional_registry.packs
        ],
        "conditional_pack_counts": conditional_registry.pack_counts(),
        "resolution_counts": conditional_registry.resolution_counts(),
    }


@router.get("/workforce-evaluation-fixtures")
async def get_workforce_evaluation_fixtures(
    current_user: User = Depends(get_current_admin),
):
    """Return the versioned synthetic fixture set without running a Provider."""
    del current_user
    return load_evaluation_fixtures()


@router.post("/templates/{template_id}/evaluations", status_code=status.HTTP_201_CREATED)
async def record_agent_template_evaluation(
    template_id: uuid.UUID,
    data: AgentRoleEvaluationIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Persist measured A/B evidence; this endpoint never runs paid models."""
    _require_platform_admin(current_user)
    template_result = await db.execute(
        select(AgentTemplate).where(AgentTemplate.id == template_id)
    )
    template = template_result.scalar_one_or_none()
    if not template or not template.is_builtin:
        raise HTTPException(status_code=404, detail="Agent template not found")
    if data.role_revision != template.role_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evaluation_revision_mismatch",
                "expected_revision": template.role_revision,
                "received_revision": data.role_revision,
            },
        )
    fixture_set = load_evaluation_fixtures()
    if data.fixture_set_version != fixture_set["fixture_set_version"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evaluation_fixture_version_mismatch",
                "expected_version": fixture_set["fixture_set_version"],
            },
        )
    try:
        required_family = expected_role_family(template.workforce_pack)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data.role_family != required_family:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evaluation_role_family_mismatch",
                "expected_family": required_family,
                "received_family": data.role_family,
            },
        )
    try:
        fixture_results = [item.model_dump() for item in data.fixture_results]
        validate_fixture_results(data.role_family, fixture_results)
        decision = evaluate_candidate_metrics(
            baseline=data.baseline_metrics.model_dump(),
            candidate=data.candidate_metrics.model_dump(),
            safety_pass=data.safety_pass,
            capability_pass=data.capability_pass,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    evaluation = AgentTemplateEvaluation(
        template_id=template.id,
        role_revision=template.role_revision,
        evaluator_version=EVALUATOR_VERSION,
        fixture_set_version=data.fixture_set_version,
        role_family=data.role_family,
        baseline_metrics=data.baseline_metrics.model_dump(),
        candidate_metrics=data.candidate_metrics.model_dump(),
        fixture_results=fixture_results,
        safety_pass=data.safety_pass,
        capability_pass=data.capability_pass,
        gate_status=decision.status,
        gate_reasons=list(decision.reasons),
        created_by=current_user.id,
    )
    db.add(evaluation)
    await db.commit()
    await db.refresh(evaluation)
    return {
        "id": str(evaluation.id),
        "template_id": str(evaluation.template_id),
        "role_revision": evaluation.role_revision,
        "evaluator_version": evaluation.evaluator_version,
        "fixture_set_version": evaluation.fixture_set_version,
        "role_family": evaluation.role_family,
        "gate_status": evaluation.gate_status,
        "gate_reasons": evaluation.gate_reasons,
        "provider_calls_performed": False,
    }


@router.get("/templates/{template_id}/evaluations")
async def list_agent_template_evaluations(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List immutable metric evidence and promotion/rollback receipts."""
    _require_platform_admin(current_user)
    result = await db.execute(
        select(AgentTemplateEvaluation)
        .where(AgentTemplateEvaluation.template_id == template_id)
        .order_by(AgentTemplateEvaluation.created_at.desc())
    )
    return [
        {
            "id": str(item.id),
            "template_id": str(item.template_id),
            "role_revision": item.role_revision,
            "evaluator_version": item.evaluator_version,
            "fixture_set_version": item.fixture_set_version,
            "role_family": item.role_family,
            "baseline_metrics": item.baseline_metrics,
            "candidate_metrics": item.candidate_metrics,
            "fixture_results": item.fixture_results,
            "safety_pass": item.safety_pass,
            "capability_pass": item.capability_pass,
            "gate_status": item.gate_status,
            "gate_reasons": item.gate_reasons,
            "promoted_at": item.promoted_at,
            "rolled_back_at": item.rolled_back_at,
            "created_at": item.created_at,
        }
        for item in result.scalars().all()
    ]


@router.post("/templates/enable-batch")
async def enable_agent_template_batch(
    data: AgentTemplateEnableBatchIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Enable at most ten evaluated candidates in one atomic local cohort."""
    _require_platform_admin(current_user)
    template_ids = list(dict.fromkeys(data.template_ids))
    template_result = await db.execute(
        select(AgentTemplate).where(AgentTemplate.id.in_(template_ids))
    )
    templates = {
        template.id: template for template in template_result.scalars().all()
    }
    blockers: dict[str, list[str]] = {}
    accepted: list[tuple[AgentTemplate, AgentTemplateEvaluation]] = []
    for template_id in template_ids:
        template = templates.get(template_id)
        evaluation = None
        contract_ready = False
        if template is not None:
            contract_ready = bool(
                template_capability_contract(template)["contract_ready"]
            )
            evaluation_result = await db.execute(
                select(AgentTemplateEvaluation)
                .where(
                    AgentTemplateEvaluation.template_id == template.id,
                    AgentTemplateEvaluation.role_revision == template.role_revision,
                )
                .order_by(AgentTemplateEvaluation.created_at.desc())
                .limit(1)
            )
            evaluation = evaluation_result.scalar_one_or_none()
        reasons = activation_gate_reasons(
            template,
            evaluation,
            contract_ready=contract_ready,
        )
        if reasons:
            blockers[str(template_id)] = list(reasons)
        else:
            accepted.append((template, evaluation))

    if blockers:
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_template_activation_blocked", "blockers": blockers},
        )

    promoted_at = datetime.now(timezone.utc)
    for template, evaluation in accepted:
        template.lifecycle_status = TEMPLATE_LIFECYCLE_ENABLED
        evaluation.promoted_at = promoted_at
        evaluation.promoted_by = current_user.id
        db.add(
            AuditLog(
                user_id=current_user.id,
                action="agent_template_promoted",
                details={
                    "template_id": str(template.id),
                    "role_key": template.role_key,
                    "role_revision": template.role_revision,
                    "evaluation_id": str(evaluation.id),
                },
            )
        )
    await db.commit()
    return {
        "enabled": [str(template.id) for template, _evaluation in accepted],
        "batch_size": len(accepted),
        "max_batch_size": 10,
    }


@router.post("/templates/{template_id}/rollback")
async def rollback_agent_template(
    template_id: uuid.UUID,
    data: AgentTemplateRollbackIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a promoted workforce candidate from new hires without deleting Agents."""
    _require_platform_admin(current_user)
    template_result = await db.execute(
        select(AgentTemplate).where(AgentTemplate.id == template_id)
    )
    template = template_result.scalar_one_or_none()
    if not template or template.workforce_decision != "add_candidate":
        raise HTTPException(
            status_code=404, detail="Promoted workforce candidate not found"
        )
    if template.lifecycle_status != TEMPLATE_LIFECYCLE_ENABLED:
        raise HTTPException(status_code=409, detail="Template is not currently enabled")

    evaluation_result = await db.execute(
        select(AgentTemplateEvaluation)
        .where(
            AgentTemplateEvaluation.template_id == template.id,
            AgentTemplateEvaluation.promoted_at.is_not(None),
            AgentTemplateEvaluation.rolled_back_at.is_(None),
        )
        .order_by(AgentTemplateEvaluation.promoted_at.desc())
        .limit(1)
    )
    evaluation = evaluation_result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    template.lifecycle_status = "candidate_disabled"
    if evaluation is not None:
        evaluation.rolled_back_at = now
        evaluation.rolled_back_by = current_user.id
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="agent_template_rolled_back",
            details={
                "template_id": str(template.id),
                "role_key": template.role_key,
                "role_revision": template.role_revision,
                "reason": data.reason,
                "existing_agents_modified": False,
            },
        )
    )
    await db.commit()
    return {
        "template_id": str(template.id),
        "lifecycle_status": template.lifecycle_status,
        "existing_agents_modified": False,
    }


@router.post("/template-capabilities/reconcile")
async def reconcile_template_capabilities(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Apply reviewed template Tool/MCP routes to existing Agents locally."""
    _require_platform_admin(current_user)
    report = await reconcile_template_tool_grants(db)
    await db.commit()
    skill_sync = await push_default_skills_to_existing_agents()
    revision_sync = await finalize_template_revision_sync(
        db,
        tool_report=report,
        skill_sync_state=skill_sync,
    )
    await db.commit()
    return {
        **report.as_log_dict(),
        "skill_sync": skill_sync,
        "revision_sync": revision_sync,
        "external_imports_performed": False,
        "next_action": (
            "Import missing MCP servers with explicit authorization, then reconcile again."
            if report.missing_mcp_servers
            else None
        ),
    }


__all__ = ["AgentTemplateEnableBatchIn", "router"]
