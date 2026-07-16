import base64
from contextlib import ExitStack
from functools import lru_cache
from io import BytesIO
import json
import shutil
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from PIL import Image

from app.services import agent_tools
from app.services.agent_tools import (
    _custom_image_reference_to_bytes,
    _bounded_base64_decode,
    _check_video_minimax,
    _resolve_minimax_tool_tier,
    _generate_image,
    _generate_image_minimax,
    _generate_image_custom_api,
    _generate_music_minimax,
    _generate_speech_minimax,
    _generate_video_minimax,
    _get_minimax_tenant_uuid,
    _json_path_get,
    _is_minimax_deterministic_rejection,
    _minimax_image_acceptance_evidence_key,
    _minimax_audio_hex_to_bytes,
    _load_minimax_image_acceptance_evidence,
    _merge_runtime_tool_config,
    _raise_for_minimax_base_resp,
    _minimax_create_video_task,
    _minimax_download_file,
    _record_minimax_tool_success,
    _render_json_template,
    _store_minimax_image_acceptance_evidence,
    MiniMaxProviderRejected,
    MinimaxBillingContextError,
)
from app.api import tools as tools_api
from app.services.quota_guard import QuotaExceeded
from app.services.media_assets import validate_generated_image
from app.services.minimax_media_profiles import resolve_minimax_media_profile


def _valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (512, 512), (24, 96, 160)).save(output, format="PNG")
    return output.getvalue()


def _media_config_schema() -> dict:
    return {
        "fields": [
            {"key": "api_key", "type": "password"},
            {"key": "base_url", "type": "text"},
        ]
    }


def test_agent_owned_media_key_requires_a_frozen_agent_endpoint_bundle():
    with pytest.raises(ValueError, match="complete Agent endpoint bundle"):
        _merge_runtime_tool_config(
            "generate_image_openai",
            base_config={"base_url": "https://builtin.example/v1"},
            tenant_config={
                "base_url": "https://tenant.example/v1",
                "api_key": "tenant-secret",
            },
            agent_config={"api_key": "agent-secret"},
            config_schema=_media_config_schema(),
        )


def test_agent_owned_media_bundle_is_not_retargeted_by_later_tenant_edits():
    merged = _merge_runtime_tool_config(
        "generate_image_openai",
        base_config={"base_url": "https://builtin.example/v1"},
        tenant_config={
            "base_url": "https://new-tenant.example/v1",
            "api_key": "new-tenant-secret",
        },
        agent_config={
            "base_url": "https://frozen-agent.example/v1",
            "api_key": "agent-secret",
        },
        config_schema=_media_config_schema(),
    )

    assert merged["base_url"] == "https://frozen-agent.example/v1"
    assert merged["api_key"] == "agent-secret"


def test_agent_media_destination_cannot_inherit_company_key():
    with pytest.raises(ValueError, match="requires an Agent-owned API key"):
        _merge_runtime_tool_config(
            "generate_image_openai",
            base_config={"base_url": "https://builtin.example/v1"},
            tenant_config={
                "base_url": "https://tenant.example/v1",
                "api_key": "tenant-secret",
            },
            agent_config={"base_url": "https://agent.example/v1"},
            config_schema=_media_config_schema(),
        )


def test_ordinary_agent_manager_cannot_change_media_destination():
    tool = SimpleNamespace(
        name="generate_image_openai",
        config_schema=_media_config_schema(),
    )
    with pytest.raises(tools_api.HTTPException) as exc_info:
        tools_api._enforce_media_endpoint_bundle_update(
            SimpleNamespace(role="member", identity=None),
            tool,
            incoming_config={"base_url": "https://agent.example/v1"},
            existing_agent_config={},
            company_config={
                "base_url": "https://tenant.example/v1",
                "api_key": "tenant-secret",
            },
        )

    assert exc_info.value.status_code == 403


