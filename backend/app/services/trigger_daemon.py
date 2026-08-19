"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from loguru import logger
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging_config import new_trace_id
from app.database import async_session
from app.models.agent import Agent
from app.models.experience import ExperienceEntry
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User
from app.services.trigger_runtime.evaluator import (
    evaluate_trigger as evaluate_trigger_runtime,
    handle_ceo_automation_gate as handle_ceo_automation_gate_runtime,
    handle_okr_collection_trigger as handle_okr_collection_trigger_runtime,
    handle_okr_report_trigger as handle_okr_report_trigger_runtime,
    mark_trigger_fired as mark_trigger_fired_runtime,
    mark_trigger_skipped as mark_trigger_skipped_runtime,
    should_skip_non_workday as should_skip_non_workday_runtime,
)
from app.services.trigger_runtime.invoker import invoke_agent_for_triggers as invoke_agent_for_triggers_runtime
from app.services.trigger_runtime import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
    enqueue_trigger_execution,
    renew_trigger_execution_leases,
)
from app.services.trigger_runtime.config import (
    AUTOMATIC_TRIGGER_EXECUTION_ENABLED,
    trigger_delivery_identity,
)

settings = get_settings()

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30   # seconds — same agent won't be invoked twice within this window
# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30     # max on_message fires per agent per hour
_on_msg_fire_log: dict[uuid.UUID, list[datetime]] = {}  # agent_id -> list of fire timestamps

_last_invoke: dict[uuid.UUID, datetime] = {}
_invocation_tasks: set[asyncio.Task[None]] = set()

_A2A_WAKE_TRIGGER_NAME = "__a2a_wake__"
_WAITING_BATCH_LEASE_RENEW_INTERVAL_SECONDS = 60


def _record_new_on_message_execution(
    agent_id: uuid.UUID,
    now: datetime,
    *,
    created: bool,
) -> bool:
    """Count only durable new events; idempotent queue hits are free no-ops."""

    if not created:
        return True
    cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
    recent = [
        timestamp
        for timestamp in _on_msg_fire_log.get(agent_id, [])
        if timestamp > cutoff
    ]
    if len(recent) >= _ON_MSG_RATE_LIMIT:
        _on_msg_fire_log[agent_id] = recent
        return False
    recent.append(now)
    _on_msg_fire_log[agent_id] = recent
    return True


