import asyncio
import base64
from functools import lru_cache
from io import BytesIO
import json
import shutil
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import httpx
from PIL import Image

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
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
    _get_agent_tenant_id,
    _get_minimax_tenant_uuid,
    _json_path_get,
    _is_minimax_deterministic_rejection,
    _minimax_image_acceptance_evidence_key,
    _minimax_http_error,
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
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.mcp_security import MCPURLPolicyError


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


@pytest.mark.asyncio
async def test_public_provider_client_marks_started_after_public_origin_preflight():
    policy_error = MCPURLPolicyError(
        "MCP endpoint must resolve only to public addresses"
    )
    policy_hook = AsyncMock(side_effect=policy_error)
    guard = MagicMock()
    guard.client_kwargs.return_value = {
        "event_hooks": {"request": [policy_hook]},
    }
    started = MagicMock()

    with (
        patch("app.services.mcp_security.MCPHTTPGuard", return_value=guard),
        patch("httpx.AsyncClient", return_value=MagicMock()) as client_class,
    ):
        agent_tools._public_only_async_client(
            "https://api.minimax.test/v1/image_generation",
            timeout=120,
            on_request_started=started,
        )

    request_hooks = client_class.call_args.kwargs["event_hooks"]["request"]
    assert request_hooks[0] is policy_hook
    with pytest.raises(MCPURLPolicyError):
        await request_hooks[0](MagicMock())
    started.assert_not_called()

    await request_hooks[1](MagicMock())
    started.assert_called_once_with()


@pytest.mark.asyncio
async def test_public_provider_client_uses_allowlisted_explicit_proxy():
    policy_hook = AsyncMock()
    guard = MagicMock()
    guard.client_kwargs.return_value = {
        "proxy": "http://127.0.0.1:7890",
        "event_hooks": {"request": [policy_hook]},
    }
    started = MagicMock()
    settings = SimpleNamespace(HTTP_PROXY="http://127.0.0.1:7890")

    with (
        patch("app.services.agent_tools.get_settings", return_value=settings),
        patch(
            "app.services.mcp_security.TrustedProviderProxyHTTPGuard",
            return_value=guard,
        ) as guard_class,
        patch("httpx.AsyncClient", return_value=MagicMock()) as client_class,
    ):
        agent_tools._public_only_async_client(
            "https://api.minimaxi.com/v1/image_generation",
            timeout=120,
            on_request_started=started,
        )

    guard_class.assert_called_once_with(
        "https://api.minimaxi.com/v1/image_generation",
        allowed_hostnames={
            "api.minimaxi.com",
            "api.minimax.io",
            "api.minimax.chat",
        },
    )
    request_hooks = client_class.call_args.kwargs["event_hooks"]["request"]
    assert request_hooks[0] is policy_hook
    await request_hooks[0](MagicMock())
    started.assert_not_called()
    await request_hooks[1](MagicMock())
    started.assert_called_once_with()


def test_explicit_proxy_does_not_retarget_unreviewed_provider_origins():
    public_guard = MagicMock()
    public_guard.client_kwargs.return_value = {}
    settings = SimpleNamespace(HTTP_PROXY="http://127.0.0.1:7890")

    with (
        patch("app.services.agent_tools.get_settings", return_value=settings),
        patch(
            "app.services.mcp_security.MCPHTTPGuard",
            return_value=public_guard,
        ) as guard_class,
        patch(
            "app.services.mcp_security.TrustedProviderProxyHTTPGuard"
        ) as proxy_guard_class,
        patch("httpx.AsyncClient", return_value=MagicMock()),
    ):
        agent_tools._public_only_async_client(
            "https://custom.example/v1/images/generations",
            timeout=120,
        )

    guard_class.assert_called_once_with(
        "https://custom.example/v1/images/generations"
    )
    proxy_guard_class.assert_not_called()


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
async def test_agent_tenant_lookup_supports_strict_paid_media_mode():
    tenant_id = uuid.uuid4()

    class _Result:
        def scalar_one_or_none(self):
            return tenant_id

    class _Session:
        async def execute(self, statement):
            del statement
            return _Result()

    class _SessionContext:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

    with patch("app.services.agent_tools.async_session", return_value=_SessionContext()):
        result = await _get_agent_tenant_id(uuid.uuid4(), strict=True)

    assert result == str(tenant_id)


