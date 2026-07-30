"""Lightweight asyncio scheduler for durable Agent Runtime cron jobs.

Runs as a background task inside the FastAPI process.
Every 30 seconds, checks for schedules whose next_run_at <= now
and registers each occurrence on the shared Runtime.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from croniter import croniter
from loguru import logger
from sqlalchemy import select, text

from app.config import get_settings


AUTOMATIC_SCHEDULE_EXECUTION_ENABLED = (
    get_settings().USER_SCHEDULE_EXECUTION_ENABLED
)


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Compute the next run time from a cron expression."""
    try:
        base = after or datetime.now(timezone.utc)
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime).replace(tzinfo=timezone.utc)
    except Exception as exc:
        logger.error(
            "Invalid cron expression rejected error_type={}",
            type(exc).__name__,
        )
        return None


@asynccontextmanager
async def _schedule_execution_lock(schedule_id: uuid.UUID):
    """Serialize one schedule across API and worker processes.

    PostgreSQL session advisory locks are held on a dedicated connection for
    the complete provider/tool run. A process crash closes the connection and
    releases the lock; another process never inherits a pooled locked session.
    """
    from app.database import engine

    if engine.dialect.name != "postgresql":
        yield True
        return

    lock_key = f"astra:user-schedule:{schedule_id}"
    async with engine.connect() as connection:
        acquired = bool(
            await connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended(:lock_key, 0))"
                ),
                {"lock_key": lock_key},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": lock_key},
                )


async def _execute_schedule(
    schedule_id: uuid.UUID,
    *,
    require_enabled: bool = True,
) -> None:
    """Execute one durable schedule after revalidating its owner and Agent."""
    if not AUTOMATIC_SCHEDULE_EXECUTION_ENABLED:
        logger.warning(
            "Schedule execution is disabled schedule_id={}",
            schedule_id,
        )
        return
    async with _schedule_execution_lock(schedule_id) as acquired:
        if not acquired:
            logger.info(
                "Schedule {} already has an active cross-process execution",
                schedule_id,
            )
            return
        await _execute_schedule_claimed(
            schedule_id,
            require_enabled=require_enabled,
        )


async def _execute_schedule_claimed(
    schedule_id: uuid.UUID,
    *,
    require_enabled: bool,
) -> None:
    """Run a schedule while its dedicated cross-process lock is held."""
    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.schedule import AgentSchedule
        from app.models.user import User

        async with async_session() as db:
            schedule = (
                await db.execute(
                    select(AgentSchedule).where(AgentSchedule.id == schedule_id)
                )
            ).scalar_one_or_none()
            if schedule is None or (require_enabled and not schedule.is_enabled):
                logger.info("Schedule {} is absent or disabled; skipping", schedule_id)
                return

            agent_id = schedule.agent_id
            instruction = schedule.instruction
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"Schedule {schedule_id}: agent {agent_id} not found")
                return

            creator = (
                await db.execute(select(User).where(User.id == schedule.created_by))
            ).scalar_one_or_none()
            identity = getattr(creator, "identity", None) if creator else None
            if (
                creator is None
                or not creator.is_active
                or (identity is not None and not identity.is_active)
                or creator.tenant_id != agent.tenant_id
                or schedule.created_by != agent.creator_id
            ):
                logger.warning(
                    "Schedule {} requester authorization is no longer valid",
                    schedule_id,
                )
                return

            if agent.status != "running" or agent.deletion_requested_at is not None:
                logger.info(f"Schedule {schedule_id}: agent {agent.id} not running, skipping")
                return

            from app.core.permissions import is_agent_expired
            if is_agent_expired(agent):
                logger.info(f"Schedule {schedule_id}: agent {agent.id} has expired, skipping")
                return

            # Build context and call LLM with failover support
            from app.services.agent_context import build_agent_context
            from app.services.llm import call_agent_llm_with_tools

            static_prompt, dynamic_prompt = await build_agent_context(
                agent_id,
                agent.name,
                agent.role_description or "",
                current_user_name=creator.display_name,
            )
            system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

            user_prompt = f"[自动调度任务] {instruction}"

            # Call LLM with unified failover support
            reply = await call_agent_llm_with_tools(
                db=db,
                agent_id=agent_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_rounds=50,
                session_id=str(schedule_id),
                requester_user_id=creator.id,
            )

            # Log activity
            from app.services.activity_logger import log_activity
            await log_activity(
                agent_id, "schedule_run",
                "定时任务执行完成",
                detail={
                    "schedule_id": str(schedule_id),
                    "instruction_chars": len(instruction),
                    "reply_chars": len(reply),
                },
            )

            logger.info(
                "Schedule {} executed for agent {} reply_chars={}",
                schedule_id,
                agent.id,
                len(reply),
            )

    except Exception as exc:
        logger.error(
            "Schedule execution failed schedule_id={} error_type={}",
            schedule_id,
            type(exc).__name__,
        )


