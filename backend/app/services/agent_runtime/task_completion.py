"""Idempotent Task product updates from terminal Runtime checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Callable
import uuid

from sqlalchemy import select

from app.models.audit import ChatMessage
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.task import Task, TaskLog
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
    RuntimeSessionFactory,
)


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_EVENTS = frozenset({"run_completed", "run_failed", "run_cancelled"})
_GROUP_TASK_CORRELATION_PREFIX = "work-task:"


class TaskRuntimeCompletionError(RuntimeError):
    """A terminal Task Run cannot be applied to its product record safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _task_log_id(run_id: uuid.UUID, checkpoint_id: str) -> uuid.UUID:
    return uuid.uuid5(run_id, f"task-terminal:{checkpoint_id}")


def _group_task_log_id(task_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(task_id, "group-task-terminal")


def _group_task_id(correlation_id: str | None) -> uuid.UUID | None:
    if not correlation_id or not correlation_id.startswith(_GROUP_TASK_CORRELATION_PREFIX):
        return None
    try:
        return uuid.UUID(correlation_id.removeprefix(_GROUP_TASK_CORRELATION_PREFIX))
    except ValueError:
        return None


def _terminal_detail(checkpoint: CheckpointObservation) -> str:
    lifecycle = checkpoint.state["lifecycle"]
    status = lifecycle["status"]
    if status == "completed":
        answer = lifecycle.get("final_answer")
        if not isinstance(answer, str) or not answer.strip():
            raise TaskRuntimeCompletionError(
                "missing_task_result",
                "completed Task checkpoint has no final answer",
            )
        return answer.strip()
    error = lifecycle.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    reason = lifecycle.get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else status


def _compact_status_detail(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:240] if compact else None


def _group_terminal_summary(event: AgentRunEvent) -> tuple[str, str | None]:
    """Return a user-safe participant status without exposing provider payloads."""
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    if event.event_type == "run_completed":
        return "已完成", None

    error = payload.get("error")
    nested_code = error.get("code") if isinstance(error, Mapping) else None
    detail = next(
        (
            compact
            for value in (
                payload.get("error_code"),
                payload.get("failure_code"),
                nested_code,
                payload.get("reason"),
            )
            if (compact := _compact_status_detail(value)) is not None
        ),
        None,
    )
    if detail is None:
        detail = _compact_status_detail(event.summary)
    return (
        ("执行失败", detail)
        if event.event_type == "run_failed"
        else ("已取消", detail)
    )


class TaskRuntimeCompletionHandler:
    """Set Task status and append exactly one terminal log per checkpoint."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        group_task_id = _group_task_id(run.correlation_id)
        if run.source_type != "task" and group_task_id is None:
            return
        status = checkpoint.state["lifecycle"]["status"]
        if status not in _TERMINAL_STATUSES:
            return
        if group_task_id is not None:
            await self._handle_group_task(
                run=run,
                task_id=group_task_id,
            )
            return
        try:
            agent_id = uuid.UUID(run.agent_id or "")
        except ValueError as exc:
            raise TaskRuntimeCompletionError(
                "invalid_task_run_identity",
                "Task Run has no valid Agent identity",
            ) from exc

        receipt_id = _task_log_id(run.run_id, checkpoint.checkpoint_id)
        async with self._session_factory() as db:
            async with db.begin():
                run_result = await db.execute(
                    select(AgentRun).where(
                        AgentRun.tenant_id == run.tenant_id,
                        AgentRun.id == run.run_id,
                        AgentRun.source_type == "task",
                    )
                )
                stored_run = run_result.scalar_one_or_none()
                if stored_run is None or stored_run.source_id is None:
                    raise TaskRuntimeCompletionError(
                        "task_source_missing",
                        "terminal Task Run has no source Task",
                    )
                try:
                    task_id = uuid.UUID(stored_run.source_id)
                except ValueError as exc:
                    raise TaskRuntimeCompletionError(
                        "invalid_task_source",
                        "terminal Task Run source_id is not a UUID",
                    ) from exc

                receipt_result = await db.execute(
                    select(TaskLog.id).where(TaskLog.id == receipt_id)
                )
                if receipt_result.scalar_one_or_none() is not None:
                    return

                task_result = await db.execute(
                    select(Task)
                    .where(
                        Task.id == task_id,
                    )
                    .with_for_update()
                )
                task = task_result.scalar_one_or_none()
                if task is None:
                    # Deleting a product Task does not delete or invalidate its
                    # authoritative execution history.
                    return
                if task.agent_id != agent_id:
                    raise TaskRuntimeCompletionError(
                        "task_agent_mismatch",
                        "terminal Runtime source Task belongs to another Agent",
                    )

                detail = _terminal_detail(checkpoint)
                is_supervision = task.type == "supervision"
                if status == "completed" and not is_supervision:
                    task.status = "done"
                    task.completed_at = self._clock()
                    content = f"✅ 任务完成\n\n{detail}"
                elif status == "completed":
                    task.status = "pending"
                    task.completed_at = None
                    content = f"✅ 督办执行完成\n\n{detail}"
                elif status == "cancelled":
                    task.status = "pending"
                    task.completed_at = None
                    label = "督办" if is_supervision else "任务"
                    content = f"⏹️ {label}执行已取消：{detail}"
                else:
                    task.status = "pending"
                    task.completed_at = None
                    label = "督办" if is_supervision else "任务"
                    content = f"❌ {label}执行失败：{detail}"
                db.add(
                    TaskLog(
                        id=receipt_id,
                        task_id=task.id,
                        content=content,
                    )
                )
                await db.flush()

    async def _handle_group_task(
        self,
        *,
        run: RuntimeRunRecord,
        task_id: uuid.UUID,
    ) -> None:
        correlation_id = f"{_GROUP_TASK_CORRELATION_PREFIX}{task_id}"
        async with self._session_factory() as db:
            async with db.begin():
                stored_run = (
                    await db.execute(
                        select(AgentRun).where(
                            AgentRun.tenant_id == run.tenant_id,
                            AgentRun.id == run.run_id,
                            AgentRun.source_type == "chat",
                            AgentRun.correlation_id == correlation_id,
                        )
                    )
                ).scalar_one_or_none()
                if stored_run is None:
                    raise TaskRuntimeCompletionError(
                        "group_task_run_missing",
                        "terminal Group Task Run is not linked to its confirmed Task",
                    )

                task = (
                    await db.execute(
                        select(Task)
                        .where(
                            Task.id == task_id,
                            Task.tenant_id == run.tenant_id,
                            Task.executor_kind == "group",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if task is None:
                    return
                receipt_id = _group_task_log_id(task.id)
                existing_receipt = (
                    await db.execute(
                        select(TaskLog.id).where(TaskLog.id == receipt_id)
                    )
                ).scalar_one_or_none()
                if existing_receipt is not None:
                    return

                participant_runs = list(
                    (
                        await db.execute(
                            select(AgentRun)
                            .where(
                                AgentRun.tenant_id == run.tenant_id,
                                AgentRun.correlation_id == correlation_id,
                                AgentRun.system_role.is_(None),
                            )
                            .order_by(AgentRun.created_at, AgentRun.id)
                        )
                    ).scalars().all()
                )
                if not participant_runs:
                    await self._reconcile_group_planning_failure(
                        db=db,
                        task=task,
                        root_run=stored_run,
                        receipt_id=receipt_id,
                    )
                    return

                participant_run_ids = [candidate.id for candidate in participant_runs]
                terminal_events = list(
                    (
                        await db.execute(
                            select(AgentRunEvent).where(
                                AgentRunEvent.tenant_id == run.tenant_id,
                                AgentRunEvent.run_id.in_(participant_run_ids),
                                AgentRunEvent.event_type.in_(tuple(_TERMINAL_EVENTS)),
                            )
                        )
                    ).scalars().all()
                )
                terminal_by_run = {event.run_id: event for event in terminal_events}
                if set(terminal_by_run) != set(participant_run_ids):
                    return

                result_by_run = await self._group_result_messages(
                    db=db,
                    tenant_id=run.tenant_id,
                    run_ids=participant_run_ids,
                )
                event_types = {event.event_type for event in terminal_by_run.values()}
                if "run_failed" in event_types:
                    task.status = "pending"
                    task.completed_at = None
                    headline = "❌ Group 协作任务未完成"
                elif "run_cancelled" in event_types:
                    task.status = "pending"
                    task.completed_at = None
                    headline = "⏹️ Group 协作任务已取消"
                else:
                    task.status = "done"
                    task.completed_at = self._clock()
                    headline = "✅ Group 协作任务完成"

                participant_snapshot = list(
                    dict(task.executor_snapshot or {}).get("participants") or []
                )
                name_by_agent = {
                    str(entry.get("agent_id")): str(entry.get("agent_name") or "Agent")
                    for entry in participant_snapshot
                    if isinstance(entry, Mapping)
                }
                result_sections: list[str] = []
                for participant_run in participant_runs:
                    name = name_by_agent.get(str(participant_run.agent_id), "Agent")
                    event = terminal_by_run[participant_run.id]
                    status_label, failure_detail = _group_terminal_summary(event)
                    result = result_by_run.get(participant_run.id)
                    section = [f"{name} · {status_label}"]
                    if result:
                        section.append(result)
                    elif failure_detail:
                        section.append(f"原因：{failure_detail}")
                    elif event.event_type == "run_completed":
                        section.append("结果已写入 Group 会话，请打开协作现场查看。")
                    result_sections.append("\n".join(section))
                detail = "\n\n".join(result_sections)
                db.add(
                    TaskLog(
                        id=receipt_id,
                        task_id=task.id,
                        content=f"{headline}\n\n{detail}",
                    )
                )
                await db.flush()

    async def _reconcile_group_planning_failure(
        self,
        *,
        db,
        task: Task,
        root_run: AgentRun,
        receipt_id: uuid.UUID,
    ) -> None:
        result_by_run = await self._group_result_messages(
            db=db,
            tenant_id=root_run.tenant_id,
            run_ids=[root_run.id],
        )
        failure = result_by_run.get(root_run.id)
        if not failure:
            return
        task.status = "pending"
        task.completed_at = None
        db.add(
            TaskLog(
                id=receipt_id,
                task_id=task.id,
                content=f"❌ Group 任务规划未完成\n\n{failure}",
            )
        )
        await db.flush()

    @staticmethod
    async def _group_result_messages(
        *,
        db,
        tenant_id: uuid.UUID,
        run_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        delivery_events = list(
            (
                await db.execute(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.tenant_id == tenant_id,
                        AgentRunEvent.run_id.in_(run_ids),
                        AgentRunEvent.event_type == "delivery_succeeded",
                    )
                    .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
                )
            ).scalars().all()
        )
        message_id_by_run: dict[uuid.UUID, uuid.UUID] = {}
        for event in delivery_events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("delivery_kind") != "terminal":
                continue
            try:
                message_id = uuid.UUID(str(payload.get("message_id")))
            except (TypeError, ValueError):
                continue
            message_id_by_run.setdefault(event.run_id, message_id)
        if not message_id_by_run:
            return {}
        messages = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.id.in_(list(message_id_by_run.values()))
                    )
                )
            ).scalars().all()
        )
        content_by_message = {message.id: message.content for message in messages}
        return {
            run_id: content_by_message[message_id]
            for run_id, message_id in message_id_by_run.items()
            if message_id in content_by_message
        }


__all__ = [
    "TaskRuntimeCompletionError",
    "TaskRuntimeCompletionHandler",
]
