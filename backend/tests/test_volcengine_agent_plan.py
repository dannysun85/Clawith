from __future__ import annotations

import base64
import json
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.credentials import CredentialCreateIn
from app.services import agent_tools
from app.services.llm import load_balancer
from app.services.llm.client import AnthropicClient, create_llm_client, get_provider_base_url
from app.services.credential_verification import build_credential_probe_request
from app.services import media_provider_routing
from app.services.llm.load_balancer import NoCredentialAvailable
from app.services.media_provider_routing import (
    DEFAULT_MEDIA_PROVIDER_ORDER,
    prepare_media_provider,
    volcengine_video_quota_model,
)
from app.services.volcengine_agent_plan import (
    DEFAULT_BASE_URL,
    DEFAULT_TEXT_BASE_URL,
    IMAGE_MODEL,
    TEXT_MODELS_BY_SAAS_TIER,
    TTS_DEFAULT_SPEAKER,
    TTS_MODEL,
    VIDEO_MODEL,
    VIDEO_MODEL_15_PRO,
    VIDEO_MODEL_ALIASES,
    VolcengineAgentPlanError,
    VolcengineAgentPlanRejected,
    create_video_task,
    image_request_payload,
    image_size_for_aspect_ratio,
    generate_speech,
    normalize_base_url,
    normalize_text_base_url,
    normalized_video_status,
    plan_tier_supports_modality,
    provider_error_from_response,
    resolve_visual_profile,
    resolve_video_model,
    resolve_text_model,
    stable_video_model_name,
    tts_request_payload,
    video_gateway_model_id,
    video_model_capabilities,
    video_request_payload,
)


def test_agent_plan_uses_dedicated_gateway_and_quality_first_route():
    assert normalize_base_url(None) == DEFAULT_BASE_URL
    assert normalize_base_url("https://ark.cn-beijing.volces.com/api/plan") == DEFAULT_BASE_URL
    assert DEFAULT_MEDIA_PROVIDER_ORDER == ("volcengine_agent_plan", "minimax")

    for unsafe in (
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://ark.cn-beijing.volces.com/api/plan/v3?key=secret",
        "https://example.com/api/plan/v3",
        "http://ark.cn-beijing.volces.com/api/plan/v3",
    ):
        with pytest.raises(ValueError):
            normalize_base_url(unsafe)


def test_agent_plan_tier_and_product_quality_profiles_are_explicit():
    assert plan_tier_supports_modality("small", "text") is True
    assert plan_tier_supports_modality("small", "image") is True
    assert plan_tier_supports_modality("small", "audio") is True
    assert plan_tier_supports_modality("medium", "video") is False
    assert plan_tier_supports_modality("large", "video") is True
    assert plan_tier_supports_modality("max", "video") is True

    assert resolve_visual_profile("image", "lite").model == IMAGE_MODEL
    assert resolve_visual_profile("image", "ultra").size == "4K"
    assert resolve_visual_profile("video", "pro").model == VIDEO_MODEL
    assert resolve_video_model("large") == VIDEO_MODEL
    with pytest.raises(ValueError, match="requires Large or Max"):
        resolve_video_model("small")
    with pytest.raises(ValueError, match="requires Large or Max"):
        resolve_visual_profile("video", "pro", plan_tier="medium")
    assert resolve_visual_profile("video", "ultra").resolution == "1080p"
    assert resolve_text_model("lite") == TEXT_MODELS_BY_SAAS_TIER["lite"]
    assert resolve_text_model("pro") == "doubao-seed-2.1-turbo"
    assert resolve_text_model("ultra") == "doubao-seed-evolving"


