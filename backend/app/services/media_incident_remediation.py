"""Auditable, idempotent remediation for exact media-generation task IDs."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.audit import AuditLog
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditReservation
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    grant_credits_in_session,
    mark_credit_reservation_settlement_ready_in_session,
    release_reserved_credits_in_session,
)
from app.services.media_generation import (
    TERMINAL_MEDIA_STATUSES,
    _delete_private_media_recovery_assets,
    _finalize_failure_in_session,
    _lock_owned_media_reservation,
)


PROVIDER_DEBT_RESOLUTIONS = {
    "provider_rejected",
    "provider_accepted",
    "close_asset_loss",
}


@dataclass(frozen=True)
class MediaRemediationItem:
    task_id: str
    tenant_id: str | None
    status_before: str
    status_after: str
    reservation_id: str | None
    reservation_status_before: str | None
    reservation_amount: int
    compensation_credits: int = 0


@dataclass(frozen=True)
class MediaRemediationResult:
    incident_key: str
    applied: bool
    items: tuple[MediaRemediationItem, ...]
    resolution: str | None = None
    evidence_ref: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_task_ids(task_ids: tuple[uuid.UUID, ...]) -> tuple[uuid.UUID, ...]:
    unique = tuple(dict.fromkeys(task_ids))
    if not unique:
        raise ValueError("at least one exact task_id is required")
    if len(unique) > 100:
        raise ValueError("at most 100 exact task IDs may be remediated at once")
    return unique


def _validate_task_scope(
    tasks: list[MediaGenerationTask],
    task_ids: tuple[uuid.UUID, ...],
    expected_tenant_id: uuid.UUID | None,
) -> None:
    found = {task.id for task in tasks}
    missing = [str(task_id) for task_id in task_ids if task_id not in found]
    if missing:
        raise ValueError(f"media task not found: {', '.join(missing)}")
    if expected_tenant_id and any(task.tenant_id != expected_tenant_id for task in tasks):
        raise ValueError("one or more media tasks are outside the expected tenant")
    if any(task.status == "succeeded" for task in tasks):
        raise ValueError("a succeeded media task must never be remediated as failed")


def _validate_refundable_state(
    task: MediaGenerationTask,
    reservation: CreditReservation | None,
) -> None:
    """Reject any state that may already represent provider-side debt."""
    if (
        task.status
        in {
            "submission_ambiguous",
            "asset_repairing",
            "asset_delivery_failed",
            "settlement_ready",
        }
        or (
            reservation is not None
            and reservation.status
            in {"provider_inflight", "settlement_ready", "finalized"}
        )
    ):
        raise ValueError(
            "provider-accepted media debt cannot be remediated as a "
            "refundable failure; repair or settle it explicitly"
        )


async def remediate_media_tasks(
    *,
    task_ids: tuple[uuid.UUID, ...],
    incident_key: str,
    expected_tenant_id: uuid.UUID | None = None,
    apply: bool = False,
) -> MediaRemediationResult:
    """Fail exact tasks and release reservations using existing row-lock logic."""

    normalized_ids = _normalize_task_ids(task_ids)
    normalized_incident_key = incident_key.strip()
    if not normalized_incident_key:
        raise ValueError("incident_key is required")

    async with async_session() as db:
        query = select(MediaGenerationTask).where(MediaGenerationTask.id.in_(normalized_ids))
        if apply:
            query = query.with_for_update()
        result = await db.execute(query)
        tasks = list(result.scalars().all())
        _validate_task_scope(tasks, normalized_ids, expected_tenant_id)
        task_by_id = {task.id: task for task in tasks}

        items: list[MediaRemediationItem] = []
        for task_id in normalized_ids:
            task = task_by_id[task_id]
            reservation = None
            if task.reservation_id:
                reservation = await _lock_owned_media_reservation(
                    db,
                    task,
                    with_for_update=apply,
                )

            if apply:
                _validate_refundable_state(task, reservation)

            status_before = task.status
            reservation_status_before = reservation.status if reservation else None
            if apply and task.status not in TERMINAL_MEDIA_STATUSES:
                await _finalize_failure_in_session(
                    db,
                    task,
                    f"Production incident remediation: {normalized_incident_key}",
                    {"remediation": normalized_incident_key},
                )
            items.append(MediaRemediationItem(
                task_id=str(task.id),
                tenant_id=str(task.tenant_id) if task.tenant_id else None,
                status_before=status_before,
                status_after=task.status if apply else status_before,
                reservation_id=str(task.reservation_id) if task.reservation_id else None,
                reservation_status_before=reservation_status_before,
                reservation_amount=int(reservation.amount) if reservation else 0,
            ))

        if apply:
            db.add(AuditLog(
                action="media_incident_remediation",
                details={
                    "incident_key": normalized_incident_key,
                    "task_ids": [str(task_id) for task_id in normalized_ids],
                    "expected_tenant_id": str(expected_tenant_id) if expected_tenant_id else None,
                },
            ))
            await db.commit()

        return MediaRemediationResult(
            incident_key=normalized_incident_key,
            applied=apply,
            items=tuple(items),
        )


def _validate_provider_debt_state(
    task: MediaGenerationTask,
    reservation: CreditReservation | None,
    resolution: str,
) -> bool:
    """Validate an exact evidence-backed resolution; return True for idempotent no-op."""
    if resolution not in PROVIDER_DEBT_RESOLUTIONS:
        raise ValueError("unsupported provider debt resolution")

    if resolution == "provider_rejected":
        if task.status == "failed" and reservation and reservation.status in {"released", "expired"}:
            return True
        if task.status != "submission_ambiguous":
            raise ValueError("provider_rejected requires a submission_ambiguous task")
        if not reservation or reservation.status != "provider_inflight":
            raise ValueError("provider_rejected requires a provider_inflight reservation")
        return False

    if task.status == "compensated" and (
        reservation is None or reservation.status == "finalized"
    ):
        return True
    if resolution == "provider_accepted":
        if task.status == "closed_nonrefundable" and (
            reservation is None or reservation.status == "finalized"
        ):
            # Repair rows closed by an older release without compensation.
            return False
        if task.status != "submission_ambiguous":
            raise ValueError("provider_accepted requires a submission_ambiguous task")
        if not reservation or reservation.status not in {"provider_inflight", "settlement_ready"}:
            raise ValueError(
                "provider_accepted requires a provider_inflight or settlement_ready reservation"
            )
        return False

    if task.status == "closed_nonrefundable" and (
        reservation is None or reservation.status == "finalized"
    ):
        # Repair rows closed by an older release without compensation.
        return False
    if task.status != "asset_delivery_failed":
        raise ValueError("close_asset_loss requires an asset_delivery_failed task")
    if reservation and reservation.status not in {"settlement_ready", "finalized"}:
        raise ValueError(
            "close_asset_loss requires an already settled or settlement_ready reservation"
        )
    return False


async def resolve_media_provider_debt(
    *,
    task_ids: tuple[uuid.UUID, ...],
    expected_tenant_id: uuid.UUID,
    incident_key: str,
    evidence_ref: str,
    resolution: str,
    actor_user_id: uuid.UUID | None = None,
    apply: bool = False,
) -> MediaRemediationResult:
    """Resolve exact provider-debt tasks with tenant fencing, evidence and audit."""
    normalized_ids = _normalize_task_ids(task_ids)
    normalized_incident_key = incident_key.strip()
    normalized_evidence = evidence_ref.strip()
    normalized_resolution = resolution.strip().lower()
    if not normalized_incident_key:
        raise ValueError("incident_key is required")
    if not normalized_evidence:
        raise ValueError("provider evidence_ref is required")
    if len(normalized_incident_key) > 200 or len(normalized_evidence) > 500:
        raise ValueError("incident_key or evidence_ref is too long")
    if normalized_resolution not in PROVIDER_DEBT_RESOLUTIONS:
        raise ValueError("unsupported provider debt resolution")

    closed_tasks: list[MediaGenerationTask] = []
    async with async_session() as db:
        query = select(MediaGenerationTask).where(MediaGenerationTask.id.in_(normalized_ids))
        if apply:
            query = query.with_for_update()
        result = await db.execute(query)
        tasks = list(result.scalars().all())
        _validate_task_scope(tasks, normalized_ids, expected_tenant_id)
        task_by_id = {task.id: task for task in tasks}

        items: list[MediaRemediationItem] = []
        compensation_total = 0
        for task_id in normalized_ids:
            task = task_by_id[task_id]
            reservation = None
            if task.reservation_id:
                reservation = await _lock_owned_media_reservation(
                    db,
                    task,
                    with_for_update=apply,
                )

            already_resolved = _validate_provider_debt_state(
                task,
                reservation,
                normalized_resolution,
            )
            status_before = task.status
            reservation_status_before = reservation.status if reservation else None
            if apply:
                if not already_resolved and normalized_resolution == "provider_rejected":
                    assert reservation is not None
                    await release_reserved_credits_in_session(
                        db,
                        reservation.id,
                        status="released",
                        release_provider_inflight=True,
                    )
                    task.status = "failed"
                elif not already_resolved:
                    if reservation and reservation.status == "provider_inflight":
                        await mark_credit_reservation_settlement_ready_in_session(
                            db,
                            reservation.id,
                            amount=reservation.amount,
                        )
                    compensation_amount = int(reservation.amount or 0) if reservation else 0
                    if compensation_amount > 0:
                        # The provider debt remains visible as a consume ledger
                        # entry, while an idempotent task-scoped refund keeps the
                        # customer whole when no usable asset can be delivered.
                        # Grant first so an accepted debt resize above the
                        # current balance can still be finalized atomically.
                        await grant_credits_in_session(
                            db,
                            tenant_id=task.tenant_id,
                            amount=compensation_amount,
                            reason="refund",
                            granted_by=getattr(task, "user_id", None),
                            ref_type="media_task",
                            ref_id=task.id,
                        )
                        compensation_total += compensation_amount
                    if reservation and reservation.status == "settlement_ready":
                        await finalize_reserved_credits_in_session(db, reservation.id)
                    task.status = "compensated"
                    task.last_response = {
                        **(
                            task.last_response
                            if isinstance(getattr(task, "last_response", None), dict)
                            else {}
                        ),
                        "status": "Compensated",
                        "resolution": normalized_resolution,
                        "refunded_credits": compensation_amount,
                    }
                    task.completion_delivery_status = "not_applicable"
                    if getattr(task, "user_id", None):
                        db.add(
                            Notification(
                                user_id=task.user_id,
                                agent_id=getattr(task, "agent_id", None),
                                type="system",
                                title="媒体生成结果已退款",
                                body=(
                                    "供应商已受理，但没有可安全交付的媒体结果。"
                                    f"系统已退回 {compensation_amount} Credits。"
                                ),
                                link=f"/agents/{task.agent_id}/chat"
                                if getattr(task, "agent_id", None)
                                else None,
                                ref_id=task.id,
                                sender_name="Astra",
                            )
                        )
                if not already_resolved:
                    task.last_error = (
                        f"Operator resolution {normalized_resolution}; "
                        f"incident={normalized_incident_key}; evidence={normalized_evidence}"
                    )[:1000]
                    task.completed_at = task.completed_at or datetime.now(timezone.utc)
                    task.next_poll_at = None
                closed_tasks.append(task)

            items.append(MediaRemediationItem(
                task_id=str(task.id),
                tenant_id=str(task.tenant_id) if task.tenant_id else None,
                status_before=status_before,
                status_after=task.status if apply else status_before,
                reservation_id=str(task.reservation_id) if task.reservation_id else None,
                reservation_status_before=reservation_status_before,
                reservation_amount=int(reservation.amount) if reservation else 0,
                compensation_credits=(
                    int(reservation.amount or 0)
                    if normalized_resolution != "provider_rejected" and reservation
                    else 0
                ),
            ))

        if apply:
            db.add(AuditLog(
                user_id=actor_user_id,
                action="media_provider_debt_resolution",
                details={
                    "incident_key": normalized_incident_key,
                    "evidence_ref": normalized_evidence,
                    "resolution": normalized_resolution,
                    "task_ids": [str(task_id) for task_id in normalized_ids],
                    "expected_tenant_id": str(expected_tenant_id),
                    "customer_compensation_credits": compensation_total,
                },
            ))
            await db.commit()

    for task in closed_tasks:
        await _delete_private_media_recovery_assets(task, strict=True)
    return MediaRemediationResult(
        incident_key=normalized_incident_key,
        applied=apply,
        items=tuple(items),
        resolution=normalized_resolution,
        evidence_ref=normalized_evidence,
    )