def test_org_admin_cannot_hide_custom_media_secret_in_headers():
    tool = SimpleNamespace(
        name="generate_image_custom",
        config_schema={"fields": [{"key": "api_key", "type": "password"}]},
    )
    with pytest.raises(tools_api.HTTPException) as exc_info:
        tools_api._enforce_media_endpoint_bundle_update(
            SimpleNamespace(role="org_admin", identity=None),
            tool,
            incoming_config={
                "base_url": "https://agent.example/v1",
                "endpoint_path": "/chat/completions",
                "extra_headers_json": '{"X-API-Key":"hidden-secret"}',
                "api_key": "agent-secret",
            },
            existing_agent_config={},
            company_config={},
        )

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("api_key", [None, "****", "••••", "********"])
def test_agent_media_destination_rotation_requires_fresh_unmasked_secret(api_key):
    tool = SimpleNamespace(
        name="generate_image_openai",
        config_schema=_media_config_schema(),
    )
    incoming = {"base_url": "https://new.example/v1"}
    if api_key is not None:
        incoming["api_key"] = api_key

    with pytest.raises(tools_api.HTTPException) as exc_info:
        tools_api._enforce_media_endpoint_bundle_update(
            SimpleNamespace(role="org_admin", identity=None),
            tool,
            incoming_config=incoming,
            existing_agent_config={
                "base_url": "https://old.example/v1",
                "api_key": "old-secret",
            },
            company_config={},
        )

    assert exc_info.value.status_code == 422
    assert "fresh unmasked API key" in exc_info.value.detail


def test_agent_media_destination_rotation_accepts_fresh_complete_bundle():
    tool = SimpleNamespace(
        name="generate_image_custom",
        config_schema={
            "fields": [
                {"key": "api_key", "type": "password"},
                {"key": "base_url", "type": "text"},
                {"key": "endpoint_path", "type": "text"},
                {"key": "extra_headers_json", "type": "text"},
            ]
        },
    )

    tools_api._enforce_media_endpoint_bundle_update(
        SimpleNamespace(role="org_admin", identity=None),
        tool,
        incoming_config={
            "api_key": "fresh-secret",
            "base_url": "https://new.example/v1",
            "endpoint_path": "/images/generations",
            "extra_headers_json": '{"X-Title":"Astra"}',
        },
        existing_agent_config={
            "api_key": "old-secret",
            "base_url": "https://old.example/v1",
            "endpoint_path": "/images/generations",
            "extra_headers_json": "",
        },
        company_config={},
    )


@pytest.mark.parametrize("api_key", [None, "****"])
def test_company_media_destination_rotation_requires_fresh_secret(api_key):
    tool = SimpleNamespace(name="generate_image_openai")
    incoming = {"base_url": "https://new.example/v1"}
    if api_key is not None:
        incoming["api_key"] = api_key

    with pytest.raises(tools_api.HTTPException) as exc_info:
        tools_api._require_fresh_media_destination_bundle(
            tool,
            incoming_config=incoming,
            existing_config={
                "base_url": "https://old.example/v1",
                "api_key": "old-secret",
            },
            scope_label="company-owned",
        )

    assert exc_info.value.status_code == 422


def test_company_custom_destination_rotation_requires_complete_bundle():
    tool = SimpleNamespace(name="generate_image_custom")
    with pytest.raises(tools_api.HTTPException) as exc_info:
        tools_api._require_fresh_media_destination_bundle(
            tool,
            incoming_config={
                "api_key": "fresh-secret",
                "base_url": "https://new.example/v1",
            },
            existing_config={
                "api_key": "old-secret",
                "base_url": "https://old.example/v1",
                "endpoint_path": "/images/generations",
                "extra_headers_json": "",
            },
            scope_label="company-owned",
        )

    assert exc_info.value.status_code == 422
    assert "complete destination bundle" in exc_info.value.detail


@pytest.mark.asyncio
async def test_custom_media_runtime_rejects_credential_like_url_query():
    with pytest.raises(ValueError, match="credential-like query"):
        await _generate_image_custom_api(
            api_key="structured-secret",
            model="image-model",
            base_url="https://gateway.example/v1?token=hidden-secret",
            endpoint_path="/chat/completions",
            request_body_template_json="",
            response_image_path="data.0.url",
            extra_headers_json="",
            timeout_seconds=30,
            prompt="cat",
            size="1024x1024",
        )


@lru_cache(maxsize=1)
def _valid_mp3_bytes() -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the real audio contract test")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=32000:cl=mono",
            "-t",
            "0.2",
            "-f",
            "mp3",
            "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    assert result.stdout
    return result.stdout


@pytest.mark.asyncio
async def test_minimax_billing_context_db_failure_is_not_treated_as_unbilled():
    with patch(
        "app.services.agent_tools._get_agent_tenant_id",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    ):
        with pytest.raises(MinimaxBillingContextError, match="No provider request was made"):
            await _get_minimax_tenant_uuid(uuid.uuid4())


