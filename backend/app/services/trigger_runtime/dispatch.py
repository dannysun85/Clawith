"""Dispatch helpers for trigger executions."""

from __future__ import annotations

from datetime import datetime
import uuid

from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.executions import (
    build_execution_runtime_trigger,
    claim_pending_trigger_executions,
)
from app.services.trigger_runtime.keys import build_scheduled_execution_key
from app.services.trigger_runtime.queue import enqueue_trigger_execution
from app.services.trigger_runtime.config import (
    MATCH_CONTEXT_VERSION,
    MATCH_CONTEXT_VERSION_KEY,
    SERVER_CONTEXT_VERSION,
    SERVER_CONTEXT_VERSION_KEY,
)


def runtime_execution_payload(trigger: AgentTrigger) -> dict:
    """Capture ephemeral trigger evaluation context into an execution payload."""
    cfg = trigger.config or {}
    payload: dict = {}
    for key in ("okr_member_id", "okr_member_type", "okr_report_date"):
        if key in cfg and cfg.get(key) is not None:
            payload[key] = cfg.get(key)

    # Only current evaluator output may supply message content/identity.
    if cfg.get(MATCH_CONTEXT_VERSION_KEY) == MATCH_CONTEXT_VERSION:
        for key in (
            "_matched_message",
            "_matched_from",
            "_matched_from_agent_id",
            "_matched_conversation_id",
            "_matched_message_id",
            "_source_message_id",
        ):
            if key in cfg and cfg.get(key) is not None:
                payload[key] = cfg.get(key)

    # Only metadata stamped by current server code may route a result.
    if cfg.get(SERVER_CONTEXT_VERSION_KEY) == SERVER_CONTEXT_VERSION:
        for key in (
            "_notification_summary",
            "_origin_session_id",
            "_origin_user_id",
            "_origin_source_channel",
        ):
            if key in cfg and cfg.get(key) is not None:
                payload[key] = cfg.get(key)
    return payload


async def enqueue_due_trigger(
    trigger: AgentTrigger,
    now: datetime,
) -> tuple[uuid.UUID | None, bool]:
    async with async_session() as db:
        execution, created = await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source=trigger.type,
            idempotency_key=build_scheduled_execution_key(trigger, now),
            payload_obj=runtime_execution_payload(trigger),
        )
        return (execution.id if execution is not None else None), created


async def claim_ready_trigger_invocations(
    now: datetime,
    *,
    limit: int,
) -> tuple[dict[uuid.UUID, list[AgentTrigger]], set[uuid.UUID]]:
    fired_by_agent: dict[uuid.UUID, list[AgentTrigger]] = {}
    force_invoke_agents: set[uuid.UUID] = set()

    claimed_executions = await claim_pending_trigger_executions(limit=max(1, limit))
    for execution, trigger in claimed_executions:
        runtime_trigger = build_execution_runtime_trigger(trigger, execution)
        fired_by_agent.setdefault(trigger.agent_id, []).append(runtime_trigger)
        force_invoke_agents.add(trigger.agent_id)

    return fired_by_agent, force_invoke_agents
