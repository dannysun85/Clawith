from __future__ import annotations

from io import BytesIO

from PIL import Image
import pytest

from scripts.creative_provider_benchmark import (
    BenchmarkContractError,
    BenchmarkCostGuardrailError,
    BenchmarkArtifactValidationError,
    agent_plan_image_size,
    benchmark_case_text,
    benchmark_generation_limit,
    display_path,
    enforce_direct_provider_benchmark_contract,
    enforce_benchmark_cost_guardrail,
    existing_successful_generation_count,
    failure_receipt_stem,
    generate_audio,
    generate_video,
    load_case,
    output_stem,
    provider_failure_receipt,
    require_paid_provider_call_authorization,
    validate_benchmark_artifact,
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


def test_benchmark_requires_explicit_paid_provider_call_authorization():
    with pytest.raises(
        BenchmarkContractError,
        match="explicit_paid_provider_call_authorization_required",
    ):
        require_paid_provider_call_authorization(False)

    require_paid_provider_call_authorization(True)


def test_direct_provider_contract_keeps_presentation_on_artifact_path():
    with pytest.raises(
        BenchmarkContractError,
        match="presentation_requires_artifact_pair",
    ):
        enforce_direct_provider_benchmark_contract(
            {"modality": "presentation"},
            paid_call_confirmed=False,
        )

    with pytest.raises(
        BenchmarkContractError,
        match="explicit_paid_provider_call_authorization_required",
    ):
        enforce_direct_provider_benchmark_contract(
            {"modality": "image"},
            paid_call_confirmed=False,
        )

    enforce_direct_provider_benchmark_contract(
        {"modality": "video"},
        paid_call_confirmed=True,
    )
    enforce_direct_provider_benchmark_contract(
        {"modality": "audio"},
        paid_call_confirmed=True,
    )


def test_benchmark_artifact_failure_preserves_provider_acceptance():
    error = BenchmarkArtifactValidationError(
        "video_artifact_contract_invalid",
        provider_receipt={"model": "provider-video", "status": "Success"},
    )

    receipt = provider_failure_receipt(error)

    assert receipt["provider_accepted"] is True
    assert receipt["status"] == "provider_artifact_contract_failed"
    assert receipt["provider_receipt"] == {
        "model": "provider-video",
        "status": "Success",
    }


@pytest.mark.asyncio
async def test_benchmark_validates_image_dimensions_and_records_contract():
    payload = BytesIO()
    Image.new("RGB", (900, 600), color=(12, 34, 56)).save(payload, format="PNG")

    contract = await validate_benchmark_artifact(
        {"modality": "image", "aspect_ratio": "3:2"},
        payload.getvalue(),
    )

    assert contract == {
        "kind": "image",
        "width": 900,
        "height": 600,
        "aspect_ratio": "3:2",
    }


@pytest.mark.asyncio
async def test_benchmark_video_contract_requires_audio_when_requested(monkeypatch):
    class FakeInfo:
        width = 720
        height = 1280
        duration_seconds = 6.0
        codec_name = "h264"
        pixel_format = "yuv420p"
        audio_codec_name = None
        fast_start = True

    async def fake_validate(*_args, **_kwargs):
        return FakeInfo()

    monkeypatch.setattr(
        "scripts.creative_provider_benchmark.validate_generated_video",
        fake_validate,
    )

    with pytest.raises(Exception, match="audio stream is required but missing"):
        await validate_benchmark_artifact(
            {
                "modality": "video",
                "aspect_ratio": "9:16",
                "duration_seconds": 6,
                "require_audio": True,
            },
            b"provider-video",
        )


@pytest.mark.asyncio
async def test_benchmark_validates_audio_and_records_contract(monkeypatch):
    class FakeInfo:
        duration_seconds = 2.75
        codec_name = "mp3"
        sample_rate = 24000
        channels = 1
        container_format = "mp3"

    async def fake_validate(*_args, **_kwargs):
        return FakeInfo()

    monkeypatch.setattr(
        "scripts.creative_provider_benchmark.validate_generated_audio",
        fake_validate,
    )

    contract = await validate_benchmark_artifact(
        {"modality": "audio", "format": "mp3", "sample_rate": 24000},
        b"provider-audio",
    )

    assert contract == {
        "kind": "audio",
        "duration_seconds": 2.75,
        "codec_name": "mp3",
        "sample_rate": 24000,
        "channels": 1,
        "container_format": "mp3",
        "requested_format": "mp3",
    }


@pytest.mark.asyncio
async def test_generate_audio_uses_agent_plan_tts_contract(monkeypatch):
    class Prepared:
        credential_id = "credential-id"
        api_key = "secret"
        base_url = "https://unused.example"
        model = "doubao-seed-tts-2.0"
        plan_tier = "large"

    async def fake_prepare(*_args, **kwargs):
        assert kwargs["modality"] == "audio"
        return Prepared()

    async def fake_generate(**kwargs):
        assert kwargs["text"] == "忙里，也有一口温度。"
        assert kwargs["audio_format"] == "mp3"
        assert kwargs["sample_rate"] == 24000
        await kwargs["on_provider_accepted"](b"audio")
        return b"audio"

    monkeypatch.setattr(
        "scripts.creative_provider_benchmark.prepare_media_provider",
        fake_prepare,
    )
    monkeypatch.setattr(
        "scripts.creative_provider_benchmark.generate_volcengine_speech",
        fake_generate,
    )

    artifact, receipt = await generate_audio(
        provider="volcengine_agent_plan",
        saas_tier="ultra",
        case={
            "modality": "audio",
            "text": "忙里，也有一口温度。",
            "format": "mp3",
            "sample_rate": 24000,
        },
    )

    assert artifact == b"audio"
    assert receipt["model"] == "doubao-seed-tts-2.0"
    assert receipt["provider_plan_tier"] == "large"
    assert receipt["provider_acceptance_audio_sha256"]


def test_presentation_benchmark_contract_is_not_treated_as_video():
    error = BenchmarkContractError("presentation_requires_artifact_pair")

    receipt = provider_failure_receipt(error)

    assert receipt["benchmark_contract_error"] == (
        "presentation_requires_artifact_pair"
    )
    assert receipt["status"] == "failed_before_artifact"


def test_presentation_case_has_a_canonical_receipt_brief_without_prompt():
    brief = benchmark_case_text(
        {
            "modality": "presentation",
            "goal": "Create an eight-slide launch proposal",
            "required_story": ["Cover", "Decision"],
        }
    )

    assert '"goal":"Create an eight-slide launch proposal"' in brief


def test_benchmark_display_path_supports_isolated_output_directories(tmp_path):
    path = tmp_path / "receipt.json"

    assert display_path(path) == str(path.resolve())


def test_benchmark_case_receipt_provenance_is_bound_to_plan_and_case(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        '{"benchmark_id":"run-1","cases":{"image":{"modality":"image","prompt":"same task","aspect_ratio":"1:1"}}}',
        encoding="utf-8",
    )

    case = load_case(plan_path, "image")

    assert len(case["__benchmark_plan_sha256"]) == 64
    assert len(case["__benchmark_case_sha256"]) == 64
    assert case["__benchmark_plan_sha256"] != case["__benchmark_case_sha256"]
    assert benchmark_case_text(case) == "same task"


def test_benchmark_cost_guardrail_reads_plan_owned_limits(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        '{"cost_guardrail":{"image_generations_per_provider":1,'
        '"video_generations_per_provider":1,'
        '"audio_generations_per_provider":1,"automatic_quality_retries":0}}',
        encoding="utf-8",
    )

    assert benchmark_generation_limit(plan_path, "image") == 1
    assert benchmark_generation_limit(plan_path, "video") == 1
    assert benchmark_generation_limit(plan_path, "audio") == 1
    assert benchmark_generation_limit(plan_path, "presentation") is None


def test_benchmark_cost_guardrail_rejects_a_second_successful_case(tmp_path):
    plan_path = tmp_path / "plan.json"
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    plan_path.write_text(
        '{"cost_guardrail":{"image_generations_per_provider":1,'
        '"video_generations_per_provider":1,"automatic_quality_retries":0}}',
        encoding="utf-8",
    )
    (output_dir / "minimax-image_case.receipt.json").write_text(
        '{"benchmark_id":"run-1","case_key":"image_case",'
        '"provider":"minimax","artifact_path":"image.png"}',
        encoding="utf-8",
    )
    case = {
        "benchmark_id": "run-1",
        "case_key": "image_case",
        "modality": "image",
    }

    assert existing_successful_generation_count(
        output_dir,
        benchmark_id="run-1",
        case_key="image_case",
        provider="minimax",
    ) == 1
    with pytest.raises(BenchmarkContractError, match="cost_guardrail_exhausted"):
        enforce_benchmark_cost_guardrail(
            plan_path,
            case=case,
            provider="minimax",
            output_dir=output_dir,
        )


def test_cost_guardrail_failure_receipt_keeps_the_usage_snapshot(tmp_path):
    error = BenchmarkCostGuardrailError(
        "cost_guardrail_exhausted",
        snapshot={
            "max_generations_per_provider": 1,
            "existing_successful_generations": 1,
        },
    )

    receipt = provider_failure_receipt(error)

    assert receipt["cost_guardrail"] == {
        "max_generations_per_provider": 1,
        "existing_successful_generations": 1,
    }


def test_cost_guardrail_rejection_does_not_overwrite_success_receipt():
    error = BenchmarkCostGuardrailError(
        "cost_guardrail_exhausted",
        snapshot={
            "max_generations_per_provider": 1,
            "existing_successful_generations": 1,
        },
    )

    assert failure_receipt_stem("minimax-image_case", error) == (
        "minimax-image_case.guardrail-blocked"
    )


def test_benchmark_cost_guardrail_rejects_automatic_retries(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        '{"cost_guardrail":{"image_generations_per_provider":1,'
        '"video_generations_per_provider":1,"automatic_quality_retries":1}}',
        encoding="utf-8",
    )

    with pytest.raises(
        BenchmarkContractError,
        match="automatic_quality_retries_must_be_zero",
    ):
        benchmark_generation_limit(plan_path, "image")


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
