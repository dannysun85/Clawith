#!/usr/bin/env python3
"""Exercise durable A2A enqueue, idempotency, lease recovery, and completion."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.tenant import Tenant  # noqa: F401 - registers FK metadata
from app.models.user import User
from app.services.trigger_daemon import wake_agent_with_context
from app.services.trigger_runtime.executions import (
    claim_pending_trigger_executions,
    mark_trigger_executions_completed,
)


async def main() -> None:
    async with async_session() as db:
        owner = User(display_name="A2A Migration Smoke", role="member")
        db.add(owner)
        await db.flush()
        source = Agent(
            name="A2A Source",
            creator_id=owner.id,
            status="idle",
        )
        target = Agent(
            name="A2A Target",
            creator_id=owner.id,
            status="idle",
        )
        db.add_all([source, target])
        await db.commit()

    message_id = uuid.uuid4()
    delivery_key = f"a2a:{message_id}"
    for _attempt in range(2):
        accepted = await wake_agent_with_context(
            target.id,
            "[From A2A Source] durable delivery smoke",
            from_agent_id=source.id,
            message_kind="task_delegate",
            idempotency_key=delivery_key,
            source_message_id=message_id,
        )
        assert accepted is True

    second_message_id = uuid.uuid4()
    second_delivery_key = f"a2a:{second_message_id}"
    assert await wake_agent_with_context(
        target.id,
        "[From A2A Source] second durable delivery",
        from_agent_id=source.id,
        message_kind="notify",
        idempotency_key=second_delivery_key,
        source_message_id=second_message_id,
    ) is True

    async with async_session() as db:
        trigger = (
            await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == target.id,
                    AgentTrigger.name == "__a2a_wake__",
                )
            )
        ).scalar_one()
        executions = (
            await db.execute(
                select(TriggerExecution).where(
                    TriggerExecution.trigger_id == trigger.id,
                    TriggerExecution.idempotency_key == delivery_key,
                )
            )
        ).scalars().all()
        assert len(executions) == 1
        assert executions[0].status == "pending"
        assert executions[0].source == "a2a"
        assert executions[0].payload["_a2a_kind"] == "task_delegate"
        expected_execution_id = executions[0].id
        second_execution_id = (
            await db.execute(
                select(TriggerExecution.id).where(
                    TriggerExecution.trigger_id == trigger.id,
                    TriggerExecution.idempotency_key == second_delivery_key,
                )
            )
        ).scalar_one()

    first_claim = await claim_pending_trigger_executions(sources=["a2a"])
    claimed_ids = [item[0].id for item in first_claim]
    assert expected_execution_id in claimed_ids
    assert second_execution_id not in claimed_ids
    execution_id = expected_execution_id

    # Simulate a worker crash after claiming the row. An expired lease must be
    # reclaimable by the next daemon instance.
    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    second_claim = await claim_pending_trigger_executions(sources=["a2a"])
    assert execution_id in [item[0].id for item in second_claim]
    assert second_execution_id not in [item[0].id for item in second_claim]
    await mark_trigger_executions_completed([execution_id])

    third_claim = await claim_pending_trigger_executions(sources=["a2a"])
    assert second_execution_id in [item[0].id for item in third_claim]
    await mark_trigger_executions_completed([second_execution_id])

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution is not None
        assert execution.status == "completed"
        assert execution.lease_owner is None
        assert execution.lease_expires_at is None

    print("A2A PostgreSQL durable queue smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
