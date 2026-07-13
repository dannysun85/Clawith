import base64
from io import BytesIO
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.services.agent_tools import (
    _custom_image_reference_to_bytes,
    _check_video_minimax,
    _resolve_minimax_tool_tier,
    _generate_image,
    _generate_image_minimax,
    _generate_music_minimax,
    _generate_speech_minimax,
    _generate_video_minimax,
    _json_path_get,
    _minimax_create_video_task,
    _record_minimax_tool_success,
    _render_json_template,
)
from app.services.quota_guard import QuotaExceeded
from app.services.media_assets import validate_generated_image
from app.services.minimax_media_profiles import resolve_minimax_media_profile


def _valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (512, 512), (24, 96, 160)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_minimax_tool_success_survives_secondary_accounting_failures():
    agent_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    record_issue = AsyncMock()

    with (
        patch(
            "app.services.llm.load_balancer.record_credential_call",
            AsyncMock(side_effect=RuntimeError("credential counter unavailable")),
        ),
        patch(
            "app.services.quota_guard.consume_agent_llm_quota",
            AsyncMock(side_effect=RuntimeError("agent counter unavailable")),
        ),
        patch(
            "app.services.agent_tools._record_minimax_tool_product_issue",
            record_issue,
        ),
    ):
        await _record_minimax_tool_success(
            agent_id,
            credential_id,
            tier="pro",
            modality="image",
            model="image-01",
        )

    assert record_issue.await_count == 2
    assert all(call.kwargs["category"] == "usage_accounting" for call in record_issue.await_args_list)