@pytest.mark.asyncio
async def test_minimax_billing_context_rejects_missing_agent_tenant():
    with patch(
        "app.services.agent_tools._get_agent_tenant_id",
        AsyncMock(return_value=None),
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
    cred = SimpleNamespace(id=cred_id, base_url=None)
    generated = _valid_png_bytes()

    async def provider(**kwargs):
        await kwargs["on_provider_accepted"]("https://asset.example/generated.png")
        return generated

    created_task = SimpleNamespace(
        request_metadata={"recovery_asset_storage_key": "_internal/recovery/image.bin"}
    )

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={"model": "image-01"})),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._get_minimax_tenant_uuid", AsyncMock(return_value=tenant_id)),
        patch("app.services.agent_tools._check_minimax_tool_allowed", AsyncMock(return_value=None)),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("image", "pro")),
        ),
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=provider)) as provider_call,
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(return_value=("_internal/provider-recovery.json", "opaque")),
        ) as store_evidence,
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(return_value=created_task),
        ) as create_task,
        patch(
            "app.services.media_generation.mark_minimax_sync_provider_accepted",
            AsyncMock(),
        ) as mark_accepted,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(),
        ) as store_recovery,
        patch(
            "app.services.media_generation.reconcile_minimax_sync_media_task",
            AsyncMock(return_value=SimpleNamespace(status="succeeded")),
        ) as reconcile,
        patch("app.services.agent_tools._record_minimax_tool_success", AsyncMock()) as record_success,
    ):
        result = await _generate_image(
            agent_id,
            tmp_path,
            {"prompt": "cat", "save_path": "workspace/images/cat.png"},
            "minimax",
        )

    assert "✅ Image generated" in result
    check_credits.assert_awaited_once_with(tenant_id, 4)
    create_task.assert_awaited_once()
    creation = create_task.await_args.kwargs
    assert creation["tenant_id"] == tenant_id
    assert creation["credential_id"] == cred_id
    assert creation["modality"] == "image"
    assert creation["tier"] == "pro"
    assert creation["model"] == "image-01"
    assert creation["credit_cost"] == 4
    assert creation["output_path"].startswith("workspace/images/cat_")
    assert creation["output_path"].endswith(".png")
    record_id = creation["record_id"]
    provider_call.assert_awaited_once()
    assert store_evidence.await_args.kwargs["recovery_id"] == record_id
    mark_accepted.assert_awaited_once_with(
        record_id,
        evidence_key="_internal/provider-recovery.json",
        accepted_metadata={"status": "Accepted"},
    )
    store_recovery.assert_awaited_once_with(
        record_id,
        generated,
        content_type="application/octet-stream",
        expected_key="_internal/recovery/image.bin",
    )
    reconcile.assert_awaited_once_with(record_id, deliver_completion=False)
    record_success.assert_awaited_once_with(
        agent_id,
        cred_id,
        tier="pro",
        modality="image",
        model="image-01",
    )


@pytest.mark.asyncio
async def test_generate_image_minimax_missing_reference_is_typed_failure_before_task(tmp_path):
    create_task = AsyncMock()

    with patch(
        "app.services.media_generation.create_minimax_sync_media_task_record",
        create_task,
    ):
        result = await agent_tools._generate_image_minimax_durable(
            agent_id=uuid.uuid4(),
            ws=tmp_path,
            arguments={"reference_image": "uploads/missing.png"},
            user_id=None,
            session_id="",
            tenant_id=uuid.uuid4(),
            credential_id=uuid.uuid4(),
            api_key="sk-test",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="lite",
            credit_cost=4,
            provider_prompt="poster",
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
    assert result.status == "failed"
    assert result.error_code == "brand_safe_media_contract_invalid"
    assert "was not found in the workspace" in result.result_summary
    create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_image_storage_failure_preserves_provider_debt_and_raw_recovery(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    generated = _valid_png_bytes()

    async def provider(**kwargs):
        await kwargs["on_provider_accepted"]("https://asset.example/generated.png")
        return generated

    with (
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=provider)),
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(return_value=("_internal/provider-recovery.json", "opaque")),
        ),
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": "_internal/recovery/image.bin"}
                )
            ),
        ) as create_task,
        patch("app.services.media_generation.mark_minimax_sync_provider_accepted", AsyncMock()) as mark_accepted,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(side_effect=OSError("object store full")),
        ) as store_recovery,
        patch("app.services.media_generation.mark_media_generation_submission_ambiguous", AsyncMock()) as ambiguous,
        patch("app.services.media_generation.mark_media_generation_submission_failed", AsyncMock()) as failed,
        patch("app.services.media_generation.reconcile_minimax_sync_media_task", AsyncMock()) as reconcile,
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()) as record_issue,
        patch("app.services.agent_tools._mark_minimax_tool_credential_failure", AsyncMock()) as mark_credential,
    ):
        result = await agent_tools._generate_image_minimax_durable(
            agent_id=agent_id,
            ws=tmp_path,
            arguments={},
            user_id=None,
            session_id="",
            tenant_id=tenant_id,
            credential_id=credential_id,
            api_key="sk-test",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="pro",
            credit_cost=4,
            provider_prompt="cat",
            save_path="workspace/images/cat.png",
            output_extension=".png",
            overlay_text="",
            overlay_position="bottom",
            brand_asset=None,
            brand_position="center",
            brand_scale=0.42,
        )

    assert "held by the durable recovery worker" in result
    assert "object store full" not in result
    record_id = create_task.await_args.kwargs["record_id"]
    mark_accepted.assert_awaited_once()
    store_recovery.assert_awaited_once_with(
        record_id,
        generated,
        content_type="application/octet-stream",
        expected_key="_internal/recovery/image.bin",
    )
    ambiguous.assert_not_awaited()
    failed.assert_not_awaited()
    reconcile.assert_not_awaited()
    mark_credential.assert_not_awaited()
    assert record_issue.await_args.kwargs["recovery_path"] == f"media-task:{record_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["pre_request", "request_started", "provider_accepted"])
