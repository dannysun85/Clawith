import hashlib
import hmac
import uuid
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from types import SimpleNamespace
import httpx

from app.api import webhooks as webhooks_api
from app.main import app


VALID_TOKEN = "a" * 32
PRIVATE_TOKEN = "b" * 32
UNKNOWN_TOKEN = "z" * 32


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class FakeSession:
    def __init__(self, triggers=None, agent=None):
        self.triggers = triggers or []
        self.agent = agent
        self.added = []
        self.committed = False
        self.expunged = []
        self.execute_calls = 0

    async def execute(self, statement):
        self.execute_calls += 1
        stmt_str = str(statement)
        if "agent_triggers" in stmt_str:
            return FakeScalarResult(self.triggers)
        elif "agents" in stmt_str:
            return FakeScalarResult(self.agent)
        return FakeScalarResult(None)

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)

    async def commit(self):
        self.committed = True


class FakeAsyncSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


@pytest.mark.asyncio
async def test_receive_webhook_success(monkeypatch, client):
    monkeypatch.setattr(webhooks_api, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    # Setup test trigger and agent
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test-trigger",
        type="webhook",
        config={"token": VALID_TOKEN, "secret": "webhook-secret"},
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)

    session = FakeSession(triggers=[trigger], agent=agent)

    # Mock dependencies and DB session
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))

    # Mock redis rate limiting
    async def fake_record_and_count_hits(token):
        return 1

    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)

    # Mock enqueue_webhook_execution
    async def fake_enqueue_webhook_execution(db, trigger, body, payload_text, payload_obj, request_headers):
        return SimpleNamespace(id=uuid.uuid4()), True

    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", fake_enqueue_webhook_execution)

    body = b'{"event":"test"}'
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    async with await client() as ac:
        response = await ac.post(
            f"/api/webhooks/t/{VALID_TOKEN}",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": signature,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert trigger in session.expunged
    assert agent in session.expunged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "headers", "expected_status"),
    [
        ({"token": VALID_TOKEN, "secret": "webhook-secret"}, {}, 401),
        ({"token": VALID_TOKEN, "secret": "webhook-secret"}, {"x-hub-signature-256": "sha256=bad"}, 401),
        ({"token": VALID_TOKEN}, {}, 403),
    ],
)
async def test_receive_webhook_fails_closed_without_valid_signature(
    monkeypatch,
    client,
    config,
    headers,
    expected_status,
):
    monkeypatch.setattr(webhooks_api, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test-trigger",
        type="webhook",
        config=config,
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)
    session = FakeSession(triggers=[trigger], agent=agent)
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))

    async def fake_record_and_count_hits(_token):
        return 1

    enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)
    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", enqueue)

    async with await client() as ac:
        response = await ac.post(
            f"/api/webhooks/t/{VALID_TOKEN}",
            content=b'{"event":"test"}',
            headers={"content-type": "application/json", **headers},
        )

    assert response.status_code == expected_status
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited_webhook_keeps_user_visible_trigger_context_without_token(
    monkeypatch,
    client,
):
    monkeypatch.setattr(webhooks_api, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="customer-visible-trigger",
        type="webhook",
        config={"token": PRIVATE_TOKEN, "secret": "webhook-secret"},
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)
    session = FakeSession(triggers=[trigger], agent=agent)
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))

    async def fake_record_and_count_hits(_token):
        return 6

    enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)
    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", enqueue)

    async with await client() as ac:
        response = await ac.post(
            f"/api/webhooks/t/{PRIVATE_TOKEN}",
            content=b'{}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 429
    audit = session.added[0]
    assert audit.action == "webhook_rate_limited"
    assert audit.details == {
        "trigger_id": str(trigger.id),
        "trigger_name": "customer-visible-trigger",
        "limit": 5,
    }
    assert "token" not in audit.details
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["short", "!" * 32, UNKNOWN_TOKEN])
async def test_unknown_webhook_tokens_never_allocate_rate_limit_keys(
    monkeypatch,
    client,
    token,
):
    monkeypatch.setattr(webhooks_api, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    session = FakeSession(triggers=[])
    monkeypatch.setattr(webhooks_api, "async_session", FakeAsyncSessionFactory(session))
    rate_limit = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", rate_limit)
    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", enqueue)

    async with await client() as ac:
        response = await ac.post(f"/api/webhooks/t/{token}", content=b"{}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    rate_limit.assert_not_awaited()
    enqueue.assert_not_awaited()
    assert session.execute_calls == (1 if token == UNKNOWN_TOKEN else 0)


@pytest.mark.asyncio
async def test_maintenance_paused_webhook_rejects_before_side_effects(
    monkeypatch,
    client,
):
    rate_limit = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(webhooks_api, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", False)
    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", rate_limit)
    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", enqueue)

    async with await client() as ac:
        response = await ac.post(
            "/api/webhooks/t/opaque_token",
            content=b'{"event":"must-not-queue"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "3600"
    assert response.json() == {
        "ok": False,
        "error": "automatic trigger execution is temporarily paused",
    }
    rate_limit.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_frontend_proxy_bounds_public_webhook_ingress():
    root = Path(__file__).parents[2]
    template = (root / "frontend/nginx.conf.template").read_text(encoding="utf-8")
    production_template = (root / "deploy/nginx/nginx.conf").read_text(encoding="utf-8")
    zone = (root / "frontend/webhook-rate-limit.conf").read_text(encoding="utf-8")
    dockerfile = (root / "frontend/Dockerfile").read_text(encoding="utf-8")

    assert "limit_req_zone $binary_remote_addr zone=webhook_ingress:10m" in zone
    webhook_location = 'location ~ "^/api/webhooks/t/[A-Za-z0-9_-]{32}$"'
    assert webhook_location in template
    assert webhook_location in production_template
    assert "limit_req zone=webhook_ingress burst=20 nodelay" in template
    assert "client_max_body_size 64k" in template
    assert "webhook-rate-limit.conf" in dockerfile
