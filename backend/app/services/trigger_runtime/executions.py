"""Execution claiming and completion helpers for distributed triggers."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User
from app.services.trigger_runtime.config import (
    trusted_execution_runtime_payload,
    without_reserved_trigger_config,
)

settings = get_settings()
TRIGGER_EXECUTION_LEASE_SECONDS = 300


ExecutionClaim = tuple[uuid.UUID, str]


def _claim_fence(claims: list[ExecutionClaim]):
    return or_(
        *(
            and_(
                TriggerExecution.id == execution_id,
                TriggerExecution.lease_owner == lease_token,
            )
            for execution_id, lease_token in claims
        )
    )


async def mark_trigger_executions_completed(claims: list[ExecutionClaim]) -> int:
    """Complete only executions still owned by the exact claim generation."""
    if not claims:
        return 0
    async with async_session() as db:
        claimed_ids = [execution_id for execution_id, _lease_owner in claims]
        trigger_ids = set(
            (
                await db.execute(
                    select(TriggerExecution.trigger_id).where(
                        TriggerExecution.id.in_(claimed_ids)
                    )
                )
            ).scalars().all()
        )
        triggers: list[AgentTrigger] = []
        if trigger_ids:
            trigger_result = await db.execute(
                select(AgentTrigger)
                .where(AgentTrigger.id.in_(trigger_ids))
                .order_by(AgentTrigger.id)
                .with_for_update()
            )
            triggers = list(trigger_result.scalars().all())
        result = await db.execute(
            update(TriggerExecution)
            .where(
                _claim_fence(claims),
                TriggerExecution.status == "processing",
            )
            .values(
                status="completed",
                finished_at=datetime.now(timezone.utc),
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
            )
            .returning(TriggerExecution.id, TriggerExecution.trigger_id)
        )
        completed_rows = result.all()
        completed_trigger_ids = {
            trigger_id for _execution_id, trigger_id in completed_rows
        }
        if completed_trigger_ids:
            unfinished_trigger_ids = set(
                (
                    await db.execute(
                        select(TriggerExecution.trigger_id)
                        .where(
                            TriggerExecution.trigger_id.in_(
                                completed_trigger_ids
                            ),
                            TriggerExecution.status.in_(("pending", "processing")),
                        )
                        .distinct()
                    )
                ).scalars().all()
            )
            for trigger in triggers:
                if trigger.id not in completed_trigger_ids:
                    continue
                if trigger.id in unfinished_trigger_ids:
                    continue
                if trigger.type == "once" or (
                    trigger.max_fires
                    and (trigger.fire_count or 0) >= trigger.max_fires
                ):
                    trigger.is_enabled = False
        await db.commit()
        return len(completed_rows)


async def mark_trigger_executions_failed(
    claims: list[ExecutionClaim],
    error_text: str,
) -> int:
    """Fail only executions still owned by the exact claim generation."""
    if not claims:
        return 0
    async with async_session() as db:
        claimed_ids = [execution_id for execution_id, _lease_owner in claims]
        trigger_ids = set(
            (
                await db.execute(
                    select(TriggerExecution.trigger_id).where(
                        TriggerExecution.id.in_(claimed_ids)
                    )
                )
            ).scalars().all()
        )
        triggers: list[AgentTrigger] = []
        if trigger_ids:
            triggers = list(
                (
                    await db.execute(
                        select(AgentTrigger)
                        .where(AgentTrigger.id.in_(trigger_ids))
                        .order_by(AgentTrigger.id)
                        .with_for_update()
                    )
                ).scalars().all()
            )
        result = await db.execute(
            update(TriggerExecution)
            .where(
                _claim_fence(claims),
                TriggerExecution.status == "processing",
            )
            .values(
                status="failed",
                finished_at=datetime.now(timezone.utc),
                lease_owner=None,
                lease_expires_at=None,
                last_error=error_text[:2000],
            )
            .returning(TriggerExecution.trigger_id)
        )
        failed_rows = list(result.scalars().all())
        failed_trigger_ids = set(failed_rows)
        if failed_trigger_ids:
            unfinished_trigger_ids = set(
                (
                    await db.execute(
                        select(TriggerExecution.trigger_id)
                        .where(
                            TriggerExecution.trigger_id.in_(
                                failed_trigger_ids
                            ),
                            TriggerExecution.status.in_(("pending", "processing")),
                        )
                        .distinct()
                    )
                ).scalars().all()
            )
            for trigger in triggers:
                if (
                    trigger.id not in failed_trigger_ids
                    or trigger.id in unfinished_trigger_ids
                ):
                    continue
                if trigger.type == "once" or (
                    trigger.max_fires
                    and (trigger.fire_count or 0) >= trigger.max_fires
                ):
                    trigger.is_enabled = False
        await db.commit()
        return len(failed_rows)


async def renew_trigger_execution_leases(
    claims: list[ExecutionClaim],
    *,
    lease_seconds: int = TRIGGER_EXECUTION_LEASE_SECONDS,
) -> int:
    """Extend live claims; a short count is a fencing failure for the caller."""
    if not claims:
        return 0
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(lease_seconds, 30))
    async with async_session() as db:
        result = await db.execute(
            update(TriggerExecution)
            .where(
                _claim_fence(claims),
                TriggerExecution.status == "processing",
            )
            .values(lease_expires_at=lease_until)
        )
        await db.commit()
        return result.rowcount or 0


async def claim_pending_trigger_executions(
    *,
    sources: list[str] | None = None,
    limit: int = 100,
) -> list[tuple[TriggerExecution, AgentTrigger]]:
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=TRIGGER_EXECUTION_LEASE_SECONDS)
    claimed_pairs: list[tuple[TriggerExecution, AgentTrigger]] = []
    sources = sources or ["webhook", "cron", "once", "interval", "poll", "on_message", "a2a"]
    eligible_execution = or_(
        TriggerExecution.status == "pending",
        (TriggerExecution.status == "processing")
        & (
            TriggerExecution.lease_expires_at.is_(None)
            | (TriggerExecution.lease_expires_at < now)
        ),
    )
    earlier_execution = aliased(TriggerExecution)
    earlier_trigger = aliased(AgentTrigger)
    # One unfinished head owns the Agent lane across every source.  Because
    # only the head row is eligible, concurrent workers contend on that same
    # row and FOR UPDATE SKIP LOCKED cannot select a later execution for the
    # same Agent. The partial unique index on processing Agent rows is the
    # database-level invariant if a future query accidentally regresses.
    earlier_is_unfinished = earlier_execution.status.in_(("pending", "processing"))
    agent_head_only = ~exists(
        select(1)
        .select_from(earlier_execution)
        .join(earlier_trigger, earlier_trigger.id == earlier_execution.trigger_id)
        .where(
            earlier_execution.agent_id == TriggerExecution.agent_id,
            earlier_is_unfinished,
            or_(
                earlier_trigger.is_enabled.is_(True),
                and_(
                    earlier_execution.status == "processing",
                    earlier_execution.lease_expires_at.is_not(None),
                    earlier_execution.lease_expires_at >= now,
                ),
            ),
            or_(
                earlier_execution.scheduled_at < TriggerExecution.scheduled_at,
                and_(
                    earlier_execution.scheduled_at == TriggerExecution.scheduled_at,
                    earlier_execution.id < TriggerExecution.id,
                ),
            ),
        )
    )
    async with async_session() as db:
        # Operator-disabled triggers cannot retain a queue head indefinitely.
        # Auto-disable for once/max_fires happens only after completion, so an
        # abandoned execution remains enabled and reclaimable after a crash.
        await db.execute(
            update(TriggerExecution)
            .where(
                TriggerExecution.trigger_id.in_(
                    select(AgentTrigger.id).where(
                        AgentTrigger.is_enabled.is_(False)
                    )
                ),
                or_(
                    TriggerExecution.status == "pending",
                    and_(
                        TriggerExecution.status == "processing",
                        or_(
                            TriggerExecution.lease_expires_at.is_(None),
                            TriggerExecution.lease_expires_at < now,
                        ),
                    ),
                ),
            )
            .values(
                status="failed",
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error="Trigger disabled before queued execution could run",
            )
        )
        result = await db.execute(
            select(TriggerExecution, AgentTrigger)
            .join(AgentTrigger, AgentTrigger.id == TriggerExecution.trigger_id)
            .join(Agent, Agent.id == AgentTrigger.agent_id)
            .join(User, User.id == Agent.creator_id)
            .join(Tenant, Tenant.id == Agent.tenant_id)
            .where(
                TriggerExecution.source.in_(sources),
                AgentTrigger.is_enabled.is_(True),
                Agent.status.in_(("running", "idle")),
                Agent.is_expired.is_(False),
                or_(Agent.expires_at.is_(None), Agent.expires_at > now),
                User.is_active.is_(True),
                Tenant.is_active.is_(True),
                eligible_execution,
                agent_head_only,
            )
            .order_by(TriggerExecution.scheduled_at.asc())
            .with_for_update(of=TriggerExecution, skip_locked=True)
            .limit(limit)
        )
        rows = result.all()
        for execution, trigger in rows:
            execution.status = "processing"
            execution.started_at = execution.started_at or now
            execution.finished_at = None
            # Every claim/reclaim gets a unique generation token.  INSTANCE_ID
            # alone cannot fence an older coroutine in the same process.
            execution.lease_owner = f"{settings.INSTANCE_ID}:{uuid.uuid4().hex}"
            execution.lease_expires_at = lease_until
            claimed_pairs.append((execution, trigger))
        await db.commit()
        for execution, trigger in claimed_pairs:
            if execution in db:
                db.expunge(execution)
            if trigger in db:
                db.expunge(trigger)
    return claimed_pairs


def build_execution_runtime_trigger(trigger: AgentTrigger, execution: TriggerExecution) -> AgentTrigger:
    runtime_cfg = {
        # Stored underscore-prefixed values from pre-hardening releases are
        # untrusted. Runtime metadata may enter only through the source-aware,
        # service-owned execution payload below.
        **without_reserved_trigger_config(trigger.config),
        "_execution_id": str(execution.id),
        "_execution_lease_token": execution.lease_owner,
    }
    runtime_cfg.update(
        trusted_execution_runtime_payload(execution.source, execution.payload)
    )
    if execution.payload_text:
        runtime_cfg["_webhook_payload"] = execution.payload_text
    return AgentTrigger(
        id=trigger.id,
        agent_id=trigger.agent_id,
        name=trigger.name,
        type=trigger.type,
        config=runtime_cfg,
        reason=trigger.reason,
        focus_ref=trigger.focus_ref,
        is_enabled=trigger.is_enabled,
        last_fired_at=trigger.last_fired_at,
        fire_count=trigger.fire_count,
        max_fires=trigger.max_fires,
        cooldown_seconds=trigger.cooldown_seconds,
        is_system=trigger.is_system,
        created_at=trigger.created_at,
        expires_at=trigger.expires_at,
    )
