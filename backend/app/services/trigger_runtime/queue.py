"""Queue trigger executions for distributed workers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution


async def enqueue_trigger_execution(
    db: AsyncSession,
    *,
    trigger: AgentTrigger,
    source: str,
    idempotency_key: str,
    payload_text: str = "",
    payload_obj: dict | None = None,
) -> tuple[TriggerExecution | None, bool]:
    """Insert one execution while serializing capacity on its parent trigger.

    The lock order is always parent trigger -> child execution.  Besides
    avoiding PostgreSQL parent/child lock-upgrade deadlocks, this makes
    ``max_fires`` an admission limit rather than a best-effort completion
    check under concurrent webhook deliveries.
    """
    trigger_id = trigger.id
    delivery_key = idempotency_key[:255]
    scheduled_at = datetime.now(timezone.utc)
    trigger_row = (
        await db.execute(
            select(AgentTrigger)
            .where(AgentTrigger.id == trigger_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if trigger_row is None or not trigger_row.is_enabled:
        await db.rollback()
        return None, False

    existing = (
        await db.execute(
            select(TriggerExecution).where(
                TriggerExecution.trigger_id == trigger_id,
                TriggerExecution.idempotency_key == delivery_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.fire_recorded_at is None:
            recorded_at = existing.scheduled_at or scheduled_at
            trigger_row.last_fired_at = recorded_at
            trigger_row.fire_count = (trigger_row.fire_count or 0) + 1
            existing.fire_recorded_at = recorded_at
        await db.commit()
        return existing, False

    fire_count = trigger_row.fire_count or 0
    at_capacity = (
        trigger_row.type == "once" and fire_count >= 1
    ) or (
        bool(trigger_row.max_fires) and fire_count >= trigger_row.max_fires
    )
    if at_capacity:
        unfinished = (
            await db.execute(
                select(TriggerExecution.id)
                .where(
                    TriggerExecution.trigger_id == trigger_id,
                    TriggerExecution.status.in_(("pending", "processing")),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if unfinished is None:
            trigger_row.is_enabled = False
        await db.commit()
        return None, False

    execution = TriggerExecution(
        trigger_id=trigger_id,
        agent_id=trigger_row.agent_id,
        source=source,
        status="pending",
        idempotency_key=delivery_key,
        payload=payload_obj if isinstance(payload_obj, dict) else {},
        payload_text=payload_text[:8000],
        scheduled_at=scheduled_at,
    )
    db.add(execution)
    await db.flush()
    trigger_row.last_fired_at = scheduled_at
    trigger_row.fire_count = (trigger_row.fire_count or 0) + 1
    execution.fire_recorded_at = scheduled_at
    await db.commit()
    return execution, True


async def enqueue_webhook_execution(
    db: AsyncSession,
    *,
    trigger: AgentTrigger,
    body: bytes,
    payload_text: str,
    payload_obj: dict | None,
    request_headers: dict[str, str],
) -> tuple[TriggerExecution | None, bool]:
    """Insert a webhook execution record.

    Returns `(execution, created)` where `created=False` means an identical
    idempotency key already exists and the event should be treated as a no-op.
    """
    delivery_key = (
        request_headers.get("x-idempotency-key")
        or request_headers.get("x-github-delivery")
        or request_headers.get("x-request-id")
        or request_headers.get("x-event-id")
        or hashlib.sha256(body).hexdigest()
    )[:255]

    return await enqueue_trigger_execution(
        db,
        trigger=trigger,
        source="webhook",
        idempotency_key=delivery_key,
        payload_text=payload_text,
        payload_obj=payload_obj,
    )
