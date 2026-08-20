"""Revision-safe shadow execution facts for durable creative deliverables.

The v1 request and Runtime path remain authoritative while these records make
future outline, candidate, page, and shot workflows recoverable.  This module
does not call a Provider and does not reserve Credits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services.creative_briefs import candidate_count_for_policy


class DeliverableExecutionError(RuntimeError):
    """A requested execution transition violates the revision contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExecutionUnitBlueprint:
    stage_key: str
    unit_key: str
    input_snapshot: dict[str, Any]


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_contract_snapshot(
    request: DeliverableRequest,
    *,
    revision_instruction: str | None = None,
    target_units: Sequence[str] = (),
    revision_stage: str | None = None,
) -> dict[str, Any]:
    """Freeze the customer and platform-owned contract for one execution."""

    return {
        "version": 1,
        "request_id": str(request.id),
        "contract_revision": int(getattr(request, "contract_revision", None) or 1),
        "work_type": request.work_type,
        "workflow_id": request.workflow_id,
        "workflow_version": request.workflow_version,
        "goal": request.goal,
        "inputs": list(request.inputs or []),
        "spec": dict(request.spec or {}),
        "tier": request.tier,
        "approval_policy": list(request.approval_policy or []),
        "output_contract": list(request.output_contract or []),
        "revision_instruction": revision_instruction,
        "target_units": list(target_units),
        "revision_stage": revision_stage,
    }