async def test_minimax_image_cancellation_persists_safe_credit_state(
    tmp_path,
    cancel_stage,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()

    async def provider(**kwargs):
        if cancel_stage != "pre_request":
            kwargs["on_provider_request_started"]()
        if cancel_stage == "provider_accepted":
            await kwargs["on_provider_accepted"](
                "https://asset.example/accepted.png"
            )
        raise asyncio.CancelledError()

    with (
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={
                        "recovery_asset_storage_key": "_internal/recovery/image.bin"
                    }
                )
            ),
        ) as create_task,
        patch(
            "app.services.agent_tools._generate_image_minimax",
            AsyncMock(side_effect=provider),
        ),
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(return_value=("_internal/provider-acceptance.json", "opaque")),
        ),
        patch(
            "app.services.media_generation.mark_minimax_sync_provider_accepted",
            AsyncMock(),
        ) as accepted,
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(return_value=True),
        ) as failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as ambiguous,
        patch(
            "app.services.media_generation.record_minimax_sync_provider_response_retry",
            AsyncMock(),
        ) as response_retry,
    ):
        with pytest.raises(asyncio.CancelledError):
            await agent_tools._generate_image_minimax_durable(
                agent_id=agent_id,
                ws=tmp_path,
                arguments={},
                user_id=None,
                session_id="",
                tenant_id=tenant_id,
                credential_id=credential_id,
                api_key="sk-test",
                base_url="https://api.minimax.test",
                model="image-01",
                tier="pro",
                credit_cost=4,
                provider_prompt="cat",
                save_path="workspace/images/cat.png",
                output_extension=".png",
                overlay_text="",
                overlay_position="bottom",
                brand_asset=None,
                brand_position="center",
                brand_scale=0.42,
            )

    record_id = create_task.await_args.kwargs["record_id"]
    response_retry.assert_not_awaited()
    if cancel_stage == "pre_request":
        failed.assert_awaited_once_with(record_id, ANY)
        ambiguous.assert_not_awaited()
        accepted.assert_not_awaited()
    elif cancel_stage == "request_started":
        failed.assert_not_awaited()
        ambiguous.assert_awaited_once_with(record_id, ANY)
        accepted.assert_not_awaited()
    else:
        failed.assert_not_awaited()
        ambiguous.assert_not_awaited()
        accepted.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_cancellation_transition_survives_repeated_abort():
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()
    transition_completed = False

    async def transition():
        nonlocal transition_completed
        transition_started.set()
        await release_transition.wait()
        transition_completed = True

    async def operation():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await agent_tools._complete_media_cancellation_transition(
                transition(),
                label="test",
            )
            raise

    task = asyncio.create_task(operation())
    await asyncio.sleep(0)
    task.cancel()
    await transition_started.wait()
    task.cancel()
    release_transition.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert transition_completed is True


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
    credential_id = uuid.uuid4()
    generated = _valid_png_bytes()

    async def provider(**kwargs):
        kwargs["on_provider_request_started"]()
        if failure_mode == "missing_image_url":
            raise ValueError("No image URL in MiniMax response")
        await kwargs["on_provider_accepted"]("https://asset.example/accepted.png")
        if failure_mode == "accepted_download_timeout":
            raise httpx.ReadTimeout("accepted image download timed out")
        return generated

    async def store_evidence(**_kwargs):
        if failure_mode == "evidence_store":
            raise OSError("evidence storage unavailable")
        return "_internal/provider-recovery.json", "opaque"

    accepted_error = (
        RuntimeError("settlement database unavailable")
        if failure_mode == "settlement_mark"
        else None
    )
    with (
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=provider)) as provider_call,
        patch(
            "app.services.agent_tools._store_minimax_image_acceptance_evidence",
            AsyncMock(side_effect=store_evidence),
        ) as evidence_store,
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": "_internal/recovery/image.bin"}
                )
            ),
        ) as create_task,
        patch(
            "app.services.media_generation.mark_minimax_sync_provider_accepted",
            AsyncMock(side_effect=accepted_error),
        ) as mark_accepted,
        patch("app.services.media_generation.store_minimax_sync_recovery_asset", AsyncMock()) as store_recovery,
        patch("app.services.media_generation.reconcile_minimax_sync_media_task", AsyncMock()) as reconcile,
        patch(
            "app.services.media_generation.record_minimax_sync_provider_response_retry",
            AsyncMock(),
        ) as recovery_retry,
        patch("app.services.media_generation.mark_media_generation_submission_ambiguous", AsyncMock()) as ambiguous,
        patch("app.services.media_generation.mark_media_generation_submission_failed", AsyncMock()) as failed,
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()) as issue,
        patch("app.services.agent_tools._mark_minimax_tool_credential_failure", AsyncMock()) as mark_credential,
    ):
        result = await agent_tools._generate_image_minimax_durable(
            agent_id=agent_id,
            ws=tmp_path,
            arguments={},
            user_id=None,
            session_id="",
            tenant_id=tenant_id,
            credential_id=credential_id,
            api_key="sk-test",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="pro",
            credit_cost=4,
            provider_prompt="cat",
            save_path="workspace/images/cat.png",
            output_extension=".png",
            overlay_text="",
            overlay_position="bottom",
            brand_asset=None,
            brand_position="center",
            brand_scale=0.42,
        )

    record_id = create_task.await_args.kwargs["record_id"]
    provider_call.assert_awaited_once()
    failed.assert_not_awaited()
    reconcile.assert_not_awaited()
    store_recovery.assert_not_awaited()
    issue.assert_awaited_once()
    if failure_mode == "missing_image_url":
        evidence_store.assert_not_awaited()
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_not_awaited()
        ambiguous.assert_awaited_once()
        assert ambiguous.await_args.args[0] == record_id
        assert isinstance(ambiguous.await_args.args[1], ValueError)
        mark_credential.assert_awaited_once()
        assert "submission outcome is uncertain" in result
    elif failure_mode == "settlement_mark":
        evidence_store.assert_awaited_once()
        mark_accepted.assert_awaited_once()
        recovery_retry.assert_awaited_once()
        ambiguous.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert "repairing the acceptance record" in result
    elif failure_mode == "evidence_store":
        evidence_store.assert_awaited_once()
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_awaited_once()
        ambiguous.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert "repairing the acceptance record" in result
    else:
        evidence_store.assert_awaited_once()
        mark_accepted.assert_awaited_once()
        recovery_retry.assert_not_awaited()
        ambiguous.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert "held by the durable recovery worker" in result