class _FakeHTTPResponse:
    def __init__(self, payload=None, *, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _CaptureAsyncClient:
    def __init__(self, post_response, *, get_response=None):
        self.post_response = post_response
        self.get_response = get_response
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response

    async def get(self, *_args, **_kwargs):
        assert self.get_response is not None
        return self.get_response


def test_render_json_template_replaces_placeholders_after_json_parse():
    payload = _render_json_template(
        '{"model":"{model}","messages":[{"role":"user","content":"Draw: {prompt}"}],"size":"{size}"}',
        {
            "model": "google/gemini-2.5-flash-image",
            "prompt": 'red "apple"\nwhite background',
            "size": "1024x1024",
        },
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == 'Draw: red "apple"\nwhite background'
    assert payload["size"] == "1024x1024"


def test_render_json_template_accepts_escaped_quote_object_text():
    payload = _render_json_template(
        r'{ \"model\": \"{model}\", \"messages\": [{ \"role\": \"user\", \"content\": \"{prompt}\" }] }',
        {
            "model": "google/gemini-2.5-flash-image",
            "prompt": "red apple",
            "size": "1024x1024",
        },
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == "red apple"


def test_render_json_template_accepts_smart_quotes():
    payload = _render_json_template(
        '{ “model”: “{model}”, “messages”: [{ “role”: “user”, “content”: “{prompt}” }] }',
        {
            "model": "google/gemini-2.5-flash-image",
            "prompt": "blue circle",
            "size": "1024x1024",
        },
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == "blue circle"


def test_json_path_get_supports_nested_lists_and_dicts():
    data = {
        "choices": [
            {
                "message": {
                    "images": [
                        {"image_url": {"url": "data:image/png;base64,abc"}}
                    ]
                }
            }
        ]
    }

    assert (
        _json_path_get(data, "choices.0.message.images.0.image_url.url")
        == "data:image/png;base64,abc"
    )
    assert _json_path_get(data, "choices.1.message") is None
    assert _json_path_get(data, "choices.foo.message") is None


@pytest.mark.asyncio
async def test_custom_image_reference_to_bytes_decodes_data_url():
    raw = b"fake-png-bytes"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    assert await _custom_image_reference_to_bytes(data_url, client=None) == raw


@pytest.mark.asyncio
async def test_minimax_image_payload_includes_validated_subject_reference():
    generated = _valid_png_bytes()
    client = _CaptureAsyncClient(
        _FakeHTTPResponse({"base_resp": {"status_code": 0}, "data": {"image_urls": ["https://asset.example/image.png"]}}),
        get_response=_FakeHTTPResponse(content=generated),
    )

    with patch("httpx.AsyncClient", return_value=client):
        result = await _generate_image_minimax(
            api_key="sk-test",
            base_url="https://api.minimax.example",
            model="image-01",
            prompt="product hero shot",
            aspect_ratio="16:9",
            reference_image="data:image/png;base64,AAAA",
        )

    assert result == generated
    payload = client.posts[0][1]["json"]
    assert payload["subject_reference"] == [
        {"type": "character", "image_file": "data:image/png;base64,AAAA"}
    ]
    assert payload["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_minimax_video_payload_carries_first_and_last_frame_contract():
    client = _CaptureAsyncClient(
        _FakeHTTPResponse({"base_resp": {"status_code": 0}, "task_id": "task-frames"})
    )

    with patch("httpx.AsyncClient", return_value=client):
        task_id = await _minimax_create_video_task(
            api_key="sk-test",
            base_url="https://api.minimax.example",
            model="MiniMax-Hailuo-02",
            prompt="rotate the product slowly",
            duration=6,
            resolution="768P",
            first_frame_image="data:image/png;base64,FIRST",
            last_frame_image="data:image/png;base64,LAST",
            prompt_optimizer=False,
        )

    assert task_id == "task-frames"
    payload = client.posts[0][1]["json"]
    assert payload["first_frame_image"].endswith("FIRST")
    assert payload["last_frame_image"].endswith("LAST")
    assert payload["prompt_optimizer"] is False


@pytest.mark.asyncio
async def test_generate_image_minimax_records_success(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    reservation = SimpleNamespace(id=reservation_id)
    generated = _valid_png_bytes()

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={"model": "image-01"})),
        patch("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value=str(tenant_id))),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._reserve_minimax_tool_credits", AsyncMock(return_value=reservation)) as reserve_credits,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "pro")),
        ),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(return_value=generated)),
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_image(
            agent_id,
            tmp_path,
            {"prompt": "cat", "save_path": "workspace/images/cat.png"},
            "minimax",
        )

    assert "✅ Image generated" in result
    saved = (tmp_path / "workspace/images/cat.png").read_bytes()
    assert validate_generated_image(saved) == (512, 512)
    record_call.assert_awaited_once_with(cred_id, tokens_used=0)
    consume_quota.assert_awaited_once_with(agent_id, model_tier="pro")
    check_credits.assert_awaited_once_with(tenant_id, 4)
    reserve_credits.assert_awaited_once()
    assert reserve_credits.await_args.kwargs["tenant_id"] == tenant_id
    assert reserve_credits.await_args.kwargs["action"] == "image"
    assert reserve_credits.await_args.kwargs["modality"] == "image"
    assert reserve_credits.await_args.kwargs["tier"] == "pro"
    assert reserve_credits.await_args.kwargs["model"] == "image-01"
    assert reserve_credits.await_args.kwargs["credits"] == 4
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_storage_failure_does_not_settle_credits(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=uuid.uuid4(), base_url=None)
    reservation = SimpleNamespace(id=reservation_id)

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value=str(tenant_id))),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
        patch("app.services.agent_tools._reserve_minimax_tool_credits", AsyncMock(return_value=reservation)),
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(return_value=_valid_png_bytes())),
        patch("pathlib.Path.write_bytes", MagicMock(side_effect=OSError("disk full"))),
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_image(agent_id, tmp_path, {"prompt": "cat"}, "minimax")

    assert "disk full" in result
    finalize_credits.assert_not_awaited()
    release_credits.assert_awaited_once_with(reservation_id)
    record_call.assert_not_awaited()
    consume_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_rejects_same_prefix_workspace_escape_before_provider(tmp_path):
    provider = AsyncMock()
    escaped_path = f"../{tmp_path.name}-outside/product.png"

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="lite")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "lite")),
        ),
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch(
            "app.services.llm.load_balancer.pick_credential",
            AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), base_url=None)),
        ),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._generate_image_minimax", provider),
    ):
        result = await _generate_image(
            uuid.uuid4(),
            tmp_path,
            {"prompt": "product", "save_path": escaped_path},
            "minimax",
        )

    assert "outside the workspace" in result
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_minimax_entitlement_denial_skips_pool(tmp_path):
    agent_id = uuid.uuid4()
    check_model = AsyncMock(side_effect=QuotaExceeded("image not allowed", "model_modality"))
    pick = AsyncMock()

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "lite")),
        ),
        patch("app.services.quota_guard.check_plan_generation_entitlement", check_model),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()) as check_quota,
        patch("app.services.llm.load_balancer.pick_credential", pick),
    ):
        result = await _generate_image(agent_id, tmp_path, {"prompt": "cat"}, "minimax")

    assert result == "⚠️ image not allowed"
    pick.assert_not_awaited()
    check_quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_minimax_auth_error_degrades_credential(tmp_path):
    agent_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    error = ValueError("MiniMax API error (1004): invalid api key")

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "lite")),
        ),
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=error)),
        patch("app.services.llm.load_balancer.mark_credential_degraded", AsyncMock()) as mark_degraded,
        patch("app.services.llm.load_balancer.mark_credential_quota_exceeded", AsyncMock()) as mark_quota,
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
    ):
        result = await _generate_image(agent_id, tmp_path, {"prompt": "cat"}, "minimax")

    assert "❌ Image generation failed (minimax): MiniMax API error (1004)" in result
    mark_degraded.assert_awaited_once_with(cred_id, immediate=True)
    mark_quota.assert_not_awaited()
    record_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_speech_minimax_records_success(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    reservation = SimpleNamespace(id=reservation_id)

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "speech-2.8-turbo", "voice_id": "v1", "format": "mp3"}),
        ),
        patch("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value=str(tenant_id))),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("audio", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._reserve_minimax_tool_credits", AsyncMock(return_value=reservation)) as reserve_credits,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_tts_http", AsyncMock(return_value=b"fake-mp3")),
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_speech_minimax(
            agent_id,
            tmp_path,
            {"text": "hello world", "save_path": "workspace/audio/hello.mp3"},
        )

    assert "✅ Speech generated" in result
    assert (tmp_path / "workspace/audio/hello.mp3").read_bytes() == b"fake-mp3"
    record_call.assert_awaited_once_with(cred_id, tokens_used=0)
    consume_quota.assert_awaited_once_with(agent_id, model_tier="pro")
    check_credits.assert_awaited_once_with(tenant_id, 1)
    reserve_credits.assert_awaited_once()
    assert reserve_credits.await_args.kwargs["tenant_id"] == tenant_id
    assert reserve_credits.await_args.kwargs["action"] == "audio"
    assert reserve_credits.await_args.kwargs["modality"] == "audio"
    assert reserve_credits.await_args.kwargs["tier"] == "pro"
    assert reserve_credits.await_args.kwargs["model"] == "speech-2.8-turbo"
    assert reserve_credits.await_args.kwargs["credits"] == 1
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_music_minimax_records_success(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url="https://minimax.example")
    reservation = SimpleNamespace(id=reservation_id)

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "music-2.6", "format": "mp3"}),
        ),
        patch("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value=str(tenant_id))),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("music", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._reserve_minimax_tool_credits", AsyncMock(return_value=reservation)) as reserve_credits,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_music_http", AsyncMock(return_value=b"fake-song")),
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_music_minimax(
            agent_id,
            tmp_path,
            {
                "prompt": "bright pop",
                "lyrics": "verse one",
                "save_path": "workspace/audio/song.mp3",
            },
        )

    assert "✅ Music generated" in result
    assert (tmp_path / "workspace/audio/song.mp3").read_bytes() == b"fake-song"
    record_call.assert_awaited_once_with(cred_id, tokens_used=0)
    consume_quota.assert_awaited_once_with(agent_id, model_tier="pro")
    check_credits.assert_awaited_once_with(tenant_id, 150)
    reserve_credits.assert_awaited_once()
    assert reserve_credits.await_args.kwargs["tenant_id"] == tenant_id
    assert reserve_credits.await_args.kwargs["action"] == "music"
    assert reserve_credits.await_args.kwargs["modality"] == "music"
    assert reserve_credits.await_args.kwargs["tier"] == "pro"
    assert reserve_credits.await_args.kwargs["model"] == "music-2.6"
    assert reserve_credits.await_args.kwargs["credits"] == 150
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_video_minimax_creates_task_metadata(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    reservation = SimpleNamespace(id=reservation_id)
    lifecycle_order: list[str] = []
    reference_path = tmp_path / "workspace/images/product.png"
    reference_path.parent.mkdir(parents=True)
    reference_path.write_bytes(_valid_png_bytes())

    async def register_task(**_kwargs):
        lifecycle_order.append("register")

    async def create_provider_task(**_kwargs):
        lifecycle_order.append("provider")
        return "task-123"

    async def mark_submitted(*_args, **_kwargs):
        lifecycle_order.append("submitted")
        return _args[0]

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "MiniMax-Hailuo-2.3", "duration": 6, "resolution": "1080P"}),
        ),
        patch("app.services.agent_tools._get_agent_tenant_id", AsyncMock(return_value=str(tenant_id))),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("video", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._reserve_minimax_tool_credits", AsyncMock(return_value=reservation)) as reserve_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_create_video_task", AsyncMock(side_effect=create_provider_task)) as create_task,
        patch("app.services.media_generation.create_minimax_video_task_record", AsyncMock(side_effect=register_task)) as register,
        patch("app.services.media_generation.mark_minimax_video_task_submitted", AsyncMock(side_effect=mark_submitted)),
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_video_minimax(
            agent_id,
            tmp_path,
            {
                "prompt": "sunrise over a city",
                "first_frame_image": "workspace/images/product.png",
                "wait_for_completion": False,
            },
        )

    assert "task_id=task-123" in result
    metadata_files = list((tmp_path / "workspace/videos").glob("*.json"))
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["task_id"] == "task-123"
    assert metadata["credential_id"] == str(cred_id)
    assert metadata["reservation_id"] == str(reservation_id)
    assert metadata["status"] == "submitted"
    assert metadata["generation_mode"] == "image_to_video"
    assert metadata["has_first_frame"] is True
    assert "data:image" not in json.dumps(metadata)
    assert metadata["save_path"].endswith(".mp4")
    assert lifecycle_order == ["register", "provider", "submitted"]
    assert register.await_args.kwargs["reservation_id"] == reservation_id
    assert create_task.await_args.kwargs["first_frame_image"].startswith("data:image/png;base64,")
    assert "automatic" in result.lower()
    record_call.assert_awaited_once_with(cred_id, tokens_used=0)
    consume_quota.assert_awaited_once_with(agent_id, model_tier="pro")
    check_credits.assert_awaited_once_with(tenant_id, 280)
    reserve_credits.assert_awaited_once()
    assert reserve_credits.await_args.kwargs["tenant_id"] == tenant_id
    assert reserve_credits.await_args.kwargs["action"] == "video"
    assert reserve_credits.await_args.kwargs["modality"] == "video"
    assert reserve_credits.await_args.kwargs["tier"] == "pro"
    assert reserve_credits.await_args.kwargs["model"] == "MiniMax-Hailuo-2.3"
    assert reserve_credits.await_args.kwargs["credits"] == 280


