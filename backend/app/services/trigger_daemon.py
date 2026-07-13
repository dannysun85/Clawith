"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.logging_config import new_trace_id
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.evaluator import (
    evaluate_trigger as evaluate_trigger_runtime,
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
)

settings = get_settings()

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30   # seconds — same agent won't be invoked twice within this window
# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30     # max on_message fires per agent per hour
_on_msg_fire_log: dict[uuid.UUID, list[datetime]] = {}  # agent_id -> list of fire timestamps

_last_invoke: dict[uuid.UUID, datetime] = {}

_A2A_WAKE_TRIGGER_NAME = "__a2a_wake__"


def _cleanup_stale_invoke_cache():
    now = datetime.now(timezone.utc)
    stale = [k for k, v in _last_invoke.items() if (now - v).total_seconds() > DEDUP_WINDOW * 2]
    for k in stale:
        del _last_invoke[k]
    # Clean up old on_message rate limiter entries
    cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
    stale_agents = []
    for aid, timestamps in _on_msg_fire_log.items():
        _on_msg_fire_log[aid] = [t for t in timestamps if t > cutoff]
        if not _on_msg_fire_log[aid]:
            stale_agents.append(aid)
    for aid in stale_agents:
        del _on_msg_fire_log[aid]


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

async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await evaluate_trigger_runtime(trigger, now)

async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    new_trace_id()
    await invoke_agent_for_triggers_runtime(agent_id, triggers)


def _build_invocation_batches(triggers: list[AgentTrigger]) -> list[list[AgentTrigger]]:
    """Keep durable A2A messages isolated while preserving queue order."""
    batches: list[list[AgentTrigger]] = []
    ordinary: list[AgentTrigger] = []
    for trigger in triggers:
        if trigger.type == "a2a":
            if ordinary:
                batches.append(ordinary)
                ordinary = []
            batches.append([trigger])
        else:
            ordinary.append(trigger)
    if ordinary:
        batches.append(ordinary)
    return batches


async def _invoke_agent_batches(agent_id: uuid.UUID, batches: list[list[AgentTrigger]]) -> None:
    """Run one agent's claimed work sequentially to avoid merged A2A replies."""
    for triggers in batches:
        await _invoke_agent_for_triggers(agent_id, triggers)


# ── Main Tick Loop ──────────────────────────────────────────────────

async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    new_trace_id()
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.is_enabled.is_(True))
        )
        all_triggers = result.scalars().all()
        # Expunge each object before session.close() is called.
        # session.close() expires all objects still in the identity map;
        # explicit expunge() detaches them WITHOUT expiry so their scalar
        # attributes remain readable outside the session context.
        for _t in all_triggers:
            db.expunge(_t)

    if not all_triggers:
        return


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
                    # Fix 3: Rate limit on_message triggers per agent
                    if trigger.type == "on_message":
                        agent_fires = _on_msg_fire_log.get(trigger.agent_id, [])
                        cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
                        recent = [t for t in agent_fires if t > cutoff]
                        if len(recent) >= _ON_MSG_RATE_LIMIT:
                            logger.warning(
                                f"[A2A Safety] Agent {trigger.agent_id} hit "
                                f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                                f"Auto-disabling trigger {trigger.id}."
                            )
                            async with async_session() as db:
                                result = await db.execute(
                                    select(AgentTrigger).where(AgentTrigger.id == trigger.id)
                                )
                                t_obj = result.scalar_one_or_none()
                                if t_obj:
                                    t_obj.is_enabled = False
                                    await db.commit()
                            continue
                        recent.append(now)
                        _on_msg_fire_log[trigger.agent_id] = recent
                    await enqueue_due_trigger(trigger, now)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.id}: {e}")

    # Claim queued executions with a DB lease so only one worker handles each event.
    try:
        fired_by_agent, force_invoke_agents = await claim_ready_trigger_invocations(now)
    except Exception as e:
        logger.warning(f"Failed to claim trigger executions: {e}")
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
            logger.warning(f"Failed to pre-update trigger state: {e}")

        asyncio.create_task(_invoke_agent_batches(agent_id, _build_invocation_batches(agent_triggers)))


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
    """Durably queue an A2A wake through the trigger execution ledger.

    The queue record is committed before this function reports success. A
    worker claims it with a DB lease, so a process restart cannot silently
    discard the message. Reusing an idempotency key is treated as an already
    accepted delivery.

    Args:
        agent_id: The agent to wake.
        message_context: The message to deliver.
        from_agent_id: The agent that initiated this wake.
        skip_dedup: Retained for API compatibility; durable A2A events are
            deduplicated by ``idempotency_key`` instead of an in-memory timer.
        a2a_session_id: Optional A2A chat session ID to mirror the reply into.
        message_kind: ``notify`` or ``task_delegate``.
        idempotency_key: Stable key for safe retries.
        source_message_id: Persisted chat message used to recover full content.
    """
    del skip_dedup
    if message_kind not in {"notify", "task_delegate"}:
        raise ValueError(f"Unsupported A2A message kind: {message_kind}")

    from app.models.agent import Agent as AgentModel

    delivery_key = (idempotency_key or f"a2a:{uuid.uuid4()}")[:255]
    async with async_session() as db:
        from_agent_name = ""
        if from_agent_id:
            sender_result = await db.execute(
                select(AgentModel.name).where(AgentModel.id == from_agent_id)
            )
            from_agent_name = sender_result.scalar_one_or_none() or ""

        trigger_result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == _A2A_WAKE_TRIGGER_NAME,
            )
        )
        trigger = trigger_result.scalar_one_or_none()
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
            db.add(trigger)
            try:
                await db.flush()
            except IntegrityError:
                # Another worker may have created the per-agent system trigger.
                await db.rollback()
                trigger_result = await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == agent_id,
                        AgentTrigger.name == _A2A_WAKE_TRIGGER_NAME,
                    )
                )
                trigger = trigger_result.scalar_one_or_none()
                if trigger is None:
                    raise

        trigger.type = "a2a"
        trigger.is_enabled = True
        trigger.is_system = True
        trigger.cooldown_seconds = 0
        await db.commit()

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


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    heartbeat_state = "enabled (~60s check)" if settings.HEARTBEAT_ENABLED else "disabled"
    logger.info(f"⚡ Trigger Daemon started (15s tick, heartbeat {heartbeat_state})")
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error(f"Trigger Daemon error: {e}")

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
                logger.error(f"Heartbeat tick error: {e}")

        await asyncio.sleep(TICK_INTERVAL)
