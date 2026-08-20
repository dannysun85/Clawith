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
        output_types = {
            str(item).strip().lower() for item in request.output_contract or ()
        }
        stages = ["deck_assemble"]
        if "pptx" in output_types:
            stages.append("pptx_render")
        if "pdf" in output_types:
            stages.append("pdf_render")
        stages.extend(("structural_qa", "semantic_qa", "visual_qa"))
        if {"pptx", "pdf"} <= output_types:
            stages.append("pptx_pdf_parity")
        for stage in stages:
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


_ARTIFACT_UNIT = {
    ("presentation", "pptx"): ("pptx_render", "deck"),
    ("presentation", "pdf"): ("pdf_render", "deck"),
    ("poster", "png"): ("deterministic_compose", "final"),
    ("video", "mp4"): ("edit_compose", "final"),
    ("report", "pdf"): ("artifact_render", "document"),
    ("spreadsheet", "xlsx"): ("artifact_render", "document"),
}

_V1_COMPATIBILITY_WORKFLOWS = {
    "builtin.presentation.v1",
    "builtin.poster.v1",
    "builtin.video.v1",
}


def _required_artifact_units(
    request: DeliverableRequest,
) -> tuple[tuple[str, str], ...]:
    required: list[tuple[str, str]] = []
    for artifact_type in request.output_contract or ():
        unit = _ARTIFACT_UNIT.get(
            (request.work_type, str(artifact_type).strip().lower())
        )
        if unit is None or unit in required:
            continue
        required.append(unit)
    return tuple(required)


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
    else:
        # A failed execution can later recover when a delayed Tool write or a
        # bounded follow-up run supplies the missing artifact.  A non-terminal
        # execution must not keep the terminal timestamp from the old failure.
        execution.completed_at = None

    units = await execution_units(db, execution.id, lock=True)
    active_statuses = {"pending", "running", "blocked", "reconciling"}
    required_artifact_units = set(_required_artifact_units(request))
    succeeded_unit_keys = {
        (unit.stage_key, unit.unit_key)
        for unit in units
        if unit.status == "succeeded"
    }
    has_verified_output = bool(required_artifact_units) and (
        required_artifact_units <= succeeded_unit_keys
    )
    is_v1_compatibility_workflow = (
        request.workflow_id in _V1_COMPATIBILITY_WORKFLOWS
    )
    # The blanket success projection is a v1-runtime compatibility bridge and
    # is only valid at final output review.  Mid-pipeline approval stages (v2
    # storyboard review) keep their per-unit truth untouched.
    if (
        is_v1_compatibility_workflow
        and request.status == "waiting_approval"
        and has_verified_output
        and request.current_stage == "output_review"
    ):
        projected_status = "succeeded"
        evidence_level = "projected_v1_runtime_completion"
    elif is_v1_compatibility_workflow and request.status == "succeeded":
        projected_status = "succeeded"
        evidence_level = "projected_v1_final_approval"
    elif is_v1_compatibility_workflow and request.status == "failed":
        projected_status = "failed"
        evidence_level = "projected_v1_runtime_failure"
    elif is_v1_compatibility_workflow and request.status == "cancelled":
        projected_status = "cancelled"
        evidence_level = "projected_v1_runtime_cancellation"
    else:
        projected_status = None
        evidence_level = None

    if projected_status is not None and evidence_level is not None:
        for unit in units:
            recovered_from_failure = (
                projected_status == "succeeded" and unit.status == "failed"
            )
            if unit.status not in active_statuses and not recovered_from_failure:
                continue
            previous_error_code = unit.last_error_code
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
                    "recovered_from_failure": recovered_from_failure,
                    "previous_error_code": (
                        previous_error_code if recovered_from_failure else None
                    ),
                },
            }
    return execution


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
    if (
        request.work_type == "presentation"
        and request.workflow_id == "builtin.presentation.v2"
    ):
        _bind_presentation_v2_artifact_evidence(
            request,
            units,
            artifacts,
            timestamp=timestamp,
        )