@pytest.mark.asyncio
async def test_resolve_minimax_tool_tier_maps_legacy_standard_to_pro():
    assert await _resolve_minimax_tool_tier(uuid.uuid4(), {"tier": "standard"}) == "pro"


@pytest.mark.asyncio
async def test_resolve_minimax_tool_tier_prefers_current_invocation():
    with patch("app.services.agent_tools._get_agent_preferred_tier", AsyncMock(return_value="lite")):
        assert await _resolve_minimax_tool_tier(
            uuid.uuid4(),
            {"tier": "pro"},
            "ultra",
        ) == "ultra"


@pytest.mark.asyncio
async def test_resolve_minimax_tool_tier_uses_agent_preference_when_config_missing():
    with patch("app.services.agent_tools._get_agent_preferred_tier", AsyncMock(return_value="ultra")):
        assert await _resolve_minimax_tool_tier(uuid.uuid4(), {}) == "ultra"


@pytest.mark.asyncio
async def test_resolve_minimax_tool_tier_uses_agent_preference_before_legacy_tool_default():
    with patch("app.services.agent_tools._get_agent_preferred_tier", AsyncMock(return_value="lite")):
        assert await _resolve_minimax_tool_tier(uuid.uuid4(), {"tier": "pro"}) == "lite"


@pytest.mark.asyncio
async def test_resolve_minimax_tool_tier_defaults_to_lite():
    with patch("app.services.agent_tools._get_agent_preferred_tier", AsyncMock(return_value=None)):
        assert await _resolve_minimax_tool_tier(uuid.uuid4(), {}) == "lite"