@pytest.mark.asyncio
async def test_medium_agent_plan_fails_before_provider_request(monkeypatch):
    credential = SimpleNamespace(
        id=uuid.uuid4(),
        provider="volcengine_agent_plan",
        base_url=DEFAULT_BASE_URL,
        plan_tier="medium",
        modality_status={},
    )
    pick_credential = AsyncMock(return_value=credential)
    monkeypatch.setattr(load_balancer, "pick_credential", pick_credential)
    monkeypatch.setattr(
        media_provider_routing.llm_utils,
        "get_credential_api_key",
        lambda _credential: "secret-plan-key",
    )
    with pytest.raises(NoCredentialAvailable) as exc_info:
        await prepare_media_provider(
            "volcengine_agent_plan",
            modality="video",
            saas_tier="pro",
            minimax_model="MiniMax-Hailuo-02",
        )

    assert exc_info.value.reason_code.value == "capability_mismatch"
    pick_credential.assert_awaited_once_with(
        "volcengine_agent_plan",
        modality="video",
        quota_modality="video",
        quota_model=volcengine_video_quota_model(VIDEO_MODEL, "720p"),
    )


@pytest.mark.asyncio
async def test_small_agent_plan_never_falls_through_to_a_video_call(monkeypatch):
    """A Small account must fail before any provider request is attempted."""

    credential = SimpleNamespace(
        id=uuid.uuid4(),
        provider="volcengine_agent_plan",
        base_url=DEFAULT_BASE_URL,
        plan_tier="small",
        modality_status={},
    )
    pick_credential = AsyncMock(return_value=credential)
    monkeypatch.setattr(load_balancer, "pick_credential", pick_credential)

    def get_key(_credential):
        return "secret-plan-key"

    monkeypatch.setattr(media_provider_routing.llm_utils, "get_credential_api_key", get_key)

    with pytest.raises(NoCredentialAvailable, match="plan tier does not support this modality"):
        await prepare_media_provider(
            "volcengine_agent_plan",
            modality="video",
            saas_tier="pro",
            minimax_model="MiniMax-Hailuo-02",
        )

    pick_credential.assert_awaited_once_with(
        "volcengine_agent_plan",
        modality="video",
        quota_modality="video",
        quota_model=volcengine_video_quota_model(VIDEO_MODEL, "720p"),
    )


@pytest.mark.asyncio
async def test_agent_plan_route_rejects_a_non_agent_plan_credential(monkeypatch):
    """A generic/legacy provider row cannot be reinterpreted as Agent Plan."""

    credential = SimpleNamespace(
        id=uuid.uuid4(),
        provider="volcengine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        plan_tier="large",
        modality_status={},
    )
    pick_credential = AsyncMock(return_value=credential)
    monkeypatch.setattr(load_balancer, "pick_credential", pick_credential)

    with pytest.raises(NoCredentialAvailable, match="explicit Agent Plan account") as exc_info:
        await prepare_media_provider(
            "volcengine_agent_plan",
            modality="image",
            saas_tier="pro",
            minimax_model="MiniMax-Hailuo-02",
        )

    assert exc_info.value.route_failover_safe is True


def test_agent_plan_text_and_tts_contracts_use_official_gateways():
    assert normalize_text_base_url(None) == DEFAULT_TEXT_BASE_URL
    assert (
        normalize_text_base_url("https://ark.cn-beijing.volces.com/api/plan/v3")
        == DEFAULT_TEXT_BASE_URL
    )
    client = create_llm_client(
        provider="volcengine_agent_plan",
        api_key="plan-key",
        model=resolve_text_model("pro"),
        base_url=DEFAULT_BASE_URL,
    )
    assert isinstance(client, AnthropicClient)
    assert client.base_url == DEFAULT_TEXT_BASE_URL
    assert get_provider_base_url("volcengine_agent_plan", DEFAULT_BASE_URL) == DEFAULT_TEXT_BASE_URL

    assert tts_request_payload(text="你好") == {
        "req_params": {
            "text": "你好",
            "speaker": TTS_DEFAULT_SPEAKER,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        }
    }
    assert TTS_MODEL == "doubao-seed-tts-2.0"