@pytest.mark.asyncio
async def test_minimax_image_stops_before_provider_when_billing_context_is_unavailable(tmp_path):
    pick_credential = AsyncMock()
    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="lite")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "lite")),
        ),
        patch("app.services.agent_tools._check_minimax_tool_allowed", AsyncMock(return_value=None)),
        patch(
            "app.services.agent_tools._get_minimax_tenant_uuid",
            AsyncMock(side_effect=MinimaxBillingContextError("billing unavailable")),
        ),
        patch("app.services.llm.load_balancer.pick_credential", pick_credential),
    ):
        result = await _generate_image(
            uuid.uuid4(),
            tmp_path,
            {"prompt": "clean background"},
            "minimax",
        )

    assert result == "❌ billing unavailable"
    pick_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_minimax_image_acceptance_evidence_is_private_and_encrypted():
    agent_id = uuid.uuid4()
    recovery_id = uuid.uuid4()
    signed_url = "https://asset.example/private.png?token=secret"
    storage = SimpleNamespace(
        write_bytes=AsyncMock(),
        read_bytes=AsyncMock(),
    )

    with patch("app.services.agent_tools.get_storage_backend", return_value=storage):
        key, opaque_reference = await _store_minimax_image_acceptance_evidence(
            agent_id=agent_id,
            recovery_id=recovery_id,
            model="image-01",
            image_url=signed_url,
            save_path="workspace/images/result.png",
        )
        raw = storage.write_bytes.await_args.args[1]
        storage.read_bytes.return_value = raw
        recovered = await _load_minimax_image_acceptance_evidence(key)

    assert key == _minimax_image_acceptance_evidence_key(agent_id, recovery_id)
    assert key.startswith("_internal/provider_recovery/minimax/image/")
    assert not key.startswith(f"{agent_id}/")
    assert opaque_reference == f"minimax-image-recovery:{recovery_id}"
    assert signed_url.encode() not in raw
    assert b"token=secret" not in raw
    assert recovered == {
        "provider": "minimax",
        "model": "image-01",
        "image_url": signed_url,
        "save_path": "workspace/images/result.png",
    }


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
    def __init__(self, payload=None, *, content=b"", status_code=200, headers=None):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = json.dumps(self._payload)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        yield self.content or self.text.encode("utf-8")

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

    def stream(self, method, url, **kwargs):
        if method == "POST":
            self.posts.append((url, kwargs))
            return self.post_response
        assert method == "GET"
        assert self.get_response is not None
        return self.get_response


class _StreamingResponse:
    def __init__(self, chunks, *, headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _StreamingAsyncClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method, url, **_kwargs):
        assert method == "GET"
        assert url == "https://asset.example/video.mp4"
        return self.response

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


def test_generated_media_decoders_reject_oversized_encoded_payloads():
    encoded = base64.b64encode(b"abcdef").decode("ascii")
    with pytest.raises(ValueError, match="safety limit"):
        _bounded_base64_decode(
            encoded,
            max_bytes=5,
            label="Image payload",
        )

    with patch("app.services.agent_tools.MAX_GENERATED_AUDIO_BYTES", 5):
        with pytest.raises(ValueError, match="safety limit"):
            _minimax_audio_hex_to_bytes(
                {
                    "base_resp": {"status_code": 0},
                    "data": {"audio": b"abcdef".hex()},
                },
                "MiniMax audio",
            )


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
async def test_minimax_image_missing_url_is_not_marked_provider_accepted():
    client = _CaptureAsyncClient(
        _FakeHTTPResponse(
            {"base_resp": {"status_code": 0}, "data": {"image_urls": []}}
        )
    )
    accepted = AsyncMock()

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="No image URL"):
            await _generate_image_minimax(
                api_key="sk-test",
                base_url="https://api.minimax.example",
                model="image-01",
                prompt="product hero shot",
                aspect_ratio="1:1",
                on_provider_accepted=accepted,
            )

    accepted.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks"),
    [
        ({"content-length": "6"}, [b"data"]),
        ({}, [b"abc", b"def"]),
    ],
)
async def test_minimax_video_download_rejects_declared_or_streamed_oversize(
    headers,
    chunks,
):
    response = _StreamingResponse(chunks, headers=headers)
    client = _StreamingAsyncClient(response)

    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("app.services.agent_tools.MAX_MINIMAX_VIDEO_DOWNLOAD_BYTES", 5),
    ):
        with pytest.raises(ValueError, match="256MB safety limit"):
            await _minimax_download_file("https://asset.example/video.mp4")