@pytest.mark.asyncio
async def test_generate_image_public_origin_preflight_failure_releases_hold(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    error = MCPURLPolicyError(
        "MCP endpoint must resolve only to public addresses"
    )

    with (
        patch(
            "app.services.agent_tools._generate_image_minimax",
            AsyncMock(side_effect=error),
        ),
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={
                        "recovery_asset_storage_key": "_internal/recovery/image.bin"
                    }
                )
            ),
        ) as create_task,
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(),
        ) as failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as ambiguous,
        patch(
            "app.services.agent_tools._record_minimax_tool_product_issue",
            AsyncMock(),
        ),
        patch(
            "app.services.agent_tools._mark_minimax_tool_credential_failure",
            AsyncMock(),
        ),
    ):
        result = await agent_tools._generate_image_minimax_durable(
            agent_id=agent_id,
            ws=tmp_path,
            arguments={},
            user_id=None,
            session_id="",
            tenant_id=tenant_id,
            credential_id=credential_id,
            api_key="sk-test",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="lite",
            credit_cost=4,
            provider_prompt="cat",
            save_path="workspace/images/cat.png",
            output_extension=".png",
            overlay_text="",
            overlay_position="bottom",
            brand_asset=None,
            brand_position="center",
            brand_scale=0.42,
            typed=True,
        )

    record_id = create_task.await_args.kwargs["record_id"]
    failed.assert_awaited_once_with(record_id, error)
    ambiguous.assert_not_awaited()
    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "failed"
    assert result.error_code == "minimax_image_network_policy_blocked"
    assert result.metadata["runtime_halt_run"] is True
    assert "do not call this tool again" in (result.result_summary or "")
    assert "remain held" not in (result.result_summary or "")
    assert "submission outcome is uncertain" not in (
        result.result_summary or ""
    )


@pytest.mark.parametrize(
    ("code", "deterministic"),
    [
        ("1000", False),
        ("1001", False),
        ("1027", False),
        ("1004", True),
        ("2056", True),
        ("2062", True),
    ],
)
def test_minimax_business_code_rejection_contract_is_shared(code, deterministic):
    with pytest.raises(ValueError) as captured:
        _raise_for_minimax_base_resp(
            {"base_resp": {"status_code": code, "status_msg": "provider status"}}
        )

    assert isinstance(captured.value, MiniMaxProviderRejected) is deterministic
    assert _is_minimax_deterministic_rejection(captured.value) is deterministic


@pytest.mark.parametrize("http_status", [400, 401, 409, 422, 429, 451])
def test_minimax_http_status_without_reviewed_business_code_is_ambiguous(http_status):
    response = SimpleNamespace(
        status_code=http_status,
        text="gateway rejection",
        json=lambda: {"message": "gateway rejection"},
    )

    error = _minimax_http_error(response)

    assert type(error) is ValueError
    assert not _is_minimax_deterministic_rejection(error)