@pytest.mark.asyncio
async def test_agent_plan_tts_stream_is_reassembled_before_acceptance(monkeypatch):
    first = b"ID3-audio-"
    second = b"payload"
    body = b"\n".join(
        [
            json.dumps(
                {"code": 0, "data": base64.b64encode(first).decode()},
            ).encode(),
            json.dumps(
                {"code": 0, "data": base64.b64encode(second).decode()},
            ).encode(),
            json.dumps({"code": 20000000}).encode(),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
        assert request.headers["x-api-resource-id"] == "seed-tts-2.0"
        assert request.headers["x-api-key"] == "secret-plan-key"
        return httpx.Response(200, content=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        agent_tools,
        "_public_only_async_client",
        lambda *_args, **_kwargs: client,
    )
    accepted = AsyncMock()

    result = await generate_speech(
        api_key="secret-plan-key",
        text="你好",
        on_provider_accepted=accepted,
    )

    assert result == first + second
    accepted.assert_awaited_once_with(first + second)


def test_agent_plan_visual_payloads_match_direct_api_contract():
    image = image_request_payload(
        prompt="clean commercial portrait",
        size="3K",
        reference_image="data:image/png;base64,abc",
    )
    assert image == {
        "model": IMAGE_MODEL,
        "prompt": "clean commercial portrait",
        "sequential_image_generation": "disabled",
        "size": "3K",
        "output_format": "png",
        "response_format": "url",
        "watermark": False,
        "image": "data:image/png;base64,abc",
    }

    video = video_request_payload(
        prompt="cinematic spokesperson advertisement",
        duration=10,
        resolution="1080p",
        ratio="9:16",
        first_frame_image="https://example.com/frame.png",
    )
    assert video["model"] == VIDEO_MODEL_ALIASES[VIDEO_MODEL]
    assert video["generate_audio"] is False
    assert video["watermark"] is False
    assert video["duration"] == 10
    assert video["resolution"] == "1080p"
    assert video["ratio"] == "9:16"
    assert video["content"][1]["image_url"]["url"].endswith("frame.png")
    assert video["content"][1]["role"] == "first_frame"


@pytest.mark.parametrize(
    ("quality", "aspect_ratio", "expected"),
    [
        ("2K", "1:1", "2048x2048"),
        ("2K", "16:9", "2560x1440"),
        ("2K", "9:16", "1440x2560"),
        ("3K", "3:4", "2304x3072"),
        ("4K", "3:2", "4096x2736"),
    ],
)
def test_agent_plan_image_size_preserves_requested_aspect_ratio(
    quality,
    aspect_ratio,
    expected,
):
    assert image_size_for_aspect_ratio(quality, aspect_ratio) == expected


def test_agent_plan_image_size_rejects_unreviewed_contracts():
    with pytest.raises(ValueError, match="quality"):
        image_size_for_aspect_ratio("HD", "1:1")
    with pytest.raises(ValueError, match="aspect_ratio"):
        image_size_for_aspect_ratio("2K", "5:4")


def test_agent_plan_skill_aliases_and_advanced_payloads_are_server_adapted():
    assert video_gateway_model_id(VIDEO_MODEL) == VIDEO_MODEL_ALIASES[VIDEO_MODEL]
    assert (
        video_gateway_model_id(VIDEO_MODEL_15_PRO)
        == "doubao-seedance-1-5-pro-251215"
    )
    assert (
        video_gateway_model_id("doubao-seedance-2-0-260128")
        == "doubao-seedance-2-0-260128"
    )
    assert (
        video_gateway_model_id("doubao-seedance-1-5-pro-251215")
        == "doubao-seedance-1-5-pro-251215"
    )
    assert (
        video_gateway_model_id("doubao-seedance-1-0-pro-250528")
        == "doubao-seedance-1-5-pro-251215"
    )
    assert stable_video_model_name("doubao-seedance-1-0-pro-250528") == VIDEO_MODEL_15_PRO
    assert stable_video_model_name("doubao-seedance-1-5-pro-251215") == VIDEO_MODEL_15_PRO
    assert video_model_capabilities(VIDEO_MODEL_15_PRO).max_duration_seconds == 12

    image = image_request_payload(
        prompt="三张一组的连贯广告分镜",
        reference_image=["https://example.com/a.png", "https://example.com/b.png"],
        reference_strength=0.8,
        sequential=True,
        enable_web_search=True,
    )
    assert image["sequential_image_generation"] == "auto"
    assert image["image"] == [
        "https://example.com/a.png",
        "https://example.com/b.png",
    ]
    assert image["reference_strength"] == 0.8
    assert image["tools"] == [{"type": "web_search"}]

    video = video_request_payload(
        prompt="首尾帧广告",
        duration=10,
        resolution="1080p",
        ratio="9:16",
        first_frame_image="https://example.com/first.png",
        last_frame_image="https://example.com/last.png",
        seed=42,
        camera_fixed=False,
        return_last_frame=True,
        enable_web_search=True,
    )
    assert [item.get("role") for item in video["content"][1:]] == [
        "first_frame",
        "last_frame",
    ]
    assert video["seed"] == 42
    assert video["return_last_frame"] is True
    assert video["tools"] == [{"type": "web_search"}]


def test_seedance_15_payload_enforces_official_capability_matrix():
    video = video_request_payload(
        prompt="真人手持产品面向镜头完成一个连续动作",
        model=VIDEO_MODEL_15_PRO,
        duration=12,
        resolution="1080p",
        ratio="9:16",
        first_frame_image="https://example.com/first.png",
        last_frame_image="https://example.com/last.png",
        generate_audio=True,
        draft=True,
        service_tier="flex",
    )

    assert video["model"] == VIDEO_MODEL_ALIASES[VIDEO_MODEL_15_PRO]
    assert video["generate_audio"] is True
    assert video["draft"] is True
    assert video["service_tier"] == "flex"
    assert [item.get("role") for item in video["content"][1:]] == [
        "first_frame",
        "last_frame",
    ]

    with pytest.raises(ValueError, match="between 4 and 12"):
        video_request_payload(
            prompt="too long",
            model=VIDEO_MODEL_15_PRO,
            duration=13,
            resolution="720p",
            ratio="16:9",
        )
    with pytest.raises(ValueError, match="does not support web search"):
        video_request_payload(
            prompt="latest event",
            model=VIDEO_MODEL_15_PRO,
            duration=6,
            resolution="720p",
            ratio="16:9",
            enable_web_search=True,
        )
    with pytest.raises(ValueError, match="resolution must be one of"):
        video_request_payload(
            prompt="4K ad",
            model=VIDEO_MODEL_15_PRO,
            duration=6,
            resolution="4k",
            ratio="16:9",
        )
    with pytest.raises(ValueError, match="ratio must be one of"):
        video_request_payload(
            prompt="adaptive ad",
            model=VIDEO_MODEL_15_PRO,
            duration=6,
            resolution="720p",
            ratio="adaptive",
        )


def test_seedance_20_rejects_15_only_cost_modes():
    with pytest.raises(ValueError, match="does not support draft mode"):
        video_request_payload(
            prompt="draft",
            model=VIDEO_MODEL,
            duration=6,
            resolution="720p",
            ratio="16:9",
            draft=True,
        )
    with pytest.raises(ValueError, match="does not support flex service tier"):
        video_request_payload(
            prompt="flex",
            model=VIDEO_MODEL,
            duration=6,
            resolution="720p",
            ratio="16:9",
            service_tier="flex",
        )


@pytest.mark.asyncio
async def test_seedance_task_submission_preserves_product_audio_intent(monkeypatch):
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "task-15-pro"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        agent_tools,
        "_public_only_async_client",
        lambda *_args, **_kwargs: client,
    )

    task_id = await create_video_task(
        api_key="secret-plan-key",
        base_url=DEFAULT_BASE_URL,
        prompt="six second commercial action",
        model=VIDEO_MODEL_15_PRO,
        duration=6,
        resolution="720p",
        ratio="9:16",
        first_frame_image=None,
        generate_audio=False,
    )

    assert task_id == "task-15-pro"
    assert captured["model"] == VIDEO_MODEL_ALIASES[VIDEO_MODEL_15_PRO]
    assert captured["generate_audio"] is False


def test_agent_plan_only_explicit_non_timeout_4xx_can_authorize_fallback():
    rejected = provider_error_from_response(
        429,
        {"error": {"code": "QuotaExceeded", "message": "plan exhausted"}},
    )
    assert isinstance(rejected, VolcengineAgentPlanRejected)
    assert rejected.provider_code == "QuotaExceeded"

    timeout = provider_error_from_response(408, {"error": {"code": "Timeout"}})
    server = provider_error_from_response(500, {"error": {"code": "InternalServiceError"}})
    assert isinstance(timeout, VolcengineAgentPlanError)
    assert not isinstance(timeout, VolcengineAgentPlanRejected)
    assert isinstance(server, VolcengineAgentPlanError)
    assert not isinstance(server, VolcengineAgentPlanRejected)


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("queued", "Queueing"),
        ("running", "Processing"),
        ("succeeded", "Success"),
        ("failed", "Fail"),
        ("cancelled", "Fail"),
        ("expired", "Fail"),
        ("future_state", "Unknown"),
    ],
)
def test_agent_plan_video_status_maps_to_existing_durable_state_machine(
    provider_status: str,
    expected: str,
):
    assert normalized_video_status({"status": provider_status}) == expected