async def _tick():
    """One scheduler tick: find and execute due schedules."""
    if not AUTOMATIC_SCHEDULE_EXECUTION_ENABLED:
        return
    from app.database import async_session
    from app.core.permissions import is_agent_expired
    from app.models.agent import Agent
    from app.models.schedule import AgentSchedule
    from app.services.audit_logger import write_audit_log
    from app.services.heartbeat_runtime import (
        enqueue_schedule_runtime,
        schedule_occurrence_id,
    )

    now = datetime.now(timezone.utc)

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentSchedule)
                .where(
                    AgentSchedule.is_enabled.is_(True),
                    AgentSchedule.next_run_at <= now,
                )
                .order_by(AgentSchedule.next_run_at, AgentSchedule.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
            due_schedules = result.scalars().all()

            if due_schedules:
                await write_audit_log(
                    "schedule_tick", {"due_count": len(due_schedules)}
                )

            for sched in due_schedules:
                occurrence_at = sched.next_run_at
                if occurrence_at is None:
                    continue
                agent_result = await db.execute(
                    select(Agent).where(
                        Agent.id == sched.agent_id,
                        Agent.deleted_at.is_(None),
                    )
                )
                agent = agent_result.scalar_one_or_none()
                if (
                    agent is None
                    or agent.status not in {"creating", "running", "idle"}
                    or is_agent_expired(agent)
                ):
                    logger.info(
                        "Schedule {} owner Agent is unavailable; advancing occurrence",
                        sched.id,
                    )
                    sched.last_run_at = now
                    sched.next_run_at = compute_next_run(sched.cron_expr, now)
                    sched.run_count = (sched.run_count or 0) + 1
                    await db.commit()
                    continue

                handle = await enqueue_schedule_runtime(
                    db,
                    agent=agent,
                    schedule_id=sched.id,
                    occurrence_id=schedule_occurrence_id(sched.id, occurrence_at),
                    instruction=sched.instruction,
                )
                if handle is None:
                    logger.error(
                        "Schedule {} Runtime is disabled; occurrence remains due",
                        sched.id,
                    )
                    await db.rollback()
                    return

                next_run = compute_next_run(sched.cron_expr, now)
                sched.last_run_at = now
                sched.next_run_at = next_run
                sched.run_count = (sched.run_count or 0) + 1
                await db.commit()
                await write_audit_log(
                    "schedule_fire",
                    {
                        "schedule_id": str(sched.id),
                        "name": sched.name,
                        "instruction_chars": len(sched.instruction),
                        "next_run": str(next_run),
                        "run_id": str(handle.run_id),
                    },
                    agent_id=sched.agent_id,
                )
                logger.info(
                    "Queued schedule {} as Runtime Run {}",
                    sched.id,
                    handle.run_id,
                )

    except Exception as exc:
        logger.error("Scheduler tick failed error_type={}", type(exc).__name__)
        await write_audit_log("schedule_error", {"error_type": type(exc).__name__})


async def start_scheduler():
    """Run the user-schedule daemon for the lifetime of the worker process.

    The operator kill switch pauses ticks but must not let this critical worker
    task return: an unexpected return is treated as a failed worker runtime and
    causes the dedicated container to restart.
    """
    if not AUTOMATIC_SCHEDULE_EXECUTION_ENABLED:
        logger.info("Agent schedule execution is disabled by the operator")
    else:
        logger.info("Agent schedule execution started")
    while True:
        if AUTOMATIC_SCHEDULE_EXECUTION_ENABLED:
            await _tick()
        await asyncio.sleep(30)
