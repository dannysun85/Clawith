import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import auth as auth_api
from app.core.auth_rate_limit import (
    AuthRateLimitPolicy,
    auth_rate_limit_client_key,
    enforce_auth_rate_limit,
    login_lookup_rate_limit_policy,
    login_rate_limit_policy,
    oauth_exchange_rate_limit_policy,
)
from app.dao.identity_dao import IdentityDAO
from app.schemas.schemas import RegisterInitRequest, UserRegister, UserUpdate
from app.services.identity_login_namespace import validate_identity_login_namespace


def _request(host: str = "203.0.113.10", *, real_ip: str | None = None) -> Request:
    headers = []
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": headers,
            "client": (host, 443),
            "scheme": "https",
            "server": ("astra.example", 443),
        }
    )


class _AtomicRateLimitRedis:
    """Small behavioral fake for the auth limiter's one Lua operation."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def eval(self, _script: str, numkeys: int, *args):
        assert numkeys == 3
        keys = tuple(str(value) for value in args[:3])
        values = tuple(int(value) for value in args[3:])
        (
            client_limit,
            identity_limit,
            global_limit,
            _client_ttl,
            _identity_ttl,
            _global_ttl,
            global_cost,
        ) = values
        async with self._lock:
            if self.counts.get(keys[0], 0) >= client_limit:
                return 1
            if self.counts.get(keys[1], 0) >= identity_limit:
                return 2
            if self.counts.get(keys[2], 0) + global_cost > global_limit:
                return 3
            self.counts[keys[0]] = self.counts.get(keys[0], 0) + 1
            self.counts[keys[1]] = self.counts.get(keys[1], 0) + 1
            self.counts[keys[2]] = self.counts.get(keys[2], 0) + global_cost
            return 0


def _policy(
    operation: str,
    *,
    client_limit: int = 2,
    identity_limit: int = 5,
    global_limit: int = 5,
    global_namespace: str | None = None,
    global_cost: int = 1,
) -> AuthRateLimitPolicy:
    return AuthRateLimitPolicy(
        operation=operation,
        client_limit=client_limit,
        identity_limit=identity_limit,
        global_limit=global_limit,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=10,
        global_namespace=global_namespace,
        global_cost=global_cost,
    )


@pytest.mark.asyncio
async def test_rejected_client_does_not_poison_identity_or_global_quota(monkeypatch):
    redis = _AtomicRateLimitRedis()
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))
    policy = _policy("client-isolation")

    outcomes = await asyncio.gather(
        *(
            enforce_auth_rate_limit(
                _request(),
                identity="identity:target",
                policy=policy,
            )
            for _ in range(20)
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in outcomes) == 2
    assert sum(
        isinstance(result, HTTPException) and result.status_code == 429
        for result in outcomes
    ) == 18

    identity_count = next(
        value for key, value in redis.counts.items() if ":identity:" in key
    )
    global_count = next(
        value for key, value in redis.counts.items() if key.endswith(":global")
    )
    assert identity_count == 2
    assert global_count == 2

    await enforce_auth_rate_limit(
        _request("198.51.100.20"),
        identity="identity:target",
        policy=policy,
    )
    assert next(
        value for key, value in redis.counts.items() if key.endswith(":global")
    ) == 3


@pytest.mark.asyncio
async def test_rejected_identity_does_not_poison_shared_global_quota(monkeypatch):
    redis = _AtomicRateLimitRedis()
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))
    policy = _policy("identity-isolation", client_limit=5, identity_limit=2)

    await enforce_auth_rate_limit(
        _request("203.0.113.1"), identity="identity:target", policy=policy
    )
    await enforce_auth_rate_limit(
        _request("203.0.113.2"), identity="identity:target", policy=policy
    )
    with pytest.raises(HTTPException) as exc:
        await enforce_auth_rate_limit(
            _request("203.0.113.3"), identity="identity:target", policy=policy
        )
    assert exc.value.status_code == 429

    await enforce_auth_rate_limit(
        _request("203.0.113.4"), identity="identity:other", policy=policy
    )
    assert next(
        value for key, value in redis.counts.items() if key.endswith(":global")
    ) == 3


@pytest.mark.asyncio
async def test_bcrypt_operations_share_one_weighted_global_budget(monkeypatch):
    redis = _AtomicRateLimitRedis()
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))
    login_policy = _policy(
        "login",
        client_limit=10,
        identity_limit=10,
        global_limit=3,
        global_namespace="bcrypt-work",
    )
    register_policy = _policy(
        "register",
        client_limit=10,
        identity_limit=10,
        global_limit=3,
        global_namespace="bcrypt-work",
        global_cost=2,
    )

    await enforce_auth_rate_limit(
        _request("203.0.113.1"), identity="identity:a", policy=login_policy
    )
    await enforce_auth_rate_limit(
        _request("203.0.113.2"), identity="identity:b", policy=register_policy
    )
    with pytest.raises(HTTPException) as exc:
        await enforce_auth_rate_limit(
            _request("203.0.113.3"), identity="identity:c", policy=login_policy
        )
    assert exc.value.status_code == 429
    assert redis.counts["auth:rate:v1:bcrypt-work:global"] == 3


@pytest.mark.asyncio
async def test_auth_rate_limit_fails_closed_when_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "app.core.events.get_redis",
        AsyncMock(side_effect=ConnectionError("redis unavailable")),
    )
    with pytest.raises(HTTPException) as exc:
        await enforce_auth_rate_limit(
            _request(), identity="identity:target", policy=_policy("fail-closed")
        )
    assert exc.value.status_code == 503


def test_client_bucket_ignores_spoofed_headers_and_honors_private_proxy():
    direct_a = auth_rate_limit_client_key(
        _request("8.8.8.8", real_ip="203.0.113.10")
    )
    direct_b = auth_rate_limit_client_key(
        _request("8.8.8.8", real_ip="203.0.113.11")
    )
    proxy_a = auth_rate_limit_client_key(
        _request("172.18.0.4", real_ip="203.0.113.10")
    )
    proxy_b = auth_rate_limit_client_key(
        _request("172.18.0.4", real_ip="203.0.113.11")
    )
    assert direct_a == direct_b
    assert proxy_a != proxy_b
    assert "8.8.8.8" not in direct_a
    assert "203.0.113.10" not in proxy_a


@pytest.mark.asyncio
async def test_login_uses_resolved_identity_bucket_before_bcrypt(monkeypatch):
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        password_login_enabled=True,
        password_hash="bcrypt-hash",
    )
    limiter = AsyncMock(
        side_effect=[None, HTTPException(status_code=429, detail="limited")]
    )
    verifier = AsyncMock()
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_login_identifier",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", limiter)
    monkeypatch.setattr(auth_api, "verify_password_async", verifier)

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(
            SimpleNamespace(
                login_identifier="victim@example.com",
                password="password",
                tenant_id=None,
            ),
            BackgroundTasks(),
            _request(),
        )
    assert exc.value.status_code == 429
    assert limiter.await_count == 2
    lookup_call, resolved_call = limiter.await_args_list
    assert lookup_call.kwargs["identity"] == "raw:victim@example.com"
    assert lookup_call.kwargs["policy"].operation == "password-login-lookup"
    assert resolved_call.kwargs["identity"] == f"identity:{identity.id}"
    assert resolved_call.kwargs["policy"].operation == "password-login"
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_prelookup_limit_rejects_before_database_work(monkeypatch):
    limiter = AsyncMock(side_effect=HTTPException(status_code=429, detail="limited"))
    lookup = AsyncMock()
    verifier = AsyncMock()
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", limiter)
    monkeypatch.setattr(auth_api.identity_dao, "get_by_login_identifier", lookup)
    monkeypatch.setattr(auth_api, "verify_password_async", verifier)

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(
            SimpleNamespace(
                login_identifier="unknown-user",
                password="password",
                tenant_id=None,
            ),
            BackgroundTasks(),
            _request(),
        )

    assert exc.value.status_code == 429
    assert limiter.await_count == 1
    assert limiter.await_args.kwargs["identity"] == "raw:unknown-user"
    assert limiter.await_args.kwargs["policy"].operation == "password-login-lookup"
    lookup.assert_not_awaited()
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_registration_limit_runs_before_policy_hash_and_db(monkeypatch):
    limiter = AsyncMock(side_effect=HTTPException(status_code=429, detail="limited"))
    resolver = AsyncMock()
    hasher = AsyncMock()
    lookup = AsyncMock()
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", limiter)
    monkeypatch.setattr(
        auth_api,
        "_resolve_password_registration_email_config",
        resolver,
    )
    monkeypatch.setattr(auth_api, "hash_password_async", hasher)
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", lookup)

    with pytest.raises(HTTPException) as exc:
        await auth_api.register_init(
            SimpleNamespace(
                email="new@example.com",
                username="new-user",
                password="password",
                display_name="New User",
                invitation_code="CODE",
                target_tenant_id=None,
            ),
            BackgroundTasks(),
            _request(),
        )
    assert exc.value.status_code == 429
    resolver.assert_not_awaited()
    hasher.assert_not_awaited()
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_action_limit_prevents_token_and_mail_side_effects(monkeypatch):
    limiter = AsyncMock(side_effect=HTTPException(status_code=429, detail="limited"))
    resolver = AsyncMock()
    lookup = AsyncMock()
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", limiter)
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        resolver,
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", lookup)
    background = BackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        await auth_api.forgot_password(
            SimpleNamespace(email="victim@example.com"),
            background,
            _request(),
        )
    assert exc.value.status_code == 429
    resolver.assert_not_awaited()
    lookup.assert_not_awaited()
    assert background.tasks == []


@pytest.mark.asyncio
async def test_password_change_limit_runs_before_verify_and_hash(monkeypatch):
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        password_login_enabled=True,
        password_hash="bcrypt-hash",
    )
    user = SimpleNamespace(identity=identity)
    current_user = SimpleNamespace(id=uuid.uuid4(), identity_id=identity.id)
    limiter = AsyncMock(side_effect=HTTPException(status_code=429, detail="limited"))
    verifier = AsyncMock()
    hasher = AsyncMock()
    monkeypatch.setattr(auth_api.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", limiter)
    monkeypatch.setattr(auth_api, "verify_password_async", verifier)
    monkeypatch.setattr(auth_api, "hash_password_async", hasher)

    with pytest.raises(HTTPException) as exc:
        await auth_api.change_password(
            {"old_password": "old-password", "new_password": "new-password"},
            _request(),
            current_user,
        )
    assert exc.value.status_code == 429
    assert limiter.await_args.kwargs["identity"] == f"identity:{identity.id}"
    verifier.assert_not_awaited()
    hasher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_factory",
    [
        login_lookup_rate_limit_policy,
        login_rate_limit_policy,
        oauth_exchange_rate_limit_policy,
    ],
    ids=["password-login-lookup", "password-login", "oauth-exchange"],
)
async def test_enterprise_nat_allows_thirty_distinct_identities(
    monkeypatch,
    policy_factory,
):
    """One office IP must not lock out a normal multi-user login burst."""
    redis = _AtomicRateLimitRedis()
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))
    policy = policy_factory()

    assert policy.client_limit >= 30
    outcomes = await asyncio.gather(
        *(
            enforce_auth_rate_limit(
                _request("203.0.113.50"),
                identity=f"identity:{index}",
                policy=policy,
            )
            for index in range(30)
        ),
        return_exceptions=True,
    )

    assert outcomes == [None] * 30


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda username: UserRegister(
            username=username,
            email="new@example.com",
            password="password",
        ),
        lambda username: RegisterInitRequest(
            username=username,
            email="new@example.com",
            password="password",
        ),
        lambda username: UserUpdate(username=username),
    ],
)
@pytest.mark.parametrize("username", ["victim@example.com", "+86 138-0013-8000"])
def test_public_schemas_reject_contact_shaped_usernames(payload_factory, username):
    with pytest.raises(ValidationError):
        payload_factory(username)


@pytest.mark.asyncio
async def test_login_identifier_resolves_email_before_conflicting_username(monkeypatch):
    victim = SimpleNamespace(id=uuid.uuid4(), email="victim@example.com")

    class _Result:
        def scalar_one_or_none(self):
            return victim

    class _DB:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return _Result()

    db = _DB()

    @asynccontextmanager
    async def _session():
        yield db

    dao = IdentityDAO()
    monkeypatch.setattr(dao, "session", _session)
    resolved = await dao.get_by_login_identifier("victim@example.com")

    assert resolved is victim
    assert len(db.statements) == 1
    sql = str(db.statements[0])
    assert "lower(identities.email)" in sql
    assert " OR " not in sql


@pytest.mark.asyncio
async def test_cross_namespace_email_conflict_is_rejected_before_write(monkeypatch):
    victim = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_username",
        AsyncMock(
            side_effect=lambda value: victim if value == "victim@example.com" else None
        ),
    )
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_email",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_phone",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as exc:
        await validate_identity_login_namespace(
            username="new-user",
            email="victim@example.com",
        )
    assert exc.value.status_code == 409