@pytest.mark.asyncio
async def test_minimax_video_download_streams_bounded_valid_payload():
    response = _StreamingResponse(
        [b"abc", b"def"],
        headers={"content-length": "6"},
    )
    client = _StreamingAsyncClient(response)

    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("app.services.agent_tools.MAX_MINIMAX_VIDEO_DOWNLOAD_BYTES", 6),
    ):
        result = await _minimax_download_file(
            "https://asset.example/video.mp4"
        )

    assert result == b"abcdef"


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
        patch("app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready", AsyncMock()) as mark_settlement_ready,
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
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(
                return_value=(
                    "_internal/provider-recovery.json",
                    f"minimax-image-recovery:{reservation_id}",
                )
            ),
        ) as store_evidence,
        patch(
            "app.services.agent_tools._delete_minimax_image_acceptance_evidence",
            AsyncMock(),
        ) as delete_evidence,
        patch("app.services.storage.store_agent_bytes", AsyncMock(return_value="stored-key")) as store_bytes,
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
    assert reserve_credits.await_args.kwargs["initial_status"] == "provider_inflight"
    mark_settlement_ready.assert_awaited_once_with(reservation_id, amount=4)
    store_evidence.assert_awaited_once()
    assert store_evidence.await_args.kwargs["agent_id"] == agent_id
    assert store_evidence.await_args.kwargs["recovery_id"] == reservation_id
    assert store_evidence.await_args.kwargs["image_url"] is None
    delete_evidence.assert_awaited_once_with("_internal/provider-recovery.json")
    store_bytes.assert_awaited_once()
    final_call = store_bytes.await_args
    assert final_call.args[:2] == (agent_id, "workspace/images/cat.png")
    assert validate_generated_image(final_call.args[2]) == (512, 512)
    assert final_call.kwargs["content_type"] == "image/png"
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_storage_failure_preserves_provider_debt_and_raw_recovery(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=uuid.uuid4(), base_url=None)
    reservation = SimpleNamespace(id=reservation_id)

    storage_calls: list[str] = []

    async def store_with_recovery(_agent_id, rel_path, _data, *, content_type=None):
        storage_calls.append(rel_path)
        if len(storage_calls) == 1:
            raise OSError("object store full")
        assert content_type == "application/octet-stream"
        return f"{agent_id}/{rel_path}"

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
        patch("app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready", AsyncMock()) as mark_settlement_ready,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(return_value=_valid_png_bytes())),
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(
                return_value=(
                    "_internal/provider-recovery.json",
                    f"minimax-image-recovery:{reservation_id}",
                )
            ),
        ),
        patch(
            "app.services.agent_tools._delete_minimax_image_acceptance_evidence",
            AsyncMock(),
        ),
        patch("app.services.storage.store_agent_bytes", AsyncMock(side_effect=store_with_recovery)),
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()) as record_issue,
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_image(agent_id, tmp_path, {"prompt": "cat"}, "minimax")

    assert "No usable asset was delivered" in result
    assert "object store full" not in result
    mark_settlement_ready.assert_awaited_once_with(reservation_id, amount=4)
    assert storage_calls[0].startswith("workspace/images/cat_")
    assert storage_calls[0].endswith(".png")
    assert storage_calls[1] == f"workspace/media_inputs/{reservation_id}_provider_image.bin"
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()
    assert record_issue.await_args.kwargs["recovery_path"] == storage_calls[1]
    record_call.assert_not_awaited()
    consume_quota.assert_not_awaited()


