"""Candidate selection receipts for v2 poster deliverables (FR-I6).

The default selection is automatic: the highest-scoring QA-passed candidate
wins, and the receipt binds the per-candidate scores, artifact hashes, and
Credits facts at decision time.  A user may re-select another QA-passed
candidate at output review through the existing approvals API; both paths
write immutable, idempotent ``deliverable_selection_receipts`` rows.

This module is provider-free: it only reads durable unit/QA/ledger facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
    DeliverableSelectionReceipt,
)
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditReservation
from app.services.deliverable_executions import DeliverableExecutionError


SELECTION_SCHEMA_VERSION = "selection-receipt-v1"
POSTER_V2_WORKFLOW_ID = "builtin.poster.v2"

_AUTO_SELECTION_NAMESPACE = uuid.UUID("7d4f1c2e-5b6a-4c8d-9e0f-1a2b3c4d5e6f")


class CandidateScore(BaseModel):
    """One candidate's selection facts, hash-bound to its artifact bytes."""

    unit_key: str = Field(min_length=1, max_length=120)
    qa_status: Literal["passed", "failed", "unevaluated"]
    score: int | None = Field(default=None, ge=0, le=100)
    artifact_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    artifact_path: str | None = Field(default=None, max_length=1000)
    eligible: bool

    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_scoreboard(
    units: Sequence[DeliverableExecutionUnit],
    *,
    enforcement: str = "shadow",
) -> tuple[CandidateScore, ...]:
    """Build the selection scoreboard from durable unit facts (pure).

    A candidate is selectable only when its automated QA passed.  Shadow mode
    keeps delivery unblocked by marking QA-failed candidates as fallback
    candidates; the caller decides whether a fallback may be auto-selected
    (it may never be user-selected).
    """

    generate_units: dict[str, DeliverableExecutionUnit] = {}
    qa_by_key: dict[str, DeliverableExecutionUnit] = {}
    for unit in units:
        if unit.stage_key == "candidate_generate":
            generate_units[unit.unit_key] = unit
        elif unit.stage_key == "candidate_qa":
            qa_by_key[unit.unit_key] = unit
    board: list[CandidateScore] = []
    for unit_key in sorted(generate_units):
        generate_unit = generate_units[unit_key]
        result_snapshot = (
            generate_unit.result_snapshot
            if isinstance(generate_unit.result_snapshot, Mapping)
            else {}
        )
        qa_unit = qa_by_key.get(unit_key)
        evaluation = (
            qa_unit.quality_evaluation
            if qa_unit is not None and isinstance(qa_unit.quality_evaluation, Mapping)
            else {}
        )
        report = evaluation.get("candidate_qa")
        report = report if isinstance(report, Mapping) else {}
        raw_status = str(report.get("status") or "")
        qa_status: Literal["passed", "failed", "unevaluated"] = (
            "passed"
            if raw_status == "passed"
            else "failed"
            if raw_status == "failed"
            else "unevaluated"
        )
        raw_score = report.get("score")
        score = int(raw_score) if isinstance(raw_score, int) and not isinstance(raw_score, bool) else None
        raw_hash = str(report.get("artifact_sha256") or "")
        artifact_sha256 = raw_hash if len(raw_hash) == 64 else None
        artifact_path = str(result_snapshot.get("candidate_artifact_path") or "") or None
        has_artifact = generate_unit.status == "succeeded" and artifact_path is not None
        if not has_artifact:
            eligible = False
        elif qa_status == "passed":
            eligible = True
        else:
            # Shadow keeps QA advisory; enforcing only ever selects QA-passed
            # candidates.  Unevaluated candidates are never eligible.
            eligible = enforcement != "enforcing" and qa_status == "failed"
        board.append(
            CandidateScore(
                unit_key=unit_key,
                qa_status=qa_status,
                score=score,
                artifact_sha256=artifact_sha256,
                artifact_path=artifact_path,
                eligible=eligible,
            )
        )
    return tuple(board)


def select_default_candidate(
    scoreboard: Sequence[CandidateScore],
    *,
    enforcement: str = "shadow",
) -> tuple[CandidateScore | None, str]:
    """FR-I6 default: the QA-passed candidate with the highest score wins.

    Ties break toward the lowest unit key for determinism.  In shadow mode an
    all-QA-failed batch falls back to the best-scoring evaluated candidate so
    advisory QA never blocks delivery; enforcing mode selects nothing when no
    candidate passed.
    """

    evaluated = [entry for entry in scoreboard if entry.qa_status != "unevaluated"]
    passed = [entry for entry in evaluated if entry.qa_status == "passed" and entry.eligible]
    if passed:
        # Highest score wins; ties break toward the lowest unit key so the
        # default selection is fully deterministic.
        winner = min(
            passed,
            key=lambda entry: (
                -(entry.score if entry.score is not None else -1),
                entry.unit_key,
            ),
        )
        return winner, "auto_top_qa_score"
    if enforcement == "enforcing":
        return None, "no_qa_passed_candidate"
    fallback = [entry for entry in scoreboard if entry.eligible]
    if not fallback:
        return None, "no_qa_passed_candidate"
    winner = min(
        fallback,
        key=lambda entry: (
            -(entry.score if entry.score is not None else -1),
            entry.unit_key,
        ),
    )
    return winner, "auto_shadow_fallback_no_qa_passed"


