#!/usr/bin/env python3
"""Exercise Runtime Trigger intake plus legacy A2A lease recovery."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import async_session
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.group import Group  # noqa: F401 - registers ChatSession FK metadata
from app.models.llm import LLMModel
from app.models.participant import Participant  # noqa: F401 - registers Group FK metadata
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.tenant import Tenant  # noqa: F401 - registers FK metadata
from app.models.user import User
from app.services import trigger_daemon
from app.services.agent_runtime.persistence import claim_next_command
from app.services.trigger_daemon import wake_agent_with_context
from app.services.trigger_runtime.executions import (
    claim_pending_trigger_executions,
    mark_trigger_executions_completed,
)
from app.services.trigger_runtime.queue import enqueue_trigger_execution


async def main() -> None:
    # Production keeps legacy automatic execution fail-closed. This isolated
    # smoke opens only its own process and routes Trigger intake through the
    # unified Runtime; the second phase directly exercises recovery of legacy
    # pending rows that can still exist during an upgrade.
    trigger_daemon.AUTOMATIC_TRIGGER_EXECUTION_ENABLED = True

    async with async_session() as db:
        tenant = Tenant(
            name="A2A Migration Smoke Company",
            slug=f"a2a-migration-smoke-{uuid.uuid4().hex[:12]}",
            im_provider="web_only",
            is_active=True,
        )
        db.add(tenant)
        await db.flush()
        owner = User(
            display_name="A2A Migration Smoke",
            role="member",
            tenant_id=tenant.id,
        )
        db.add(owner)
        await db.flush()
        model_id = (
            await db.execute(
                select(LLMModel.id)
                .where(
                    LLMModel.enabled.is_(True),
                    LLMModel.modality == "text",
                )
                .order_by(LLMModel.created_at, LLMModel.id)
                .limit(1)
            )
        ).scalar_one()
        source = Agent(
            name="A2A Source",
            creator_id=owner.id,
            tenant_id=tenant.id,
            primary_model_id=model_id,
            status="idle",
        )
        target = Agent(
            name="A2A Target",
            creator_id=owner.id,
            tenant_id=tenant.id,
            primary_model_id=model_id,
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

    async with async_session() as db:
        ordinary_trigger = AgentTrigger(
            agent_id=target.id,
            name="ordinary serialization smoke",
            type="interval",
            config={"minutes": 5},
            reason="cross-source serialization smoke",
            is_enabled=True,
        )
        db.add(ordinary_trigger)
        await db.commit()
        ordinary_execution, created = await enqueue_trigger_execution(
            db,
            trigger=ordinary_trigger,
            source="interval",
            idempotency_key="ordinary-between-a2a",
        )
        assert created is True
        assert ordinary_execution is not None
        ordinary_execution_id = ordinary_execution.id

    second_message_id = uuid.uuid4()
    second_delivery_key = f"a2a:{second_message_id}"
    assert (
        await wake_agent_with_context(
            target.id,
            "[From A2A Source] second durable delivery",
            from_agent_id=source.id,
            message_kind="notify",
            idempotency_key=second_delivery_key,
            source_message_id=second_message_id,
        )
        is True
    )

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
            (
                await db.execute(
                    select(TriggerExecution).where(
                        TriggerExecution.trigger_id == trigger.id,
                        TriggerExecution.idempotency_key == delivery_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(executions) == 1
        assert executions[0].status == "processing"
        assert executions[0].lease_owner is None
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

        runtime_execution_ids = {
            expected_execution_id,
            ordinary_execution_id,
            second_execution_id,
        }
        runs = list(
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.source_type == "trigger",
                        AgentRun.source_execution_id.in_(str(item) for item in runtime_execution_ids),
                    )
                )
            ).scalars()
        )
        assert len(runs) == 3
        expected_lane = f"trigger:{tenant.id}:{target.id}"
        assert {run.scheduling_lane_key for run in runs} == {expected_lane}
        assert all(run.scheduling_position_id in runtime_execution_ids for run in runs)
        run_ids = {run.id for run in runs}
        session_ids = {run.session_id for run in runs if run.session_id is not None}
        commands = list(
            (await db.execute(select(AgentRunCommand).where(AgentRunCommand.run_id.in_(run_ids)))).scalars()
        )
        assert len(commands) == 3
        assert all(command.status == "pending" for command in commands)

    async with async_session() as db:
        claimed = await claim_next_command(
            db,
            claimant="a2a-runtime-postgres-smoke",
            claim_ttl_seconds=60,
            max_attempts=3,
        )
        assert claimed is not None and claimed.run_id in run_ids
        await db.commit()

    # The first claimed Runtime command owns the Agent lane; later Trigger
    # commands remain pending until a terminal checkpoint releases it.
    async with async_session() as db:
        held = list(
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.id.in_(run_ids),
                        AgentRun.lane_held.is_(True),
                    )
                )
            ).scalars()
        )
        assert len(held) == 1
        pending_count = len(
            list(
                (
                    await db.execute(
                        select(AgentRunCommand).where(
                            AgentRunCommand.run_id.in_(run_ids),
                            AgentRunCommand.status == "pending",
                        )
                    )
                ).scalars()
            )
        )
        assert pending_count == 2

        # Runtime schema tables must be empty before the later downgrade lane.
        await db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
        await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(str(item) for item in session_ids)))
        await db.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
        await db.execute(delete(TriggerExecution).where(TriggerExecution.id.in_(runtime_execution_ids)))
        await db.commit()

    # Recreate the same arrival order as pre-v1.11 durable rows. These rows do
    # not own Runtime Runs and must retain lease fencing during an upgrade.
    scheduled_at = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add_all(
            [
                TriggerExecution(
                    id=expected_execution_id,
                    trigger_id=trigger.id,
                    agent_id=target.id,
                    source="a2a",
                    status="pending",
                    idempotency_key=delivery_key,
                    payload={"_a2a_kind": "task_delegate"},
                    payload_text="",
                    scheduled_at=scheduled_at,
                ),
                TriggerExecution(
                    id=ordinary_execution_id,
                    trigger_id=ordinary_trigger.id,
                    agent_id=target.id,
                    source="interval",
                    status="pending",
                    idempotency_key="ordinary-between-a2a",
                    payload={},
                    payload_text="",
                    scheduled_at=scheduled_at + timedelta(seconds=1),
                ),
                TriggerExecution(
                    id=second_execution_id,
                    trigger_id=trigger.id,
                    agent_id=target.id,
                    source="a2a",
                    status="pending",
                    idempotency_key=second_delivery_key,
                    payload={"_a2a_kind": "notify"},
                    payload_text="",
                    scheduled_at=scheduled_at + timedelta(seconds=2),
                ),
            ]
        )
        await db.commit()

    first_claim = await claim_pending_trigger_executions(sources=["a2a"])
    claimed_ids = [item[0].id for item in first_claim]
    assert expected_execution_id in claimed_ids
    assert second_execution_id not in claimed_ids
    execution_id = expected_execution_id
    first_execution = next(item[0] for item in first_claim if item[0].id == execution_id)
    first_lease_token = first_execution.lease_owner
    assert first_lease_token

    # The active head owns this Agent's A2A lane until it completes or its
    # lease expires. A second daemon tick must not claim the next message.
    active_head_claim = await claim_pending_trigger_executions(sources=["a2a"])
    assert second_execution_id not in [item[0].id for item in active_head_claim]
    cross_source_claim = await claim_pending_trigger_executions()
    assert ordinary_execution_id not in [item[0].id for item in cross_source_claim]

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
    reclaimed_execution = next(item[0] for item in second_claim if item[0].id == execution_id)
    second_lease_token = reclaimed_execution.lease_owner
    assert second_lease_token and second_lease_token != first_lease_token

    # A coroutine from the expired claim generation must not overwrite the
    # result owned by the reclaimed generation.
    assert await mark_trigger_executions_completed([(execution_id, first_lease_token)]) == 0
    assert await mark_trigger_executions_completed([(execution_id, second_lease_token)]) == 1

    # The ordinary execution was queued between the two A2A messages. A claim
    # restricted to A2A must still respect that cross-source head.
    blocked_second_a2a = await claim_pending_trigger_executions(sources=["a2a"])
    assert second_execution_id not in [item[0].id for item in blocked_second_a2a]

    ordinary_claim = await claim_pending_trigger_executions()
    assert ordinary_execution_id in [item[0].id for item in ordinary_claim]
    ordinary_execution = next(item[0] for item in ordinary_claim if item[0].id == ordinary_execution_id)
    assert ordinary_execution.lease_owner

    ordinary_blocks_a2a = await claim_pending_trigger_executions(sources=["a2a"])
    assert second_execution_id not in [item[0].id for item in ordinary_blocks_a2a]
    assert await mark_trigger_executions_completed([(ordinary_execution_id, ordinary_execution.lease_owner)]) == 1

    final_claim = await claim_pending_trigger_executions(sources=["a2a"])
    assert second_execution_id in [item[0].id for item in final_claim]
    final_execution = next(item[0] for item in final_claim if item[0].id == second_execution_id)
    assert final_execution.lease_owner
    assert await mark_trigger_executions_completed([(second_execution_id, final_execution.lease_owner)]) == 1

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution is not None
        assert execution.status == "completed"
        assert execution.lease_owner is None
        assert execution.lease_expires_at is None

    print("A2A PostgreSQL durable queue smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