@pytest.mark.parametrize(
    "failure_mode",
    [
        "settlement_mark",
        "evidence_store",
        "accepted_download_timeout",
        "missing_image_url",
    ],
)
@pytest.mark.asyncio
async def test_generate_image_accepted_provider_faults_never_refund_or_replay(
    tmp_path,
    failure_mode,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    reservation = SimpleNamespace(id=reservation_id)
    credential = SimpleNamespace(id=credential_id, base_url=None)
    generated = _valid_png_bytes()
    storage_calls: list[str] = []

    async def provider(**kwargs):
        if failure_mode == "missing_image_url":
            raise ValueError("No image URL in MiniMax response")
        await kwargs["on_provider_accepted"]("https://asset.example/accepted.png")
        if failure_mode == "accepted_download_timeout":
            raise httpx.ReadTimeout("accepted image download timed out")
        return generated

    async def store(_agent_id, rel_path, _data, *, content_type=None):
        storage_calls.append(rel_path)
        return f"{agent_id}/{rel_path}"

    async def store_evidence(**_kwargs):
        if failure_mode == "evidence_store":
            raise OSError("evidence storage unavailable")
        return (
            "_internal/provider-recovery.json",
            f"minimax-image-recovery:{reservation_id}",
        )

    mark_side_effect = (
        [RuntimeError("settlement database unavailable"), None]
        if failure_mode == "settlement_mark"
        else None
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={}))
        )
        stack.enter_context(
            patch(
                "app.services.agent_tools._get_agent_tenant_id",
                AsyncMock(return_value=str(tenant_id)),
            )
        )
        stack.enter_context(
            patch(
                "app.services.agent_tools._resolve_minimax_tool_tier",
                AsyncMock(return_value="pro"),
            )
        )
        stack.enter_context(
            patch(
                "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
                AsyncMock(return_value=resolve_minimax_media_profile("image", "pro")),
            )
        )
        stack.enter_context(
            patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock())
        )
        reserve = stack.enter_context(
            patch(
                "app.services.agent_tools._reserve_minimax_tool_credits",
                AsyncMock(return_value=reservation),
            )
        )
        mark_settlement = stack.enter_context(
            patch(
                "app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready",
                AsyncMock(side_effect=mark_side_effect),
            )
        )
        finalize = stack.enter_context(
            patch(
                "app.services.agent_tools._finalize_minimax_tool_reservation",
                AsyncMock(),
            )
        )
        release = stack.enter_context(
            patch(
                "app.services.agent_tools._release_minimax_tool_reservation",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch(
                "app.services.quota_guard.check_plan_generation_entitlement",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock())
        )
        stack.enter_context(
            patch(
                "app.services.llm.load_balancer.pick_credential",
                AsyncMock(return_value=credential),
            )
        )
        stack.enter_context(
            patch(
                "app.services.llm.utils.get_credential_api_key",
                MagicMock(return_value="sk-test"),
            )
        )
        provider_call = stack.enter_context(
            patch(
                "app.services.agent_tools._generate_image_minimax",
                AsyncMock(side_effect=provider),
            )
        )
        evidence_store = stack.enter_context(
            patch(
                "app.services.agent_tools._store_minimax_image_acceptance_evidence",
                AsyncMock(side_effect=store_evidence),
            )
        )
        stack.enter_context(
            patch(
                "app.services.agent_tools._delete_minimax_image_acceptance_evidence",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch("app.services.storage.store_agent_bytes", AsyncMock(side_effect=store))
        )
        issue = stack.enter_context(
            patch(
                "app.services.agent_tools._record_minimax_tool_product_issue",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch("app.services.llm.load_balancer.record_credential_call", AsyncMock())
        )
        stack.enter_context(
            patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock())
        )
        mark_credential = stack.enter_context(
            patch(
                "app.services.agent_tools._mark_minimax_tool_credential_failure",
                AsyncMock(),
            )
        )
        result = await _generate_image(
            agent_id,
            tmp_path,
            {"prompt": "cat", "save_path": "workspace/images/cat.png"},
            "minimax",
        )

    assert reserve.await_args.kwargs["initial_status"] == "provider_inflight"
    provider_call.assert_awaited_once()
    release.assert_not_awaited()
    if failure_mode == "missing_image_url":
        evidence_store.assert_not_awaited()
        mark_settlement.assert_not_awaited()
        finalize.assert_not_awaited()
        mark_credential.assert_awaited_once()
        assert "being held for safe reconciliation" in result
        assert storage_calls == []
    elif failure_mode == "settlement_mark":
        evidence_store.assert_awaited()
        mark_credential.assert_not_awaited()
        finalize.assert_awaited_once_with(reservation_id)
        assert mark_settlement.await_count == 2
        assert "✅ Image generated" in result
        assert issue.await_count >= 1
    elif failure_mode == "evidence_store":
        evidence_store.assert_awaited()
        mark_credential.assert_not_awaited()
        finalize.assert_awaited_once_with(reservation_id)
        mark_settlement.assert_awaited_once_with(reservation_id, amount=4)
        assert "✅ Image generated" in result
        assert issue.await_count >= 1
    else:
        evidence_store.assert_awaited()
        mark_credential.assert_not_awaited()
        finalize.assert_awaited_once_with(reservation_id)
        mark_settlement.assert_awaited_once_with(reservation_id, amount=4)
        assert "being held for safe reconciliation" in result
        assert storage_calls == []


@pytest.mark.parametrize(
    ("code", "deterministic"),
    [("1000", False), ("1001", False), ("2056", True), ("2062", True)],
)
def test_minimax_business_code_rejection_contract_is_shared(code, deterministic):
    with pytest.raises(ValueError) as captured:
        _raise_for_minimax_base_resp(
            {"base_resp": {"status_code": code, "status_msg": "provider status"}}
        )

    assert isinstance(captured.value, MiniMaxProviderRejected) is deterministic
    assert _is_minimax_deterministic_rejection(captured.value) is deterministic


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
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    reservation = SimpleNamespace(id=reservation_id)
    error = MiniMaxProviderRejected("MiniMax API error (1004): invalid api key")

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "lite")),
        ),
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch(
            "app.services.agent_tools._get_agent_tenant_id",
            AsyncMock(return_value=str(tenant_id)),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
        patch(
            "app.services.agent_tools._reserve_minimax_tool_credits",
            AsyncMock(return_value=reservation),
        ),
        patch(
            "app.services.agent_tools._release_minimax_tool_reservation",
            AsyncMock(),
        ) as release_credits,
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=error)),
        patch("app.services.llm.load_balancer.mark_credential_degraded", AsyncMock()) as mark_degraded,
        patch("app.services.llm.load_balancer.mark_credential_quota_exceeded", AsyncMock()) as mark_quota,
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
    ):
        result = await _generate_image(agent_id, tmp_path, {"prompt": "cat"}, "minimax")

    assert "❌ Image generation failed (minimax). Provider code: 1004." in result
    assert "invalid api key" not in result
    mark_degraded.assert_awaited_once_with(cred_id, immediate=True)
    mark_quota.assert_not_awaited()
    record_call.assert_not_awaited()
    release_credits.assert_awaited_once()