def test_minimax_http_error_uses_reviewed_business_code_not_status_class():
    response = SimpleNamespace(
        status_code=500,
        text="provider response",
        json=lambda: {
            "base_resp": {"status_code": 1004, "status_msg": "invalid api key"}
        },
    )

    error = _minimax_http_error(response)

    assert isinstance(error, MiniMaxProviderRejected)


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
    error = MiniMaxProviderRejected("MiniMax API error (1004): invalid api key")

    with (
        patch("app.services.agent_tools._generate_image_minimax", AsyncMock(side_effect=error)),
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": "_internal/recovery/image.bin"}
                )
            ),
        ) as create_task,
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(),
        ) as mark_failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as mark_ambiguous,
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()),
        patch("app.services.llm.load_balancer.mark_credential_degraded", AsyncMock()) as mark_degraded,
        patch("app.services.llm.load_balancer.mark_credential_quota_exceeded", AsyncMock()) as mark_quota,
    ):
        result = await agent_tools._generate_image_minimax_durable(
            agent_id=agent_id,
            ws=tmp_path,
            arguments={},
            user_id=None,
            session_id="",
            tenant_id=tenant_id,
            credential_id=cred_id,
            api_key="sk-test",
            base_url="https://api.minimax.test",
            model="image-01",
            tier="lite",
            credit_cost=4,
            provider_prompt="cat",
            save_path="workspace/images/cat.png",
            output_extension=".png",
            overlay_text="",
            overlay_position="bottom",
            brand_asset=None,
            brand_position="center",
            brand_scale=0.42,
        )

    assert "❌ Image generation failed (minimax). Provider code: 1004." in result
    assert "invalid api key" not in result
    record_id = create_task.await_args.kwargs["record_id"]
    mark_failed.assert_awaited_once_with(record_id, error)
    mark_ambiguous.assert_not_awaited()
    mark_degraded.assert_awaited_once_with(cred_id, immediate=True)
    mark_quota.assert_not_awaited()


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
    credential = SimpleNamespace(
        id=cred_id,
        api_key="sk-test",
        base_url="https://api.minimax.test",
    )
    generated = _valid_mp3_bytes()

    async def provider(**kwargs):
        await kwargs["on_provider_accepted"](generated)
        return generated

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "speech-2.8-turbo", "voice_id": "v1", "format": "mp3"}),
        ),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("audio", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._get_minimax_tenant_uuid", AsyncMock(return_value=tenant_id)),
        patch(
            "app.services.agent_tools._prepare_minimax_tool_credential",
            AsyncMock(return_value=(credential, None)),
        ),
        patch("app.services.agent_tools._minimax_tts_http", AsyncMock(side_effect=provider)) as provider_call,
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": "_internal/recovery/audio.mp3"}
                )
            ),
        ) as create_task,
        patch("app.services.media_generation.mark_minimax_sync_provider_accepted", AsyncMock()) as mark_accepted,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(),
        ) as store_recovery,
        patch(
            "app.services.media_generation.reconcile_minimax_sync_media_task",
            AsyncMock(return_value=SimpleNamespace(status="succeeded")),
        ) as reconcile,
        patch("app.services.agent_tools._record_minimax_tool_success", AsyncMock()) as record_success,
    ):
        result = await _generate_speech_minimax(
            agent_id,
            tmp_path,
            {"text": "hello world", "save_path": "workspace/audio/hello.mp3"},
        )

    assert "✅ Speech generated" in result
    check_credits.assert_awaited_once_with(tenant_id, 1)
    creation = create_task.await_args.kwargs
    assert creation["tenant_id"] == tenant_id
    assert creation["credential_id"] == cred_id
    assert creation["modality"] == "audio"
    assert creation["tier"] == "pro"
    assert creation["model"] == "speech-2.8-turbo"
    assert creation["credit_cost"] == 1
    assert creation["output_path"].startswith("workspace/audio/hello_")
    record_id = creation["record_id"]
    provider_call.assert_awaited_once()
    store_recovery.assert_awaited_once_with(
        record_id,
        generated,
        content_type="audio/mpeg",
        expected_key="_internal/recovery/audio.mp3",
    )
    mark_accepted.assert_not_awaited()
    reconcile.assert_awaited_once_with(record_id, deliver_completion=False)
    record_success.assert_awaited_once_with(
        agent_id,
        cred_id,
        tier="pro",
        modality="audio",
        model="speech-2.8-turbo",
    )


@pytest.mark.asyncio
async def test_generate_music_minimax_records_success(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=cred_id,
        api_key="sk-test",
        base_url="https://api.minimax.test",
    )
    generated = _valid_mp3_bytes()

    async def provider(**kwargs):
        await kwargs["on_provider_accepted"](generated)
        return generated

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "music-2.6", "format": "mp3"}),
        ),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("music", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._get_minimax_tenant_uuid", AsyncMock(return_value=tenant_id)),
        patch(
            "app.services.agent_tools._prepare_minimax_tool_credential",
            AsyncMock(return_value=(credential, None)),
        ),
        patch("app.services.agent_tools._minimax_music_http", AsyncMock(side_effect=provider)) as provider_call,
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": "_internal/recovery/music.mp3"}
                )
            ),
        ) as create_task,
        patch("app.services.media_generation.mark_minimax_sync_provider_accepted", AsyncMock()) as mark_accepted,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(),
        ) as store_recovery,
        patch(
            "app.services.media_generation.reconcile_minimax_sync_media_task",
            AsyncMock(return_value=SimpleNamespace(status="succeeded")),
        ) as reconcile,
        patch("app.services.agent_tools._record_minimax_tool_success", AsyncMock()) as record_success,
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
    check_credits.assert_awaited_once_with(tenant_id, 150)
    creation = create_task.await_args.kwargs
    assert creation["tenant_id"] == tenant_id
    assert creation["credential_id"] == cred_id
    assert creation["modality"] == "music"
    assert creation["tier"] == "pro"
    assert creation["model"] == "music-2.6"
    assert creation["credit_cost"] == 150
    assert creation["output_path"].startswith("workspace/audio/song_")
    record_id = creation["record_id"]
    provider_call.assert_awaited_once()
    store_recovery.assert_awaited_once_with(
        record_id,
        generated,
        content_type="audio/mpeg",
        expected_key="_internal/recovery/music.mp3",
    )
    mark_accepted.assert_not_awaited()
    reconcile.assert_awaited_once_with(record_id, deliver_completion=False)
    record_success.assert_awaited_once_with(
        agent_id,
        cred_id,
        tier="pro",
        modality="music",
        model="music-2.6",
    )


