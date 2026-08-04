"""Runtime ownership tests for durable MiniMax video completion."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import media_generation
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.agent_tools import _check_video_minimax
from app.services.builtin_tool_definitions import builtin_model_definition
from app.services.media_assets import OverlayReceipt, VideoInfo


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
    ("modality", "runtime_managed", "deliver_completion"),
    [
        ("video", True, False),
        ("video", False, True),
        ("image", True, False),
        ("image", False, True),
    ],
)
async def test_media_daemon_does_not_duplicate_runtime_completion_delivery(
    monkeypatch,
    modality,
    runtime_managed,
    deliver_completion,
) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        modality=modality,
        request_metadata={"runtime_managed_completion": runtime_managed},
    )
    video_reconcile = AsyncMock()
    sync_reconcile = AsyncMock()
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
        video_reconcile,
    )
    monkeypatch.setattr(
        media_generation,
        "reconcile_minimax_sync_media_task",
        sync_reconcile,
    )
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_RECONCILIATION_CONCURRENCY=1),
    )

    assert await media_generation.reconcile_pending_media_generation_tasks() == 1
    reconcile = video_reconcile if modality == "video" else sync_reconcile
    if deliver_completion:
        reconcile.assert_awaited_once_with(task_id)
    else:
        reconcile.assert_awaited_once_with(
            task_id,
            deliver_completion=False,
        )


@pytest.mark.asyncio
async def test_unbranded_provider_video_is_normalized_before_success(
    monkeypatch,
) -> None:
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider="minimax",
        modality="video",
        model="MiniMax-Hailuo-2.3",
        provider_task_id="provider-unbranded-video",
        status="submitted",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={},
        last_response=None,
        last_error=None,
        output_size=None,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
    )
    provider_bytes = b"provider-video"
    normalized_bytes = b"browser-safe-video"

    class Storage:
        async def exists(self, _key: str) -> bool:
            return True

        async def is_file(self, _key: str) -> bool:
            return True

        async def read_bytes(self, _key: str) -> bytes:
            return normalized_bytes

    async def normalize(
        raw,
        text,
        *,
        text_position,
        brand_asset,
        brand_position,
        brand_scale,
        sanitize_generated_background,
    ):
        assert raw == provider_bytes
        assert text == ""
        assert text_position == "bottom"
        assert brand_asset is None
        assert brand_position == "center"
        assert brand_scale == 0.42
        assert sanitize_generated_background is False
        return normalized_bytes, OverlayReceipt()

    async def store(_record_id, data, **kwargs):
        assert data == normalized_bytes
        task.status = "settlement_ready"
        task.last_response = kwargs["status_data"]
        task.output_size = len(data)
        return task

    async def finalize(*_args, **_kwargs):
        task.status = "succeeded"
        task.completed_at = datetime.now(timezone.utc)
        return task

    validate = AsyncMock(
        side_effect=[
            VideoInfo(640, 360, 1.0, "hevc", "yuv420p10le", None, False),
            VideoInfo(640, 360, 1.0, "h264", "yuv420p", None, True),
        ]
    )
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        media_generation,
        "_claim_success_download",
        AsyncMock(return_value=("claimed", task)),
    )
    monkeypatch.setattr(
        media_generation,
        "_store_authoritative_media_output",
        AsyncMock(side_effect=store),
    )
    monkeypatch.setattr(
        media_generation,
        "_stored_media_output_is_usable",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        media_generation,
        "_finalize_success",
        AsyncMock(side_effect=finalize),
    )
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    normalize_mock = AsyncMock(side_effect=normalize)
    monkeypatch.setattr(media_generation, "apply_video_brand_overlays", normalize_mock)
    monkeypatch.setattr(media_generation, "validate_generated_video", validate)
    monkeypatch.setattr(
        "app.services.agent_tools._load_minimax_tool_credential_by_id",
        AsyncMock(
            return_value=SimpleNamespace(
                api_key="key",
                base_url="https://minimax.test",
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._minimax_retrieve_file_download_url",
        AsyncMock(return_value="https://files.test/video"),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._minimax_download_file",
        AsyncMock(return_value=provider_bytes),
    )

    outcome = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data={"status": "Success", "file_id": "file-1"},
    )

    assert outcome.status == "succeeded"
    normalize_mock.assert_awaited_once()
    assert validate.await_args_list[0].kwargs["require_browser_safe"] is False
    assert validate.await_args_list[1].kwargs == {"label": "Final brand-safe video"}
