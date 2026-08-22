"""Task entrypoint cutover tests for the durable Runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.config import Settings
from app.models.agent import Agent
from app.models.task import Task, TaskLog
from app.services.agent_runtime.contracts import RunHandle, StartRunCommand
from app.services.task_executor import (
    TaskRuntimeIntakeError,
    enqueue_group_task_runtime,
    enqueue_task_runtime,
    execute_task,
)
from app.services.product_information_architecture import (
    product_information_architecture_snapshot,
)


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


def _settings(*, enabled: bool) -> Settings:
    return Settings(
        _env_file=None,
        AGENT_RUNTIME_V2_ENABLED=enabled,
        AGENT_RUNTIME_V2_SOURCE_TYPES="task" if enabled else "",
    )


def _records(*, task_type: str = "todo") -> tuple[Task, Agent]:
    agent_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        title="Prepare the report",
        description="Use the current workspace evidence",
        intent="Use the current workspace evidence",
        type=task_type,
        status="pending",
        priority="medium",
        created_by=creator_id,
    )
    agent = Agent(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=creator_id,
        name="Analyst",
        role_description="Analyze evidence",
        primary_model_id=uuid.uuid4(),
        status="idle",
    )
    return task, agent


@pytest.mark.asyncio
async def test_todo_registration_updates_task_in_same_caller_session() -> None:
    task, agent = _records()
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        result = await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert result == handle
    assert task.status == "doing"
    assert len(session.added) == 1
    assert isinstance(session.added[0], TaskLog)
    command = start_run.await_args.args[0]
    assert isinstance(command, StartRunCommand)
    assert command.source_type == "task"
    assert command.source_id == str(task.id)
    assert command.source_execution_id == f"task:{task.id}"
    assert command.model_id == agent.primary_model_id
    assert command.payload["fallback_model_id"] is None
    assert command.payload["model_modality"] == "text"
    assert command.delivery_status == "not_required"
    assert command.payload["task_id"] == str(task.id)
    assert command.payload["work_task_id"] == str(task.id)
    assert command.payload["application_tools_enabled"] is True


@pytest.mark.asyncio
async def test_work_retry_uses_a_new_idempotent_runtime_attempt_identity() -> None:
    task, agent = _records()
    task.origin_type = "workbench"
    task.confirmation_fingerprint = "a" * 64
    task.confirmed_at = datetime(2026, 8, 19, tzinfo=UTC)
    task.work_statement = {
        "version": 2,
        "work_type": "general",
        "expected_output": "task_result",
        "acceptance_contract": {
            "version": 1,
            "criteria": ["给出准确的客户上线方案"],
            "owner_review_required": True,
        },
        "product_information_architecture": product_information_architecture_snapshot(),
    }
    session = _Session()
    attempt_id = uuid.uuid4()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            execution_id=attempt_id,
            owner_change_request="补齐五家设计伙伴逐家的负责人和失败退出条件。",
            settings_override=_settings(enabled=True),
        )

    command = start_run.await_args.args[0]
    assert command.source_execution_id == f"task:{task.id}:attempt:{attempt_id}"
    assert command.idempotency_key == f"start:task:{task.id}:attempt:{attempt_id}"
    assert "上一次业务验收要求修改" in command.goal
    assert "五家设计伙伴逐家的负责人" in command.goal
    assert command.payload["owner_change_request"] == (
        "补齐五家设计伙伴逐家的负责人和失败退出条件。"
    )
    assert "Astra 产品入口目录（astra-product-ia-1.12.0-r1" in command.goal
    assert "公司管理 → 企业知识与集成 → 组织同步" in command.goal
    assert "报告中心" not in command.goal


@pytest.mark.asyncio
async def test_confirmed_creative_workbench_task_is_a_tool_free_brief_run() -> None:
    task, agent = _records()
    task.origin_type = "workbench"
    task.work_type = "image"
    task.confirmation_fingerprint = "b" * 64
    task.confirmed_at = datetime(2026, 8, 4, tzinfo=UTC)
    task.work_statement = {
        "work_type": "image",
        "expected_output": "confirmed_image_brief",
        "delivery_mode": "task_only",
    }
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    command = start_run.await_args.args[0]
    assert command.payload["application_tools_enabled"] is False
    assert "仅负责整理并返回可确认的 Brief" in command.goal
    assert "正式制作必须由用户进入独立 Deliverable" in command.goal


@pytest.mark.asyncio
async def test_temporary_expert_role_reaches_runtime_goal_and_audit_payload() -> None:
    task, agent = _records()
    task.executor_kind = "temporary_expert"
    task.executor_snapshot = {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "expert_role": "Enterprise contract risk reviewer",
        "scope": "task_run_only",
        "inherits_long_term_memory": False,
    }
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    command = start_run.await_args.args[0]
    assert "Enterprise contract risk reviewer" in command.goal
    assert "仅本任务" in command.goal
    assert command.payload["executor_kind"] == "temporary_expert"
    assert command.payload["executor_snapshot"]["expert_role"] == (
        "Enterprise contract risk reviewer"
    )


@pytest.mark.asyncio
async def test_unconfirmed_workbench_task_cannot_enter_runtime() -> None:
    task, agent = _records()
    task.origin_type = "workbench"

    with pytest.raises(TaskRuntimeIntakeError) as raised:
        await enqueue_task_runtime(
            _Session(),  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert raised.value.code == "task_confirmation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_type", ["workbench", "group"])
async def test_confirmed_group_task_enters_native_group_runtime_with_stable_identity(
    origin_type: str,
) -> None:
    task, agent = _records()
    task.origin_type = origin_type
    task.executor_kind = "group"
    task.confirmation_fingerprint = "a" * 64
    task.confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    group_id = uuid.uuid4()
    session_id = uuid.uuid4()
    sender_participant_id = uuid.uuid4()
    agent_participant_id = uuid.uuid4()
    task.group_id = group_id
    task.executor_snapshot = {
        "group_id": str(group_id),
        "group_name": "Campaign Group",
        "group_session_id": str(session_id),
        "sender_participant_id": str(sender_participant_id),
        "participants": [
            {
                "participant_id": str(agent_participant_id),
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "responsibility": "primary_owner",
            }
        ],
    }
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )
    session = _Session()

    with patch(
        "app.services.task_executor.enqueue_group_message",
        new=AsyncMock(
            return_value=SimpleNamespace(
                error_code=None,
                error_message=None,
                run_handles=(handle,),
            )
        ),
    ) as enqueue:
        result = await enqueue_group_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            primary_agent=agent,
            owner_change_request="补充每位参与者的可验证结果。",
        )

    assert result == handle
    assert task.status == "doing"
    assert isinstance(session.added[0], TaskLog)
    kwargs = enqueue.await_args.kwargs
    assert kwargs["group_id"] == group_id
    assert kwargs["session_id"] == session_id
    assert kwargs["sender_participant_id"] == sender_participant_id
    assert kwargs["mention_participant_ids"] == [agent_participant_id]
    assert kwargs["message_id"] == uuid.uuid5(task.id, "group-work-task-message")
    assert kwargs["correlation_id"] == f"work-task:{task.id}"
    assert kwargs["work_task_id"] == task.id
    assert kwargs["application_tools_enabled"] is True
    assert "上一次业务验收要求修改" in kwargs["content"]
    assert "补充每位参与者的可验证结果" in kwargs["content"]


@pytest.mark.asyncio
async def test_idempotent_task_retry_does_not_duplicate_queue_log() -> None:
    task, agent = _records()
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=False,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ):
        await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert task.status == "doing"
    assert session.added == []


@pytest.mark.asyncio
async def test_supervision_uses_a_distinct_runtime_occurrence() -> None:
    supervision, agent = _records(task_type="supervision")
    supervision.supervision_target_name = "Alice"
    session = _Session()
    execution_id = uuid.uuid4()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        supervision_result = await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=supervision,
            agent=agent,
            execution_id=execution_id,
            settings_override=_settings(enabled=True),
        )

    assert supervision_result == handle
    command = start_run.await_args.args[0]
    assert command.source_execution_id == (
        f"task:{supervision.id}:supervision:{execution_id}"
    )
    assert command.payload["task_type"] == "supervision"
    assert "督办对象: Alice" in command.goal


@pytest.mark.asyncio
async def test_disabled_rollout_does_not_silently_start_runtime() -> None:
    task, agent = _records()

    with patch(
        "app.services.task_executor.RuntimeCommandIntake.start_run",
        new=AsyncMock(),
    ) as start_run:
        result = await enqueue_task_runtime(
            _Session(),  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=False),
        )

    assert result is None
    start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_entrypoint_never_falls_back_to_the_legacy_tool_loop() -> None:
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    with (
        patch(
            "app.services.task_executor._try_enqueue_runtime_task",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.task_executor._log_error",
            new=AsyncMock(),
        ) as log_error,
        patch(
            "app.services.task_executor.AUTOMATIC_TASK_EXECUTION_ENABLED",
            True,
        ),
    ):
        await execute_task(task_id, agent_id)

    log_error.assert_awaited_once()
    assert "未回退旧执行循环" in log_error.await_args.args[1]


@pytest.mark.asyncio
async def test_selected_task_requires_tenant_and_model() -> None:
    task, agent = _records()
    agent.tenant_id = None

    with pytest.raises(TaskRuntimeIntakeError, match="tenant") as raised:
        await enqueue_task_runtime(
            _Session(),  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert raised.value.code == "agent_tenant_missing"
