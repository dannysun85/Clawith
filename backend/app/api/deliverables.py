"""Authenticated APIs for deliverable briefs, preflight, and approval state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user, get_current_user_from_bearer_or_browser_session
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.audit import AuditLog
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableQualityReview,
    DeliverableQualityReviewAssignment,
    DeliverableQualityReviewEvidence,
    DeliverableRequest,
    DeliverableSelectionReceipt,
)
from app.models.user import User
from app.models.task import Task
from app.schemas.deliverable import (
    DeliverableActionIn,
    DeliverableApprovalIn,
    DeliverableApprovalReadinessOut,
    DeliverableApprovalReceiptOut,
    DeliverableArtifactOut,
    DeliverableBriefOut,
    DeliverableClarificationIn,
    DeliverableExecutionOut,
    DeliverableExecutionUnitOut,
    DeliverablePreflightIn,
    DeliverableQualityReviewerOut,
    DeliverableQualityReviewArtifactOut,
    DeliverableQualityReviewAssignmentOut,
    DeliverableQualityReviewCreate,
    DeliverableQualityReviewEvidenceIn,
    DeliverableQualityReviewOut,
    DeliverableQualityReviewSubmissionIn,
    DeliverableRequestCreate,
    DeliverableRequestOut,
    DeliverableRequestUpdate,
    DeliverableSelectionReceiptOut,
)
from app.services.candidate_qa import qa_summary_from_evaluation
from app.services.selection_receipts import apply_user_selection
from app.services.creative_briefs import (
    CREATIVE_BRIEF_SCHEMA_VERSION,
    POSTER_V2_WORKFLOW_ID,
    PRESENTATION_BRIEF_SCHEMA_VERSION,
    PRESENTATION_V2_WORKFLOW_ID,
    VIDEO_BRIEF_SCHEMA_VERSION,
    VIDEO_V2_WORKFLOW_ID,
    brief_projection,
    compile_creative_brief,
    compile_presentation_brief,
    compile_video_brief,
    current_request_brief,
    presentation_brief_projection,
    upsert_request_structured_brief,
    video_brief_projection,
)
from app.services.deliverable_artifacts import (
    DeliverableArtifactError,
    approve_deliverable_artifacts,
    rebind_poster_selection_artifact,
    read_deliverable_artifact_snapshot,
)
from app.services.deliverable_executions import (
    DeliverableExecutionError,
    add_initial_execution_shadow,
    bind_artifacts_to_current_execution,
    create_revision_execution,
    current_execution,
    ensure_execution_shadow,
    execution_units,
    project_execution_lifecycle,
    record_execution_preflight,
)
from app.services.deliverable_quality_gate import (
    creative_quality_gate_required_for_request,
    deliverable_approval_readiness,
    selected_deliverable_artifacts,
)
from app.services.deliverable_quality_reviews import (
    DeliverableQualityReviewError,
    build_managed_evidence_receipt,
    build_managed_review_contract,
    build_reviewer_batch,
    canonical_payload_sha256,
    finalize_managed_review,
    required_evidence_kinds,
    review_creation_fingerprint,
    reviewer_submission_fingerprint,
    selected_artifact_hashes,
)
from app.services.access_control import is_company_governor
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    list_agent_launchable_workflows,
    preflight_workflow,
    request_fingerprint,
    require_workflow,
    validate_workflow_spec,
)
from app.services.storage import get_storage_backend, guess_content_type
from app.services.work_deliverable_contract import work_task_deliverable_contract


router = APIRouter(prefix="/api/deliverables", tags=["deliverables"])


def _workflow_error(exc: DeliverableWorkflowError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": exc.code, "message": str(exc)},
    )


def _execution_error(exc: DeliverableExecutionError) -> HTTPException:
    status_code = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
        if exc.code in {
            "deliverable_revision_instruction_required",
            "deliverable_revision_target_invalid",
        }
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _execution_out(
    db: AsyncSession,
    execution: DeliverableExecution,
) -> DeliverableExecutionOut:
    units = await execution_units(db, execution.id)
    receipt_result = await db.execute(
        select(DeliverableApprovalReceipt)
        .where(DeliverableApprovalReceipt.execution_id == execution.id)
        .order_by(
            DeliverableApprovalReceipt.created_at,
            DeliverableApprovalReceipt.id,
        )
    )
    approvals = tuple(receipt_result.scalars().all())
    selection_result = await db.execute(
        select(DeliverableSelectionReceipt)
        .where(DeliverableSelectionReceipt.execution_id == execution.id)
        .order_by(
            DeliverableSelectionReceipt.created_at,
            DeliverableSelectionReceipt.id,
        )
    )
    selections = tuple(selection_result.scalars().all())
    return DeliverableExecutionOut.model_validate(
        {
            **{
                field: getattr(execution, field)
                for field in DeliverableExecutionOut.model_fields
                if field not in {"units", "approvals", "selections"}
            },
            "units": [
                DeliverableExecutionUnitOut.model_validate(
                    {
                        **{
                            field: getattr(unit, field)
                            for field in DeliverableExecutionUnitOut.model_fields
                            if field != "qa_summary"
                        },
                        # Read-only sanitized projection; prompt text is never
                        # part of quality_evaluation.
                        "qa_summary": qa_summary_from_evaluation(unit.quality_evaluation),
                    }
                )
                for unit in units
            ],
            "approvals": [
                DeliverableApprovalReceiptOut.model_validate(receipt)
                for receipt in approvals
            ],
            "selections": [
                DeliverableSelectionReceiptOut.model_validate(receipt)
                for receipt in selections
            ],
        }
    )


async def _apply_brief_projection(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution: DeliverableExecution | None = None,
):
    """Persist the v2 creative brief and park incomplete briefs fail-closed.

    A confirmed brief leaves the launch preflight untouched; a clarifying brief
    keeps the request ``ready`` but blocks the execution with a
    ``brief_missing:<field>`` reason so no Provider task can be created.
    """

    brief_row = await upsert_request_structured_brief(db, request)
    if brief_row is None:
        return None
    if execution is None and request.current_execution_id is not None:
        execution = await current_execution(db, request, lock=True)
    if execution is not None:
        if brief_row.status == "confirmed":
            if execution.status == "blocked" and str(
                execution.blocked_reason or ""
            ).startswith("brief_missing:"):
                execution.status = "ready"
                execution.blocked_reason = None
        else:
            execution.status = "blocked"
            first_missing = next(iter(brief_row.missing_fields or []), "unknown")
            execution.blocked_reason = f"brief_missing:{first_missing}"[:200]
    return brief_row


async def _record_provider_free_preflight(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution: DeliverableExecution | None = None,
) -> dict[str, Any]:
    """Revalidate and persist launch readiness without reserving or submitting."""

    workflow = require_workflow(
        request.work_type,
        request.workflow_id,
        request.workflow_version,
    )
    preflight = await preflight_workflow(
        db,
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        workflow=workflow,
        tier=request.tier,
        spec=request.spec,
        goal=request.goal,
        inputs=request.inputs,
    )
    if execution is None:
        execution = await ensure_execution_shadow(db, request, lock=True)
    record_execution_preflight(request, execution, preflight)
    return preflight


def _brief_out_from_row(brief_row) -> DeliverableBriefOut:
    return DeliverableBriefOut(
        schema_version=brief_row.schema_version,
        status=brief_row.status,
        missing_fields=list(brief_row.missing_fields or []),
        brief_sha256=brief_row.brief_sha256,
        candidate_count=(brief_row.brief or {}).get("candidate_policy", {}).get("effective"),
        brief=brief_row.brief or None,
        updated_at=brief_row.updated_at,
    )


async def _supersede_quality_reviews_for_revision(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    now: datetime,
) -> tuple[uuid.UUID, ...]:
    result = await db.execute(
        select(DeliverableQualityReview)
        .where(
            DeliverableQualityReview.tenant_id == request.tenant_id,
            DeliverableQualityReview.request_id == request.id,
            DeliverableQualityReview.status != "superseded",
        )
        .with_for_update()
    )
    superseded: list[uuid.UUID] = []
    for review in result.scalars().all():
        review.status = "superseded"
        review.sealed_at = now
        review.version += 1
        superseded.append(review.id)
    return tuple(superseded)


async def _require_direct_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    user: User,
) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if (
        session is None
        or session.deleted_at is not None
        or session.tenant_id != user.tenant_id
        or session.agent_id != agent_id
        or session.user_id != user.id
        or session.session_type != "direct"
    ):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


async def _request_out(db: AsyncSession, request: DeliverableRequest) -> DeliverableRequestOut:
    # Server-side ``onupdate`` columns (notably ``updated_at``) are expired by
    # SQLAlchemy after a flush.  Pydantic reads attributes synchronously, so an
    # expired value would otherwise trigger async lazy IO and ``MissingGreenlet``
    # while serializing a successful mutation response.
    if sa_inspect(request).expired_attributes:
        await db.refresh(request)
    result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.request_id == request.id,
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
        )
        .order_by(
            DeliverableArtifactRevision.artifact_key,
            DeliverableArtifactRevision.revision_number.desc(),
        )
    )
    artifact_models = tuple(result.scalars().all())
    artifacts = [
        DeliverableArtifactOut.model_validate(artifact)
        for artifact in artifact_models
    ]
    settings = get_settings()
    quality_gate_required = creative_quality_gate_required_for_request(
        request,
        enabled=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED,
        tenant_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS,
    )
    readiness = deliverable_approval_readiness(
        request,
        artifact_models,
        require_creative_quality_gate=quality_gate_required,
    )
    if readiness.approvable:
        execution_blockers = await _output_execution_blockers(db, request)
        if execution_blockers:
            readiness = readiness.model_copy(
                update={
                    "approvable": False,
                    "blockers": tuple(
                        dict.fromkeys((*readiness.blockers, *execution_blockers))
                    ),
                }
            )
    fields = {
        field: getattr(request, field)
        for field in DeliverableRequestOut.model_fields
        if field not in {"artifacts", "approval_readiness"}
    }
    # Compatibility for model instances created by older tests or rolling
    # workers before the shadow-execution migration is applied.
    fields["contract_revision"] = int(fields.get("contract_revision") or 1)
    fields["current_execution_id"] = fields.get("current_execution_id") or None
    fields["latest_preflight"] = fields.get("latest_preflight") or None
    return DeliverableRequestOut.model_validate(
        {
            **fields,
            "artifacts": artifacts,
            "approval_readiness": DeliverableApprovalReadinessOut(
                **readiness.model_dump()
            ),
        }
    )


async def _owned_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    user: User,
    lock: bool = False,
) -> DeliverableRequest:
    query = select(DeliverableRequest).where(
        DeliverableRequest.id == request_id,
        DeliverableRequest.tenant_id == user.tenant_id,
        DeliverableRequest.created_by_user_id == user.id,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Deliverable request not found")
    await check_agent_access(
        db,
        user,
        request.agent_id,
        lock_authority=lock,
    )
    return request


def _is_company_admin(user: User) -> bool:
    """Return tenant governance authority; global platform authority is separate."""
    return is_company_governor(user)


def _quality_review_error(exc: DeliverableQualityReviewError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _manageable_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    user: User,
    lock: bool = False,
) -> DeliverableRequest:
    query = select(DeliverableRequest).where(
        DeliverableRequest.id == request_id,
        DeliverableRequest.tenant_id == user.tenant_id,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Deliverable request not found")
    _agent, access_level = await check_agent_access(
        db,
        user,
        request.agent_id,
        lock_authority=lock,
    )
    if request.created_by_user_id != user.id and access_level != "manage":
        raise HTTPException(status_code=404, detail="Deliverable request not found")
    return request


def _ensure_quality_review_allowlisted(request: DeliverableRequest) -> None:
    settings = get_settings()
    if not creative_quality_gate_required_for_request(
        request,
        enabled=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED,
        tenant_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_quality_review_not_allowlisted",
                "message": "Managed quality review is not enabled for this tenant or Agent",
            },
        )


async def _request_artifacts(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    lock: bool = False,
) -> tuple[DeliverableArtifactRevision, ...]:
    query = (
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
            DeliverableArtifactRevision.request_id == request.id,
        )
        .order_by(
            DeliverableArtifactRevision.artifact_key,
            DeliverableArtifactRevision.revision_number.desc(),
        )
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    return selected_deliverable_artifacts(request, tuple(result.scalars().all()))


async def _output_execution_blockers(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution: DeliverableExecution | None = None,
    lock: bool = False,
) -> tuple[str, ...]:
    """Require durable execution truth before final output approval."""

    if request.current_stage != "output_review":
        return ()
    current = execution or await current_execution(db, request, lock=lock)
    if not isinstance(current, DeliverableExecution):
        return ("deliverable_execution_missing",)
    units = await execution_units(db, current.id, lock=lock)
    if not units or any(unit.status != "succeeded" for unit in units):
        return ("deliverable_execution_incomplete",)
    return ()


async def _synchronize_and_require_output_execution(
    db: AsyncSession,
    request: DeliverableRequest,
) -> tuple[DeliverableArtifactRevision, ...]:
    """Bind verified artifacts, project recovery, then enforce unit closure."""

    execution = await ensure_execution_shadow(db, request, lock=True)
    artifacts = await _request_artifacts(db, request, lock=True)
    if artifacts:
        await bind_artifacts_to_current_execution(db, request, artifacts)
    await project_execution_lifecycle(db, request)
    blockers = await _output_execution_blockers(
        db,
        request,
        execution=execution,
        lock=True,
    )
    if blockers:
        code = blockers[0]
        message = (
            "The deliverable execution record is missing"
            if code == "deliverable_execution_missing"
            else "Every deliverable execution unit must succeed before final approval"
        )
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": message},
        )
    return artifacts


async def _review_rows(
    db: AsyncSession,
    review: DeliverableQualityReview,
    *,
    lock: bool = False,
) -> tuple[
    tuple[DeliverableQualityReviewAssignment, ...],
    tuple[DeliverableQualityReviewEvidence, ...],
]:
    assignment_query = (
        select(DeliverableQualityReviewAssignment)
        .where(
            DeliverableQualityReviewAssignment.tenant_id == review.tenant_id,
            DeliverableQualityReviewAssignment.review_id == review.id,
        )
        .order_by(DeliverableQualityReviewAssignment.created_at, DeliverableQualityReviewAssignment.id)
    )
    evidence_query = (
        select(DeliverableQualityReviewEvidence)
        .where(
            DeliverableQualityReviewEvidence.tenant_id == review.tenant_id,
            DeliverableQualityReviewEvidence.review_id == review.id,
        )
        .order_by(DeliverableQualityReviewEvidence.created_at, DeliverableQualityReviewEvidence.id)
    )
    if lock:
        assignment_query = assignment_query.with_for_update()
        evidence_query = evidence_query.with_for_update()
    assignment_result = await db.execute(assignment_query)
    evidence_result = await db.execute(evidence_query)
    return (
        tuple(assignment_result.scalars().all()),
        tuple(evidence_result.scalars().all()),
    )


async def _review_access(
    db: AsyncSession,
    *,
    review_id: uuid.UUID,
    user: User,
    lock: bool = False,
) -> tuple[
    DeliverableQualityReview,
    DeliverableRequest,
    tuple[DeliverableArtifactRevision, ...],
    tuple[DeliverableQualityReviewAssignment, ...],
    tuple[DeliverableQualityReviewEvidence, ...],
]:
    review_query = select(DeliverableQualityReview).where(
        DeliverableQualityReview.id == review_id,
        DeliverableQualityReview.tenant_id == user.tenant_id,
    )
    if lock:
        review_query = review_query.with_for_update()
    review_result = await db.execute(review_query)
    review = review_result.scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="Quality review not found")
    request = await db.get(DeliverableRequest, review.request_id)
    if request is None or request.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Quality review not found")
    assignments, evidence = await _review_rows(db, review, lock=lock)
    is_manager = (
        review.created_by_user_id == user.id
        or request.created_by_user_id == user.id
        or _is_company_admin(user)
    )
    is_reviewer = any(item.reviewer_user_id == user.id for item in assignments)
    if not is_manager and not is_reviewer:
        raise HTTPException(status_code=404, detail="Quality review not found")
    artifacts = await _request_artifacts(db, request, lock=lock)
    return review, request, artifacts, assignments, evidence


def _quality_review_out(
    *,
    review: DeliverableQualityReview,
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
    assignments: Sequence[DeliverableQualityReviewAssignment],
    evidence: Sequence[DeliverableQualityReviewEvidence],
    current_user: User,
) -> DeliverableQualityReviewOut:
    scenario = review.scenario
    is_manager = (
        review.created_by_user_id == current_user.id
        or request.created_by_user_id == current_user.id
        or _is_company_admin(current_user)
    )
    visible_assignments = (
        tuple(assignments)
        if is_manager
        else tuple(
            item for item in assignments if item.reviewer_user_id == current_user.id
        )
    )
    current_assignment = next(
        (
            item
            for item in assignments
            if item.reviewer_user_id == current_user.id
        ),
        None,
    )
    require_av_sync = bool((scenario.get("metadata") or {}).get("require_av_sync"))
    receipt = review.receipt or {}
    return DeliverableQualityReviewOut(
        id=review.id,
        request_id=review.request_id,
        modality=review.modality,  # type: ignore[arg-type]
        status=review.status,  # type: ignore[arg-type]
        version=review.version,
        minimum_reviewers=review.minimum_reviewers,
        assigned_reviewer_count=review.assigned_reviewer_count,
        submitted_reviewer_count=sum(
            item.status == "submitted" for item in assignments
        ),
        artifact_hashes=dict(review.artifact_hashes),
        brief=str(scenario.get("brief") or request.goal),
        requirements=list(scenario.get("requirements") or []),
        hard_gates=list(scenario.get("hard_gates") or []),
        quality_dimensions=list(scenario.get("quality_dimensions") or []),
        required_evidence_kinds=list(
            required_evidence_kinds(
                review.modality,
                require_av_sync=require_av_sync,
            )
        ),
        automated_evidence=[
            {
                "kind": item.kind,
                "status": item.status,
                "source_ref": item.source_ref if is_manager else None,
                "findings": list((item.receipt or {}).get("findings") or []),
            }
            for item in evidence
        ],
        assignments=[
            DeliverableQualityReviewAssignmentOut(
                reviewer_user_id=item.reviewer_user_id,
                reviewer_display_name=item.reviewer_display_name if is_manager else None,
                reviewer_role=item.reviewer_role if is_manager else None,
                status=item.status,  # type: ignore[arg-type]
                is_current_user=item.reviewer_user_id == current_user.id,
                submitted_at=item.submitted_at,
            )
            for item in visible_assignments
        ],
        artifacts=[
            DeliverableQualityReviewArtifactOut(
                id=artifact.id,
                artifact_key=artifact.artifact_key,
                artifact_type=artifact.artifact_type,
                content_hash=artifact.content_hash,
                revision_number=artifact.revision_number,
                download_url=(
                    f"/api/deliverables/quality-reviews/{review.id}/"
                    f"artifacts/{artifact.id}/download"
                ),
            )
            for artifact in artifacts
            if review.artifact_hashes.get(artifact.artifact_key) == artifact.content_hash
        ],
        current_user_can_manage=is_manager,
        current_user_can_submit=bool(
            review.status == "open"
            and current_assignment is not None
            and current_assignment.status == "assigned"
        ),
        current_user_can_add_evidence=bool(
            review.status == "open" and _is_company_admin(current_user)
        ),
        receipt_ref=str(receipt.get("receipt_ref")) if receipt.get("receipt_ref") else None,
        created_at=review.created_at,
        sealed_at=review.sealed_at,
    )


def _supersede_review_for_changed_artifacts(
    review: DeliverableQualityReview,
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    now: datetime | None = None,
) -> bool:
    """Project current Artifact hashes into a stale Review without erasing its receipt."""
    if review.status == "superseded":
        return False
    if selected_artifact_hashes(artifacts) == dict(review.artifact_hashes):
        return False
    review.status = "superseded"
    review.sealed_at = now or datetime.now(UTC)
    review.version += 1
    return True


@router.get("/workflows")
async def list_deliverable_workflows(
    agent_id: uuid.UUID = Query(...),
    tier: str = Query("lite", pattern="^(lite|pro|ultra)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if agent.tenant_id is None or agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    workflows = await list_agent_launchable_workflows(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        tier=tier,
    )
    return {"workflows": [workflow.model_dump() for workflow in workflows]}


@router.post("/preflight")
async def preflight_deliverable(
    data: DeliverablePreflightIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, data.agent_id)
    if agent.tenant_id is None or agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    try:
        workflow = require_workflow(data.work_type, data.workflow_id, data.workflow_version)
        result = await preflight_workflow(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            workflow=workflow,
            tier=data.tier,
            spec=data.spec,
            goal=data.goal,
            inputs=data.inputs,
        )
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return {"workflow_id": workflow.workflow_id, "workflow_version": workflow.workflow_version, **result}


@router.post("/requests", response_model=DeliverableRequestOut, status_code=status.HTTP_201_CREATED)
async def create_deliverable_request(
    data: DeliverableRequestCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, data.agent_id)
    if agent.tenant_id is None or agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    await _require_direct_session(
        db,
        session_id=data.session_id,
        agent_id=data.agent_id,
        user=current_user,
    )
    try:
        workflow = require_workflow(data.work_type, data.workflow_id, data.workflow_version)
        normalized_spec = validate_workflow_spec(workflow, data.spec)
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc

    approval_policy = data.approval_policy or list(workflow.approval_policy)
    output_contract = data.output_contract or list(workflow.output_contract)
    if approval_policy != list(workflow.approval_policy) or output_contract != list(workflow.output_contract):
        raise HTTPException(
            status_code=422,
            detail={"code": "workflow_contract_mismatch", "message": "Workflow approval/output contract is server-owned"},
        )
    fingerprint_payload = {
        "agent_id": str(data.agent_id),
        "session_id": str(data.session_id),
        "task_id": str(data.task_id) if data.task_id else None,
        "work_type": data.work_type,
        "workflow_id": workflow.workflow_id,
        "workflow_version": workflow.workflow_version,
        "goal": data.goal.strip(),
        "inputs": [item.model_dump(mode="json") for item in data.inputs],
        "spec": normalized_spec,
        "tier": data.tier,
        "approval_policy": approval_policy,
        "output_contract": output_contract,
    }
    fingerprint = request_fingerprint(fingerprint_payload)
    if data.task_id is not None:
        linked_task = (
            await db.execute(
                select(Task).where(
                    Task.id == data.task_id,
                    Task.tenant_id == agent.tenant_id,
                    Task.created_by == current_user.id,
                    Task.agent_id == agent.id,
                )
            )
        ).scalar_one_or_none()
        if linked_task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        contract = work_task_deliverable_contract(linked_task)
        mismatches: list[str] = []
        if linked_task.status != "done":
            mismatches.append("task_status")
        if contract is None:
            mismatches.append("task_contract")
        else:
            if data.work_type != contract.work_type:
                mismatches.append("work_type")
            if data.goal.strip() != contract.goal:
                mismatches.append("goal")
            mismatches.extend(
                key
                for key, value in contract.spec.items()
                if normalized_spec.get(key) != value
            )
        if data.inputs:
            # Work tasks do not yet persist task-owned source files. Refuse to
            # borrow unrelated files from the current chat session.
            mismatches.append("inputs")
        if mismatches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "task_deliverable_contract_mismatch",
                    "message": (
                        "Formal delivery must preserve the completed Work task contract: "
                        + ", ".join(sorted(set(mismatches)))
                    ),
                },
            )
    existing_result = await db.execute(
        select(DeliverableRequest).where(
            DeliverableRequest.tenant_id == agent.tenant_id,
            DeliverableRequest.created_by_user_id == current_user.id,
            DeliverableRequest.client_request_id == data.client_request_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="client_request_id was already used for another brief")
        return await _request_out(db, existing)

    request = DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        created_by_user_id=current_user.id,
        agent_id=agent.id,
        session_id=data.session_id,
        task_id=data.task_id,
        client_request_id=data.client_request_id,
        request_fingerprint=fingerprint,
        work_type=data.work_type,
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.workflow_version,
        goal=data.goal.strip(),
        inputs=[item.model_dump(mode="json") for item in data.inputs],
        spec=normalized_spec,
        tier=data.tier,
        approval_policy=approval_policy,
        output_contract=output_contract,
        status="ready",
        current_stage="brief_confirmed",
        contract_revision=1,
    )
    try:
        async with db.begin_nested():
            db.add(request)
            # The execution shadow has a composite FK back to the request.
            # Flush the parent first because SQLAlchemy cannot infer this
            # dependency from the manually-managed tenant/request columns.
            await db.flush()
            execution = add_initial_execution_shadow(db, request)
            await db.flush()
            # The execution shadow owns the target row of
            # ``current_execution_id``.  Link it only after the shadow and
            # its units have been inserted so the FK cannot race its parent.
            request.current_execution_id = execution.id
            await db.flush()
            # FR-I1: v2 poster requests persist their structured brief here;
            # an incomplete brief parks the execution as blocked.
            if await _apply_brief_projection(db, request, execution=execution) is not None:
                await db.flush()
            # The client-side preview is advisory.  Re-run the same
            # Provider-free check inside the write transaction and persist its
            # next_action so refresh/reconnect cannot turn a blocked brief into
            # an apparently launchable one.
            await _record_provider_free_preflight(db, request, execution=execution)
    except IntegrityError:
        concurrent_result = await db.execute(
            select(DeliverableRequest).where(
                DeliverableRequest.tenant_id == agent.tenant_id,
                DeliverableRequest.created_by_user_id == current_user.id,
                DeliverableRequest.client_request_id == data.client_request_id,
            )
        )
        concurrent = concurrent_result.scalar_one_or_none()
        if concurrent is None:
            raise
        if concurrent.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another brief",
            )
        request = concurrent
    return await _request_out(db, request)


@router.get("/requests", response_model=list[DeliverableRequestOut])
async def list_deliverable_requests(
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    query = (
        select(DeliverableRequest)
        .where(
            DeliverableRequest.tenant_id == current_user.tenant_id,
            DeliverableRequest.created_by_user_id == current_user.id,
            DeliverableRequest.agent_id == agent_id,
        )
        .order_by(DeliverableRequest.created_at.desc(), DeliverableRequest.id.desc())
        .limit(limit)
    )
    if session_id is not None:
        query = query.where(DeliverableRequest.session_id == session_id)
    result = await db.execute(query)
    return [await _request_out(db, request) for request in result.scalars().all()]


@router.get("/requests/{request_id}", response_model=DeliverableRequestOut)
async def get_deliverable_request(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user)
    return await _request_out(db, request)


@router.get(
    "/requests/{request_id}/executions",
    response_model=list[DeliverableExecutionOut],
)
async def list_deliverable_executions(
    request_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(
        db,
        request_id=request_id,
        user=current_user,
        lock=True,
    )
    adopting_legacy_execution = request.current_execution_id is None
    preserved_request_updated_at = request.updated_at
    # Older requests are upgraded lazily. This creates Provider-free execution
    # facts only; it never launches a model or reserves credits.
    await ensure_execution_shadow(db, request, lock=True)
    # Historical requests predate execution/unit lineage. Bind their selected,
    # already-verified artifacts to the lazily-created shadow before lifecycle
    # projection. The artifact fact is what proves production completed; the
    # request status alone is not enough to fabricate successful units.
    selected_artifacts = await _request_artifacts(db, request, lock=True)
    if selected_artifacts:
        await bind_artifacts_to_current_execution(
            db,
            request,
            selected_artifacts,
        )
    # A lazily-created shadow for a historical request starts with pending
    # blueprint units. Project the already-authoritative v1 request lifecycle
    # before returning it, otherwise a delivered artifact is rendered as
    # "0/N production steps complete" in the current UI.
    await project_execution_lifecycle(db, request)
    if adopting_legacy_execution:
        # Attaching the compatibility lineage is not a business event. Preserve
        # the historical request timestamp so merely opening a drawer cannot
        # reorder old work on the task index.
        request.updated_at = preserved_request_updated_at
    await db.flush()
    result = await db.execute(
        select(DeliverableExecution)
        .where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.request_id == request.id,
        )
        .order_by(
            DeliverableExecution.execution_number.desc(),
            DeliverableExecution.id.desc(),
        )
        .limit(limit)
    )
    return [
        await _execution_out(db, execution)
        for execution in result.scalars().all()
    ]


@router.post(
    "/requests/{request_id}/approvals",
    response_model=DeliverableRequestOut,
)
async def record_deliverable_approval(
    request_id: uuid.UUID,
    data: DeliverableApprovalIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(
        db,
        request_id=request_id,
        user=current_user,
        lock=True,
    )
    normalized_instruction = data.instruction.strip() if data.instruction else None
    action_payload = {
        "stage": data.stage,
        "action": data.action,
        "instruction": normalized_instruction,
        "target_units": data.target_units,
    }
    fingerprint = request_fingerprint(action_payload)
    existing_result = await db.execute(
        select(DeliverableApprovalReceipt).where(
            DeliverableApprovalReceipt.tenant_id == request.tenant_id,
            DeliverableApprovalReceipt.request_id == request.id,
            DeliverableApprovalReceipt.client_action_id == data.client_action_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_action_id was already used for a different decision",
            )
        return await _request_out(db, request)

    if request.version != data.expected_version:
        raise HTTPException(
            status_code=409,
            detail="Deliverable request changed; reload before acting",
        )
    settings = get_settings()
    # FR-V2/FR-P3: non-final stage approvals are a v2-only, flag-gated
    # capability; v1 requests keep the final-only 409 compatibility branch.
    stage_flow = bool(
        settings.DELIVERABLE_STAGE_APPROVALS_ENABLED
        and request.workflow_id == VIDEO_V2_WORKFLOW_ID
    )
    outline_stage_flow = bool(
        settings.DELIVERABLE_STAGE_APPROVALS_ENABLED
        and request.workflow_id == PRESENTATION_V2_WORKFLOW_ID
    )
    failed_retry = bool(
        data.action == "request_changes"
        and data.stage == "final"
        and request.status == "failed"
    )
    if (
        data.stage != "final"
        and not (stage_flow and data.stage == "storyboard")
        and not (outline_stage_flow and data.stage == "outline")
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_stage_approval_not_ready",
                "message": "Only final delivery approval is enabled in this compatibility release",
            },
        )
    if stage_flow and data.stage == "storyboard":
        if request.status != "waiting_approval" or request.current_stage != "storyboard_review":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "deliverable_stage_approval_not_ready",
                    "message": "The storyboard is not awaiting a decision",
                },
            )
    elif outline_stage_flow and data.stage == "outline":
        if request.status != "waiting_approval" or request.current_stage != "outline_review":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "deliverable_stage_approval_not_ready",
                    "message": "The deck outline is not awaiting a decision",
                },
            )
    elif (
        stage_flow
        and data.action == "request_changes"
        and request.status == "ready"
        and request.current_stage == "shot_review"
    ):
        # FR-V4: a failed shot is redone through a targeted revision without
        # dragging the whole request back through the storyboard.
        pass
    elif (
        not failed_retry
        and (request.status != "waiting_approval" or request.current_stage != "output_review")
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_final_approval_not_ready",
                "message": "Deliverable must be in final output review",
            },
        )

    decision_execution = await ensure_execution_shadow(db, request, lock=True)
    if data.action == "approve" and data.stage == "final":
        await _synchronize_and_require_output_execution(db, request)
    revision_stage = (
        "storyboard"
        if stage_flow and data.stage == "storyboard"
        else "outline"
        if outline_stage_flow and data.stage == "outline"
        else "shot"
        if stage_flow and request.current_stage == "shot_review"
        else "final"
    )
    if (
        data.action == "request_changes"
        and revision_stage in {"storyboard", "outline"}
        and data.target_units
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "deliverable_revision_target_not_allowed",
                "message": "Storyboard and outline revisions must revise the planning stage before production targets exist",
            },
        )
    if data.action == "request_changes" and revision_stage == "shot":
        review_units = await execution_units(db, decision_execution.id, lock=True)
        failed_shot_keys = {
            unit.unit_key
            for unit in review_units
            if unit.stage_key in {"shot_generate", "shot_qa"}
            and unit.status == "failed"
        }
        requested_shot_keys = {
            unit_key.strip()
            for unit_key in data.target_units
            if unit_key.strip()
        }
        if not requested_shot_keys:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "deliverable_failed_shot_target_required",
                    "message": "Select at least one failed shot to redo",
                },
            )
        invalid_shot_keys = sorted(requested_shot_keys - failed_shot_keys)
        if invalid_shot_keys:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "deliverable_failed_shot_target_invalid",
                    "message": "Only failed shots can be redone: " + ", ".join(invalid_shot_keys),
                },
            )
    now = datetime.now(UTC)
    next_execution_id: uuid.UUID | None = None
    superseded_review_ids: tuple[uuid.UUID, ...] = ()
    if data.action == "approve":
        if stage_flow and data.stage == "storyboard":
            # The storyboard approval releases the paid-work gate; the follow-up
            # shot run is a fresh short continuation, so the intake run pointer
            # is released and history stays on the execution.
            request.status = "ready"
            request.current_stage = "storyboard_approved"
            request.agent_run_id = None
            request.completed_at = None
            request.last_error_code = None
            request.version += 1
        elif outline_stage_flow and data.stage == "outline":
            # The outline approval releases the render gate; the follow-up
            # production run is a fresh short continuation, so the intake run
            # pointer is released and history stays on the execution.
            request.status = "ready"
            request.current_stage = "outline_approved"
            request.agent_run_id = None
            request.completed_at = None
            request.last_error_code = None
            request.version += 1
        else:
            # FR-I6: a v2 poster final approval may carry one candidate unit
            # key in target_units to re-select the delivered candidate.  The
            # selection receipt and artifact rebind are recorded before the
            # standard artifact approval runs; v1 requests never take this
            # branch and keep their final-only semantics untouched.
            if request.workflow_id == POSTER_V2_WORKFLOW_ID and data.target_units:
                if len(data.target_units) != 1:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "deliverable_selection_target_invalid",
                            "message": "Select exactly one candidate unit for delivery",
                        },
                    )
                try:
                    await apply_user_selection(
                        db,
                        request=request,
                        selected_unit_key=data.target_units[0],
                        actor_user_id=current_user.id,
                        client_selection_id=data.client_action_id,
                        now=now,
                    )
                    await rebind_poster_selection_artifact(
                        db,
                        request=request,
                        selected_unit_key=data.target_units[0],
                        now=now,
                    )
                except DeliverableExecutionError as exc:
                    raise _execution_error(exc) from exc
                except DeliverableArtifactError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
            try:
                artifacts = await approve_deliverable_artifacts(db, request=request)
            except DeliverableArtifactError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            for artifact in artifacts:
                artifact.status = "approved"
                artifact.approved_by_user_id = current_user.id
                artifact.approved_at = now
            request.status = "succeeded"
            request.current_stage = "delivered"
            request.completed_at = now
            request.last_error_code = None
            request.version += 1
    elif data.action == "request_changes":
        if normalized_instruction is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "deliverable_revision_instruction_required",
                    "message": "Revision instructions are required",
                },
            )
        try:
            next_execution, _created = await create_revision_execution(
                db,
                request,
                client_revision_id=data.client_action_id,
                instruction=normalized_instruction,
                target_units=data.target_units,
                revision_stage=revision_stage,
            )
        except DeliverableExecutionError as exc:
            raise _execution_error(exc) from exc
        next_execution_id = next_execution.id
        superseded_review_ids = await _supersede_quality_reviews_for_revision(
            db,
            request,
            now=now,
        )
    elif data.action == "cancel":
        request.status = "cancelled"
        request.current_stage = "cancelled"
        request.completed_at = now
        request.last_error_code = None
        request.version += 1
    else:  # pragma: no cover - constrained by the API schema
        raise HTTPException(status_code=409, detail="Unsupported approval action")

    await project_execution_lifecycle(db, request, now=now)
    receipt = DeliverableApprovalReceipt(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=decision_execution.id,
        actor_user_id=current_user.id,
        client_action_id=data.client_action_id,
        request_fingerprint=fingerprint,
        request_version=data.expected_version,
        stage=data.stage,
        action=data.action,
        instruction=normalized_instruction,
        target_units=list(data.target_units),
        receipt={
            "version": 1,
            "decision_execution_id": str(decision_execution.id),
            "next_execution_id": str(next_execution_id) if next_execution_id else None,
            "result_request_version": request.version,
            "recorded_at": now.isoformat(),
        },
    )
    db.add(receipt)
    db.add(
        AuditLog(
            tenant_id=request.tenant_id,
            user_id=current_user.id,
            agent_id=request.agent_id,
            action=f"deliverable.approval.{data.action}",
            details={
                "tenant_id": str(request.tenant_id),
                "request_id": str(request.id),
                "execution_id": str(decision_execution.id),
                "next_execution_id": str(next_execution_id) if next_execution_id else None,
                "client_action_id": str(data.client_action_id),
                "stage": data.stage,
                "target_units": list(data.target_units),
                "superseded_quality_review_ids": [
                    str(review_id) for review_id in superseded_review_ids
                ],
            },
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="A concurrent delivery decision changed this request",
        ) from exc
    return await _request_out(db, request)


@router.get(
    "/requests/{request_id}/quality-reviewers",
    response_model=list[DeliverableQualityReviewerOut],
)
async def list_deliverable_quality_reviewers(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _manageable_request(
        db,
        request_id=request_id,
        user=current_user,
    )
    _ensure_quality_review_allowlisted(request)
    result = await db.execute(
        select(User)
        .options(selectinload(User.identity))
        .where(
            User.tenant_id == request.tenant_id,
            User.is_active.is_(True),
        )
        .order_by(User.display_name, User.id)
    )
    reviewers = []
    for user in result.scalars().all():
        reason = None
        if user.id == request.created_by_user_id:
            reason = "deliverable_creator_cannot_review"
        elif user.identity_id is None or user.identity is None or not user.identity.is_active:
            reason = "reviewer_identity_unavailable"
        reviewers.append(
            DeliverableQualityReviewerOut(
                user_id=user.id,
                display_name=user.display_name,
                role=user.role,
                eligible=reason is None,
                ineligible_reason=reason,
            )
        )
    return reviewers


@router.post(
    "/requests/{request_id}/quality-reviews",
    response_model=DeliverableQualityReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_deliverable_quality_review(
    request_id: uuid.UUID,
    data: DeliverableQualityReviewCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _manageable_request(
        db,
        request_id=request_id,
        user=current_user,
        lock=True,
    )
    _ensure_quality_review_allowlisted(request)
    if request.version != data.expected_request_version:
        raise HTTPException(
            status_code=409,
            detail="Deliverable request changed; reload before starting review",
        )
    if request.status != "waiting_approval" or request.current_stage != "output_review":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_quality_review_not_ready",
                "message": "Deliverable must be in final output review",
            },
        )
    artifacts = await _synchronize_and_require_output_execution(db, request)
    if len(artifacts) != len(set(request.output_contract)):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_artifact_missing",
                "message": "The complete artifact set is required before review can start",
            },
        )
    existing_readiness = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=True,
    )
    if existing_readiness.quality_status == "blocked":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_quality_block_requires_revision",
                "message": "A blocked artifact set must be revised before a new review",
            },
        )
    if existing_readiness.quality_status in {"passed", "invalid"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": f"deliverable_quality_review_{existing_readiness.quality_status}",
                "message": "The current artifact quality receipt does not permit a new review",
            },
        )

    review_id = uuid.uuid4()
    try:
        scenario, package, artifact_hashes = build_managed_review_contract(
            request,
            artifacts,
            review_id=str(review_id),
        )
    except DeliverableQualityReviewError as exc:
        raise _quality_review_error(exc) from exc
    fingerprint = review_creation_fingerprint(
        request=request,
        artifact_hashes=artifact_hashes,
        reviewer_user_ids=[str(item) for item in data.reviewer_user_ids],
    )
    existing_result = await db.execute(
        select(DeliverableQualityReview).where(
            DeliverableQualityReview.tenant_id == request.tenant_id,
            DeliverableQualityReview.request_id == request.id,
            DeliverableQualityReview.client_review_id == data.client_review_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_review_id was already used for a different review",
            )
        review, request, artifacts, assignments, evidence = await _review_access(
            db,
            review_id=existing.id,
            user=current_user,
        )
        return _quality_review_out(
            review=review,
            request=request,
            artifacts=artifacts,
            assignments=assignments,
            evidence=evidence,
            current_user=current_user,
        )
    open_result = await db.execute(
        select(DeliverableQualityReview.id).where(
            DeliverableQualityReview.request_id == request.id,
            DeliverableQualityReview.status == "open",
        )
    )
    if open_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deliverable_quality_review_already_open",
                "message": "This deliverable already has an open review",
            },
        )

    reviewer_result = await db.execute(
        select(User)
        .options(selectinload(User.identity))
        .where(User.id.in_(data.reviewer_user_ids))
    )
    reviewer_users = tuple(reviewer_result.scalars().all())
    if len(reviewer_users) != len(data.reviewer_user_ids):
        raise HTTPException(status_code=422, detail="One or more reviewers do not exist")
    by_id = {user.id: user for user in reviewer_users}
    ordered_reviewers = tuple(by_id[item] for item in data.reviewer_user_ids)
    identity_ids: list[uuid.UUID] = []
    for reviewer in ordered_reviewers:
        if (
            reviewer.tenant_id != request.tenant_id
            or not reviewer.is_active
            or reviewer.identity_id is None
            or reviewer.identity is None
            or not reviewer.identity.is_active
        ):
            raise HTTPException(
                status_code=422,
                detail="Every reviewer must be an active user with an active identity in this tenant",
            )
        if reviewer.id == request.created_by_user_id:
            raise HTTPException(
                status_code=422,
                detail="The deliverable creator cannot count as an independent reviewer",
            )
        identity_ids.append(reviewer.identity_id)
    if len(set(identity_ids)) != len(identity_ids):
        raise HTTPException(
            status_code=422,
            detail="Every reviewer must have a distinct physical identity",
        )

    review = DeliverableQualityReview(
        id=review_id,
        tenant_id=request.tenant_id,
        request_id=request.id,
        created_by_user_id=current_user.id,
        client_review_id=data.client_review_id,
        request_fingerprint=fingerprint,
        modality=scenario.modality,
        status="open",
        minimum_reviewers=3,
        assigned_reviewer_count=len(ordered_reviewers),
        artifact_hashes=artifact_hashes,
        scenario=scenario.model_dump(mode="json"),
        review_package=package.model_dump(mode="json"),
        version=1,
    )
    assignments = tuple(
        DeliverableQualityReviewAssignment(
            tenant_id=request.tenant_id,
            review_id=review.id,
            reviewer_user_id=reviewer.id,
            reviewer_identity_id=reviewer.identity_id,
            reviewer_display_name=reviewer.display_name,
            reviewer_role=reviewer.role,
            reviewer_receipt_ref=f"managed-reviewer:{review.id}:{reviewer.identity_id}",
            status="assigned",
        )
        for reviewer in ordered_reviewers
    )
    db.add(review)
    db.add_all(assignments)
    db.add(
        AuditLog(
            tenant_id=request.tenant_id,
            user_id=current_user.id,
            agent_id=request.agent_id,
            action="deliverable.quality_review.created",
            details={
                "tenant_id": str(request.tenant_id),
                "request_id": str(request.id),
                "review_id": str(review.id),
                "artifact_hashes": artifact_hashes,
                "reviewer_user_ids": [str(item.reviewer_user_id) for item in assignments],
            },
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="A concurrent review creation changed this deliverable",
        ) from exc
    return _quality_review_out(
        review=review,
        request=request,
        artifacts=artifacts,
        assignments=assignments,
        evidence=(),
        current_user=current_user,
    )


@router.get(
    "/requests/{request_id}/quality-reviews/latest",
    response_model=DeliverableQualityReviewOut | None,
)
async def get_latest_deliverable_quality_review(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _manageable_request(
        db,
        request_id=request_id,
        user=current_user,
    )
    result = await db.execute(
        select(DeliverableQualityReview)
        .where(
            DeliverableQualityReview.tenant_id == request.tenant_id,
            DeliverableQualityReview.request_id == request.id,
        )
        .order_by(
            DeliverableQualityReview.created_at.desc(),
            DeliverableQualityReview.id.desc(),
        )
        .limit(1)
    )
    review = result.scalar_one_or_none()
    if review is None:
        return None
    review, request, artifacts, assignments, evidence = await _review_access(
        db,
        review_id=review.id,
        user=current_user,
        lock=True,
    )
    if _supersede_review_for_changed_artifacts(review, artifacts):
        await db.flush()
    return _quality_review_out(
        review=review,
        request=request,
        artifacts=artifacts,
        assignments=assignments,
        evidence=evidence,
        current_user=current_user,
    )


@router.get(
    "/quality-reviews/{review_id}",
    response_model=DeliverableQualityReviewOut,
)
async def get_deliverable_quality_review(
    review_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    review, request, artifacts, assignments, evidence = await _review_access(
        db,
        review_id=review_id,
        user=current_user,
        lock=True,
    )
    if _supersede_review_for_changed_artifacts(review, artifacts):
        await db.flush()
    return _quality_review_out(
        review=review,
        request=request,
        artifacts=artifacts,
        assignments=assignments,
        evidence=evidence,
        current_user=current_user,
    )


@router.post(
    "/quality-reviews/{review_id}/submissions",
    response_model=DeliverableQualityReviewOut,
)
async def submit_deliverable_quality_review(
    review_id: uuid.UUID,
    data: DeliverableQualityReviewSubmissionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    review, request, artifacts, assignments, evidence = await _review_access(
        db,
        review_id=review_id,
        user=current_user,
        lock=True,
    )
    assignment = next(
        (
            item
            for item in assignments
            if item.reviewer_user_id == current_user.id
        ),
        None,
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Quality review not found")
    fingerprint = reviewer_submission_fingerprint(data)
    if assignment.status == "submitted":
        if (
            assignment.client_submission_id == data.client_submission_id
            and assignment.submission_fingerprint == fingerprint
        ):
            return _quality_review_out(
                review=review,
                request=request,
                artifacts=artifacts,
                assignments=assignments,
                evidence=evidence,
                current_user=current_user,
            )
        raise HTTPException(
            status_code=409,
            detail="This reviewer has already sealed a submission",
        )
    if review.status != "open":
        raise HTTPException(status_code=409, detail="Quality review is already sealed")
    if review.version != data.expected_version:
        raise HTTPException(
            status_code=409,
            detail="Quality review changed; reload before submitting",
        )
    if selected_artifact_hashes(artifacts) != dict(review.artifact_hashes):
        review.status = "superseded"
        review.sealed_at = datetime.now(UTC)
        review.version += 1
        db.add(
            AuditLog(
                tenant_id=request.tenant_id,
                user_id=current_user.id,
                agent_id=request.agent_id,
                action="deliverable.quality_review.superseded",
                details={
                    "tenant_id": str(request.tenant_id),
                    "request_id": str(request.id),
                    "review_id": str(review.id),
                    "reason": "artifact_hash_changed",
                },
            )
        )
        await db.flush()
        return _quality_review_out(
            review=review,
            request=request,
            artifacts=artifacts,
            assignments=assignments,
            evidence=evidence,
            current_user=current_user,
        )
    try:
        batch = build_reviewer_batch(review, assignment, data)
    except DeliverableQualityReviewError as exc:
        raise _quality_review_error(exc) from exc

    assignment.client_submission_id = data.client_submission_id
    assignment.submission_fingerprint = fingerprint
    assignment.submission = batch.model_dump(mode="json")
    assignment.status = "submitted"
    assignment.submitted_at = datetime.now(UTC)
    review.version += 1
    try:
        receipt = finalize_managed_review(
            review,
            artifacts,
            assignments,
            evidence,
        )
    except DeliverableQualityReviewError as exc:
        raise _quality_review_error(exc) from exc
    db.add(
        AuditLog(
            tenant_id=request.tenant_id,
            user_id=current_user.id,
            agent_id=request.agent_id,
            action="deliverable.quality_review.submitted",
            details={
                "tenant_id": str(request.tenant_id),
                "request_id": str(request.id),
                "review_id": str(review.id),
                "reviewer_user_id": str(current_user.id),
                "client_submission_id": str(data.client_submission_id),
                "sealed_status": receipt.status if receipt else None,
            },
        )
    )
    await db.flush()
    return _quality_review_out(
        review=review,
        request=request,
        artifacts=artifacts,
        assignments=assignments,
        evidence=evidence,
        current_user=current_user,
    )


@router.post(
    "/quality-reviews/{review_id}/evidence",
    response_model=DeliverableQualityReviewOut,
)
async def add_deliverable_quality_review_evidence(
    review_id: uuid.UUID,
    data: DeliverableQualityReviewEvidenceIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not _is_company_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")
    review, request, artifacts, assignments, evidence = await _review_access(
        db,
        review_id=review_id,
        user=current_user,
        lock=True,
    )
    fingerprint = canonical_payload_sha256(
        data.model_dump(mode="json", exclude={"expected_version"})
    )
    existing = next(
        (
            item
            for item in evidence
            if item.submitted_by_user_id == current_user.id
            and item.client_evidence_id == data.client_evidence_id
        ),
        None,
    )
    if existing is not None:
        if existing.evidence_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_evidence_id was already used for different evidence",
            )
        return _quality_review_out(
            review=review,
            request=request,
            artifacts=artifacts,
            assignments=assignments,
            evidence=evidence,
            current_user=current_user,
        )
    if review.status != "open":
        raise HTTPException(status_code=409, detail="Quality review is already sealed")
    if review.version != data.expected_version:
        raise HTTPException(
            status_code=409,
            detail="Quality review changed; reload before adding evidence",
        )
    if selected_artifact_hashes(artifacts) != dict(review.artifact_hashes):
        review.status = "superseded"
        review.sealed_at = datetime.now(UTC)
        review.version += 1
        db.add(
            AuditLog(
                tenant_id=request.tenant_id,
                user_id=current_user.id,
                agent_id=request.agent_id,
                action="deliverable.quality_review.superseded",
                details={
                    "tenant_id": str(request.tenant_id),
                    "request_id": str(request.id),
                    "review_id": str(review.id),
                    "reason": "artifact_hash_changed",
                },
            )
        )
        await db.flush()
        return _quality_review_out(
            review=review,
            request=request,
            artifacts=artifacts,
            assignments=assignments,
            evidence=evidence,
            current_user=current_user,
        )
    receipt_ref = f"managed-evidence:{review.id}:{data.kind}:{data.client_evidence_id}"
    try:
        receipt = build_managed_evidence_receipt(
            review,
            kind=data.kind,
            status=data.status,
            source_ref=data.source_ref,
            findings=data.findings,
            receipt_ref=receipt_ref,
        )
    except DeliverableQualityReviewError as exc:
        raise _quality_review_error(exc) from exc
    evidence_row = DeliverableQualityReviewEvidence(
        tenant_id=review.tenant_id,
        review_id=review.id,
        submitted_by_user_id=current_user.id,
        client_evidence_id=data.client_evidence_id,
        evidence_fingerprint=fingerprint,
        receipt_ref=receipt_ref,
        kind=data.kind,
        status=data.status,
        source_ref=data.source_ref,
        receipt=receipt.model_dump(mode="json"),
    )
    db.add(evidence_row)
    next_evidence = (*evidence, evidence_row)
    review.version += 1
    try:
        quality_receipt = finalize_managed_review(
            review,
            artifacts,
            assignments,
            next_evidence,
        )
    except DeliverableQualityReviewError as exc:
        raise _quality_review_error(exc) from exc
    db.add(
        AuditLog(
            tenant_id=request.tenant_id,
            user_id=current_user.id,
            agent_id=request.agent_id,
            action="deliverable.quality_review.evidence_added",
            details={
                "tenant_id": str(request.tenant_id),
                "request_id": str(request.id),
                "review_id": str(review.id),
                "kind": data.kind,
                "status": data.status,
                "source_ref": data.source_ref,
                "sealed_status": quality_receipt.status if quality_receipt else None,
            },
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Evidence for this review kind already exists",
        ) from exc
    return _quality_review_out(
        review=review,
        request=request,
        artifacts=artifacts,
        assignments=assignments,
        evidence=next_evidence,
        current_user=current_user,
    )


@router.get("/quality-reviews/{review_id}/artifacts/{artifact_id}/download")
async def download_quality_review_artifact(
    review_id: uuid.UUID,
    artifact_id: uuid.UUID,
    inline: bool = False,
    current_user: User = Depends(get_current_user_from_bearer_or_browser_session),
    db: AsyncSession = Depends(get_db),
):
    review, _request, artifacts, _assignments, _evidence = await _review_access(
        db,
        review_id=review_id,
        user=current_user,
    )
    artifact = next((item for item in artifacts if item.id == artifact_id), None)
    if (
        artifact is None
        or review.artifact_hashes.get(artifact.artifact_key) != artifact.content_hash
    ):
        raise HTTPException(status_code=404, detail="Review artifact not found")
    storage = get_storage_backend()
    try:
        data = await read_deliverable_artifact_snapshot(storage, artifact=artifact)
    except DeliverableArtifactError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    filename = Path(artifact.workspace_path).name or f"deliverable.{artifact.artifact_type}"
    disposition = "inline" if inline else "attachment"
    encoded_filename = quote(filename, safe="")
    return Response(
        content=data,
        media_type=artifact.mime_type or guess_content_type(filename),
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/artifacts/{artifact_id}/download")
async def download_deliverable_artifact(
    artifact_id: uuid.UUID,
    inline: bool = False,
    current_user: User = Depends(get_current_user_from_bearer_or_browser_session),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DeliverableArtifactRevision).where(
            DeliverableArtifactRevision.id == artifact_id,
            DeliverableArtifactRevision.tenant_id == current_user.tenant_id,
        )
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Deliverable artifact not found")
    await _owned_request(db, request_id=artifact.request_id, user=current_user)
    storage = get_storage_backend()
    try:
        data = await read_deliverable_artifact_snapshot(storage, artifact=artifact)
    except DeliverableArtifactError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    filename = Path(artifact.workspace_path).name or f"deliverable.{artifact.artifact_type}"
    disposition = "inline" if inline else "attachment"
    encoded_filename = quote(filename, safe="")
    return Response(
        content=data,
        media_type=artifact.mime_type or guess_content_type(filename),
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}"},
    )


@router.patch("/requests/{request_id}", response_model=DeliverableRequestOut)
async def update_deliverable_request(
    request_id: uuid.UUID,
    data: DeliverableRequestUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user, lock=True)
    if request.status not in {"draft", "ready"} or request.agent_run_id is not None:
        raise HTTPException(status_code=409, detail="Launched deliverable requests cannot be edited")
    if request.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Deliverable request changed; reload before editing")
    workflow = require_workflow(request.work_type, request.workflow_id, request.workflow_version)
    next_spec = request.spec if data.spec is None else data.spec
    try:
        next_spec = validate_workflow_spec(workflow, next_spec)
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc
    request.goal = data.goal.strip() if data.goal is not None else request.goal
    request.inputs = (
        [item.model_dump(mode="json") for item in data.inputs]
        if data.inputs is not None
        else request.inputs
    )
    request.spec = next_spec
    request.tier = data.tier or request.tier
    request.version += 1
    execution = await ensure_execution_shadow(db, request, lock=True)
    await _apply_brief_projection(db, request, execution=execution)
    await _record_provider_free_preflight(db, request, execution=execution)
    await db.flush()
    return await _request_out(db, request)


@router.get("/requests/{request_id}/brief", response_model=DeliverableBriefOut)
async def get_deliverable_brief(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user)
    brief_row = await current_request_brief(db, request)
    if brief_row is not None:
        return _brief_out_from_row(brief_row)
    if request.workflow_id == VIDEO_V2_WORKFLOW_ID:
        video_brief, video_missing = compile_video_brief(
            request.goal,
            request.spec,
            request.inputs,
            tier=request.tier,
        )
        projection = video_brief_projection(video_brief, video_missing)
        return DeliverableBriefOut(
            schema_version=VIDEO_BRIEF_SCHEMA_VERSION,
            status=projection["status"],
            missing_fields=list(projection["missing_fields"]),
            brief_sha256=projection.get("brief_sha256"),
            brief=video_brief.model_dump(mode="json") if video_brief is not None else None,
        )
    if request.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        presentation_brief, presentation_missing = compile_presentation_brief(
            request.goal,
            request.spec,
            request.inputs,
            output_contract=request.output_contract or ("pptx",),
        )
        projection = presentation_brief_projection(
            presentation_brief,
            presentation_missing,
        )
        return DeliverableBriefOut(
            schema_version=PRESENTATION_BRIEF_SCHEMA_VERSION,
            status=projection["status"],
            missing_fields=list(projection["missing_fields"]),
            brief_sha256=projection.get("brief_sha256"),
            brief=(
                presentation_brief.model_dump(mode="json")
                if presentation_brief is not None
                else None
            ),
        )
    if request.workflow_id != POSTER_V2_WORKFLOW_ID:
        raise HTTPException(
            status_code=404,
            detail="Creative brief is only available for v2 deliverable requests",
        )
    brief, missing_fields = compile_creative_brief(
        request.goal,
        request.spec,
        request.inputs,
        tier=request.tier,
        delivery_formats=request.output_contract or ("png",),
    )
    projection = brief_projection(brief, missing_fields)
    return DeliverableBriefOut(
        schema_version=CREATIVE_BRIEF_SCHEMA_VERSION,
        status=projection["status"],
        missing_fields=list(projection["missing_fields"]),
        brief_sha256=projection.get("brief_sha256"),
        candidate_count=projection.get("candidate_count"),
        brief=brief.model_dump(mode="json") if brief is not None else None,
    )


@router.post("/requests/{request_id}/clarifications", response_model=DeliverableBriefOut)
async def submit_deliverable_clarifications(
    request_id: uuid.UUID,
    data: DeliverableClarificationIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user, lock=True)
    if request.status not in {"draft", "ready"} or request.agent_run_id is not None:
        raise HTTPException(status_code=409, detail="Launched deliverable requests cannot be edited")
    if request.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Deliverable request changed; reload before editing")
    workflow = require_workflow(request.work_type, request.workflow_id, request.workflow_version)
    if workflow.workflow_id not in {POSTER_V2_WORKFLOW_ID, VIDEO_V2_WORKFLOW_ID, PRESENTATION_V2_WORKFLOW_ID}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "deliverable_clarification_not_applicable",
                "message": "Clarifications only apply to v2 deliverable requests",
            },
        )
    merged_spec = {**dict(request.spec or {}), **dict(data.answers)}
    try:
        next_spec = validate_workflow_spec(workflow, merged_spec)
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc
    request.spec = next_spec
    request.version += 1
    execution = await ensure_execution_shadow(db, request, lock=True)
    brief_row = await _apply_brief_projection(db, request, execution=execution)
    await _record_provider_free_preflight(db, request, execution=execution)
    await db.flush()
    return _brief_out_from_row(brief_row)


@router.post("/requests/{request_id}/actions", response_model=DeliverableRequestOut)
async def apply_deliverable_action(
    request_id: uuid.UUID,
    data: DeliverableActionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user, lock=True)
    if request.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Deliverable request changed; reload before acting")
    if request.current_stage == "output_review":
        now = datetime.now(UTC)
        if data.action == "approve":
            await _synchronize_and_require_output_execution(db, request)
            try:
                artifacts = await approve_deliverable_artifacts(db, request=request)
            except DeliverableArtifactError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            for artifact in artifacts:
                artifact.status = "approved"
                artifact.approved_by_user_id = current_user.id
                artifact.approved_at = now
            request.status = "succeeded"
            request.current_stage = "delivered"
            request.completed_at = now
            request.last_error_code = None
            request.version += 1
            if request.current_execution_id is not None:
                await project_execution_lifecycle(db, request, now=now)
            await db.flush()
            return await _request_out(db, request)
        if data.action == "request_changes":
            try:
                await create_revision_execution(
                    db,
                    request,
                    client_revision_id=uuid.uuid4(),
                    instruction="用户通过兼容入口要求修改最终交付，请保留原工作说明并生成新版本。",
                )
            except DeliverableExecutionError as exc:
                raise _execution_error(exc) from exc
            await _supersede_quality_reviews_for_revision(db, request, now=now)
            await db.flush()
            return await _request_out(db, request)
        raise HTTPException(status_code=409, detail="Only approve or request_changes is valid during output review")
    transitions = {
        ("draft", "submit"): ("ready", "brief_confirmed"),
        ("waiting_approval", "approve"): ("ready", "approval_granted"),
        ("waiting_approval", "request_changes"): ("draft", "changes_requested"),
        ("draft", "cancel"): ("cancelled", "cancelled"),
        ("ready", "cancel"): ("cancelled", "cancelled"),
    }
    transition = transitions.get((request.status, data.action))
    if transition is None:
        raise HTTPException(status_code=409, detail="Action is not valid for the current request state")
    request.status, request.current_stage = transition
    request.version += 1
    if request.current_execution_id is not None:
        await project_execution_lifecycle(db, request)
    await db.flush()
    return await _request_out(db, request)