@pytest.mark.asyncio
async def test_check_video_minimax_downloads_ready_video(tmp_path):
    agent_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    meta_dir = tmp_path / "workspace/videos"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "task.json"
    meta_path.write_text(
        json.dumps(
            {
                "provider": "minimax",
                "task_id": "task-123",
                "credential_id": str(cred_id),
                "reservation_id": str(reservation_id),
                "prompt": "city",
                "save_path": "",
            }
        ),
        encoding="utf-8",
    )
    credential = SimpleNamespace(id=cred_id, api_key="sk-test", base_url="https://minimax.example")

    settlement_order: list[str] = []

    async def download_video(*_args, **_kwargs):
        settlement_order.append("download")
        return "workspace/videos/out.mp4"

    async def finalize_video(*_args, **_kwargs):
        settlement_order.append("finalize")

    with (
        patch("app.services.agent_tools._load_minimax_tool_credential_by_id", AsyncMock(return_value=credential)),
        patch(
            "app.services.agent_tools._minimax_query_video_task",
            AsyncMock(return_value={"status": "Success", "file_id": "file-123"}),
        ),
        patch(
            "app.services.agent_tools._download_minimax_video_from_status",
            AsyncMock(side_effect=download_video),
        ),
        patch(
            "app.services.agent_tools._finalize_minimax_tool_reservation",
            AsyncMock(side_effect=finalize_video),
        ) as finalize_reservation,
        patch("app.services.media_generation.find_media_generation_task", AsyncMock(return_value=None)),
    ):
        result = await _check_video_minimax(
            agent_id,
            tmp_path,
            {"task_meta_path": "workspace/videos/task.json"},
        )

    assert "✅ MiniMax video is ready" in result
    updated = json.loads(meta_path.read_text(encoding="utf-8"))
    assert updated["status"] == "Success"
    assert updated["downloaded_path"] == "workspace/videos/out.mp4"
    assert settlement_order == ["download", "finalize"]
    finalize_reservation.assert_awaited_once_with(reservation_id)