def auto_client_selection_id(execution_id: uuid.UUID) -> uuid.UUID:
    """Deterministic idempotency key for the automatic selection receipt."""

    return uuid.uuid5(_AUTO_SELECTION_NAMESPACE, f"{execution_id}:auto")


async def _selection_cost_breakdown(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    candidate_unit_ids: Mapping[str, uuid.UUID],
) -> dict[str, Any]:
    """Per-candidate Credits facts from the durable reservation ledger."""

    unit_ids = tuple(candidate_unit_ids.values())
    if not unit_ids:
        return {"candidates": [], "reserved_credits_total": 0}
    task_result = await db.execute(
        select(MediaGenerationTask).where(
            MediaGenerationTask.tenant_id == request.tenant_id,
            MediaGenerationTask.deliverable_unit_id.in_(unit_ids),
        )
    )
    tasks_by_unit: dict[uuid.UUID, MediaGenerationTask] = {}
    for task in task_result.scalars().all():
        if task.deliverable_unit_id is not None:
            tasks_by_unit[task.deliverable_unit_id] = task
    reservation_ids = tuple(
        dict.fromkeys(
            task.reservation_id for task in tasks_by_unit.values() if task.reservation_id
        )
    )
    reservations: dict[uuid.UUID, CreditReservation] = {}
    if reservation_ids:
        reservation_result = await db.execute(
            select(CreditReservation).where(CreditReservation.id.in_(reservation_ids))
        )
        reservations = {row.id: row for row in reservation_result.scalars().all()}
    entries: list[dict[str, Any]] = []
    reserved_total = 0
    for unit_key in sorted(candidate_unit_ids):
        task = tasks_by_unit.get(candidate_unit_ids[unit_key])
        reservation = (
            reservations.get(task.reservation_id)
            if task is not None and task.reservation_id is not None
            else None
        )
        amount = int(reservation.amount or 0) if reservation is not None else 0
        reserved_total += amount
        entries.append(
            {
                "unit_key": unit_key,
                "media_generation_task_id": str(task.id) if task is not None else None,
                "reservation_id": str(reservation.id) if reservation is not None else None,
                "reservation_status": reservation.status if reservation is not None else None,
                "reserved_credits": amount,
            }
        )
    return {"candidates": entries, "reserved_credits_total": reserved_total}


async def _current_poster_v2_execution(
    db: AsyncSession,
    request: DeliverableRequest,
) -> DeliverableExecution | None:
    if request.workflow_id != POSTER_V2_WORKFLOW_ID or request.current_execution_id is None:
        return None
    result = await db.execute(
        select(DeliverableExecution).where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.id == request.current_execution_id,
        )
    )
    return result.scalar_one_or_none()


async def _execution_units(
    db: AsyncSession,
    execution: DeliverableExecution,
) -> tuple[DeliverableExecutionUnit, ...]:
    result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == execution.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
        )
        .with_for_update()
    )
    return tuple(result.scalars().all())


def _selection_unit(
    units: Sequence[DeliverableExecutionUnit],
) -> DeliverableExecutionUnit | None:
    return next(
        (
            unit
            for unit in units
            if unit.stage_key == "selection" and unit.unit_key == "final"
        ),
        None,
    )


def _record_selection_unit(
    units: Sequence[DeliverableExecutionUnit],
    *,
    receipt: DeliverableSelectionReceipt,
    selected: CandidateScore,
    now: datetime,
) -> None:
    unit = _selection_unit(units)
    if unit is None:
        return
    unit.status = "succeeded"
    unit.completed_at = now
    unit.last_error_code = None
    unit.result_snapshot = {
        **dict(unit.result_snapshot or {}),
        "selection_schema_version": SELECTION_SCHEMA_VERSION,
        "selection_receipt_id": str(receipt.id),
        "selected_unit_key": selected.unit_key,
        "selected_artifact_sha256": selected.artifact_sha256,
        "selection_actor": receipt.actor,
    }


