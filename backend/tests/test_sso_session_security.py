"""Security and lifecycle contracts for temporary SSO relay sessions."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException, Request, Response
import pytest

from app.api import sso as sso_api
from app.models.identity import SSOScanSession
from app.services import sso_scan_session_service as session_service


class _Redis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.expirations: list[tuple[str, int]] = []

    async def eval(self, _script: str, numkeys: int, *values) -> int:
        keys = values[:numkeys]
        client_limit, tenant_limit, global_limit, ttl = map(
            int,
            values[numkeys:],
        )
        limits = (client_limit, tenant_limit, global_limit)
        for index, (key, limit) in enumerate(zip(keys, limits), start=1):
            if self.counts.get(key, 0) >= limit:
                return index
        for key in keys:
            self.counts[key] = self.counts.get(key, 0) + 1
            if self.counts[key] == 1:
                self.expirations.append((key, ttl))
        return 0


class _RowCountResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _CleanupDB:
    def __init__(self, rowcounts: list[int]):
        self.rowcounts = list(rowcounts)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _RowCountResult(self.rowcounts.pop(0))


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SessionDB:
    def __init__(self, session):
        self.session = session
        self.flush_count = 0

    async def execute(self, _statement):
        return _ScalarResult(self.session)

    async def flush(self):
        self.flush_count += 1


class _CreateSessionDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1


class _ProviderRows:
    def all(self):
        return [("google_workspace", "Google Workspace")]


class _SettingResult:
    def scalar_one_or_none(self):
        return SimpleNamespace(value={"enabled": True})


class _ProviderMetadataDB:
    def __init__(self):
        self.tenant = SimpleNamespace(is_active=True, sso_enabled=True)
        self.executions = 0

    async def get(self, _model, _tenant_id):
        return self.tenant

    async def execute(self, _statement):
        self.executions += 1
        if self.executions == 1:
            return _SettingResult()
        return _ProviderRows()


def _request(
    ip: str = "203.0.113.10",
    *,
    peer: str = "127.0.0.1",
    forwarded: str | None = None,
):
    headers = {"x-real-ip": ip}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=peer),
    )


def _http_request(
    *,
    scheme: str = "https",
    cookie: str = "",
    path: str = "/api/sso/session",
) -> Request:
    headers = [(b"host", b"astra.example")]
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.10", 443),
            "server": ("astra.example", 443),
        }
    )


@pytest.mark.asyncio
async def test_anonymous_sso_session_creation_is_rate_limited_without_raw_ip_keys():
    redis = _Redis()
    tenant_id = uuid.uuid4()

    with patch("app.core.events.get_redis", new=AsyncMock(return_value=redis)):
        for _ in range(30):
            await sso_api._enforce_sso_session_creation_rate_limit(
                _request(),
                tenant_id,
            )
        with pytest.raises(HTTPException) as exc:
            await sso_api._enforce_sso_session_creation_rate_limit(
                _request(),
                tenant_id,
            )

    assert exc.value.status_code == 429
    assert all("203.0.113.10" not in key for key in redis.counts)
    assert redis.expirations


@pytest.mark.asyncio
async def test_rejected_client_cannot_exhaust_tenant_or_global_sso_quota():
    redis = _Redis()
    tenant_id = uuid.uuid4()
    rejected = 0

    with patch("app.core.events.get_redis", new=AsyncMock(return_value=redis)):
        for _ in range(300):
            try:
                await sso_api._enforce_sso_session_creation_rate_limit(
                    _request(),
                    tenant_id,
                )
            except HTTPException as exc:
                assert exc.status_code == 429
                rejected += 1

        await sso_api._enforce_sso_session_creation_rate_limit(
            _request(ip="203.0.113.11"),
            tenant_id,
        )

    assert rejected == 270
    tenant_counts = [
        count for key, count in redis.counts.items() if ":tenant:" in key
    ]
    global_counts = [
        count for key, count in redis.counts.items() if ":global:" in key
    ]
    client_counts = [
        count for key, count in redis.counts.items() if ":client:" in key
    ]
    assert tenant_counts == [31]
    assert global_counts == [31]
    assert sorted(client_counts) == [1, 30]


def test_sso_rate_limit_ignores_spoofed_forwarded_for_from_public_peer():
    baseline = sso_api._sso_rate_limit_client_key(
        _request(peer="198.51.100.20", forwarded="1.1.1.1")
    )
    changed_forwarded = sso_api._sso_rate_limit_client_key(
        _request(peer="198.51.100.20", forwarded="8.8.8.8")
    )
    changed_real_ip = sso_api._sso_rate_limit_client_key(
        _request(ip="9.9.9.9", peer="198.51.100.20", forwarded="8.8.8.8")
    )

    assert baseline == changed_forwarded == changed_real_ip


def test_sso_rate_limit_accepts_nginx_real_ip_only_from_private_peer():
    first = sso_api._sso_rate_limit_client_key(
        _request(ip="1.1.1.1", peer="172.20.0.5", forwarded="9.9.9.9")
    )
    second = sso_api._sso_rate_limit_client_key(
        _request(ip="8.8.8.8", peer="172.20.0.5", forwarded="9.9.9.9")
    )

    assert first != second


@pytest.mark.asyncio
async def test_sso_session_creation_fails_closed_when_rate_limit_store_is_down():
    with patch(
        "app.core.events.get_redis",
        new=AsyncMock(side_effect=RuntimeError("redis unavailable")),
    ), pytest.raises(HTTPException) as exc:
        await sso_api._enforce_sso_session_creation_rate_limit(_request(), None)

    assert exc.value.status_code == 503
    assert "redis" not in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_sso_browser_binding_cookie_covers_callback_and_consume_paths():
    request = _http_request()
    response = Response()
    db = _CreateSessionDB()

    with patch.object(
        sso_api,
        "_enforce_sso_session_creation_rate_limit",
        new=AsyncMock(),
    ):
        result = await sso_api.create_sso_session(
            request=request,
            response=response,
            tenant_id=None,
            db=db,
        )

    cookies = [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    ]
    assert len(cookies) == 1
    issued = cookies[0]
    assert f"{session_service.sso_initiator_cookie_name(uuid.UUID(result['session_id']))}=" in issued
    assert "Path=/api" in issued
    assert "HttpOnly" in issued
    assert "SameSite=lax" in issued
    assert "Secure" in issued
    assert "Domain=" not in issued
    assert db.commits == 1

    cleared_response = Response()
    sso_api._clear_sso_initiator_cookie(
        cleared_response,
        request,
        uuid.UUID(result["session_id"]),
    )
    cleared = [
        value.decode("latin-1")
        for key, value in cleared_response.raw_headers
        if key.lower() == b"set-cookie"
    ][0]
    assert "Path=/api" in cleared
    assert "Max-Age=0" in cleared
    assert "HttpOnly" in cleared
    assert "SameSite=lax" in cleared
    assert "Secure" in cleared
    assert "Domain=" not in cleared


@pytest.mark.asyncio
async def test_provider_metadata_listing_does_not_allocate_relay_session():
    db = _ProviderMetadataDB()

    providers = await sso_api.list_sso_providers(uuid.uuid4(), db)

    assert providers == [
        {"provider_type": "google_workspace", "name": "Google Workspace"},
    ]
    assert db.executions == 2


def test_callback_browser_binding_rejects_missing_wrong_or_other_session_cookie():
    sid = uuid.uuid4()
    nonce = "initiator-secret"
    session = SSOScanSession(
        id=sid,
        status="pending",
        initiator_nonce_hash=session_service.hash_sso_initiator_nonce(nonce),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    for cookie in (
        "",
        f"{session_service.sso_initiator_cookie_name(sid)}=wrong-secret",
        f"{session_service.sso_initiator_cookie_name(uuid.uuid4())}={nonce}",
    ):
        with pytest.raises(HTTPException) as exc:
            session_service.verify_sso_callback_initiator(
                session,
                _http_request(cookie=cookie, path="/api/auth/feishu/callback"),
            )
        assert exc.value.status_code == 403

    session_service.verify_sso_callback_initiator(
        session,
        _http_request(
            cookie=f"{session_service.sso_initiator_cookie_name(sid)}={nonce}",
            path="/api/auth/feishu/callback",
        ),
    )


@pytest.mark.asyncio
async def test_cleanup_expires_credentials_and_deletes_old_rows_idempotently():
    db = _CleanupDB([2, 0, 3])
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)

    expired, deleted = await session_service.cleanup_expired_sso_sessions(
        db,
        now=now,
        retention_minutes=60,
    )

    assert (expired, deleted) == (2, 3)
    assert len(db.statements) == 3


@pytest.mark.asyncio
async def test_expired_authorized_session_never_returns_jwt_and_is_repeat_safe():
    nonce = "initiator-secret"
    session = SSOScanSession(
        id=uuid.uuid4(),
        status="authorized",
        access_token="encrypted-jwt",
        initiator_nonce_hash=session_service.hash_sso_initiator_nonce(nonce),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db = _SessionDB(session)

    first = await session_service.consume_authorized_sso_session(
        db,
        session.id,
        initiator_nonce=nonce,
    )
    second = await session_service.consume_authorized_sso_session(
        db,
        session.id,
        initiator_nonce=nonce,
    )

    assert first is None
    assert second is None
    assert session.status == "expired"
    assert session.access_token is None
    assert db.flush_count == 2


def test_sso_session_expiry_index_is_part_of_release_migration():
    migration = (
        Path(__file__).parents[1]
        / "alembic/versions/106_secure_sso_password_login.py"
    )
    source = migration.read_text(encoding="utf-8")
    assert "ix_sso_scan_sessions_expires_at" in source
