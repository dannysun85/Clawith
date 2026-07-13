"""Tests for the Douyin official OpenAPI Agent integration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace

import httpx
import pytest

from app.api import douyin as douyin_api
from app.models.douyin import DouyinPublishJob
from app.services.douyin.client import DouyinOpenAPIClient
from app.services.douyin.errors import DouyinAuthError, DouyinNotConfiguredError
from app.services.douyin.operations import douyin_operations_service
from app.services.douyin.policy import capability_status, has_capability


class DummyResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return [] if self.value is None else [self.value]


class RecordingDB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement, _params=None):
        if not self.responses:
            return DummyResult()
        value = self.responses.pop(0)
        return value if isinstance(value, DummyResult) else DummyResult(value)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        for value in self.added:
            if hasattr(value, "id") and getattr(value, "id", None) is None:
                setattr(value, "id", uuid.uuid4())


def test_douyin_webhook_signature_is_fail_closed(monkeypatch):
    raw_body = b'{"event":"create_video"}'
    monkeypatch.setattr(douyin_api.settings, "DOUYIN_CLIENT_SECRET", "client-secret")
    signature = hashlib.sha1(b"client-secret" + raw_body).hexdigest()

    assert douyin_api._verify_douyin_webhook_signature(raw_body, signature) is True
    assert douyin_api._verify_douyin_webhook_signature(raw_body, "wrong") is False
    assert douyin_api._verify_douyin_webhook_signature(raw_body, None) is False

    monkeypatch.setattr(douyin_api.settings, "DOUYIN_CLIENT_SECRET", "")
    assert douyin_api._verify_douyin_webhook_signature(raw_body, signature) is False


@pytest.mark.asyncio
async def test_douyin_webhook_message_claim_is_atomic(monkeypatch):
    calls = []

    class FakeRedis:
        async def set(self, key, value, *, ex, nx):
            calls.append((key, value, ex, nx))
            return len(calls) == 1

        async def delete(self, key):
            calls.append(("delete", key))

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(douyin_api, "get_redis", fake_get_redis)

    assert await douyin_api._claim_douyin_webhook_message("msg-1") is True
    assert await douyin_api._claim_douyin_webhook_message("msg-1") is False
    assert await douyin_api._claim_douyin_webhook_message(None) is False
    assert calls[0] == ("douyin:webhook:msg:msg-1", "1", 86400, True)

    await douyin_api._release_douyin_webhook_message("msg-1")
    assert calls[-1] == ("delete", "douyin:webhook:msg:msg-1")


@pytest.mark.asyncio
async def test_douyin_client_requires_official_credentials():
    client = DouyinOpenAPIClient(client_key="", client_secret="")

    with pytest.raises(DouyinNotConfiguredError):
        await client.exchange_code("oauth-code")


@pytest.mark.asyncio
async def test_douyin_client_exchange_code_normalizes_token_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/access_token/"
        assert b"client_key=client-key" in request.content
        assert b"client_secret=client-secret" in request.content
        return httpx.Response(
            200,
            json={
                "data": {
                    "error_code": 0,
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "open_id": "open-id",
                    "scope": "video.create,data.external.item",
                    "expires_in": 7200,
                    "refresh_expires_in": 86400,
                },
                "extra": {"log_id": "log-1"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.douyin.com") as http:
        client = DouyinOpenAPIClient(client=http, client_key="client-key", client_secret="client-secret")
        payload = await client.exchange_code("oauth-code")

    assert payload["access_token"] == "access-token"
    assert payload["refresh_token"] == "refresh-token"
    assert payload["open_id"] == "open-id"
    assert payload["scope"] == ["video.create", "data.external.item"]
    assert payload["access_token_expires_at"] > datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_douyin_client_maps_official_auth_error():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {"error_code": 10010, "description": "invalid token"},
                "extra": {"log_id": "official-log"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.douyin.com") as http:
        client = DouyinOpenAPIClient(client=http, client_key="client-key", client_secret="client-secret")
        with pytest.raises(DouyinAuthError) as exc:
            await client.exchange_code("bad-code")

    assert exc.value.code == "10010"
    assert exc.value.log_id == "official-log"


def test_douyin_capability_status_uses_any_approved_scope_in_group():
    rows = {row["key"]: row for row in capability_status(["h5.share", "video.create", "video.comment"])}

    assert rows["collaborative_publish"]["status"] == "ready"
    assert rows["direct_publish"]["status"] == "ready"
    assert rows["comment_manage"]["status"] == "ready"
    assert rows["data_read"]["status"] == "missing"
    assert has_capability(["h5.share"], "collaborative_publish") is True
    assert has_capability([], "collaborative_publish") is False


@pytest.mark.asyncio
async def test_douyin_client_gets_client_token_and_share_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/client_token/":
            assert request.headers["content-type"].startswith("application/json")
            return httpx.Response(200, json={"data": {"error_code": 0, "access_token": "client-token", "expires_in": 7200}})
        if request.url.path == "/share-id/":
            assert request.headers["access-token"] == "client-token"
            assert request.url.params.get("need_callback") == "true"
            return httpx.Response(
                200,
                json={"data": {"error_code": 0, "share_id": "share-123"}, "extra": {"error_code": 0, "logid": "log-share"}},
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.douyin.com") as http:
        client = DouyinOpenAPIClient(client=http, client_key="client-key", client_secret="client-secret")
        token = await client.get_client_token()
        share = await client.create_share_id(token["client_token"])

    assert token["client_token"] == "client-token"
    assert share["share_id"] == "share-123"
    assert share["official_log_id"] == "log-share"


def test_douyin_h5_share_schema_is_server_signed():
    job = SimpleNamespace(
        title="新品视频",
        hashtags=["新品", "#门店"],
        visibility="public_after_review",
    )

    payload = douyin_operations_service._build_h5_share_schema(
        job=job,
        ticket="ticket-value",
        share_id="share-123",
        media_payload={"video_path": "https://cdn.example.com/video.mp4"},
        nonce_str="nonce-value",
        timestamp="1700000000",
    )

    query = parse_qs(urlparse(payload["schema_url"]).query)
    expected = hashlib.md5(b"nonce_str=nonce-value&ticket=ticket-value&timestamp=1700000000").hexdigest()
    assert query["share_type"] == ["h5"]
    assert query["state"] == ["share-123"]
    assert query["video_path"] == ["https://cdn.example.com/video.mp4"]
    assert query["signature"] == [expected]


@pytest.mark.asyncio
async def test_douyin_publish_job_is_approval_first_and_persists_schedule():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    db = RecordingDB(
        responses=[
            agent,  # _assert_agent_in_tenant
            None,  # _get_existing_publish_job
            None,  # _get_or_first_account
        ]
    )

    job = await douyin_operations_service.create_publish_job(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        account_id=None,
        content_type="video",
        title="新品短视频",
        body="审批前不会自动发布",
        hashtags=["新品"],
        visibility="public_after_review",
        asset_refs=[{"official_video_id": "video-123"}],
        scheduled_at=scheduled_at,
        idempotency_key="stable-key",
    )

    assert isinstance(job, DouyinPublishJob)
    assert job.status == "approval_required"
    assert job.approval_status == "pending"
    assert job.scheduled_at == scheduled_at
    assert job.redacted_request_summary["asset_count"] == 1
    assert any(getattr(item, "action_type", None) == "douyin_publish_job" for item in db.added)


@pytest.mark.asyncio
async def test_douyin_run_publish_job_prepares_h5_user_confirm_package():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    job = DouyinPublishJob(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        created_by=uuid.uuid4(),
        approval_id=approval_id,
        content_type="video",
        title="新品短视频",
        body="请在抖音端确认发布",
        hashtags=["新品"],
        visibility="public_after_review",
        asset_refs=[{"video_path": "https://cdn.example.com/video.mp4"}],
        idempotency_key="stable-key",
        approval_status="pending",
        status="approval_required",
        redacted_request_summary={"title": "新品短视频"},
        response_summary={},
    )
    approval = SimpleNamespace(id=approval_id, status="approved", details={})
    account = SimpleNamespace(id=account_id, scopes=["h5.share", "open.get.ticket", "aweme.share"], status="active")
    db = RecordingDB(responses=[job, approval, account])

    class FakeClient:
        async def get_client_token(self):
            return {"client_token": "client-token", "expires_in": 7200}

        async def get_open_ticket(self, _client_token):
            return {"ticket": "ticket-value", "expires_in": 7200}

        async def create_share_id(self, _client_token, *, need_callback=True, default_hashtag=None):
            assert need_callback is True
            assert default_hashtag == "新品"
            return {"share_id": "share-123", "official_log_id": "log-share", "expires_in": 3600}

    result = await douyin_operations_service.run_publish_job(db, job_id=job.id, client=FakeClient())

    assert result.status == "awaiting_user_publish"
    assert result.publish_mode == "collaborative_h5"
    assert result.share_id == "share-123"
    assert result.share_schema_url.startswith("snssdk1128://openplatform/share?")
    assert "用户" in result.response_summary["message"]