def test_media_provider_failure_message_never_leaks_response_body():
    secret = "provider response api_key=must-not-leak"

    result = agent_tools._safe_media_failure_message(
        "Video generation",
        "minimax",
        RuntimeError(f"MiniMax API error (1004): {secret}"),
    )

    assert "Provider code: 1004" in result
    assert secret not in result


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
        patch("app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready", AsyncMock()) as mark_settlement_ready,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_tts_http", AsyncMock(return_value=_valid_mp3_bytes())),
        patch("app.services.storage.store_agent_bytes", AsyncMock(return_value="stored-key")) as store_bytes,
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()) as record_call,
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()) as consume_quota,
    ):
        result = await _generate_speech_minimax(
            agent_id,
            tmp_path,
            {"text": "hello world", "save_path": "workspace/audio/hello.mp3"},
        )

    assert "✅ Speech generated" in result
    assert (tmp_path / "workspace/audio/hello.mp3").read_bytes() == _valid_mp3_bytes()
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
    assert reserve_credits.await_args.kwargs["initial_status"] == "provider_inflight"
    mark_settlement_ready.assert_awaited_once_with(reservation_id, amount=1)
    store_bytes.assert_awaited_once_with(
        agent_id,
        "workspace/audio/hello.mp3",
        _valid_mp3_bytes(),
        content_type="audio/mpeg",
    )
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
        patch("app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready", AsyncMock()) as mark_settlement_ready,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_credits,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_credits,
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_music_http", AsyncMock(return_value=_valid_mp3_bytes())),
        patch("app.services.storage.store_agent_bytes", AsyncMock(return_value="stored-key")) as store_bytes,
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
    assert (tmp_path / "workspace/audio/song.mp3").read_bytes() == _valid_mp3_bytes()
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
    assert reserve_credits.await_args.kwargs["initial_status"] == "provider_inflight"
    mark_settlement_ready.assert_awaited_once_with(reservation_id, amount=150)
    store_bytes.assert_awaited_once_with(
        agent_id,
        "workspace/audio/song.mp3",
        _valid_mp3_bytes(),
        content_type="audio/mpeg",
    )
    finalize_credits.assert_awaited_once_with(reservation_id)
    release_credits.assert_not_awaited()