@pytest.mark.parametrize("modality", ["audio", "music"])
@pytest.mark.parametrize(
    "failure_mode",
    [
        "provider_success_storage_failure",
        "settlement_mark_failure",
        "ambiguous_timeout",
        "deterministic_rejection",
        "pre_request_policy_failure",
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
    credential_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        api_key="sk-test",
        base_url="https://api.minimax.test",
    )

    if modality == "audio":
        save_path = "workspace/audio/hello.mp3"
        recovery_key = "_internal/recovery/audio.mp3"
        provider_bytes = _valid_mp3_bytes()
        credit_cost = 1
        model = "speech-2.8-turbo"
    else:
        save_path = "workspace/audio/song.mp3"
        recovery_key = "_internal/recovery/music.mp3"
        provider_bytes = _valid_mp3_bytes()
        credit_cost = 150
        model = "music-2.6"

    async def provider(on_accepted, on_request_started):
        if failure_mode == "pre_request_policy_failure":
            raise MCPURLPolicyError(
                "MCP endpoint must resolve only to public addresses"
            )
        on_request_started()
        if failure_mode == "ambiguous_timeout":
            raise httpx.ReadTimeout("provider outcome unknown")
        if failure_mode == "deterministic_rejection":
            raise MiniMaxProviderRejected(
                "MiniMax API error (2056): plan exhausted"
            )
        if failure_mode == "settlement_mark_failure":
            await on_accepted(None)
        else:
            await on_accepted(provider_bytes)
        return provider_bytes

    recovery_error = (
        OSError("durable recovery storage unavailable")
        if failure_mode == "provider_success_storage_failure"
        else None
    )
    accepted_error = (
        RuntimeError("provider acceptance database unavailable")
        if failure_mode == "settlement_mark_failure"
        else None
    )
    with (
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={"recovery_asset_storage_key": recovery_key}
                )
            ),
        ) as create_task,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(side_effect=recovery_error),
        ) as store_recovery,
        patch(
            "app.services.media_generation.mark_minimax_sync_provider_accepted",
            AsyncMock(side_effect=accepted_error),
        ) as mark_accepted,
        patch("app.services.media_generation.reconcile_minimax_sync_media_task", AsyncMock()) as reconcile,
        patch(
            "app.services.media_generation.record_minimax_sync_provider_response_retry",
            AsyncMock(),
        ) as recovery_retry,
        patch("app.services.media_generation.mark_media_generation_submission_ambiguous", AsyncMock()) as ambiguous,
        patch("app.services.media_generation.mark_media_generation_submission_failed", AsyncMock()) as failed,
        patch("app.services.agent_tools._record_minimax_tool_product_issue", AsyncMock()) as issue,
        patch("app.services.agent_tools._mark_minimax_tool_credential_failure", AsyncMock()) as mark_credential,
        patch("app.services.agent_tools._record_minimax_tool_success", AsyncMock()) as record_success,
    ):
        result = await agent_tools._generate_minimax_audio_durable(
            agent_id=agent_id,
            user_id=None,
            session_id="",
            tenant_id=tenant_id,
            credential=credential,
            modality=modality,
            tier="pro",
            model=model,
            credit_cost=credit_cost,
            save_path=save_path,
            audio_format="mp3",
            content_type="audio/mpeg",
            sample_rate=32000 if modality == "audio" else 44100,
            provider_call=provider,
        )

    creation = create_task.await_args.kwargs
    record_id = creation["record_id"]
    assert creation["tenant_id"] == tenant_id
    assert creation["modality"] == modality
    assert creation["credit_cost"] == credit_cost
    reconcile.assert_not_awaited()
    record_success.assert_not_awaited()
    issue.assert_awaited_once()
    assert issue.await_args.kwargs["recovery_path"] == f"media-task:{record_id}"
    if failure_mode == "provider_success_storage_failure":
        store_recovery.assert_awaited_once_with(
            record_id,
            provider_bytes,
            content_type="audio/mpeg",
            expected_key=recovery_key,
        )
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_awaited_once()
        ambiguous.assert_not_awaited()
        failed.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert "repairing the acceptance record" in result
    elif failure_mode == "settlement_mark_failure":
        store_recovery.assert_not_awaited()
        mark_accepted.assert_awaited_once()
        recovery_retry.assert_awaited_once()
        ambiguous.assert_not_awaited()
        failed.assert_not_awaited()
        mark_credential.assert_not_awaited()
        assert "repairing the acceptance record" in result
    elif failure_mode == "ambiguous_timeout":
        store_recovery.assert_not_awaited()
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_not_awaited()
        ambiguous.assert_awaited_once()
        failed.assert_not_awaited()
        mark_credential.assert_awaited_once()
        assert "submission outcome is uncertain" in result
    elif failure_mode == "deterministic_rejection":
        store_recovery.assert_not_awaited()
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_not_awaited()
        ambiguous.assert_not_awaited()
        failed.assert_awaited_once()
        mark_credential.assert_awaited_once()
        assert "Provider code: 2056" in result
    else:
        store_recovery.assert_not_awaited()
        mark_accepted.assert_not_awaited()
        recovery_retry.assert_not_awaited()
        ambiguous.assert_not_awaited()
        failed.assert_awaited_once()
        mark_credential.assert_awaited_once()
        assert "remain held" not in result
        assert "submission outcome is uncertain" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("modality", ["audio", "music"])