def test_agent_plan_credential_schema_prevents_payg_and_unsupported_video_tiers():
    value = CredentialCreateIn(
        provider="volcengine_agent_plan",
        label="Agent Plan Large",
        api_key="plan-key",
        base_url="https://ark.cn-beijing.volces.com/api/plan",
        plan_tier="LARGE",
        capabilities=["image", "video"],
    )
    assert value.base_url == DEFAULT_BASE_URL
    assert value.plan_tier == "large"

    with pytest.raises(ValidationError, match="requires Large or Max"):
        CredentialCreateIn(
            provider="volcengine_agent_plan",
            label="Agent Plan Medium",
            api_key="plan-key",
            plan_tier="medium",
            capabilities=["image", "video"],
        )
    with pytest.raises(ValidationError, match="explicit text/image/audio/video"):
        CredentialCreateIn(
            provider="volcengine_agent_plan",
            label="Missing capabilities",
            api_key="plan-key",
            plan_tier="large",
        )
    with pytest.raises(ValidationError):
        CredentialCreateIn(
            provider="volcengine_agent_plan",
            label="Wrong gateway",
            api_key="ark-payg-key",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            plan_tier="large",
            capabilities=["image"],
        )

    voice = CredentialCreateIn(
        provider="volcengine_agent_plan",
        label="Agent Plan Small voice",
        api_key="plan-key",
        plan_tier="small",
        capabilities=["text", "audio"],
    )
    assert voice.capabilities == ["text", "audio"]

    with pytest.raises(ValidationError, match="only text/image/audio/video"):
        CredentialCreateIn(
            provider="volcengine_agent_plan",
            label="No music entitlement",
            api_key="plan-key",
            plan_tier="large",
            capabilities=["music"],
        )