async def _quarantine_rate_limited_execution(
    trigger: AgentTrigger,
    execution_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Disable the trigger and retire the just-created over-limit event."""

    async with async_session() as db:
        trigger_result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.id == trigger.id)
        )
        stored_trigger = trigger_result.scalar_one_or_none()
        if stored_trigger:
            stored_trigger.is_enabled = False
        if execution_id is not None:
            await db.execute(
                update(TriggerExecution)
                .where(
                    TriggerExecution.id == execution_id,
                    TriggerExecution.status == "pending",
                )
                .values(
                    status="failed",
                    finished_at=now,
                    last_error="on_message rate limit exceeded",
                )
            )
        await db.commit()


def _cleanup_stale_invoke_cache():
    now = datetime.now(timezone.utc)
    # Clean up old on_message rate limiter entries
    cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
    stale_agents = []
    for aid, timestamps in _on_msg_fire_log.items():
        _on_msg_fire_log[aid] = [t for t in timestamps if t > cutoff]
        if not _on_msg_fire_log[aid]:
            stale_agents.append(aid)
    for aid in stale_agents:
        del _on_msg_fire_log[aid]


_RETIRED_EXPERIENCE_TTL_DAYS = 30
_last_exp_purge_day = None  # date of the last purge; runs at most once per UTC day


async def _purge_expired_retired_experiences():
    """Hard-delete experience entries retired more than 30 days ago and not re-published.

    Re-publishing clears `retired_at`, so only entries still sitting in the 已下架 bin
    past the TTL are removed. experience_references cascade at the DB level. Runs once
    per day off the daemon tick.
    """
    global _last_exp_purge_day
    today = datetime.now(timezone.utc).date()
    if _last_exp_purge_day == today:
        return
    _last_exp_purge_day = today
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETIRED_EXPERIENCE_TTL_DAYS)
    async with async_session() as db:
        ids = (
            await db.execute(
                select(ExperienceEntry.id).where(
                    ExperienceEntry.status == "retired",
                    ExperienceEntry.retired_at.is_not(None),
                    ExperienceEntry.retired_at < cutoff,
                )
            )
        ).scalars().all()
        if not ids:
            return
        await db.execute(delete(ExperienceEntry).where(ExperienceEntry.id.in_(ids)))
        await db.commit()
        logger.info(f"🧹 Purged {len(ids)} retired experience entries older than {_RETIRED_EXPERIENCE_TTL_DAYS}d")


async def _should_skip_non_workday(trigger: AgentTrigger, local_now: datetime) -> bool:
    return await should_skip_non_workday_runtime(trigger, local_now)


async def _mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_skipped_runtime(trigger_id, now)


async def _mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_fired_runtime(trigger_id, now)


async def _handle_okr_report_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await handle_okr_report_trigger_runtime(trigger, now)


async def _handle_okr_collection_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await handle_okr_collection_trigger_runtime(trigger, now)


async def _handle_ceo_automation_gate(trigger: AgentTrigger, now: datetime) -> bool:
    return await handle_ceo_automation_gate_runtime(trigger, now)


async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await evaluate_trigger_runtime(trigger, now)


async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    new_trace_id()
    await invoke_agent_for_triggers_runtime(agent_id, triggers)


def _build_invocation_batches(triggers: list[AgentTrigger]) -> list[list[AgentTrigger]]:
    """Keep A2A and distinct delivery principals isolated in queue order."""
    batches: list[list[AgentTrigger]] = []
    ordinary: list[AgentTrigger] = []
    ordinary_identity: tuple[str, str, str] | None = None
    for trigger in triggers:
        if trigger.type == "a2a":
            if ordinary:
                batches.append(ordinary)
                ordinary = []
                ordinary_identity = None
            batches.append([trigger])
        else:
            identity = trigger_delivery_identity(trigger.config)
            if ordinary and identity != ordinary_identity:
                batches.append(ordinary)
                ordinary = []
            ordinary.append(trigger)
            ordinary_identity = identity
    if ordinary:
        batches.append(ordinary)
    return batches


async def _invoke_agent_batches(agent_id: uuid.UUID, batches: list[list[AgentTrigger]]) -> None:
    """Run batches sequentially while retaining every waiting execution claim.

    The claim query can return several principals for one Agent at once.  Only
    the active batch is renewed by the invoker, so this outer keeper renews all
    later batches until each one starts.  Without it, a long first generation
    could let a later batch expire and be executed concurrently by another
    worker.
    """

    claims_by_batch: list[list[tuple[uuid.UUID, str]]] = []
    for triggers in batches:
        claims: list[tuple[uuid.UUID, str]] = []
        for trigger in triggers:
            config = trigger.config or {}
            execution_id = config.get("_execution_id")
            lease_token = config.get("_execution_lease_token")
            if execution_id and lease_token:
                claims.append((uuid.UUID(str(execution_id)), str(lease_token)))
        claims_by_batch.append(claims)

    waiting_claims = {
        claim
        for batch_claims in claims_by_batch[1:]
        for claim in batch_claims
    }
    claims_lock = asyncio.Lock()
    invocation_task = asyncio.current_task()

    async def _renew_waiting_claims() -> None:
        while True:
            await asyncio.sleep(_WAITING_BATCH_LEASE_RENEW_INTERVAL_SECONDS)
            async with claims_lock:
                if not waiting_claims:
                    return
                claims = list(waiting_claims)
                try:
                    renewed = await renew_trigger_execution_leases(claims)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Waiting trigger batch lease renewal failed "
                        "agent_id={} error_type={}",
                        agent_id,
                        type(exc).__name__,
                    )
                    renewed = 0
                if renewed != len(claims):
                    logger.error(
                        "Waiting trigger batch lease fence lost "
                        "agent_id={} expected={} renewed={}",
                        agent_id,
                        len(claims),
                        renewed,
                    )
                    if invocation_task is not None:
                        invocation_task.cancel()
                    return

    lease_keeper = (
        asyncio.create_task(_renew_waiting_claims())
        if waiting_claims
        else None
    )
    try:
        for index, triggers in enumerate(batches):
            if index:
                async with claims_lock:
                    waiting_claims.difference_update(claims_by_batch[index])
            await _invoke_agent_for_triggers(agent_id, triggers)
    finally:
        if lease_keeper is not None:
            lease_keeper.cancel()
            await asyncio.gather(lease_keeper, return_exceptions=True)


def _max_invocation_concurrency() -> int:
    return max(1, int(settings.TRIGGER_MAX_CONCURRENCY))


def _claim_batch_size() -> int:
    return max(1, int(settings.TRIGGER_CLAIM_BATCH_SIZE))


def _available_invocation_slots() -> int:
    return max(0, _max_invocation_concurrency() - _active_invocation_count())


def _active_invocation_count() -> int:
    return sum(not task.done() for task in _invocation_tasks)


async def _capture_trigger_runtime_issue(
    error: BaseException,
    *,
    operation: str,
    agent_id: uuid.UUID | None = None,
) -> None:
    """Persist a privacy-safe trigger failure without breaking the worker."""
    from app.services.production_issue_monitor import record_production_issue

    category = "database" if isinstance(error, (SQLAlchemyError, TimeoutError)) else "trigger"
    await record_production_issue(
        source="trigger_runtime",
        category=category,
        summary="Trigger runtime operation failed",
        severity="error",
        error_code=type(error).__name__,
        operation=operation,
        agent_id=agent_id,
        metadata={
            "component": "trigger_daemon",
            "error_type": type(error).__name__,
        },
    )


async def _run_invocation_task(
    agent_id: uuid.UUID,
    batches: list[list[AgentTrigger]],
) -> None:
    try:
        await _invoke_agent_batches(agent_id, batches)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Trigger invocation task crashed agent_id={} error_type={}",
            agent_id,
            type(exc).__name__,
        )
        await _capture_trigger_runtime_issue(
            exc,
            operation="invoke_agent_batches",
            agent_id=agent_id,
        )


def _invocation_task_done(task: asyncio.Task[None]) -> None:
    _invocation_tasks.discard(task)
    try:
        _ = task.exception()
    except asyncio.CancelledError:
        return


def _schedule_invocation_task(
    agent_id: uuid.UUID,
    batches: list[list[AgentTrigger]],
) -> None:
    task = asyncio.create_task(
        _run_invocation_task(agent_id, batches),
        name=f"trigger-invoke-{agent_id}",
    )
    _invocation_tasks.add(task)
    task.add_done_callback(_invocation_task_done)


# ── Main Tick Loop ──────────────────────────────────────────────────

async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    if not AUTOMATIC_TRIGGER_EXECUTION_ENABLED:
        return
    new_trace_id()
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger)
            .join(Agent, Agent.id == AgentTrigger.agent_id)
            .join(User, User.id == Agent.creator_id)
            .join(Tenant, Tenant.id == Agent.tenant_id)
            .where(
                AgentTrigger.is_enabled.is_(True),
                Agent.status.in_(("running", "idle")),
                Agent.is_expired.is_(False),
                (Agent.expires_at.is_(None) | (Agent.expires_at > now)),
                User.is_active.is_(True),
                Tenant.is_active.is_(True),
            )
        )
        all_triggers = result.scalars().all()
        # Expunge each object before session.close() is called.
        # session.close() expires all objects still in the identity map;
        # explicit expunge() detaches them WITHOUT expiry so their scalar
        # attributes remain readable outside the session context.
        for _t in all_triggers:
            db.expunge(_t)

    # Evaluate and enqueue due triggers. Agent invocation happens only after
    # executions are claimed through the distributed execution queue.
    for trigger in all_triggers:
        # Auto-disable expired triggers
        if trigger.expires_at and now >= trigger.expires_at:
            async with async_session() as db:
                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                t = result.scalar_one_or_none()
                if t:
                    t.is_enabled = False
                    await db.commit()
            continue

        try:
            if await _evaluate_trigger(trigger, now):
                handled = await _handle_okr_report_trigger(trigger, now)
                if not handled:
                    handled = await _handle_okr_collection_trigger(trigger, now)
                if not handled:
                    handled = await _handle_ceo_automation_gate(trigger, now)
                if not handled:
                    execution_id, created = await enqueue_due_trigger(
                        trigger,
                        now,
                    )
                    if trigger.type == "on_message" and not _record_new_on_message_execution(
                        trigger.agent_id,
                        now,
                        created=created,
                    ):
                        logger.warning(
                            f"[A2A Safety] Agent {trigger.agent_id} hit "
                            f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                            f"Auto-disabling trigger {trigger.id}."
                        )
                        await _quarantine_rate_limited_execution(
                            trigger,
                            execution_id,
                            now,
                        )
        except Exception as e:
            logger.warning(
                "Error evaluating trigger {} error_type={}",
                trigger.id,
                type(e).__name__,
            )
            await _capture_trigger_runtime_issue(
                e,
                operation="evaluate_trigger",
                agent_id=trigger.agent_id,
            )

    available_slots = _available_invocation_slots()
    if available_slots <= 0:
        logger.warning(
            "Trigger invocation capacity exhausted active={} limit={}",
            _active_invocation_count(),
            _max_invocation_concurrency(),
        )
        return

    # Claim queued executions with a DB lease so only one worker handles each event.
    try:
        claim_limit = min(_claim_batch_size(), available_slots)
        fired_by_agent, force_invoke_agents = await claim_ready_trigger_invocations(
            now,
            limit=claim_limit,
        )
    except Exception as e:
        logger.warning(
            "Failed to claim trigger executions error_type={}",
            type(e).__name__,
        )
        await _capture_trigger_runtime_issue(e, operation="claim_trigger_executions")
        fired_by_agent = {}
        force_invoke_agents = set()

    # Invoke each agent (with dedup window)
    for agent_id, agent_triggers in fired_by_agent.items():
        last = _last_invoke.get(agent_id)
        if agent_id not in force_invoke_agents and last and (now - last).total_seconds() < DEDUP_WINDOW:
            continue  # Skip — invoked too recently
        _last_invoke[agent_id] = now

        # ── Immediately update trigger state BEFORE launching async task ──
        # This prevents the next tick from re-evaluating the same trigger as
        # "should fire" while the LLM call is still running (which can take
        # minutes). Without this, the 15s tick interval + 30s dedup window
        # would cause repeated invocations for long-running triggers.
        try:
            async with async_session() as db:
                for t in agent_triggers:
                    cfg = t.config or {}
                    if isinstance(cfg, str):
                        import json
                        try:
                            cfg = json.loads(cfg)
                        except (json.JSONDecodeError, TypeError):
                            cfg = {}
                    if cfg.get("_execution_id"):
                        continue
                    result = await db.execute(
                        select(AgentTrigger).where(AgentTrigger.id == t.id)
                    )
                    trigger = result.scalar_one_or_none()
                    if trigger:
                        trigger.last_fired_at = now
                        trigger.fire_count += 1
                        # Auto-disable single-shot types only
                        if trigger.type == "once":
                            trigger.is_enabled = False
                        if trigger.max_fires and trigger.fire_count >= trigger.max_fires:
                            trigger.is_enabled = False
                await db.commit()
        except Exception as e:
            logger.warning(
                "Failed to pre-update trigger state agent_id={} error_type={}",
                agent_id,
                type(e).__name__,
            )
            await _capture_trigger_runtime_issue(
                e,
                operation="update_trigger_state",
                agent_id=agent_id,
            )

        _schedule_invocation_task(
            agent_id,
            _build_invocation_batches(agent_triggers),
        )


async def enqueue_agent_wake_with_context(
    db: AsyncSession,
    agent_id: uuid.UUID,
    message_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    a2a_session_id: str | None = None,
    message_kind: str = "notify",
    idempotency_key: str | None = None,
    source_message_id: uuid.UUID | None = None,
) -> bool:
    """Atomically enqueue A2A work in the caller's database transaction."""

    if not AUTOMATIC_TRIGGER_EXECUTION_ENABLED:
        logger.warning(
            "A2A wake rejected while automatic trigger execution is paused "
            "agent_id={}",
            agent_id,
        )
        return False
    if message_kind not in {"notify", "task_delegate"}:
        raise ValueError(f"Unsupported A2A message kind: {message_kind}")

    from app.models.agent import Agent as AgentModel

    delivery_key = (idempotency_key or f"a2a:{uuid.uuid4()}")[:255]
    if from_agent_id and a2a_session_id:
        from app.models.chat_session import ChatSession
        from app.services.a2a_authorization import validate_active_a2a_lane

        try:
            parsed_session_id = uuid.UUID(str(a2a_session_id))
        except (TypeError, ValueError) as exc:
            raise PermissionError("A2A queue session is invalid") from exc
        queued_session = await db.get(ChatSession, parsed_session_id)
        if queued_session is None or not queued_session.user_id:
            raise PermissionError("A2A queue session is unavailable")
        await validate_active_a2a_lane(
            db,
            source_agent_id=from_agent_id,
            target_agent_id=agent_id,
            owner_user_id=queued_session.user_id,
            session_id=queued_session.id,
            lock_relationship=True,
        )

    from_agent_name = ""
    if from_agent_id:
        sender_result = await db.execute(
            select(AgentModel.name).where(AgentModel.id == from_agent_id)
        )
        from_agent_name = sender_result.scalar_one_or_none() or ""

    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"clawith:a2a-trigger:v1:{agent_id}"},
        )
    trigger = (
        await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == _A2A_WAKE_TRIGGER_NAME,
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        trigger = AgentTrigger(
            agent_id=agent_id,
            name=_A2A_WAKE_TRIGGER_NAME,
            type="a2a",
            config={},
            reason="Process a durably queued agent-to-agent message.",
            is_enabled=True,
            is_system=True,
            cooldown_seconds=0,
        )
        try:
            async with db.begin_nested():
                db.add(trigger)
                await db.flush()
        except IntegrityError:
            trigger = (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == agent_id,
                        AgentTrigger.name == _A2A_WAKE_TRIGGER_NAME,
                    )
                )
            ).scalar_one()

    trigger.type = "a2a"
    trigger.is_enabled = True
    trigger.is_system = True
    trigger.cooldown_seconds = 0

    payload = {
        "from_agent_name": from_agent_name,
        "_matched_from": from_agent_name or "agent",
        "_matched_message": message_context[:8000],
        "_a2a_kind": message_kind,
    }
    if from_agent_id:
        payload["_matched_from_agent_id"] = str(from_agent_id)
    if a2a_session_id:
        payload["_a2a_session_id"] = a2a_session_id
    if source_message_id:
        payload["_source_message_id"] = str(source_message_id)

    trigger_id = trigger.id
    _execution, created = await enqueue_trigger_execution(
        db,
        trigger=trigger,
        source="a2a",
        idempotency_key=delivery_key,
        payload_obj=payload,
    )
    if not created:
        logger.info(
            "[A2A] Delivery already queued for agent {} trigger={}",
            agent_id,
            trigger_id,
        )
    return True