@pytest.mark.parametrize("cancel_stage", ["pre_request", "request_started", "provider_accepted"])
async def test_minimax_audio_cancellation_persists_safe_credit_state(
    modality,
    cancel_stage,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=uuid.uuid4(),
        api_key="sk-test",
        base_url="https://api.minimax.test",
    )
    provider_bytes = _valid_mp3_bytes()

    async def provider(on_accepted, on_request_started):
        if cancel_stage != "pre_request":
            on_request_started()
        if cancel_stage == "provider_accepted":
            await on_accepted(provider_bytes)
        raise asyncio.CancelledError()

    with (
        patch(
            "app.services.media_generation.create_minimax_sync_media_task_record",
            AsyncMock(
                return_value=SimpleNamespace(
                    request_metadata={
                        "recovery_asset_storage_key": "_internal/recovery/audio.mp3"
                    }
                )
            ),
        ) as create_task,
        patch(
            "app.services.media_generation.store_minimax_sync_recovery_asset",
            AsyncMock(),
        ) as store_recovery,
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(return_value=True),
        ) as failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as ambiguous,
        patch(
            "app.services.media_generation.record_minimax_sync_provider_response_retry",
            AsyncMock(),
        ) as response_retry,
    ):
        with pytest.raises(asyncio.CancelledError):
            await agent_tools._generate_minimax_audio_durable(
                agent_id=agent_id,
                user_id=None,
                session_id="",
                tenant_id=tenant_id,
                credential=credential,
                modality=modality,
                tier="pro",
                model="speech-2.8-turbo" if modality == "audio" else "music-2.6",
                credit_cost=1 if modality == "audio" else 150,
                save_path=f"workspace/audio/{modality}.mp3",
                audio_format="mp3",
                content_type="audio/mpeg",
                sample_rate=32000,
                provider_call=provider,
            )

    record_id = create_task.await_args.kwargs["record_id"]
    response_retry.assert_not_awaited()
    if cancel_stage == "pre_request":
        failed.assert_awaited_once_with(record_id, ANY)
        ambiguous.assert_not_awaited()
        store_recovery.assert_not_awaited()
    elif cancel_stage == "request_started":
        failed.assert_not_awaited()
        ambiguous.assert_awaited_once_with(record_id, ANY)
        store_recovery.assert_not_awaited()
    else:
        failed.assert_not_awaited()
        ambiguous.assert_not_awaited()
        store_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_video_minimax_creates_task_metadata(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    cred = SimpleNamespace(id=cred_id, base_url=None)
    lifecycle_order: list[str] = []
    reference_path = tmp_path / "workspace/images/product.png"
    reference_path.parent.mkdir(parents=True)
    reference_path.write_bytes(_valid_png_bytes())

    async def register_task(**_kwargs):
        lifecycle_order.append("register")
        return SimpleNamespace(reservation_id=reservation_id)

    async def create_provider_task(**_kwargs):
        _kwargs["on_provider_request_started"]()
        lifecycle_order.append("provider")
        return "task-123"

    async def mark_submitted(*_args, **_kwargs):
        lifecycle_order.append("submitted")
        return _args[0]

    async def store_provider_identity(**_kwargs):
        lifecycle_order.append("evidence")
        return "_internal/provider-recovery.json"

    with (
        patch(
            "app.services.agent_tools._get_tool_config",
            AsyncMock(return_value={"model": "MiniMax-Hailuo-2.3", "duration": 6, "resolution": "1080P"}),
        ),
        patch("app.services.agent_tools._get_minimax_tenant_uuid", AsyncMock(return_value=tenant_id)),
        patch("app.services.agent_tools._resolve_minimax_tool_tier", AsyncMock(return_value="pro")),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("video", "pro")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()) as check_credits,
        patch("app.services.agent_tools._check_minimax_tool_allowed", AsyncMock(return_value=None)),
        patch("app.services.llm.load_balancer.pick_credential", AsyncMock(return_value=cred)),
        patch("app.services.llm.utils.get_credential_api_key", MagicMock(return_value="sk-test")),
        patch("app.services.agent_tools._minimax_create_video_task", AsyncMock(side_effect=create_provider_task)) as create_task,
        patch("app.services.media_generation.create_minimax_video_task_record", AsyncMock(side_effect=register_task)) as register,
        patch(
            "app.services.media_generation.store_minimax_video_provider_identity_evidence",
            AsyncMock(side_effect=store_provider_identity),
        ),
        patch("app.services.media_generation.mark_minimax_video_task_submitted", AsyncMock(side_effect=mark_submitted)),
        patch("app.services.media_generation.validate_media_origin_session", AsyncMock()),
        patch("app.services.agent_tools._record_minimax_tool_success", AsyncMock()) as record_success,
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
    assert lifecycle_order == ["register", "provider", "evidence", "submitted"]
    assert register.await_args.kwargs["tenant_id"] == tenant_id
    assert register.await_args.kwargs["credential_id"] == cred_id
    assert register.await_args.kwargs["tier"] == "pro"
    assert register.await_args.kwargs["credit_cost"] == 280
    assert create_task.await_args.kwargs["first_frame_image"].startswith("data:image/png;base64,")
    assert "automatic" in result.lower()
    record_success.assert_awaited_once_with(
        agent_id,
        cred_id,
        tier="pro",
        modality="video",
        model="MiniMax-Hailuo-2.3",
    )
    check_credits.assert_awaited_once_with(tenant_id, 280)


@pytest.mark.asyncio
async def test_generate_video_public_origin_preflight_failure_releases_hold(tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    credential = SimpleNamespace(id=credential_id, base_url=None)
    error = MCPURLPolicyError(
        "MCP endpoint must resolve only to public addresses"
    )

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.agent_tools._get_minimax_tenant_uuid",
            AsyncMock(return_value=tenant_id),
        ),
        patch(
            "app.services.agent_tools._resolve_minimax_tool_tier",
            AsyncMock(return_value="lite"),
        ),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("video", "lite")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
        patch("app.services.agent_tools._check_minimax_tool_allowed", AsyncMock(return_value=None)),
        patch(
            "app.services.llm.load_balancer.pick_credential",
            AsyncMock(return_value=credential),
        ),
        patch(
            "app.services.llm.utils.get_credential_api_key",
            MagicMock(return_value="sk-test"),
        ),
        patch(
            "app.services.agent_tools._minimax_create_video_task",
            AsyncMock(side_effect=error),
        ),
        patch(
            "app.services.media_generation.create_minimax_video_task_record",
            AsyncMock(return_value=SimpleNamespace(reservation_id=reservation_id)),
        ) as create_task,
        patch(
            "app.services.media_generation.validate_media_origin_session",
            AsyncMock(),
        ),
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(return_value=True),
        ) as failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as ambiguous,
        patch(
            "app.services.agent_tools._release_minimax_tool_reservation",
            AsyncMock(),
        ) as release,
        patch(
            "app.services.agent_tools._mark_minimax_tool_credential_failure",
            AsyncMock(),
        ),
        patch(
            "app.services.agent_tools._record_minimax_tool_product_issue",
            AsyncMock(),
        ),
    ):
        result = await _generate_video_minimax(
            agent_id,
            tmp_path,
            {"prompt": "safe preflight failure", "wait_for_completion": False},
        )

    record_id = create_task.await_args.kwargs["record_id"]
    failed.assert_awaited_once_with(record_id, error)
    ambiguous.assert_not_awaited()
    release.assert_not_awaited()
    assert "retained the Credits hold" not in result
    assert "submission outcome is uncertain" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["pre_request", "request_started", "provider_accepted"])