async def ensure_auto_selection(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    enforcement: str = "shadow",
    now: datetime | None = None,
) -> DeliverableSelectionReceipt | None:
    """Record the default QA-driven selection once per execution.

    Idempotent via the deterministic ``client_selection_id``; a replay returns
    the existing receipt without changing it.  Returns ``None`` when no
    candidate has been evaluated yet or no candidate is selectable.
    """

    execution = await _current_poster_v2_execution(db, request)
    if execution is None:
        return None
    client_selection_id = auto_client_selection_id(execution.id)
    existing_result = await db.execute(
        select(DeliverableSelectionReceipt).where(
            DeliverableSelectionReceipt.tenant_id == request.tenant_id,
            DeliverableSelectionReceipt.request_id == request.id,
            DeliverableSelectionReceipt.client_selection_id == client_selection_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing
    units = await _execution_units(db, execution)
    board = candidate_scoreboard(units, enforcement=enforcement)
    if not any(entry.qa_status != "unevaluated" for entry in board):
        return None
    selected, reason = select_default_candidate(board, enforcement=enforcement)
    if selected is None:
        return None
    timestamp = now or datetime.now(UTC)
    candidate_unit_ids = {
        unit.unit_key: unit.id
        for unit in units
        if unit.stage_key == "candidate_generate"
    }
    receipt = DeliverableSelectionReceipt(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        actor_user_id=None,
        selected_unit_key=selected.unit_key,
        candidate_scores=[entry.model_dump(mode="json") for entry in board],
        selection_reason=reason,
        cost_breakdown=await _selection_cost_breakdown(
            db,
            request=request,
            candidate_unit_ids=candidate_unit_ids,
        ),
        actor="auto",
        client_selection_id=client_selection_id,
        request_fingerprint=_canonical_sha256(
            {
                "schema_version": SELECTION_SCHEMA_VERSION,
                "execution_id": str(execution.id),
                "actor": "auto",
                "selected_unit_key": selected.unit_key,
            }
        ),
    )
    db.add(receipt)
    _record_selection_unit(units, receipt=receipt, selected=selected, now=timestamp)
    await db.flush()
    return receipt


async def apply_user_selection(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    selected_unit_key: str,
    actor_user_id: uuid.UUID,
    client_selection_id: uuid.UUID,
    now: datetime | None = None,
) -> DeliverableSelectionReceipt:
    """Record a user re-selection of another QA-passed candidate.

    Only QA-passed candidates are user-selectable in every enforcement mode.
    The idempotency key is the approval action id: a replay returns the stored
    receipt, and a reused key with a different selection fails closed.
    """

    execution = await _current_poster_v2_execution(db, request)
    if execution is None:
        raise DeliverableExecutionError(
            "deliverable_selection_not_available",
            "Candidate selection requires a v2 poster execution",
        )
    fingerprint = _canonical_sha256(
        {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "execution_id": str(execution.id),
            "actor": "user",
            "selected_unit_key": selected_unit_key,
        }
    )
    existing_result = await db.execute(
        select(DeliverableSelectionReceipt).where(
            DeliverableSelectionReceipt.tenant_id == request.tenant_id,
            DeliverableSelectionReceipt.request_id == request.id,
            DeliverableSelectionReceipt.client_selection_id == client_selection_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise DeliverableExecutionError(
                "deliverable_selection_id_reused",
                "client_action_id was already used for a different selection",
            )
        return existing
    units = await _execution_units(db, execution)
    board = candidate_scoreboard(units, enforcement="enforcing")
    selected = next(
        (entry for entry in board if entry.unit_key == selected_unit_key),
        None,
    )
    if selected is None:
        raise DeliverableExecutionError(
            "deliverable_selection_candidate_unknown",
            f"Unknown candidate unit: {selected_unit_key}",
        )
    if not selected.eligible:
        raise DeliverableExecutionError(
            "deliverable_selection_candidate_ineligible",
            "Only QA-passed candidates can be selected for delivery",
        )
    timestamp = now or datetime.now(UTC)
    candidate_unit_ids = {
        unit.unit_key: unit.id
        for unit in units
        if unit.stage_key == "candidate_generate"
    }
    receipt = DeliverableSelectionReceipt(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        actor_user_id=actor_user_id,
        selected_unit_key=selected.unit_key,
        candidate_scores=[entry.model_dump(mode="json") for entry in board],
        selection_reason="user_selected_at_output_review",
        cost_breakdown=await _selection_cost_breakdown(
            db,
            request=request,
            candidate_unit_ids=candidate_unit_ids,
        ),
        actor="user",
        client_selection_id=client_selection_id,
        request_fingerprint=fingerprint,
    )
    db.add(receipt)
    _record_selection_unit(units, receipt=receipt, selected=selected, now=timestamp)
    await db.flush()
    return receipt


async def latest_selection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    execution_id: uuid.UUID | None = None,
) -> DeliverableSelectionReceipt | None:
    """Newest selection receipt for one request (optionally one execution)."""

    query = select(DeliverableSelectionReceipt).where(
        DeliverableSelectionReceipt.tenant_id == tenant_id,
        DeliverableSelectionReceipt.request_id == request_id,
    )
    if execution_id is not None:
        query = query.where(DeliverableSelectionReceipt.execution_id == execution_id)
    result = await db.execute(
        query.order_by(
            DeliverableSelectionReceipt.created_at.desc(),
            DeliverableSelectionReceipt.id.desc(),
        ).limit(1)
    )
    return result.scalar_one_or_none()


__all__ = [
    "POSTER_V2_WORKFLOW_ID",
    "SELECTION_SCHEMA_VERSION",
    "CandidateScore",
    "apply_user_selection",
    "auto_client_selection_id",
    "candidate_scoreboard",
    "ensure_auto_selection",
    "latest_selection",
    "select_default_candidate",
]
