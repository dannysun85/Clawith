"""Auditable, idempotent remediation for exact media-generation task IDs."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.database import async_session
from app.models.audit import AuditLog
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditReservation
from app.services.media_generation import (
    TERMINAL_MEDIA_STATUSES,
    _finalize_failure_in_session,
)


@dataclass(frozen=True)
class MediaRemediationItem:
    task_id: str
    tenant_id: str | None
    status_before: str
    status_after: str
    reservation_id: str | None
    reservation_status_before: str | None
    reservation_amount: int


@dataclass(frozen=True)
class MediaRemediationResult:
    incident_key: str
    applied: bool
    items: tuple[MediaRemediationItem, ...]

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
                reservation = await db.get(
                    CreditReservation,
                    task.reservation_id,
                    with_for_update=apply,
                )
                if not reservation:
                    raise ValueError(f"reservation missing for media task {task.id}")
                if reservation.tenant_id != task.tenant_id:
                    raise ValueError(f"reservation tenant mismatch for media task {task.id}")

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
