"""Duration-aware video route selection on the chat quick path.

Provider-free: the provider boundary is stubbed at ``prepare_media_provider``
and the provider adapters; no real Provider call or Credits movement occurs.
"""

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.agent_tools import _generate_video_minimax
from app.services.llm.load_balancer import NoCredentialAvailable
from app.services.media_provider_routing import (
    minimax_video_supported_durations,
    video_route_max_duration_seconds,
)
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.volcengine_agent_plan import VolcengineAgentPlanRejected


def test_route_duration_capability_helpers():
    assert minimax_video_supported_durations("lite") == frozenset({6})
    assert minimax_video_supported_durations("pro") == frozenset({6, 10})
    assert minimax_video_supported_durations("ultra") == frozenset({6, 10})
    assert video_route_max_duration_seconds(
        "volcengine_agent_plan", "doubao-seedance-2.0"
    ) == 15
    assert video_route_max_duration_seconds(
        "volcengine_agent_plan", "doubao-seedance-1.5-pro"
    ) == 12
    assert video_route_max_duration_seconds("minimax", "MiniMax-Hailuo-2.3") is None


def _minimax_credential() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-Hailuo-2.3",
        daily_allowance_claim_id=None,
    )


def _seedance_credential(model: str = "doubao-seedance-2.0") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="volcengine_agent_plan",
        model=model,
        resolution="720p",
        api_key="sk-test",
        base_url="https://ark.test",
    )


def _base_patches(prepare) -> tuple:
    return (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.agent_tools._resolve_minimax_tool_tier",
            AsyncMock(return_value="pro"),
        ),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("video", "pro")),
        ),
        patch(
            "app.services.agent_tools._get_minimax_tenant_uuid",
            AsyncMock(return_value=uuid.uuid4()),
        ),
        patch(
            "app.services.media_provider_routing.prepare_media_provider",
            prepare,
        ),
        patch(
            "app.services.media_daily_allowance.release_daily_allowance_claim",
            AsyncMock(),
        ),
        patch(
            "app.services.agent_tools._record_minimax_tool_product_issue",
            AsyncMock(),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
    )


def _entered_stack(prepare, extra=()) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    for patcher in (*_base_patches(prepare), *extra):
        stack.enter_context(patcher)
    return stack


@pytest.mark.asyncio
async def test_in_bucket_duration_stays_on_the_minimax_route(tmp_path):
    """A 10s Pro request fits the MiniMax billing bucket: no failover."""

    prepare = AsyncMock(return_value=_minimax_credential())
    constrain = MagicMock(side_effect=RuntimeError("route selected"))
    with _entered_stack(
        prepare,
        extra=(
            patch(
                "app.services.minimax_media_profiles.constrain_minimax_video_request",
                constrain,
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="route selected"):
            await _generate_video_minimax(
                uuid.uuid4(),
                tmp_path,
                {"prompt": "ten second product motion", "duration": 10},
                typed=True,
            )

    assert prepare.await_count == 1
    assert prepare.await_args_list[0].args[0] == "minimax"


@pytest.mark.asyncio
async def test_15s_request_auto_routes_to_seedance_and_honors_duration(tmp_path):
    """The production-incident shape: 15s exceeds every MiniMax bucket."""

    prepare = AsyncMock(side_effect=[_minimax_credential(), _seedance_credential()])
    rejection = VolcengineAgentPlanRejected(
        "Volcengine Agent Plan error (QuotaExceeded): quota exhausted",
        provider_code="QuotaExceeded",
        http_status=429,
    )

    async def reject_provider_request(**kwargs):
        kwargs["on_provider_request_started"]()
        raise rejection

    create_video_task = AsyncMock(side_effect=reject_provider_request)
    with _entered_stack(
        prepare,
        extra=(
            patch(
                "app.services.media_generation.validate_media_origin_session",
                AsyncMock(),
            ),
            patch(
                "app.services.media_generation.create_minimax_video_task_record",
                AsyncMock(
                    return_value=SimpleNamespace(
                        id=uuid.uuid4(),
                        reservation_id=uuid.uuid4(),
                    )
                ),
            ),
            patch(
                "app.services.volcengine_agent_plan.create_video_task",
                create_video_task,
            ),
            patch(
                "app.services.media_generation.mark_media_generation_submission_failed",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.services.media_generation.mark_media_generation_submission_ambiguous",
                AsyncMock(),
            ),
            patch(
                "app.services.agent_tools._mark_media_provider_credential_failure",
                AsyncMock(),
            ),
        ),
    ):
        result = await _generate_video_minimax(
            uuid.uuid4(),
            tmp_path,
            {"prompt": "15 second anime explainer", "duration": 15},
            typed=True,
        )

    assert [call.args[0] for call in prepare.await_args_list] == [
        "minimax",
        "volcengine_agent_plan",
    ]
    # The exact requested duration reaches the capable route; no silent bucket.
    assert create_video_task.await_args.kwargs["duration"] == 15
    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "failed"
    assert result.error_code == "media_video_generation_failed"


@pytest.mark.asyncio
async def test_duration_beyond_every_route_fails_closed_without_credits(tmp_path):
    prepare = AsyncMock(side_effect=[_minimax_credential(), _seedance_credential()])
    credit_check = AsyncMock()
    with _entered_stack(
        prepare,
        extra=(
            patch(
                "app.services.agent_tools._check_minimax_credit_amount",
                credit_check,
            ),
        ),
    ):
        result = await _generate_video_minimax(
            uuid.uuid4(),
            tmp_path,
            {"prompt": "thirty second keynote trailer", "duration": 30},
            typed=True,
        )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "failed"
    assert result.error_code == "media_video_duration_exceeds_route_capability"
    assert result.metadata["requested_duration_seconds"] == 30
    assert result.metadata["max_supported_duration_seconds"] == 15
    assert result.metadata["provider_routes"] == [
        {
            "provider": "minimax",
            "status": "incompatible_request_shape",
            "reason_code": "duration_not_supported",
            "max_duration_seconds": 10,
        },
        {
            "provider": "volcengine_agent_plan",
            "status": "incompatible_request_shape",
            "reason_code": "duration_not_supported",
            "max_duration_seconds": 15,
        },
    ]
    assert "concat_videos" in result.result_summary
    assert "No Credits were consumed" in result.result_summary
    credit_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_duration_skip_takes_priority_over_later_credential_gap(tmp_path):
    """A capable-but-unconfigured long route still fails closed, not silent."""

    prepare = AsyncMock(
        side_effect=[
            _minimax_credential(),
            NoCredentialAvailable("volcengine_agent_plan", "video"),
        ]
    )
    with _entered_stack(prepare):
        result = await _generate_video_minimax(
            uuid.uuid4(),
            tmp_path,
            {"prompt": "15 second anime explainer", "duration": 15},
            typed=True,
        )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "failed"
    assert result.error_code == "media_video_duration_exceeds_route_capability"
    assert result.metadata["requested_duration_seconds"] == 15
    assert result.metadata["max_supported_duration_seconds"] == 10
