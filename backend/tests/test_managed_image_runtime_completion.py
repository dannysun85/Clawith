"""Runtime ownership tests for accepted managed-image completion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.agent_tools import (
    _check_image_generation,
    _generate_image_minimax_durable,
    _minimax_tool_result,
)
from app.services.builtin_tool_definitions import builtin_model_definition


def _durable_image_task(agent_id: uuid.UUID) -> SimpleNamespace:
    request_id = uuid.uuid4()
    digest = "a" * 64
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        provider="volcengine_agent_plan",
        provider_task_id=None,
        modality="image",
        model="doubao-seedream-5.0-lite",
        output_path="workspace/images/poster.png",
        request_metadata={
            "tier": "ultra",
            "deliverable_request_id": str(request_id),
            "expected_overlay_blocks_sha256": digest,
        },
        last_response={
            "status": "Success",
            "_astra_media_contract": {
                "deliverable_request_id": str(request_id),
                "expected_overlay_blocks_sha256": digest,
            },
        },
    )


def test_image_check_contract_uses_only_durable_task_identity() -> None:
    schema = builtin_model_definition("check_image_generation")["function"][
        "parameters"
    ]

    assert schema["required"] == ["task_record_id"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "legacy_error_code",
    [
        "media_image_delivery_pending",
        "media_image_recovery_pending",
        "media_image_acceptance_repair_pending",
    ],
)
def test_pending_image_outcome_preserves_rolling_frontend_error_code(
    legacy_error_code: str,
) -> None:
    result = _minimax_tool_result(
        "delivery is still pending",
        typed=True,
        status="pending",
        error_code=legacy_error_code,
        agent_id=uuid.uuid4(),
        modality="image",
        record_id=uuid.uuid4(),
        runtime_metadata={"runtime_async_pending": True},
    )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "pending"
    assert result.error_code == legacy_error_code
    assert result.metadata["runtime_async_pending"] is True


@pytest.mark.asyncio
async def test_accepted_image_generation_enters_runtime_managed_polling(
    tmp_path,
) -> None:
    record_id = uuid.uuid4()
    generated = b"generated-image"
    created_task = SimpleNamespace(
        request_metadata={"recovery_asset_storage_key": "_internal/recovery/image.bin"}
    )

    async def provider(**kwargs):
        kwargs["on_provider_request_started"]()
        await kwargs["on_provider_accepted"]("https://asset.example/poster.png")
        return generated

    with (
        patch(
            "app.services.agent_tools._generate_image_minimax",
            AsyncMock(side_effect=provider),
        ),
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(return_value=created_task),
        ) as create_task,
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(return_value=("_internal/provider-recovery.json", "opaque")),
        ),
        patch(
            "app.services.media_generation.mark_minimax_sync_provider_accepted",
            AsyncMock(),
        ),
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(),
        ),
        patch(
            "app.services.media_generation.reconcile_minimax_sync_media_task",
            AsyncMock(return_value=SimpleNamespace(status="processing")),
        ),
        patch(
            "app.services.agent_tools._record_media_provider_success",
            AsyncMock(),
        ),
        patch("uuid.uuid4", return_value=record_id),
    ):
        result = await _generate_image_minimax_durable(
            agent_id=uuid.UUID(int=1),
            ws=tmp_path,
            arguments={"aspect_ratio": "9:16"},
            user_id=None,
            session_id="session-1",
            tenant_id=uuid.UUID(int=2),
            credential_id=uuid.UUID(int=3),
            api_key="test-key",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="ultra",
            credit_cost=4,
            provider_prompt="commercial poster background without text",
            save_path="workspace/images/poster.png",
            output_extension=".png",
            overlay_text="",
            overlay_position="bottom",
            brand_asset=None,
            brand_position="center",
            brand_scale=0.42,
            typed=True,
        )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "pending"
    assert result.error_code == "media_image_delivery_pending"
    assert result.metadata["runtime_async_pending"] is True
    assert create_task.await_args.kwargs["request_metadata"][
        "runtime_managed_completion"
    ] is True
    operation = result.metadata["async_operation"]
    assert operation["operation_id"] == str(record_id)
    assert operation["poll"] == {
        "tool": "check_image_generation",
        "arguments": {"task_record_id": str(record_id)},
        "interval_ms": operation["poll"]["interval_ms"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "expected_status", "pending"),
    [
        ("processing", "pending", True),
        ("succeeded", "succeeded", False),
        ("failed", "failed", False),
        ("compensated", "failed", False),
        ("asset_delivery_failed", "failed", False),
    ],
)
async def test_image_check_settles_the_same_runtime_operation(
    provider_status: str,
    expected_status: str,
    pending: bool,
) -> None:
    agent_id = uuid.uuid4()
    task = _durable_image_task(agent_id)
    outcome = SimpleNamespace(
        status=provider_status,
        output_path=task.output_path if provider_status == "succeeded" else None,
        error="delivery failed" if provider_status == "failed" else None,
    )
    find_task = AsyncMock(side_effect=[task, task])

    with (
        patch(
            "app.services.media_generation.find_media_generation_task_by_id",
            find_task,
        ),
        patch(
            "app.services.media_generation.reconcile_minimax_sync_media_task",
            AsyncMock(return_value=outcome),
        ) as reconcile,
    ):
        result = await _check_image_generation(
            agent_id,
            {"task_record_id": str(task.id)},
            typed=True,
        )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == expected_status
    assert result.metadata["runtime_async_pending"] is pending
    operation = result.metadata["async_operation"]
    assert operation["operation_id"] == str(task.id)
    assert operation["poll"]["tool"] == "check_image_generation"
    assert operation["poll"]["arguments"] == {
        "task_record_id": str(task.id)
    }
    assert result.metadata["provider"] == task.provider
    reconcile.assert_awaited_once_with(task.id, deliver_completion=False)
    assert find_task.await_args_list[0].kwargs["modality"] == "image"
    if provider_status == "succeeded":
        assert result.artifact_refs == (
            f"workspace://{agent_id}/{task.output_path}",
        )
        assert result.metadata["deliverable_request_id"] == (
            task.request_metadata["deliverable_request_id"]
        )
        assert result.metadata["expected_overlay_blocks_sha256"] == (
            task.request_metadata["expected_overlay_blocks_sha256"]
        )
