import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api import feishu as feishu_api
from app.services import feishu_service as feishu_service_module


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, *, headers: dict | None = None, chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAsyncClient:
    def __init__(
        self,
        *,
        send_payload: dict | None = None,
        patch_payload: dict | None = None,
        download_response: _FakeResponse | None = None,
    ):
        self._send_payload = send_payload or {"code": 0, "msg": "ok", "data": {"message_id": "m_1"}}
        self._patch_payload = patch_payload or {"code": 0, "msg": "ok"}
        self._download_response = download_response or _FakeResponse(200, {}, chunks=[b"file"])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **_kwargs):
        if "app_access_token/internal" in url:
            return _FakeResponse(200, {"app_access_token": "token_x"})
        return _FakeResponse(200, self._send_payload)

    async def patch(self, _url, **_kwargs):
        return _FakeResponse(200, self._patch_payload)

    def stream(self, _method, _url, **_kwargs):
        return _FakeStreamContext(self._download_response)


@pytest.mark.asyncio
async def test_send_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(send_payload={"code": 99991663, "msg": "rate limited"}),
    )

    with pytest.raises(RuntimeError, match="code=99991663"):
        await feishu_service_module.feishu_service.send_message(
            "app_id",
            "app_secret",
            "ou_xxx",
            "text",
            "{\"text\":\"hello\"}",
            stage="unit_test_send",
        )


@pytest.mark.asyncio
async def test_patch_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(patch_payload={"code": 10019, "msg": "invalid card content"}),
    )

    with pytest.raises(RuntimeError, match="code=10019"):
        await feishu_service_module.feishu_service.patch_message(
            "app_id",
            "app_secret",
            "om_xxx",
            "{\"content\":\"test\"}",
            stage="unit_test_patch",
        )


@pytest.mark.asyncio
async def test_download_message_resource_stops_at_declared_size_limit(monkeypatch):
    response = _FakeResponse(200, {}, headers={"content-length": "4"}, chunks=[b"data"])
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(download_response=response),
    )

    with pytest.raises(feishu_service_module.FeishuResourceTooLargeError):
        await feishu_service_module.feishu_service.download_message_resource(
            "app_id",
            "app_secret",
            "message_id",
            "file_key",
            max_bytes=3,
        )


@pytest.mark.asyncio
async def test_download_message_resource_stops_when_stream_exceeds_limit(monkeypatch):
    response = _FakeResponse(200, {}, chunks=[b"12", b"34"])
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(download_response=response),
    )

    with pytest.raises(feishu_service_module.FeishuResourceTooLargeError):
        await feishu_service_module.feishu_service.download_message_resource(
            "app_id",
            "app_secret",
            "message_id",
            "file_key",
            max_bytes=3,
        )


@pytest.mark.asyncio
async def test_download_message_resource_returns_bounded_stream(monkeypatch):
    response = _FakeResponse(200, {}, chunks=[b"12", b"34"])
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(download_response=response),
    )

    content = await feishu_service_module.feishu_service.download_message_resource(
        "app_id",
        "app_secret",
        "message_id",
        "file_key",
        max_bytes=4,
    )

    assert content == b"1234"


@pytest.mark.asyncio
async def test_feishu_timeout_cancels_unified_failover_once_without_replay(monkeypatch):
    calls: list[dict] = []
    cancelled = asyncio.Event()

    async def fake_unified_failover(**kwargs):
        calls.append(kwargs)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_unified_failover,
    )

    primary = SimpleNamespace(
        model="MiniMax-M3",
        request_timeout=0.01,
        supports_vision=True,
    )
    fallback = SimpleNamespace(
        model="MiniMax-M3-highspeed",
        request_timeout=0.01,
        supports_vision=True,
    )
    agent = SimpleNamespace(
        name="Channel agent",
        role_description="assistant",
        context_window_size=20,
        is_expired=False,
        expires_at=None,
    )
    route_meta = SimpleNamespace(saas_tier="pro", modality="text")

    result = await feishu_api._call_llm_with_config(
        agent,
        primary,
        fallback,
        route_meta,
        uuid.uuid4(),
        "hello",
    )

    assert "timed out" in result
    assert cancelled.is_set()
    assert len(calls) == 1
    assert calls[0]["primary_model"] is primary
    assert calls[0]["fallback_model"] is fallback
    assert calls[0]["route_meta"] is route_meta
    assert calls[0]["messages"] == [{"role": "user", "content": "hello"}]