@pytest.mark.asyncio
async def test_check_video_minimax_releases_reserved_credits_on_provider_failure(tmp_path):
    agent_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    meta_dir = tmp_path / "workspace/videos"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "task.json"
    meta_path.write_text(
        json.dumps(
            {
                "provider": "minimax",
                "task_id": "task-123",
                "credential_id": str(cred_id),
                "reservation_id": str(reservation_id),
                "prompt": "city",
                "save_path": "",
            }
        ),
        encoding="utf-8",
    )
    credential = SimpleNamespace(id=cred_id, api_key="sk-test", base_url="https://minimax.example")

    with (
        patch("app.services.agent_tools._load_minimax_tool_credential_by_id", AsyncMock(return_value=credential)),
        patch(
            "app.services.agent_tools._minimax_query_video_task",
            AsyncMock(return_value={"status": "Fail", "fail_reason": "provider quota"}),
        ),
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_reservation,
        patch("app.services.media_generation.find_media_generation_task", AsyncMock(return_value=None)),
    ):
        result = await _check_video_minimax(
            agent_id,
            tmp_path,
            {"task_meta_path": "workspace/videos/task.json"},
        )

    assert "❌ MiniMax video task failed: provider quota" in result
    updated = json.loads(meta_path.read_text(encoding="utf-8"))
    assert updated["status"] == "Fail"
    release_reservation.assert_awaited_once_with(reservation_id)
