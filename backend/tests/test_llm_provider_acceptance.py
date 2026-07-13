"""Provider-acceptance fencing for Credits and stream retries."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.llm.client import (
    GeminiClient,
    LLMError,
    LLMMessage,
    OpenAICompatibleClient,
    llm_provider_may_have_accepted,
)


class _InterruptedSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("stream disconnected")

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
