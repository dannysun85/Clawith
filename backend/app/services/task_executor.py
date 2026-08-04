"""Durable Runtime intake for todo and supervision Task executions."""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.task import Task, TaskLog
from app.services.agent_runtime.adapter import RuntimeCommandIntake
from app.services.agent_runtime.config import decide_runtime_v2
from app.services.agent_runtime.contracts import RunHandle, StartRunCommand
from app.services.agent_runtime.model_route import (
    RuntimeModelRouteError,
    resolve_runtime_model_route,
)
from app.services.group_message_service import (
    GroupMessageServiceError,
    enqueue_group_message,
)

settings = get_settings()
AUTOMATIC_TASK_EXECUTION_ENABLED = settings.USER_TASK_EXECUTION_ENABLED
_BRIEF_ONLY_WORK_TYPES = frozenset({"image", "video", "presentation", "document"})


class TaskRuntimeIntakeError(RuntimeError):
    """A Task selected for Runtime v2 cannot be registered safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _task_goal(task: Task) -> str:
    if task.type == "supervision":
        goal = f"[督办任务] {task.title}"
    else:
        goal = f"[任务执行] {task.title}"
    if task.description:
        goal += f"\n任务描述: {task.description}"
    statement = dict(task.work_statement or {})
    if statement:
        goal += (
            f"\n已确认工作类型: {statement.get('work_type') or task.work_type}"
            f"\n已确认交付边界: {statement.get('expected_output') or 'task_result'}"
        )
        criteria = statement.get("completion_criteria")
        if isinstance(criteria, list):
            normalized_criteria = [str(item).strip() for item in criteria if str(item).strip()]
            if normalized_criteria:
                goal += "\n完成标准:\n- " + "\n- ".join(normalized_criteria)
    if _application_tools_enabled_for_task(task) is False:
        goal += (
            "\n\n本 Run 仅负责整理并返回可确认的 Brief。"
            "应用工具已由运行时关闭；不得生成、上传或声称已经交付正式产物。"
            "正式制作必须由用户进入独立 Deliverable 预检与确认流程。"
        )
    if task.executor_kind == "temporary_expert":
        expert_role = str((task.executor_snapshot or {}).get("expert_role") or "").strip()
        if expert_role:
            goal += (
                f"\n临时专家角色（仅本任务）: {expert_role}"
                "\n请在此任务范围内以该专业角色分析和执行，不建立长期员工身份或长期记忆。"
            )
    if task.type == "supervision":
        if task.supervision_target_name:
            goal += f"\n督办对象: {task.supervision_target_name}"
        return goal + "\n\n请执行此督办任务：联系督办对象，了解进展，并汇报结果。"
    return goal + "\n\n请认真完成此任务，给出详细的执行结果。"


def _application_tools_enabled_for_task(task: Task) -> bool:
    """Keep a confirmed creative Brief separate from paid formal delivery."""

    return not (
        task.origin_type == "workbench"
        and task.work_type in _BRIEF_ONLY_WORK_TYPES
    )


async def enqueue_task_runtime(
    db: AsyncSession,
    *,
    task: Task,
    agent: Agent,
    execution_id: uuid.UUID | None = None,
    settings_override: Settings | None = None,
) -> RunHandle | None:
    """Register one Task execution in the caller transaction when v2 is selected."""
    runtime_settings = settings_override or settings
    if task.type not in {"todo", "supervision"}:
        raise TaskRuntimeIntakeError(
            "task_type_unsupported",
            f"Runtime does not support Task type {task.type!r}",
        )
    if task.origin_type == "workbench" and (
        not task.confirmation_fingerprint or task.confirmed_at is None
    ):
        raise TaskRuntimeIntakeError(
            "task_confirmation_required",
            "A workbench Task must preserve explicit user confirmation before execution",
        )
    decision = decide_runtime_v2(
        agent_id=agent.id,
        source_type="task",
        settings=runtime_settings,
    )
    if not decision.use_v2:
        return None
    if task.agent_id != agent.id:
        raise TaskRuntimeIntakeError(
            "task_agent_mismatch",
            "Task does not belong to the requested Agent",
        )
    if agent.tenant_id is None:
        raise TaskRuntimeIntakeError(
            "agent_tenant_missing",
            "Runtime Task Agent has no tenant",
        )
    try:
        route = await resolve_runtime_model_route(agent)
    except RuntimeModelRouteError as exc:
        raise TaskRuntimeIntakeError(
            "agent_model_missing",
            "Runtime Task Agent has no available model route",
        ) from exc

    if task.type == "supervision":
        occurrence_id = execution_id or uuid.uuid4()
        source_execution_id = f"task:{task.id}:supervision:{occurrence_id}"
    else:
        source_execution_id = f"task:{task.id}"

    handle = await RuntimeCommandIntake(
        db,
        settings=runtime_settings,
    ).start_run(
        StartRunCommand(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source_type="task",
            source_id=str(task.id),
            source_execution_id=source_execution_id,
            goal=_task_goal(task),
            run_kind="background",
            model_id=route.model_id,
            delivery_status="not_required",
            idempotency_key=f"start:{source_execution_id}",
            payload={
                "task_id": str(task.id),
                "work_task_id": str(task.id),
                "task_type": task.type,
                "title": task.title,
                "description": task.description,
                "executor_kind": task.executor_kind,
                "executor_snapshot": dict(task.executor_snapshot or {}),
                "work_type": task.work_type,
                "work_statement": dict(task.work_statement or {}),
                "confirmation_fingerprint": task.confirmation_fingerprint,
                "confirmed_at": task.confirmed_at.isoformat() if task.confirmed_at else None,
                "application_tools_enabled": _application_tools_enabled_for_task(task),
                "saas_tier": route.saas_tier,
                "model_modality": route.modality,
                "fallback_model_id": (
                    str(route.fallback_model_id)
                    if route.fallback_model_id is not None
                    else None
                ),
            },
            origin_user_id=task.created_by,
            actor_user_id=task.created_by,
        )
    )
    task.status = "doing"
    if handle.created:
        db.add(
            TaskLog(
                task_id=task.id,
                content=f"🤖 已进入持久化执行队列（Run {handle.run_id}）",
            )
        )
    return handle


def _snapshot_uuid(snapshot: dict, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(snapshot[field]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskRuntimeIntakeError(
            "group_task_snapshot_invalid",
            f"Group Task executor snapshot has no valid {field}",
        ) from exc


async def enqueue_group_task_runtime(
    db: AsyncSession,
    *,
    task: Task,
    primary_agent: Agent,
    settings_override: Settings | None = None,
) -> RunHandle:
    """Start one confirmed Work task through the native Group Runtime."""
    if task.origin_type != "workbench" or task.executor_kind != "group":
        raise TaskRuntimeIntakeError(
            "group_task_contract_invalid",
            "Only a confirmed Group workbench Task may use Group Runtime intake",
        )
    if not task.confirmation_fingerprint or task.confirmed_at is None:
        raise TaskRuntimeIntakeError(
            "task_confirmation_required",
            "A Group workbench Task must preserve explicit user confirmation",
        )
    if task.agent_id != primary_agent.id or task.tenant_id != primary_agent.tenant_id:
        raise TaskRuntimeIntakeError(
            "task_agent_mismatch",
            "Group Task primary owner does not match its confirmed Agent",
        )
    snapshot = dict(task.executor_snapshot or {})
    participants = snapshot.get("participants")
    if not isinstance(participants, list) or not participants:
        raise TaskRuntimeIntakeError(
            "group_task_snapshot_invalid",
            "Group Task has no confirmed Agent participants",
        )
    try:
        mention_participant_ids = [
            uuid.UUID(str(participant["participant_id"]))
            for participant in participants
            if isinstance(participant, dict)
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskRuntimeIntakeError(
            "group_task_snapshot_invalid",
            "Group Task contains an invalid Agent participant",
        ) from exc
    if len(mention_participant_ids) != len(participants):
        raise TaskRuntimeIntakeError(
            "group_task_snapshot_invalid",
            "Group Task contains an invalid Agent participant",
        )

    try:
        intake = await enqueue_group_message(
            db,
            tenant_id=task.tenant_id,
            group_id=_snapshot_uuid(snapshot, "group_id"),
            session_id=_snapshot_uuid(snapshot, "group_session_id"),
            sender_participant_id=_snapshot_uuid(snapshot, "sender_participant_id"),
            content=_task_goal(task),
            mention_participant_ids=mention_participant_ids,
            message_id=uuid.uuid5(task.id, "group-work-task-message"),
            correlation_id=f"work-task:{task.id}",
            work_task_id=task.id,
            application_tools_enabled=_application_tools_enabled_for_task(task),
            settings_override=settings_override,
        )
    except GroupMessageServiceError as exc:
        raise TaskRuntimeIntakeError(exc.code, str(exc)) from exc
    if intake.error_code is not None or not intake.run_handles:
        raise TaskRuntimeIntakeError(
            intake.error_code or "group_task_dispatch_failed",
            intake.error_message or "Group Task did not create a Runtime Run",
        )

    task.status = "doing"
    handle = intake.run_handles[0]
    if handle.created:
        db.add(
            TaskLog(
                task_id=task.id,
                content=(
                    "👥 已进入 Group 协作现场"
                    f"（{snapshot.get('group_name') or snapshot['group_id']} / "
                    f"Run {handle.run_id}）"
                ),
            )
        )
    return handle


async def _try_enqueue_runtime_task(
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    execution_id: uuid.UUID,
) -> RunHandle | None:
    async with async_session() as db:
        async with db.begin():
            task_result = await db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.agent_id == agent_id,
                ).with_for_update()
            )
            task = task_result.scalar_one_or_none()
            if task is None:
                raise TaskRuntimeIntakeError(
                    "task_not_found",
                    "Task does not exist for the requested Agent",
                )
            if task.status != "pending":
                raise TaskRuntimeIntakeError(
                    "task_not_pending",
                    "Only a pending Task may enter Runtime",
                )
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                raise TaskRuntimeIntakeError(
                    "agent_not_found",
                    "Task Agent does not exist",
                )
            from app.core.permissions import (
                get_agent_access_level_for_user_id,
                is_agent_executable,
            )
            from app.models.user import User

            creator_result = await db.execute(
                select(User).where(User.id == task.created_by)
            )
            creator = creator_result.scalar_one_or_none()
            identity = getattr(creator, "identity", None) if creator else None
            access_level = await get_agent_access_level_for_user_id(
                db,
                task.created_by,
                agent,
            )
            if (
                creator is None
                or not creator.is_active
                or (identity is not None and not identity.is_active)
                or creator.tenant_id != agent.tenant_id
                or access_level is None
            ):
                raise TaskRuntimeIntakeError(
                    "requester_unauthorized",
                    "Task requester is no longer authorized",
                )
            if not is_agent_executable(agent):
                raise TaskRuntimeIntakeError(
                    "agent_not_executable",
                    "Task Agent is not executable",
                )
            return await enqueue_task_runtime(
                db,
                task=task,
                agent=agent,
                execution_id=execution_id,
            )


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Register one Task execution; the Runtime worker owns all model/tool work."""
    if not AUTOMATIC_TASK_EXECUTION_ENABLED:
        logger.warning(
            "Automatic task execution is paused task_id={} agent_id={}",
            task_id,
            agent_id,
        )
        return
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    try:
        runtime_handle = await _try_enqueue_runtime_task(
            task_id,
            agent_id,
            execution_id=uuid.uuid4(),
        )
    except TaskRuntimeIntakeError as exc:
        if exc.code in {
            "agent_not_executable",
            "requester_unauthorized",
            "task_not_found",
            "task_not_pending",
        }:
            logger.warning(
                "[TaskExec] Runtime intake rejected task={} code={}",
                task_id,
                exc.code,
            )
            return
        logger.error(f"[TaskExec] Runtime intake failed ({exc.code}): {exc}")
        await _log_error(task_id, f"持久化执行登记失败: {exc.code}")
        return
    except Exception as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        logger.error(f"[TaskExec] Runtime intake failed ({error_code}): {exc}")
        await _log_error(task_id, f"持久化执行登记失败: {error_code}")
        return
    if runtime_handle is not None:
        logger.info(
            f"[TaskExec] Task {task_id} queued as Runtime Run {runtime_handle.run_id}"
        )
        return
    await _log_error(
        task_id,
        "统一 Runtime 当前未对 task 入口启用；未回退旧执行循环",
    )


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    logger.error(
        "[TaskExec] Error recorded task={} message_chars={}",
        task_id,
        len(message),
    )
    async with async_session() as db:
        db.add(TaskLog(task_id=task_id, content=f"❌ {message}"))
        await db.commit()
