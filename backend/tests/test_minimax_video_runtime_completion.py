"""Runtime ownership tests for durable MiniMax video completion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import media_generation
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.agent_tools import _check_video_minimax
from app.services.builtin_tool_definitions import builtin_model_definition


def _durable_task(agent_id: uuid.UUID):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        provider="minimax",
        provider_task_id="provider-video-1",
        modality="video",
        model="MiniMax-Hailuo-02",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={"tier": "ultra", "runtime_managed_completion": True},
        reservation_id=uuid.uuid4(),
    )


def test_video_check_contract_accepts_workspace_or_durable_identity() -> None:
    schema = builtin_model_definition("check_video_minimax")["function"][
        "parameters"
    ]

    assert schema.get("required", []) == []
    assert schema["anyOf"] == [
        {"required": ["task_meta_path"]},
        {"required": ["task_record_id"]},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "expected_status", "pending"),
    [
        ("processing", "pending", True),
        ("succeeded", "succeeded", False),
        ("failed", "failed", False),
    ],
)
async def test_video_check_settles_same_runtime_operation_without_metadata_file(
    tmp_path,
    provider_status,
    expected_status,
    pending,
) -> None:
    agent_id = uuid.uuid4()
    task = _durable_task(agent_id)
    outcome = SimpleNamespace(
        status=provider_status,
        output_path=task.output_path if provider_status == "succeeded" else None,
        error="provider failed" if provider_status == "failed" else None,
    )

    with (
        patch(
            "app.services.media_generation.find_media_generation_task_by_id",
            AsyncMock(return_value=task),
        ),
        patch(
            "app.services.media_generation.reconcile_minimax_video_task",
            AsyncMock(return_value=outcome),
        ) as reconcile,
    ):
        result = await _check_video_minimax(
            agent_id,
            tmp_path,
            {"task_record_id": str(task.id)},
            typed=True,
        )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == expected_status
    assert result.metadata["runtime_async_pending"] is pending
    operation = result.metadata["async_operation"]
    assert operation["operation_id"] == str(task.id)
    assert operation["poll"]["arguments"] == {
        "task_record_id": str(task.id),
        "task_meta_path": task.metadata_path,
    }
    reconcile.assert_awaited_once_with(task.id, deliver_completion=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_managed", "deliver_completion"),
    [(True, False), (False, True)],
)
async def test_media_daemon_does_not_duplicate_runtime_completion_delivery(
    monkeypatch,
    runtime_managed,
    deliver_completion,
) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        modality="video",
        request_metadata={"runtime_managed_completion": runtime_managed},
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(
        media_generation,
        "_claim_due_task_ids",
        AsyncMock(return_value=[task_id]),
    )
    monkeypatch.setattr(
        media_generation,
        "_load_task",
        AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        media_generation,
        "reconcile_minimax_video_task",
        reconcile,
    )
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_RECONCILIATION_CONCURRENCY=1),
    )

    assert await media_generation.reconcile_pending_media_generation_tasks() == 1
    if deliver_completion:
        reconcile.assert_awaited_once_with(task_id)
    else:
        reconcile.assert_awaited_once_with(
            task_id,
            deliver_completion=False,
        )