def test_agent_plan_credential_probe_is_read_only_task_listing():
    request = build_credential_probe_request(
        provider="volcengine_agent_plan",
        base_url="https://ark.cn-beijing.volces.com/api/plan/v3",
        api_key="secret-plan-key",
    )

    assert request.url == (
        "https://ark.cn-beijing.volces.com/api/plan/v3"
        "/contents/generations/tasks?page_num=1&page_size=1"
    )
    assert request.headers == {"Authorization": "Bearer secret-plan-key"}


def test_failover_gate_blocks_ambiguous_or_accepted_duplicate_work():
    explicit_rejection = VolcengineAgentPlanRejected(
        "rejected",
        provider_code="QuotaExceeded",
        http_status=429,
    )
    transport_failure = VolcengineAgentPlanError("socket closed")

    assert agent_tools._media_failover_is_safe(
        explicit_rejection,
        provider_request_started=True,
        provider_accepted=False,
    )
    assert agent_tools._media_failover_is_safe(
        transport_failure,
        provider_request_started=False,
        provider_accepted=False,
    )
    assert not agent_tools._media_failover_is_safe(
        transport_failure,
        provider_request_started=True,
        provider_accepted=False,
    )
    assert not agent_tools._media_failover_is_safe(
        explicit_rejection,
        provider_request_started=True,
        provider_accepted=True,
    )