async def wake_agent_with_context(
    agent_id: uuid.UUID,
    message_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
    message_kind: str = "notify",
    idempotency_key: str | None = None,
    source_message_id: uuid.UUID | None = None,
) -> bool:
    """Durably queue an A2A wake in an owned transaction."""

    del skip_dedup
    async with async_session() as db:
        accepted = await enqueue_agent_wake_with_context(
            db,
            agent_id,
            message_context,
            from_agent_id=from_agent_id,
            a2a_session_id=a2a_session_id,
            message_kind=message_kind,
            idempotency_key=idempotency_key,
            source_message_id=source_message_id,
        )
        if accepted:
            await db.commit()
        return accepted


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    if not settings.TRIGGER_DAEMON_ENABLED:
        logger.warning("Trigger Daemon disabled by TRIGGER_DAEMON_ENABLED=false")
    elif not AUTOMATIC_TRIGGER_EXECUTION_ENABLED:
        logger.info("Automatic trigger execution is paused by release policy")
    if (
        not settings.TRIGGER_DAEMON_ENABLED
        or not AUTOMATIC_TRIGGER_EXECUTION_ENABLED
    ):
        # Dedicated workers treat every registered daemon as a critical
        # long-lived task. Keep a cancellable idle coroutine alive while the
        # lane is intentionally paused so health does not report a crash.
        while True:
            await asyncio.sleep(3600)
    heartbeat_state = "enabled (~60s check)" if settings.HEARTBEAT_ENABLED else "disabled"
    logger.info(
        "⚡ Trigger Daemon started ({}s tick, heartbeat {}, max_concurrency={}, claim_batch={})",
        TICK_INTERVAL,
        heartbeat_state,
        _max_invocation_concurrency(),
        _claim_batch_size(),
    )
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error("Trigger Daemon error_type={}", type(e).__name__)
            await _capture_trigger_runtime_issue(e, operation="trigger_daemon_tick")

        # Run heartbeat check every 4th tick (~60 seconds) only when the
        # independent global kill switch is enabled. Explicit triggers above
        # continue running regardless of this setting.
        if settings.HEARTBEAT_ENABLED:
            _heartbeat_counter += 1
        if _heartbeat_counter >= 4:
            _heartbeat_counter = 0
            _cleanup_stale_invoke_cache()
            try:
                from app.services.heartbeat import _heartbeat_tick
                await _heartbeat_tick()
            except Exception as e:
                logger.error("Heartbeat tick error_type={}", type(e).__name__)
                await _capture_trigger_runtime_issue(e, operation="heartbeat_tick")

        await asyncio.sleep(TICK_INTERVAL)