async def test_minimax_video_cancellation_persists_safe_credit_state(
    tmp_path,
    cancel_stage,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    credential = SimpleNamespace(id=credential_id, base_url=None)
    bind_attempts = 0

    async def provider(**kwargs):
        if cancel_stage != "pre_request":
            kwargs["on_provider_request_started"]()
        if cancel_stage == "provider_accepted":
            return "provider-task-cancelled"
        raise asyncio.CancelledError()

    async def mark_submitted(record_id, **_kwargs):
        nonlocal bind_attempts
        bind_attempts += 1
        if bind_attempts == 1:
            raise asyncio.CancelledError()
        return record_id

    with (
        patch("app.services.agent_tools._get_tool_config", AsyncMock(return_value={})),
        patch(
            "app.services.agent_tools._get_minimax_tenant_uuid",
            AsyncMock(return_value=tenant_id),
        ),
        patch(
            "app.services.agent_tools._resolve_minimax_tool_tier",
            AsyncMock(return_value="lite"),
        ),
        patch(
            "app.services.minimax_media_profiles.load_platform_minimax_media_profile",
            AsyncMock(return_value=resolve_minimax_media_profile("video", "lite")),
        ),
        patch("app.services.agent_tools._check_minimax_credit_amount", AsyncMock()),
        patch("app.services.agent_tools._check_minimax_tool_allowed", AsyncMock(return_value=None)),
        patch(
            "app.services.llm.load_balancer.pick_credential",
            AsyncMock(return_value=credential),
        ),
        patch(
            "app.services.llm.utils.get_credential_api_key",
            MagicMock(return_value="sk-test"),
        ),
        patch(
            "app.services.agent_tools._minimax_create_video_task",
            AsyncMock(side_effect=provider),
        ),
        patch(
            "app.services.media_generation.validate_media_origin_session",
            AsyncMock(),
        ),
        patch(
            "app.services.media_generation.create_minimax_video_task_record",
            AsyncMock(return_value=SimpleNamespace(reservation_id=reservation_id)),
        ) as create_task,
        patch(
            "app.services.media_generation.store_minimax_video_provider_identity_evidence",
            AsyncMock(return_value="_internal/provider-video.json"),
        ) as store_identity,
        patch(
            "app.services.media_generation.mark_minimax_video_task_submitted",
            AsyncMock(side_effect=mark_submitted),
        ) as bind_provider,
        patch(
            "app.services.media_generation.mark_media_generation_submission_failed",
            AsyncMock(return_value=True),
        ) as failed,
        patch(
            "app.services.media_generation.mark_media_generation_submission_ambiguous",
            AsyncMock(),
        ) as ambiguous,
    ):
        with pytest.raises(asyncio.CancelledError):
            await _generate_video_minimax(
                agent_id,
                tmp_path,
                {"prompt": "cancelled video", "wait_for_completion": False},
            )

    record_id = create_task.await_args.kwargs["record_id"]
    if cancel_stage == "pre_request":
        failed.assert_awaited_once_with(record_id, ANY)
        ambiguous.assert_not_awaited()
        bind_provider.assert_not_awaited()
    elif cancel_stage == "request_started":
        failed.assert_not_awaited()
        ambiguous.assert_awaited_once_with(record_id, ANY)
        bind_provider.assert_not_awaited()
    else:
        failed.assert_not_awaited()
        ambiguous.assert_not_awaited()
        assert bind_provider.await_count == 2
        assert store_identity.await_count == 2


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
            AsyncMock(return_value=SimpleNamespace(reservation_id=reservation.id)),
        ),
        patch(
            "app.services.media_generation.mark_minimax_video_task_submitted",
            AsyncMock(side_effect=mark_submitted),
        ) as bind_provider,
        patch(
            "app.services.media_generation.store_minimax_video_provider_identity_evidence",
            AsyncMock(return_value="_internal/provider-recovery.json"),
        ),
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
        patch("app.services.media_generation.find_media_generation_task", AsyncMock(return_value=None)),
        patch("app.services.media_generation.reconcile_minimax_video_task", AsyncMock()) as reconcile,
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
    reconcile.assert_not_awaited()
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