def _bind_presentation_v2_artifact_evidence(
    request: DeliverableRequest,
    units: Mapping[tuple[str, str], DeliverableExecutionUnit],
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    timestamp: datetime,
) -> None:
    """Close v2 deck units only from verified artifact and QA receipts.

    The v1 lifecycle bridge projects a request-level terminal status.  V2 must
    instead preserve page/stage truth: each unit below receives the precise
    artifact or QA fact that satisfied it.  Two legacy no-op units are handled
    explicitly because executions created before output-aware blueprints may
    still contain PDF/parity rows for a PPTX-only contract.
    """

    verified: dict[str, tuple[DeliverableArtifactRevision, Mapping[str, Any]]] = {}
    for artifact in artifacts:
        evaluation = (
            artifact.evaluation
            if isinstance(artifact.evaluation, Mapping)
            else {}
        )
        facts = evaluation.get("facts")
        if evaluation.get("verified") is True and isinstance(facts, Mapping):
            verified[artifact.artifact_type] = (artifact, facts)

    output_types = {
        str(item).strip().lower() for item in request.output_contract or ()
    }

    def satisfy(
        stage_key: str,
        unit_key: str,
        *,
        evidence_level: str,
        evidence: Mapping[str, Any],
    ) -> None:
        unit = units.get((stage_key, unit_key))
        if unit is None or unit.status in {"cancelled", "superseded"}:
            return
        unit.status = "succeeded"
        unit.completed_at = timestamp
        unit.last_error_code = None
        unit.result_snapshot = {
            **dict(unit.result_snapshot or {}),
            "presentation_v2_evidence": {
                "version": 1,
                "evidence_level": evidence_level,
                **dict(evidence),
            },
        }

    # Compatibility for executions materialized before the blueprint became
    # output-contract aware.  These rows represent satisfied no-op contracts,
    # not provider work, and carry that distinction in their durable receipt.
    for artifact_type, stage_key in (("pptx", "pptx_render"), ("pdf", "pdf_render")):
        if artifact_type not in output_types:
            satisfy(
                stage_key,
                "deck",
                evidence_level="not_applicable_output_contract",
                evidence={"artifact_type": artifact_type, "contracted": False},
            )
    if not {"pptx", "pdf"} <= output_types:
        satisfy(
            "pptx_pdf_parity",
            "deck",
            evidence_level="not_applicable_output_contract",
            evidence={
                "required_artifact_types": sorted(output_types),
                "parity_required": False,
            },
        )

    required_artifacts = {
        item for item in output_types if item in {"pptx", "pdf"}
    }
    if not required_artifacts or not required_artifacts <= verified.keys():
        return

    primary_type = "pptx" if "pptx" in verified else "pdf"
    primary_artifact, primary_facts = verified[primary_type]
    expected_pages = _slide_count(request)
    observed_pages = primary_facts.get("page_count")
    if not isinstance(observed_pages, int) or observed_pages != expected_pages:
        return
    artifact_evidence = {
        "artifact_revision_id": str(primary_artifact.id),
        "artifact_type": primary_type,
        "content_hash": primary_artifact.content_hash,
        "page_count": observed_pages,
    }

    # A page-targeted revision intentionally reuses the approved planning
    # files instead of re-running the outline stage.  The verified conversion
    # contract proves that both outline.json and slide_spec.json were present,
    # structurally valid, page-aligned, and used to build this exact artifact.
    # Record that evidence on the new execution so inherited planning work is
    # shown as complete instead of remaining misleadingly pending.
    if primary_facts.get("slide_spec_gate") == 1:
        for stage_key in ("outline", "slide_spec"):
            satisfy(
                stage_key,
                "deck",
                evidence_level="verified_planning_contract",
                evidence={**artifact_evidence, "slide_spec_gate": 1},
            )

    for index in range(1, observed_pages + 1):
        satisfy(
            "slide_render",
            f"slide-{index:02d}",
            evidence_level="verified_artifact_page",
            evidence={**artifact_evidence, "page_number": index},
        )
    satisfy(
        "deck_assemble",
        "deck",
        evidence_level="verified_artifact_assembly",
        evidence=artifact_evidence,
    )
    satisfy(
        "structural_qa",
        "deck",
        evidence_level="verified_artifact_contract",
        evidence={
            **artifact_evidence,
            "aspect_ratio": primary_facts.get("aspect_ratio"),
            "width": primary_facts.get("width"),
            "height": primary_facts.get("height"),
        },
    )

    pptx_entry = verified.get("pptx")
    visual_ok = pptx_entry is None
    visual_evidence: dict[str, Any] = dict(artifact_evidence)
    if pptx_entry is not None:
        pptx_artifact, pptx_facts = pptx_entry
        required_gates = (
            "slide_spec_gate",
            "density_gate",
            "data_slide_editability_gate",
        )
        visual_ok = all(pptx_facts.get(key) == 1 for key in required_gates)
        if "picture_coverage_gate" in pptx_facts:
            visual_ok = visual_ok and pptx_facts.get("picture_coverage_gate") == 1
        visual_evidence = {
            "artifact_revision_id": str(pptx_artifact.id),
            "artifact_type": "pptx",
            "content_hash": pptx_artifact.content_hash,
            **{key: pptx_facts.get(key) for key in required_gates},
            "picture_coverage_gate": pptx_facts.get("picture_coverage_gate"),
        }
    if visual_ok:
        satisfy(
            "visual_qa",
            "deck",
            evidence_level="verified_visual_contract",
            evidence=visual_evidence,
        )

    semantic_unit = units.get(("semantic_qa", "deck"))
    semantic_evaluation = (
        semantic_unit.quality_evaluation
        if semantic_unit is not None
        and isinstance(semantic_unit.quality_evaluation, Mapping)
        else {}
    )
    semantic_report = semantic_evaluation.get("semantic_qa")
    if isinstance(semantic_report, Mapping) and semantic_report.get("status") == "passed":
        subject_similarity = semantic_report.get("subject_similarity")
        inventory_sha256 = (
            subject_similarity.get("inventory_sha256")
            if isinstance(subject_similarity, Mapping)
            else None
        )
        if isinstance(inventory_sha256, str) and inventory_sha256.strip():
            satisfy(
                "source_inventory",
                "deck",
                evidence_level="semantic_inventory_receipt",
                evidence={
                    "inventory_sha256": inventory_sha256,
                    "slide_spec_sha256": semantic_report.get("artifact_sha256"),
                    "assertion_count": subject_similarity.get("assertion_count"),
                    "assumption_count": subject_similarity.get("assumption_count"),
                },
            )
        satisfy(
            "semantic_qa",
            "deck",
            evidence_level="semantic_qa_receipt",
            evidence={
                "schema_version": semantic_report.get("schema_version"),
                "artifact_sha256": semantic_report.get("artifact_sha256"),
                "score": semantic_report.get("score"),
                "enforcement": semantic_evaluation.get("enforcement"),
            },
        )

    if {"pptx", "pdf"} <= output_types:
        _pdf_artifact, pdf_facts = verified["pdf"]
        if pdf_facts.get("visual_consistency_gate") == 1:
            satisfy(
                "pptx_pdf_parity",
                "deck",
                evidence_level="verified_pptx_pdf_parity",
                evidence={
                    "page_count": observed_pages,
                    "visual_consistency_gate": 1,
                    "pptx_content_hash": verified["pptx"][0].content_hash,
                    "pdf_content_hash": verified["pdf"][0].content_hash,
                },
            )


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

    revision_requested_at = datetime.now(UTC)
    if current.status != "failed":
        current.status = "succeeded"
        current.current_stage = "revision_requested"
        current.completed_at = revision_requested_at
    elif current.completed_at is None:
        # A retry creates a new execution but must not rewrite the failed
        # execution into success.  Its stage and error remain the historical
        # explanation for why the user regenerated the deliverable.
        current.completed_at = revision_requested_at
    for unit in current_units:
        if unit.status in {"pending", "running", "blocked", "reconciling"}:
            unit.status = "superseded"
            unit.completed_at = revision_requested_at
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