@pytest.mark.parametrize("modality", ["audio", "music"])
@pytest.mark.parametrize(
    "failure_mode",
    [
        "provider_success_storage_failure",
        "settlement_mark_failure",
        "ambiguous_timeout",
        "deterministic_rejection",
    ],
)
@pytest.mark.asyncio
async def test_sync_minimax_audio_accounting_fault_matrix(
    tmp_path,
    modality,
    failure_mode,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    reservation = SimpleNamespace(id=reservation_id)
    credential = SimpleNamespace(id=credential_id, base_url=None)

    if modality == "audio":
        generator = _generate_speech_minimax
        provider_patch = "app.services.agent_tools._minimax_tts_http"
        profile = resolve_minimax_media_profile("audio", "pro")
        arguments = {
            "text": "hello world",
            "save_path": "workspace/audio/hello.mp3",
        }
        final_path = "workspace/audio/hello.mp3"
        recovery_path = f"workspace/media_inputs/{reservation_id}_provider_audio.mp3"
        provider_bytes = _valid_mp3_bytes()
        credit_cost = 1
    else:
        generator = _generate_music_minimax
        provider_patch = "app.services.agent_tools._minimax_music_http"
        profile = resolve_minimax_media_profile("music", "pro")
        arguments = {
            "prompt": "bright pop",
            "lyrics": "verse one",
            "save_path": "workspace/audio/song.mp3",
        }
        final_path = "workspace/audio/song.mp3"
        recovery_path = f"workspace/media_inputs/{reservation_id}_provider_music.mp3"
        provider_bytes = _valid_mp3_bytes()
        credit_cost = 150

    provider_error = None
    if failure_mode == "ambiguous_timeout":
        provider_error = httpx.ReadTimeout("provider outcome unknown")
    elif failure_mode == "deterministic_rejection":
        provider_error = MiniMaxProviderRejected(
            "MiniMax API error (2056): plan exhausted"
        )
    provider = AsyncMock(
        side_effect=provider_error,
        return_value=None if provider_error else provider_bytes,
    )
    storage_calls: list[str] = []

    async def store(_agent_id, rel_path, _data, *, content_type=None):
        storage_calls.append(rel_path)
        if failure_mode == "provider_success_storage_failure" and len(storage_calls) == 1:
            raise OSError("durable delivery failed")
        assert content_type == "audio/mpeg"
        return f"{agent_id}/{rel_path}"

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={}))
        )
        stack.enter_context(
            patch(
                "app.services.agent_tools._get_agent_tenant_id",
                AsyncMock(return_value=str(tenant_id)),
            )
        )
        stack.enter_context(
            patch(
                "app.services.agent_tools._resolve_minimax_tool_tier",
                AsyncMock(return_value="pro"),
            )
        )
        stack.enter_context(
            patch(
                "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
                AsyncMock(return_value=profile),
            )
        )
        stack.enter_context(
            patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock())
        )
        reserve = stack.enter_context(
            patch(
                "app.services.agent_tools._reserve_minimax_tool_credits",
                AsyncMock(return_value=reservation),
            )
        )
        mark_settlement = stack.enter_context(
            patch(
                "app.services.agent_tools._mark_minimax_tool_reservation_settlement_ready",
                AsyncMock(
                    side_effect=(
                        [RuntimeError("settlement unavailable"), None]
                        if failure_mode == "settlement_mark_failure"
                        else None
                    )
                ),
            )
        )
        finalize = stack.enter_context(
            patch(
                "app.services.agent_tools._finalize_minimax_tool_reservation",
                AsyncMock(),
            )
        )
        release = stack.enter_context(
            patch(
                "app.services.agent_tools._release_minimax_tool_reservation",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch(
                "app.services.quota_guard.check_plan_generation_entitlement",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock())
        )
        stack.enter_context(
            patch(
                "app.services.llm.load_balancer.pick_credential",
                AsyncMock(return_value=credential),
            )
        )
        stack.enter_context(
            patch(
                "app.services.llm.utils.get_credential_api_key",
                MagicMock(return_value="sk-test"),
            )
        )
        stack.enter_context(patch(provider_patch, provider))
        stack.enter_context(
            patch("app.services.storage.store_agent_bytes", AsyncMock(side_effect=store))
        )
        issue = stack.enter_context(
            patch(
                "app.services.agent_tools._record_minimax_tool_product_issue",
                AsyncMock(),
            )
        )
        mark_credential = stack.enter_context(
            patch(
                "app.services.agent_tools._mark_minimax_tool_credential_failure",
                AsyncMock(),
            )
        )
        result = await generator(agent_id, tmp_path, arguments)

    assert "failed (minimax)" in result
    assert reserve.await_args.kwargs["initial_status"] == "provider_inflight"
    issue.assert_awaited()
    if failure_mode in {
        "provider_success_storage_failure",
        "settlement_mark_failure",
    }:
        if failure_mode == "provider_success_storage_failure":
            mark_settlement.assert_awaited_once_with(
                reservation_id,
                amount=credit_cost,
            )
            assert storage_calls == [final_path, recovery_path]
        else:
            assert mark_settlement.await_count == 2
            assert storage_calls == [recovery_path]
        finalize.assert_awaited_once_with(reservation_id)
        release.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert issue.await_args.kwargs["recovery_path"] == recovery_path
    elif failure_mode == "ambiguous_timeout":
        mark_settlement.assert_not_awaited()
        finalize.assert_not_awaited()
        release.assert_not_awaited()
        mark_credential.assert_awaited_once()
        assert storage_calls == []
    else:
        mark_settlement.assert_not_awaited()
        finalize.assert_not_awaited()
        release.assert_awaited_once_with(
            reservation_id,
            release_provider_inflight=True,
        )
        mark_credential.assert_awaited_once()
        assert storage_calls == []


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
async def test_generate_video_minimax_binds_provider_before_metadata_enospc(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    reservation = SimpleNamespace(id=reservation_id)
    durable_bound = False
    write_attempts = 0

    async def mark_submitted(record_id, **_kwargs):
        nonlocal durable_bound
        durable_bound = True
        return record_id

    def fail_metadata_write(_path, *_args, **_kwargs):
        nonlocal write_attempts
        write_attempts += 1
        assert durable_bound is True
        raise OSError(28, "No space left on device")

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
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
        patch(
            "app.services.agent_tools._reserve_minimax_tool_credits",
            AsyncMock(return_value=reservation),
        ),
        patch("app.services.quota_guard.check_plan_generation_entitlement", AsyncMock()),
        patch("app.services.quota_guard.check_agent_llm_quota", AsyncMock()),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch(
            "app.services.agent_tools._minimax_create_video_task",
            AsyncMock(return_value="provider-task-enospc"),
        ),
        patch(
            "app.services.media_generation.create_minimax_video_task_record",
            AsyncMock(),
        ),
        patch(
            "app.services.media_generation.mark_minimax_video_task_submitted",
            AsyncMock(side_effect=mark_submitted),
        ) as bind_provider,
        patch("app.services.llm.load_balancer.record_credential_call", AsyncMock()),
        patch("app.services.quota_guard.consume_agent_llm_quota", AsyncMock()),
        patch("pathlib.Path.write_text", autospec=True, side_effect=fail_metadata_write),
    ):
        result = await _generate_video_minimax(
            agent_id,
            tmp_path,
            {"prompt": "durable video", "wait_for_completion": False},
        )

    assert durable_bound is True
    assert write_attempts >= 1
    bind_provider.assert_awaited_once()
    assert bind_provider.await_args.kwargs["provider_task_id"] == "provider-task-enospc"
    assert "task_id=provider-task-enospc" in result
    assert "durable task remains recoverable" in result


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
async def test_check_video_minimax_rejects_unbound_editable_legacy_metadata(tmp_path):
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
    with (
        patch("app.services.agent_tools._load_minimax_tool_credential_by_id", AsyncMock()) as load_credential,
        patch("app.services.agent_tools._minimax_query_video_task", AsyncMock()) as query_provider,
        patch("app.services.agent_tools._finalize_minimax_tool_reservation", AsyncMock()) as finalize_reservation,
        patch("app.services.media_generation.find_media_generation_task", AsyncMock(return_value=None)),
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()),
    ):
        result = await _check_video_minimax(
            agent_id,
            tmp_path,
            {"task_meta_path": "workspace/videos/task.json"},
        )

    assert "not bound to a durable Agent task" in result
    load_credential.assert_not_awaited()
    query_provider.assert_not_awaited()
    finalize_reservation.assert_not_awaited()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["reservation_id"] == str(reservation_id)


@pytest.mark.asyncio
async def test_check_video_minimax_never_releases_from_unbound_legacy_metadata(tmp_path):
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
    with (
        patch("app.services.agent_tools._load_minimax_tool_credential_by_id", AsyncMock()) as load_credential,
        patch("app.services.agent_tools._minimax_query_video_task", AsyncMock()) as query_provider,
        patch("app.services.agent_tools._release_minimax_tool_reservation", AsyncMock()) as release_reservation,
        patch("app.services.media_generation.find_media_generation_task", AsyncMock(return_value=None)),
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()),
    ):
        result = await _check_video_minimax(
            agent_id,
            tmp_path,
            {"task_meta_path": "workspace/videos/task.json"},
        )

    assert "not bound to a durable Agent task" in result
    load_credential.assert_not_awaited()
    query_provider.assert_not_awaited()
    release_reservation.assert_not_awaited()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["reservation_id"] == str(reservation_id)
