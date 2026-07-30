"""Provider-acceptance fencing for Credits and stream retries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.llm import caller as llm_caller
from app.services.llm.client import (
    AnthropicClient,
    GeminiClient,
    LLMError,
    LLMMessage,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
    llm_provider_may_have_accepted,
)


class _InterruptedSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("stream disconnected")

    async def aclose(self) -> None:
        return None


class _SSESequence(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stream_read_failure_after_success_response_is_never_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_InterruptedSSE(), request=request)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="interrupted after request start"):
            await client.stream(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert calls == 1
    assert client.provider_response_started is True
    assert llm_provider_may_have_accepted(client) is True


@pytest.mark.asyncio
async def test_stream_connect_failure_retries_without_retaining_provider_hold(monkeypatch):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.llm.client.asyncio.sleep", AsyncMock())

    try:
        with pytest.raises(LLMError, match="after 3 attempts"):
            await client.stream(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert calls == 3
    assert client.provider_response_started is False
    assert llm_provider_may_have_accepted(client) is False


@pytest.mark.asyncio
async def test_complete_cancellation_after_network_start_keeps_provider_hold():
    entered_provider = asyncio.Event()
    never_finish = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered_provider.set()
        await never_finish.wait()
        return httpx.Response(200, json={"choices": []}, request=request)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        client.complete(messages=[LLMMessage(role="user", content="hello")])
    )

    await entered_provider.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()

    assert client.provider_response_started is False
    assert llm_provider_may_have_accepted(client) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "provider_may_have_accepted"),
    [(429, False), (408, True), (500, True)],
)
async def test_http_failures_release_only_deterministic_rejections(
    status_code,
    provider_may_have_accepted,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="provider error", request=request)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match=f"HTTP {status_code}"):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is provider_may_have_accepted


@pytest.mark.asyncio
async def test_http_failure_preserves_privacy_safe_provider_evidence():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit", "message": "busy"}},
            headers={"x-request-id": "provider-request-7", "retry-after": "3"},
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError) as raised:
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert raised.value.http_status == 429
    assert raised.value.provider_code == "rate_limit"
    assert raised.value.provider_trace_id == "provider-request-7"
    assert raised.value.retry_after_seconds == 3.0


@pytest.mark.asyncio
async def test_minimax_business_error_in_http_200_releases_provider_hold():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "base_resp": {
                    "status_code": 2056,
                    "status_msg": "quota exhausted",
                },
            },
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="code=2056"):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert client.provider_response_started is False
    assert client.provider_output_started is False
    assert llm_provider_may_have_accepted(client) is False


@pytest.mark.asyncio
async def test_minimax_2062_high_traffic_rejection_releases_provider_hold():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "base_resp": {
                    "status_code": 2062,
                    "status_msg": "Token Plan traffic is high",
                },
            },
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="code=2062"):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("output_before_error", [False, True])
async def test_transient_stream_business_error_always_retains_provider_hold(
    output_before_error,
):
    chunks = []
    if output_before_error:
        chunks.append(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
    chunks.append(
        b'data: {"base_resp":{"status_code":1000,"status_msg":"transient"}}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SSESequence(*chunks), request=request)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="code=1000"):
            await client.stream(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert client.provider_output_started is output_before_error
    assert llm_provider_may_have_accepted(client) is True


@pytest.mark.asyncio
async def test_deterministic_stream_quota_rejection_releases_provider_hold():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_SSESequence(
                b'data: {"base_resp":{"status_code":2056,"status_msg":"quota"}}\n\n'
            ),
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="code=2056"):
            await client.stream(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is False


@pytest.mark.asyncio
async def test_transient_nonstream_business_error_retains_provider_hold():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "base_resp": {"status_code": 1001, "status_msg": "timeout"},
            },
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="code=1001"):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is True


@pytest.mark.asyncio
async def test_minimax_nonstream_reasoning_details_are_preserved_for_tool_replay():
    reasoning_details = [
        {"type": "reasoning.text", "text": "inspect the workbook", "index": 0}
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "reasoning_details": reasoning_details,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }],
                    },
                }],
                "usage": {"total_tokens": 10},
            },
            request=request,
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        response = await client.complete(
            messages=[LLMMessage(role="user", content="make a report")]
        )
    finally:
        await client.close()

    replayed = LLMMessage(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
        reasoning_details=response.reasoning_details,
    ).to_openai_format()
    assert response.reasoning_details == reasoning_details
    assert replayed["reasoning_details"] == reasoning_details
    assert replayed["tool_calls"] == response.tool_calls


@pytest.mark.asyncio
async def test_minimax_stream_reasoning_snapshots_are_aggregated_without_duplication():
    chunks = (
        b'data: {"choices":[{"delta":{"reasoning_details":'
        b'[{"type":"reasoning.text","text":"inspect","index":0}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_details":'
        b'[{"type":"reasoning.text","text":"inspect then act","index":0}],'
        b'"tool_calls":[{"index":0,"id":"call_1","function":'
        b'{"name":"read_file","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SSESequence(*chunks), request=request)

    thinking_parts: list[str] = []

    async def on_thinking(text: str) -> None:
        thinking_parts.append(text)

    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="MiniMax-M3",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        response = await client.stream(
            messages=[LLMMessage(role="user", content="make a report")],
            on_thinking=on_thinking,
        )
    finally:
        await client.close()

    assert response.reasoning_content == "inspect then act"
    assert response.reasoning_details == [
        {"type": "reasoning.text", "text": "inspect then act", "index": 0}
    ]
    assert "".join(thinking_parts) == "inspect then act"
    assert response.tool_calls[0]["id"] == "call_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_class", "payload"),
    [
        (GeminiClient, {"error": {"code": 400, "message": "bad request"}}),
        (AnthropicClient, {"type": "error", "error": {"type": "invalid_request"}}),
    ],
)
async def test_native_provider_explicit_business_error_releases_hold(
    client_class,
    payload,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = client_class(
        api_key="test-key",
        base_url="https://provider.test",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError, match="API error"):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "provider_may_have_accepted"),
    [
        (
            {"status": "failed", "error": {"message": "rejected"}, "output": []},
            False,
        ),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "timeout"}},
            True,
        ),
    ],
)
async def test_responses_api_releases_only_deterministic_failed_payloads(
    payload,
    provider_may_have_accepted,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = OpenAIResponsesClient(
        api_key="test-key",
        base_url="https://provider.test/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with pytest.raises(LLMError):
            await client.complete(messages=[LLMMessage(role="user", content="hello")])
    finally:
        await client.close()

    assert llm_provider_may_have_accepted(client) is provider_may_have_accepted


@pytest.mark.parametrize(
    "result",
    [
        "[Error] Too many tool call rounds",
        "[Error] Tool execution failed: RuntimeError",
        "[Error] Credits settlement failed",
    ],
)
def test_local_runtime_errors_never_trigger_model_failover(result):
    assert llm_caller.is_retryable_error(result) is False


def test_client_creation_failure_can_still_use_fallback():
    assert llm_caller.is_retryable_error(
        "[Error] Failed to create LLM client: connection unavailable"
    ) is True


def test_credential_pool_miss_is_retryable_but_user_facing_message_is_clean():
    error = llm_caller.NoCredentialAvailable(
        "volcengine_agent_plan",
        "text",
        reason_code=llm_caller.CredentialUnavailableReason.CAPABILITY_MISMATCH,
    )
    result = llm_caller._credential_unavailable_result(error)

    assert llm_caller.is_retryable_error(result) is True
    assert llm_caller._user_facing_llm_error_result(result) == (
        "⚠️ 平台尚未配置当前功能所需的模型能力，请联系管理员。"
    )
    assert "credential unavailable before provider request" not in (
        llm_caller._user_facing_llm_error_result(result)
    )


@pytest.mark.asyncio
async def test_credential_pool_miss_uses_configured_fallback(monkeypatch):
    primary = SimpleNamespace(
        provider="volcengine_agent_plan",
        model="doubao-seed-2.1-turbo",
    )
    fallback = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        supports_vision=False,
    )
    calls = []

    async def fake_call(model, *_args, **_kwargs):
        calls.append(model)
        if model is primary:
            error = llm_caller.NoCredentialAvailable(
                "volcengine_agent_plan",
                "text",
                reason_code=llm_caller.CredentialUnavailableReason.CAPABILITY_MISMATCH,
            )
            return llm_caller._credential_unavailable_result(error)
        return "fallback result"

    monkeypatch.setattr(llm_caller, "call_llm", fake_call)

    result = await llm_caller.call_llm_with_failover(
        primary,
        fallback,
        messages=[],
        agent_name="agent",
        role_description="worker",
    )

    assert result == "fallback result"
    assert calls == [primary, fallback]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_state", ["ambiguous", "work_started"])
async def test_failover_is_blocked_when_primary_provider_cannot_be_safely_replayed(
    monkeypatch,
    guard_state,
):
    primary = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    fallback = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        supports_vision=False,
    )
    calls = []

    async def fake_call(model, *_args, failover_guard=None, **_kwargs):
        calls.append(model)
        if model is primary:
            if guard_state == "ambiguous":
                failover_guard.mark_provider_outcome_ambiguous()
            else:
                failover_guard.mark_provider_work_started()
            return "[LLM call error] timeout"
        return "fallback result"

    monkeypatch.setattr(llm_caller, "call_llm", fake_call)

    result = await llm_caller.call_llm_with_failover(
        primary,
        fallback,
        messages=[],
        agent_name="agent",
        role_description="worker",
    )

    assert result == "[LLM call error] timeout"
    assert calls == [primary]


@pytest.mark.asyncio
async def test_failover_remains_available_after_deterministic_connection_rejection(
    monkeypatch,
):
    primary = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    fallback = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        supports_vision=False,
    )
    calls = []

    async def fake_call(model, *_args, failover_guard=None, **_kwargs):
        calls.append(model)
        if model is primary:
            return "[LLM call error] connection refused"
        return "fallback result"

    monkeypatch.setattr(llm_caller, "call_llm", fake_call)

    result = await llm_caller.call_llm_with_failover(
        primary,
        fallback,
        messages=[],
        agent_name="agent",
        role_description="worker",
    )

    assert result == "fallback result"
    assert calls == [primary, fallback]


@pytest.mark.asyncio
async def test_gemini_openai_fallback_exposes_provider_acceptance_state():
    client = GeminiClient(
        api_key="test-key",
        base_url="https://provider.test/v1/openai",
        model="gemini-compatible",
    )
    fallback = await client._get_openai_fallback_client()
    fallback.provider_request_started = True

    try:
        assert llm_provider_may_have_accepted(client) is True
    finally:
        await client.close()
