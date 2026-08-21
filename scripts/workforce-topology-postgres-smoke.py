#!/usr/bin/env python3
"""Exercise bounded topology source queries against a scaled PostgreSQL fixture."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, text

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ChatMessage  # noqa: F401 - registers media FK target metadata
from app.models.chat_session import ChatSession  # noqa: F401 - registers run FK target metadata
from app.models.deliverable import (  # noqa: F401 - registers media FK target metadata
    DeliverableExecution,
    DeliverableExecutionUnit,
)
from app.models.llm import LLMModel  # noqa: F401 - registers Agent/Tenant FK target metadata
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditReservation  # noqa: F401 - registers media FK target metadata
from app.models.tenant import Tenant
from app.models.user import User
from app.services.workforce_topology import (
    TOPOLOGY_PER_AGENT_SOURCE_ROWS,
    _load_topology_execution_summaries,
)


async def main() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        tenant = Tenant(
            id=tenant_id,
            name="Workforce Topology PostgreSQL Smoke",
            slug=f"workforce-topology-{tenant_id.hex[:12]}",
            im_provider="web_only",
            is_active=True,
        )
        owner = User(
            id=user_id,
            display_name="Topology Smoke Owner",
            role="org_owner",
            tenant_id=tenant_id,
        )
        agents = [
            Agent(
                name=f"Topology Agent {index}",
                creator_id=user_id,
                tenant_id=tenant_id,
                status="idle",
            )
            for index in range(3)
        ]
        db.add(tenant)
        await db.flush()
        db.add(owner)
        await db.flush()
        db.add_all(agents)
        await db.flush()

        runs: list[AgentRun] = []
        events: list[AgentRunEvent] = []
        media_tasks: list[MediaGenerationTask] = []
        source_rows = TOPOLOGY_PER_AGENT_SOURCE_ROWS + 7
        for agent_index, agent in enumerate(agents):
            for row_index in range(source_rows):
                active = row_index == source_rows - 1
                created_at = now - timedelta(minutes=(90 if active else row_index))
                run = AgentRun(
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    source_type="chat",
                    origin_user_id=user_id,
                    goal=f"Topology run {agent_index}-{row_index}",
                    run_kind="foreground",
                    model_id=None,
                    model_turn_limit=8,
                    runtime_type="legacy",
                    runtime_thread_id=f"topology-smoke-{uuid.uuid4().hex}",
                    graph_name="direct_chat",
                    graph_version="postgres-smoke",
                    delivery_status="not_required",
                    delivery_target={"kind": "direct"},
                    created_at=created_at,
                    updated_at=created_at,
                )
                runs.append(run)
                db.add(run)
                await db.flush()
                events.extend(
                    [
                        AgentRunEvent(
                            run_id=run.id,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            event_type="run_created",
                            summary="Created",
                            idempotency_key=f"{run.id}:created",
                            source_checkpoint_id=f"{run.id}:created",
                            created_at=created_at,
                        ),
                        AgentRunEvent(
                            run_id=run.id,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            event_type="status_changed",
                            summary="Running",
                            payload={"activity_type": "thinking"},
                            idempotency_key=f"{run.id}:running",
                            source_checkpoint_id=f"{run.id}:running",
                            created_at=created_at + timedelta(seconds=1),
                        ),
                    ]
                )
                if not active:
                    events.append(
                        AgentRunEvent(
                            run_id=run.id,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            event_type="run_completed",
                            summary="Completed",
                            idempotency_key=f"{run.id}:completed",
                            source_checkpoint_id=f"{run.id}:completed",
                            created_at=created_at + timedelta(seconds=2),
                        )
                    )
                media_tasks.append(
                    MediaGenerationTask(
                        tenant_id=tenant_id,
                        agent_id=agent.id,
                        user_id=user_id,
                        provider="smoke",
                        modality="image",
                        provider_task_id=f"topology-smoke-{uuid.uuid4().hex}",
                        status="processing" if active else "succeeded",
                        metadata_path=f"smoke/{uuid.uuid4().hex}.json",
                        output_path=f"smoke/{uuid.uuid4().hex}.png",
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
        db.add_all([*events, *media_tasks])
        await db.commit()

    agent_ids = {agent.id for agent in agents}
    async with async_session() as db:
        await db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        projected = await _load_topology_execution_summaries(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            employee_ids=agent_ids,
            auditable_agent_ids=agent_ids,
            since=now - timedelta(hours=24),
        )

    assert set(projected) == agent_ids
    for summary in projected.values():
        assert summary.status == "running"
        assert summary.active_count == 2
        assert summary.recently_finished_count == 2 * (
            TOPOLOGY_PER_AGENT_SOURCE_ROWS - 1
        )

    async with async_session() as db:
        await db.execute(
            delete(MediaGenerationTask).where(
                MediaGenerationTask.tenant_id == tenant_id
            )
        )
        await db.execute(
            delete(AgentRunEvent).where(AgentRunEvent.tenant_id == tenant_id)
        )
        await db.execute(delete(AgentRun).where(AgentRun.tenant_id == tenant_id))
        await db.execute(delete(Agent).where(Agent.tenant_id == tenant_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()

    print("workforce_topology_postgres_smoke=ok")


async def _run() -> None:
    try:
        await main()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
