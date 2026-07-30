from __future__ import annotations

import pytest

from scripts.creative_provider_benchmark import (
    BenchmarkContractError,
    agent_plan_image_size,
    generate_video,
    output_stem,
    provider_failure_receipt,
)
from app.services.llm.load_balancer import (
    CredentialUnavailableReason,
    NoCredentialAvailable,
)
from app.services.volcengine_agent_plan import VolcengineAgentPlanRejected


def test_benchmark_rejection_receipt_keeps_structured_provider_evidence_only():
    error = VolcengineAgentPlanRejected(
        "provider message must not be persisted",
        provider_code="UnsupportedModel",
        http_status=400,
    )

    receipt = provider_failure_receipt(error)

    assert receipt == {
        "error_type": "VolcengineAgentPlanRejected",
        "provider_accepted": False,
        "provider_error_code": "UnsupportedModel",
        "provider_http_status": 400,
        "status": "rejected_before_acceptance",
    }
    assert "message" not in receipt


def test_benchmark_explicit_video_models_get_distinct_receipt_names():
    stem = output_stem(
        "volcengine_agent_plan",
        "video_spokesperson",
        explicit_volcengine_video_model="doubao-seedance-2.0-mini",
    )

    assert stem == (
        "volcengine_agent_plan-video_spokesperson-doubao-seedance-2.0-mini"
    )


def test_benchmark_preserves_agent_plan_image_aspect_ratio():
    assert (
        agent_plan_image_size({"aspect_ratio": "3:4"}, "4K")
        == "3072x4096"
    )


def test_benchmark_records_structured_credential_capability_failure():
    error = NoCredentialAvailable(
        "volcengine_agent_plan",
        "video",
        CredentialUnavailableReason.CAPABILITY_MISMATCH,
    )

    receipt = provider_failure_receipt(error)

    assert receipt["credential_unavailable_reason"] == "capability_mismatch"
    assert receipt["provider_accepted"] is False


def test_benchmark_contract_failure_is_structured():
    error = BenchmarkContractError(
        "minimax_non_16_9_video_requires_first_frame"
    )

    receipt = provider_failure_receipt(error)

    assert receipt["benchmark_contract_error"] == (
        "minimax_non_16_9_video_requires_first_frame"
    )
    assert receipt["provider_accepted"] is False


@pytest.mark.asyncio
async def test_benchmark_blocks_minimax_portrait_t2v_before_provider_selection():
    with pytest.raises(
        BenchmarkContractError,
        match="minimax_non_16_9_video_requires_first_frame",
    ):
        await generate_video(
            provider="minimax",
            saas_tier="ultra",
            case={
                "aspect_ratio": "9:16",
                "duration_seconds": 10,
                "prompt": "portrait commercial",
            },
            timeout_seconds=1,
        )