def safe_preflight_snapshot(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only product capability facts; never credential or secret data."""

    allowed = {
        "available",
        "launchable",
        "reasons",
        "capability_status",
        "next_action",
        "tier",
        "normalized_spec",
        "credit_estimate",
        "creates_reservation",
        "creative_brief",
    }
    snapshot = {
        key: preflight[key]
        for key in allowed
        if key in preflight
    }
    snapshot["version"] = 1
    snapshot["checked_at"] = datetime.now(UTC).isoformat()
    snapshot["evidence_level"] = "provider_free_preflight"
    return snapshot


def _candidate_count(request: DeliverableRequest) -> int:
    """FR-I3: the tier default is authoritative; spec may only tune it down."""

    return candidate_count_for_policy(request.tier, request.spec)


def _slide_count(request: DeliverableRequest) -> int:
    value = (request.spec or {}).get("page_count")
    return max(1, min(int(value) if isinstance(value, int) else 8, 50))


def _shot_count(request: DeliverableRequest) -> int:
    configured = (request.spec or {}).get("shot_count")
    if isinstance(configured, int) and not isinstance(configured, bool):
        return max(1, min(configured, 12))
    raw_duration = (request.spec or {}).get("duration")
    try:
        duration = int(raw_duration or 6)
    except (TypeError, ValueError):
        duration = 6
    return max(1, min(math.ceil(duration / 4), 12))


def execution_unit_blueprints(
    request: DeliverableRequest,
    *,
    target_units: Sequence[str] = (),
) -> tuple[ExecutionUnitBlueprint, ...]:
    """Create a deterministic, Provider-free impact graph for one execution."""

    requested_targets = {item.strip() for item in target_units if item.strip()}
    units: list[ExecutionUnitBlueprint] = []

    def add(stage_key: str, unit_key: str, **facts: Any) -> None:
        units.append(
            ExecutionUnitBlueprint(
                stage_key=stage_key,
                unit_key=unit_key,
                input_snapshot={
                    "version": 1,
                    "work_type": request.work_type,
                    "stage_key": stage_key,
                    "unit_key": unit_key,
                    **facts,
                },
            )
        )

    if request.work_type == "presentation":
        add("source_inventory", "deck")
        add("outline", "deck")
        add("slide_spec", "deck")
        for index in range(1, _slide_count(request) + 1):
            unit_key = f"slide-{index:02d}"
            if not requested_targets or unit_key in requested_targets:
                add("slide_render", unit_key, slide_number=index)
        for stage in (
            "deck_assemble",
            "pptx_render",
            "pdf_render",
            "structural_qa",
            "semantic_qa",
            "visual_qa",
            "pptx_pdf_parity",
        ):
            add(stage, "deck")
    elif request.work_type == "poster":
        add("composition_plan", "canvas")
        for index in range(1, _candidate_count(request) + 1):
            unit_key = f"candidate-{index:02d}"
            if not requested_targets or unit_key in requested_targets:
                add("candidate_generate", unit_key, candidate_number=index)
                add("candidate_qa", unit_key, candidate_number=index)
        add("selection", "final")
        add("deterministic_compose", "final")
        add("final_qa", "final")
    elif request.work_type == "video":
        add("script", "video")
        add("storyboard", "video")
        for index in range(1, _shot_count(request) + 1):
            unit_key = f"shot-{index:02d}"
            if not requested_targets or unit_key in requested_targets:
                add("shot_spec_compile", unit_key, shot_number=index)
                add("keyframe_pack", unit_key, shot_number=index)
                add("shot_generate", unit_key, shot_number=index)
                add("shot_qa", unit_key, shot_number=index)
        add("edit_compose", "final")
        add("caption_voice_music", "final")
        add("package_qa", "final")
    else:
        add("content_compile", "document")
        add("artifact_render", "document")
        add("artifact_qa", "document")
    return tuple(units)


def _materialize_units(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    blueprints: Iterable[ExecutionUnitBlueprint],
) -> tuple[DeliverableExecutionUnit, ...]:
    contract_hash = _canonical_sha256(execution.contract_snapshot)
    units = []
    for blueprint in blueprints:
        dependency_hash = _canonical_sha256(
            {
                "contract_hash": contract_hash,
                "stage_key": blueprint.stage_key,
                "unit_key": blueprint.unit_key,
                "input_snapshot": blueprint.input_snapshot,
            }
        )
        units.append(
            DeliverableExecutionUnit(
                id=uuid.uuid4(),
                tenant_id=request.tenant_id,
                request_id=request.id,
                execution_id=execution.id,
                stage_key=blueprint.stage_key,
                unit_key=blueprint.unit_key,
                status="pending",
                dependency_hash=dependency_hash,
                attempt_count=0,
                input_snapshot=blueprint.input_snapshot,
                result_snapshot={},
                quality_evaluation={},
            )
        )
    return tuple(units)


def build_execution_shadow(
    request: DeliverableRequest,
    *,
    execution_number: int,
    kind: str,
    idempotency_key: uuid.UUID,
    current_stage: str,
    revision_instruction: str | None = None,
    target_units: Sequence[str] = (),
    revision_stage: str | None = None,
) -> tuple[DeliverableExecution, tuple[DeliverableExecutionUnit, ...]]:
    contract_snapshot = execution_contract_snapshot(
        request,
        revision_instruction=revision_instruction,
        target_units=target_units,
        revision_stage=revision_stage,
    )
    execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=execution_number,
        kind=kind,
        status="ready",
        current_stage=current_stage,
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot=contract_snapshot,
        preflight_snapshot={},
        revision_instruction=revision_instruction,
        idempotency_key=idempotency_key,
        request_fingerprint=_canonical_sha256(contract_snapshot),
    )
    return execution, _materialize_units(
        request,
        execution,
        execution_unit_blueprints(request, target_units=target_units),
    )


def add_initial_execution_shadow(
    db: AsyncSession,
    request: DeliverableRequest,
) -> DeliverableExecution:
    """Dual-write an initial shadow without altering the v1 launch contract."""

    execution, units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="brief_confirmed",
    )
    request.contract_revision = int(getattr(request, "contract_revision", None) or 1)
    db.add(execution)
    db.add_all(units)
    return execution


async def current_execution(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    lock: bool = False,
) -> DeliverableExecution | None:
    query = select(DeliverableExecution).where(
        DeliverableExecution.tenant_id == request.tenant_id,
        DeliverableExecution.request_id == request.id,
    )
    if request.current_execution_id is not None:
        query = query.where(DeliverableExecution.id == request.current_execution_id)
    else:
        query = query.order_by(DeliverableExecution.execution_number.desc()).limit(1)
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    execution = result.scalar_one_or_none()
    if execution is not None and request.current_execution_id is None:
        request.current_execution_id = execution.id
    return execution


async def ensure_execution_shadow(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    lock: bool = False,
) -> DeliverableExecution:
    execution = await current_execution(db, request, lock=lock)
    if execution is not None:
        return execution
    execution = add_initial_execution_shadow(db, request)
    await db.flush()
    # ``deliverable_requests.current_execution_id`` points at the execution
    # row.  The execution must be inserted before the request can safely be
    # updated with that foreign key; assigning it before the first flush makes
    # PostgreSQL reject the insert in the dual-write path.
    request.current_execution_id = execution.id
    await db.flush()
    return execution


def record_execution_preflight(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    preflight: Mapping[str, Any],
) -> None:
    snapshot = safe_preflight_snapshot(preflight)
    request.latest_preflight = snapshot
    execution.preflight_snapshot = snapshot
    if bool(preflight.get("launchable")):
        execution.status = "ready"
        execution.blocked_reason = None
    else:
        execution.status = "blocked"
        reasons = preflight.get("reasons")
        first_reason = reasons[0] if isinstance(reasons, list) and reasons else None
        execution.blocked_reason = str(first_reason or "deliverable_capability_unavailable")[:200]


async def attach_execution_run(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    launched_at: datetime,
) -> DeliverableExecution:
    execution = await ensure_execution_shadow(db, request, lock=True)
    if execution.intake_run_id is not None and execution.intake_run_id != run_id:
        raise DeliverableExecutionError(
            "deliverable_execution_run_mismatch",
            "The active deliverable execution is already linked to another run",
        )
    execution.intake_run_id = run_id
    execution.launch_message_id = message_id
    execution.status = "running"
    execution.current_stage = "running"
    execution.launched_at = launched_at
    return execution


async def project_execution_lifecycle(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    now: datetime | None = None,
) -> DeliverableExecution | None:
    """Mirror the compatibility Request state without making shadow authoritative."""

    execution = await current_execution(db, request, lock=True)
    if execution is None:
        return None
    timestamp = now or datetime.now(UTC)
    execution.current_stage = request.current_stage
    execution.last_error_code = request.last_error_code
    mapping = {
        "ready": "ready",
        "running": "running",
        "waiting_approval": "waiting_approval",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    execution.status = mapping.get(request.status, execution.status)
    if execution.status in {"succeeded", "failed", "cancelled"}:
        execution.completed_at = request.completed_at or timestamp

    units = await execution_units(db, execution.id, lock=True)
    active_statuses = {"pending", "running", "blocked", "reconciling"}
    has_verified_output = any(unit.status == "succeeded" for unit in units)
    # The blanket success projection is a v1-runtime compatibility bridge and
    # is only valid at final output review.  Mid-pipeline approval stages (v2
    # storyboard review) keep their per-unit truth untouched.
    if (
        request.status == "waiting_approval"
        and has_verified_output
        and request.current_stage == "output_review"
    ):
        projected_status = "succeeded"
        evidence_level = "projected_v1_runtime_completion"
    elif request.status == "succeeded":
        projected_status = "succeeded"
        evidence_level = "projected_v1_final_approval"
    elif request.status == "failed":
        projected_status = "failed"
        evidence_level = "projected_v1_runtime_failure"
    elif request.status == "cancelled":
        projected_status = "cancelled"
        evidence_level = "projected_v1_runtime_cancellation"
    else:
        projected_status = None
        evidence_level = None

    if projected_status is not None and evidence_level is not None:
        for unit in units:
            if unit.status not in active_statuses:
                continue
            unit.status = projected_status
            unit.completed_at = timestamp
            unit.last_error_code = (
                request.last_error_code
                if projected_status == "failed"
                else None
            )
            unit.result_snapshot = {
                **dict(unit.result_snapshot or {}),
                "lifecycle_projection": {
                    "version": 1,
                    "evidence_level": evidence_level,
                    "request_status": request.status,
                    "request_stage": request.current_stage,
                    "verified_output_gate": has_verified_output,
                },
            }
    return execution


_ARTIFACT_UNIT = {
    ("presentation", "pptx"): ("pptx_render", "deck"),
    ("presentation", "pdf"): ("pdf_render", "deck"),
    ("poster", "png"): ("deterministic_compose", "final"),
    ("video", "mp4"): ("edit_compose", "final"),
    ("report", "pdf"): ("artifact_render", "document"),
    ("spreadsheet", "xlsx"): ("artifact_render", "document"),
}


async def bind_artifacts_to_current_execution(
    db: AsyncSession,
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    now: datetime | None = None,
) -> None:
    execution = await current_execution(db, request, lock=True)
    if execution is None:
        return
    result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(DeliverableExecutionUnit.execution_id == execution.id)
        .with_for_update()
    )
    units = {
        (unit.stage_key, unit.unit_key): unit
        for unit in result.scalars().all()
    }
    timestamp = now or datetime.now(UTC)
    for artifact in artifacts:
        stage_unit = _ARTIFACT_UNIT.get((request.work_type, artifact.artifact_type))
        if stage_unit is None:
            continue
        unit = units.get(stage_unit)
        artifact.execution_id = execution.id
        artifact.stage_key, artifact.unit_key = stage_unit
        if unit is not None:
            artifact.unit_id = unit.id
            unit.status = "succeeded"
            unit.completed_at = timestamp
            unit.last_error_code = None
            result_snapshot = {
                "version": 1,
                "artifact_revision_id": str(artifact.id),
                "artifact_key": artifact.artifact_key,
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
            }
            # FR-P7: surface the font substitution mapping on the render unit
            # so output review and revision history can display it.
            evaluation = artifact.evaluation if isinstance(artifact.evaluation, Mapping) else {}
            facts = evaluation.get("facts") if isinstance(evaluation.get("facts"), Mapping) else {}
            font_substitutions = facts.get("font_substitutions")
            if isinstance(font_substitutions, list) and font_substitutions:
                result_snapshot["font_substitutions"] = font_substitutions
            unit.result_snapshot = result_snapshot


async def execution_units(
    db: AsyncSession,
    execution_id: uuid.UUID,
    *,
    lock: bool = False,
) -> tuple[DeliverableExecutionUnit, ...]:
    query = (
        select(DeliverableExecutionUnit)
        .where(DeliverableExecutionUnit.execution_id == execution_id)
        .order_by(
            DeliverableExecutionUnit.created_at,
            DeliverableExecutionUnit.stage_key,
            DeliverableExecutionUnit.unit_key,
        )
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    return tuple(result.scalars().all())


async def create_revision_execution(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    client_revision_id: uuid.UUID,
    instruction: str,
    target_units: Sequence[str] = (),
    revision_stage: str | None = None,
) -> tuple[DeliverableExecution, bool]:
    """Create a new execution without mutating prior run or Artifact facts."""

    cleaned_instruction = instruction.strip()
    if not cleaned_instruction:
        raise DeliverableExecutionError(
            "deliverable_revision_instruction_required",
            "A revision instruction is required",
        )
    normalized_targets = tuple(dict.fromkeys(item.strip() for item in target_units if item.strip()))
    current = await ensure_execution_shadow(db, request, lock=True)
    revision_contract = execution_contract_snapshot(
        request,
        revision_instruction=cleaned_instruction,
        target_units=normalized_targets,
        revision_stage=revision_stage,
    )
    revision_fingerprint = _canonical_sha256(revision_contract)
    existing_result = await db.execute(
        select(DeliverableExecution).where(
            DeliverableExecution.request_id == request.id,
            DeliverableExecution.idempotency_key == client_revision_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != revision_fingerprint:
            raise DeliverableExecutionError(
                "deliverable_revision_id_reused",
                "client_revision_id was already used for a different revision",
            )
        return existing, False

    current_units = await execution_units(db, current.id, lock=True)
    known_targets = {unit.unit_key for unit in current_units}
    unknown_targets = sorted(set(normalized_targets) - known_targets)
    if unknown_targets:
        raise DeliverableExecutionError(
            "deliverable_revision_target_invalid",
            "Unknown revision targets: " + ", ".join(unknown_targets),
        )

    current.status = "succeeded"
    current.current_stage = "revision_requested"
    current.completed_at = datetime.now(UTC)
    for unit in current_units:
        if unit.status in {"pending", "running", "blocked", "reconciling"}:
            unit.status = "superseded"
            unit.completed_at = current.completed_at
            unit.last_error_code = None
            unit.result_snapshot = {
                **dict(unit.result_snapshot or {}),
                "lifecycle_projection": {
                    "version": 1,
                    "evidence_level": "superseded_by_customer_revision",
                },
            }
    # Release the partial unique "one active execution" slot before adding
    # the replacement execution in the same transaction.
    await db.flush()
    request.contract_revision = int(getattr(request, "contract_revision", None) or 1) + 1
    execution, units = build_execution_shadow(
        request,
        execution_number=current.execution_number + 1,
        kind="revision",
        idempotency_key=client_revision_id,
        current_stage="revision_ready",
        revision_instruction=cleaned_instruction,
        target_units=normalized_targets,
        revision_stage=revision_stage,
    )
    # ``build_execution_shadow`` snapshots the incremented contract revision.
    if execution.request_fingerprint != revision_fingerprint:
        revision_fingerprint = execution.request_fingerprint

    artifact_result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
            DeliverableArtifactRevision.request_id == request.id,
            DeliverableArtifactRevision.status == "candidate",
        )
        .with_for_update()
    )
    for artifact in artifact_result.scalars().all():
        artifact.status = "rejected"

    request.current_execution_id = execution.id
    request.agent_run_id = None
    request.launch_message_id = None
    request.status = "ready"
    request.current_stage = "revision_ready"
    request.completed_at = None
    request.last_error_code = None
    request.version += 1
    db.add(execution)
    db.add_all(units)
    return execution, True


__all__ = [
    "DeliverableExecutionError",
    "ExecutionUnitBlueprint",
    "add_initial_execution_shadow",
    "attach_execution_run",
    "bind_artifacts_to_current_execution",
    "build_execution_shadow",
    "create_revision_execution",
    "current_execution",
    "ensure_execution_shadow",
    "execution_contract_snapshot",
    "execution_unit_blueprints",
    "execution_units",
    "project_execution_lifecycle",
    "record_execution_preflight",
    "safe_preflight_snapshot",
]