@pytest.mark.asyncio
async def test_unsupported_video_model_opens_exact_provider_evidence_circuit(
    monkeypatch,
):
    credential_id = uuid.uuid4()
    mark_modality = AsyncMock()
    mark_degraded = AsyncMock()
    mark_quota = AsyncMock()
    mark_rate = AsyncMock()
    monkeypatch.setattr(
        load_balancer,
        "mark_credential_modality_quota_exceeded",
        mark_modality,
    )
    monkeypatch.setattr(load_balancer, "mark_credential_degraded", mark_degraded)
    monkeypatch.setattr(load_balancer, "mark_credential_quota_exceeded", mark_quota)
    monkeypatch.setattr(load_balancer, "mark_credential_rate_saturated", mark_rate)

    error = VolcengineAgentPlanRejected(
        "configured Agent Plan does not authorize the model",
        provider_code="UnsupportedModel",
        http_status=400,
    )
    await agent_tools._mark_media_provider_credential_failure(
        credential_id,
        error,
        provider="volcengine_agent_plan",
        modality="video",
        model=VIDEO_MODEL,
    )

    mark_modality.assert_awaited_once_with(
        credential_id,
        "video",
        model=VIDEO_MODEL,
        error_code="UnsupportedModel",
    )
    mark_degraded.assert_not_awaited()
    mark_quota.assert_not_awaited()
    mark_rate.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_quota_error_is_scoped_to_requested_resolution(monkeypatch):
    credential_id = uuid.uuid4()
    mark_modality = AsyncMock()
    monkeypatch.setattr(
        load_balancer,
        "mark_credential_modality_quota_exceeded",
        mark_modality,
    )
    monkeypatch.setattr(
        load_balancer,
        "mark_credential_degraded",
        AsyncMock(),
    )
    monkeypatch.setattr(
        load_balancer,
        "mark_credential_quota_exceeded",
        AsyncMock(),
    )
    monkeypatch.setattr(
        load_balancer,
        "mark_credential_rate_saturated",
        AsyncMock(),
    )

    error = VolcengineAgentPlanRejected(
        "requested video exceeds the remaining AFP allowance",
        provider_code="QuotaExceeded",
        http_status=429,
    )
    quota_model = volcengine_video_quota_model(VIDEO_MODEL, "1080p")
    await agent_tools._mark_media_provider_credential_failure(
        credential_id,
        error,
        provider="volcengine_agent_plan",
        modality="video",
        model=VIDEO_MODEL,
        quota_model=quota_model,
    )

    mark_modality.assert_awaited_once_with(
        credential_id,
        "video",
        model=quota_model,
        error_code="QuotaExceeded",
    )
    assert quota_model.endswith("@1080p")
    assert volcengine_video_quota_model(VIDEO_MODEL, "480P").endswith("@480p")
