import contextlib
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks

from app.api import auth as auth_api
from app.api.notification import BroadcastRequest, broadcast_notification
from app.core.security import verify_password, hash_password
from app.models.user import User
from app.schemas.schemas import ForgotPasswordRequest, ResetPasswordRequest
from app.services import (
    email_verification_service,
    password_reset_service,
    system_email_service,
)
from app.dao import system_setting_dao
from app.database import transaction


async def run_with_db(db, func, *args, **kwargs):
    async with transaction(db):
        return await func(*args, **kwargs)


@pytest.fixture(autouse=True)
def _stub_auth_rate_limit(monkeypatch):
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", AsyncMock())


@pytest.mark.asyncio
async def test_strict_email_policy_resolution_distinguishes_lookup_failure(monkeypatch):
    lookup = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(system_setting_dao, "get_value", lookup)

    with pytest.raises(system_email_service.SystemEmailConfigResolutionError):
        await system_email_service.resolve_email_config_async(raise_on_error=True)

    lookup.assert_awaited_once_with("system_email_platform", {})


class DummyScalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class DummyResult:
    def __init__(self, value=None, values=None):
        self._value = value
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return DummyScalars(self._values)


class MockPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def setex(self, key, ttl, value):
        self.commands.append(("setex", key, ttl, value))
        return self

    def delete(self, key):
        self.commands.append(("delete", key))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self):
        for cmd in self.commands:
            if cmd[0] == "setex":
                _, key, ttl, value = cmd
                self.redis.setex_calls.append((key, ttl, value))
                self.redis._data[key] = value
            elif cmd[0] == "delete":
                _, key = cmd
                self.redis.deleted.append(key)
                self.redis._data.pop(key, None)
        self.commands.clear()


class MockRedis:
    def __init__(self, initial_data=None):
        self._data = initial_data or {}
        self.deleted = []
        self.setex_calls = []

    async def get(self, key):
        return self._data.get(key)

    async def delete(self, key):
        self.deleted.append(key)
        self._data.pop(key, None)

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self._data[key] = value

    async def eval(self, script, numkeys, *values):
        if script == email_verification_service._CREATE_TOKEN_SCRIPT:
            assert numkeys == 2
            user_key, token_key, token_prefix, ttl, token_data, token_hash = values
            old_token_hash = self._data.get(user_key)
            if old_token_hash:
                old_token_key = f"{token_prefix}{old_token_hash}"
                self.deleted.append(old_token_key)
                self._data.pop(old_token_key, None)
            self.setex_calls.extend(
                [
                    (token_key, ttl, token_data),
                    (user_key, ttl, token_hash),
                ]
            )
            self._data[token_key] = token_data
            self._data[user_key] = token_hash
            return 1

        if script == email_verification_service._CONSUME_TOKEN_SCRIPT:
            assert numkeys == 1
            token_key, user_prefix, token_hash = values
            token_data = self._data.pop(token_key, None)
            if token_data is None:
                return None
            self.deleted.append(token_key)
            identity_id = json.loads(token_data)["identity_id"]
            user_key = f"{user_prefix}{identity_id}"
            if self._data.get(user_key) == token_hash:
                self._data.pop(user_key, None)
                self.deleted.append(user_key)
            return token_data

        if script == email_verification_service._INVALIDATE_TOKEN_SCRIPT:
            assert numkeys == 1
            user_key, token_prefix = values
            token_hash = self._data.pop(user_key, None)
            self.deleted.append(user_key)
            if token_hash:
                token_key = f"{token_prefix}{token_hash}"
                self._data.pop(token_key, None)
                self.deleted.append(token_key)
            return 1

        if script == password_reset_service._CREATE_TOKEN_SCRIPT:
            assert numkeys == 2
            user_key, token_key, token_prefix, ttl, token_data, token_hash = values
            old_token_hash = self._data.get(user_key)
            if old_token_hash:
                old_token_key = f"{token_prefix}{old_token_hash}"
                self.deleted.append(old_token_key)
                self._data.pop(old_token_key, None)
            self.setex_calls.extend(
                [
                    (token_key, ttl, token_data),
                    (user_key, ttl, token_hash),
                ]
            )
            self._data[token_key] = token_data
            self._data[user_key] = token_hash
            return 1

        if script == password_reset_service._CONSUME_TOKEN_SCRIPT:
            assert numkeys == 1
            token_key, user_prefix, token_hash = values
            token_data = self._data.pop(token_key, None)
            if token_data is None:
                return None
            self.deleted.append(token_key)
            identity_id = json.loads(token_data)["identity_id"]
            user_key = f"{user_prefix}{identity_id}"
            if self._data.get(user_key) == token_hash:
                self._data.pop(user_key, None)
                self.deleted.append(user_key)
            return token_data

        if script == password_reset_service._INVALIDATE_TOKEN_SCRIPT:
            assert numkeys == 1
            user_key, token_prefix = values
            token_hash = self._data.pop(user_key, None)
            self.deleted.append(user_key)
            if token_hash:
                token_key = f"{token_prefix}{token_hash}"
                self._data.pop(token_key, None)
                self.deleted.append(token_key)
            return 1

        raise AssertionError("unexpected Redis script")

    def pipeline(self, transaction=True):
        return MockPipeline(self)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed = []
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, statement):
        self.executed.append(statement)
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.flushed = True
        self.committed = True


def make_user(**overrides):
    auth_version = overrides.pop("auth_version", 0)
    values = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "old-hash",
        "password_login_enabled": True,
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    user = User(**values)
    user.identity.auth_version = auth_version
    # SQLAlchemy column defaults are applied on flush; this factory is used
    # without a database round-trip, so model the persisted global state.
    user.identity.is_active = True
    return user


@pytest.mark.asyncio
async def test_email_verification_resend_invalidates_older_code_atomically(monkeypatch):
    identity_id = uuid.uuid4()
    old_hash = "old-email-code-hash"
    mock_redis = MockRedis(
        initial_data={
            f"email_verify:user:{identity_id}": old_hash,
            f"email_verify:token:{old_hash}": "old-token-data",
        }
    )

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(email_verification_service, "get_redis", fake_get_redis)
    monkeypatch.setattr(
        email_verification_service.secrets,
        "token_urlsafe",
        lambda _length: "secure-email-verification-nonce-1234567890",
    )
    monkeypatch.setattr(
        email_verification_service,
        "get_settings",
        lambda: SimpleNamespace(EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES=15),
    )

    raw_token, _expires_at = (
        await email_verification_service.email_verification_service.create_email_verification_token(
            identity_id,
            "alice@example.com",
        )
    )

    assert raw_token == (
        f"{identity_id}.secure-email-verification-nonce-1234567890"
    )
    assert f"email_verify:token:{old_hash}" in mock_redis.deleted
    current_hash = mock_redis._data[f"email_verify:user:{identity_id}"]
    assert set(key for key in mock_redis._data if key.startswith("email_verify:token:")) == {
        f"email_verify:token:{current_hash}"
    }


@pytest.mark.asyncio
async def test_email_verification_code_is_exactly_once_under_concurrency(monkeypatch):
    identity_id = uuid.uuid4()
    raw_token = f"{identity_id}.secure-email-verification-nonce-1234567890"
    token_hash = email_verification_service.email_verification_service._hash_token(raw_token)
    token_data = json.dumps(
        {"identity_id": str(identity_id), "email": "alice@example.com"}
    )
    mock_redis = MockRedis(
        initial_data={
            f"email_verify:token:{token_hash}": token_data,
            f"email_verify:user:{identity_id}": token_hash,
        }
    )

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(email_verification_service, "get_redis", fake_get_redis)

    results = await asyncio.gather(
        email_verification_service.email_verification_service.consume_email_verification_token(
            raw_token
        ),
        email_verification_service.email_verification_service.consume_email_verification_token(
            raw_token
        ),
    )

    assert sum(result is not None for result in results) == 1
    consumed = next(result for result in results if result is not None)
    assert consumed == {"identity_id": identity_id, "email": "alice@example.com"}


@pytest.mark.asyncio
async def test_same_email_nonce_is_isolated_by_identity_namespace(monkeypatch):
    first_identity = uuid.uuid4()
    second_identity = uuid.uuid4()
    mock_redis = MockRedis()

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(email_verification_service, "get_redis", fake_get_redis)
    monkeypatch.setattr(
        email_verification_service.secrets,
        "token_urlsafe",
        lambda _length: "same-secure-nonce-with-at-least-32-characters",
    )
    monkeypatch.setattr(
        email_verification_service,
        "get_settings",
        lambda: SimpleNamespace(EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES=15),
    )

    first_token, _ = (
        await email_verification_service.email_verification_service.create_email_verification_token(
            first_identity,
            "first@example.com",
        )
    )
    second_token, _ = (
        await email_verification_service.email_verification_service.create_email_verification_token(
            second_identity,
            "second@example.com",
        )
    )

    assert first_token != second_token
    first_result, second_result = await asyncio.gather(
        email_verification_service.email_verification_service.consume_email_verification_token(
            first_token
        ),
        email_verification_service.email_verification_service.consume_email_verification_token(
            second_token
        ),
    )
    assert first_result == {
        "identity_id": first_identity,
        "email": "first@example.com",
    }
    assert second_result == {
        "identity_id": second_identity,
        "email": "second@example.com",
    }


@pytest.mark.asyncio
async def test_legacy_six_digit_email_code_is_rejected_without_redis_lookup(monkeypatch):
    redis_lookup = AsyncMock()
    monkeypatch.setattr(email_verification_service, "get_redis", redis_lookup)

    result = (
        await email_verification_service.email_verification_service.consume_email_verification_token(
            "123456"
        )
    )

    assert result is None
    redis_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_change_invalidates_current_verification_token(monkeypatch):
    identity_id = uuid.uuid4()
    token_hash = "issued-to-old-address"
    user_key = f"email_verify:user:{identity_id}"
    token_key = f"email_verify:token:{token_hash}"
    mock_redis = MockRedis(
        initial_data={
            user_key: token_hash,
            token_key: "old-address-token-data",
        }
    )

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(email_verification_service, "get_redis", fake_get_redis)

    await email_verification_service.email_verification_service.invalidate_email_verification_tokens(
        identity_id
    )

    assert user_key not in mock_redis._data
    assert token_key not in mock_redis._data
    assert set(mock_redis.deleted) == {user_key, token_key}


@pytest.mark.asyncio
async def test_create_password_reset_token_invalidates_older_tokens(monkeypatch):
    monkeypatch.setattr(
        password_reset_service,
        "get_settings",
        lambda: SimpleNamespace(PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=15, PUBLIC_BASE_URL=""),
    )
    user_id = uuid.uuid4()
    mock_redis = MockRedis(initial_data={f"pwd_reset:user:{user_id}": "old-token-hash"})
    async def fake_get_redis(): return mock_redis
    monkeypatch.setattr(password_reset_service, "get_redis", fake_get_redis)

    raw_token, expires_at = await password_reset_service.create_password_reset_token(
        user_id,
        "alice@example.com",
        3,
    )

    # Verify old token invalidation
    assert "pwd_reset:token:old-token-hash" in mock_redis.deleted

    # Verify new token storage
    assert len(mock_redis.setex_calls) == 2
    # Verify raw token is long
    assert len(raw_token) >= 20
    assert expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_concurrent_password_reset_issuance_leaves_one_valid_token(monkeypatch):
    monkeypatch.setattr(
        password_reset_service,
        "get_settings",
        lambda: SimpleNamespace(PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=15),
    )
    identity_id = uuid.uuid4()
    mock_redis = MockRedis()
    issued_tokens = iter(("first-reset-token", "second-reset-token"))

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(password_reset_service, "get_redis", fake_get_redis)
    monkeypatch.setattr(
        password_reset_service.secrets,
        "token_urlsafe",
        lambda _length: next(issued_tokens),
    )

    results = await asyncio.gather(
        password_reset_service.create_password_reset_token(
            identity_id,
            "alice@example.com",
            7,
        ),
        password_reset_service.create_password_reset_token(
            identity_id,
            "alice@example.com",
            7,
        ),
    )

    user_key = f"pwd_reset:user:{identity_id}"
    current_hash = mock_redis._data[user_key]
    live_token_keys = {
        key
        for key in mock_redis._data
        if key.startswith(password_reset_service.TOKEN_PREFIX)
    }
    assert {raw_token for raw_token, _expires_at in results} == {
        "first-reset-token",
        "second-reset-token",
    }
    assert live_token_keys == {f"pwd_reset:token:{current_hash}"}


@pytest.mark.asyncio
async def test_build_password_reset_url_uses_env_public_base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com/")

    url = await password_reset_service.build_password_reset_url("abc123")

    assert url == "https://app.example.com/reset-password?token=abc123"


@pytest.mark.asyncio
async def test_build_password_reset_url_uses_persisted_platform_setting(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    setting = SimpleNamespace(value={"public_base_url": "https://stored.example.com/"})

    class FakeSession:
        async def execute(self, _statement):
            return DummyResult(value=setting)

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(password_reset_service, "async_session", FakeSessionContext)

    url = await password_reset_service.build_password_reset_url("abc123")

    assert url == "https://stored.example.com/reset-password?token=abc123"


@pytest.mark.asyncio
async def test_consume_password_reset_token_works_correctly(monkeypatch):
    user_id = uuid.uuid4()
    raw_token = "raw-token"
    token_hash = password_reset_service._hash_token(raw_token)
    
    initial_data = {
        f"pwd_reset:token:{token_hash}": json.dumps(
            {
                "identity_id": str(user_id),
                "email": "alice@example.com",
                "auth_version": 3,
            }
        ),
        f"pwd_reset:user:{user_id}": token_hash,
    }
    mock_redis = MockRedis(initial_data=initial_data)
    async def fake_get_redis(): return mock_redis
    monkeypatch.setattr(password_reset_service, "get_redis", fake_get_redis)

    result = await password_reset_service.consume_password_reset_token(raw_token)

    assert result is not None
    assert result["identity_id"] == user_id
    assert result["email"] == "alice@example.com"
    assert result["auth_version"] == 3
    # Should be deleted after consumption
    assert f"pwd_reset:token:{token_hash}" in mock_redis.deleted
    assert f"pwd_reset:user:{user_id}" in mock_redis.deleted


@pytest.mark.asyncio
async def test_consume_legacy_password_reset_token_without_auth_version_fails_closed(
    monkeypatch,
):
    identity_id = uuid.uuid4()
    raw_token = "legacy-raw-token"
    token_hash = password_reset_service._hash_token(raw_token)
    mock_redis = MockRedis(
        initial_data={
            f"pwd_reset:token:{token_hash}": json.dumps(
                {"identity_id": str(identity_id), "email": "alice@example.com"}
            ),
            f"pwd_reset:user:{identity_id}": token_hash,
        }
    )

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(password_reset_service, "get_redis", fake_get_redis)

    assert await password_reset_service.consume_password_reset_token(raw_token) is None


@pytest.mark.asyncio
async def test_consume_password_reset_token_is_exactly_once_under_concurrency(monkeypatch):
    user_id = uuid.uuid4()
    raw_token = "concurrent-raw-token"
    token_hash = password_reset_service._hash_token(raw_token)
    mock_redis = MockRedis(
        initial_data={
            f"pwd_reset:token:{token_hash}": json.dumps(
                {
                    "identity_id": str(user_id),
                    "email": "alice@example.com",
                    "auth_version": 4,
                }
            ),
            f"pwd_reset:user:{user_id}": token_hash,
        }
    )

    async def fake_get_redis():
        return mock_redis

    monkeypatch.setattr(password_reset_service, "get_redis", fake_get_redis)

    results = await asyncio.gather(
        password_reset_service.consume_password_reset_token(raw_token),
        password_reset_service.consume_password_reset_token(raw_token),
    )

    assert sum(result is not None for result in results) == 1
    assert {result["identity_id"] for result in results if result} == {user_id}
    assert {result["email"] for result in results if result} == {"alice@example.com"}
    assert {result["auth_version"] for result in results if result} == {4}


@pytest.mark.asyncio
async def test_forgot_password_returns_generic_response_for_unknown_email(monkeypatch):
    async def fake_resolve_email_config_async():
        return system_email_service.SystemEmailConfig(
            from_address="bot@example.com",
            from_name="Astra",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="bot@example.com",
            smtp_password="secret",
            smtp_ssl=True,
            smtp_timeout_seconds=15,
        )
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )
    background_tasks = BackgroundTasks()

    # Patch identity_dao.get_by_email to return None
    from app.dao import identity_dao

    async def fake_get_by_email(email):
        return None

    monkeypatch.setattr(identity_dao, "get_by_email", fake_get_by_email)

    response = await auth_api.forgot_password(
        ForgotPasswordRequest(email="missing@example.com"),
        background_tasks,
        SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"), headers={}),
    )

    assert response == {
        "ok": True,
        "message": "If an account with that email exists, a password reset email has been sent.",
    }
    assert background_tasks.tasks == []





@pytest.mark.asyncio
async def test_forgot_password_queues_background_email(monkeypatch):
    async def fake_resolve_email_config_async():
        return system_email_service.SystemEmailConfig(
            from_address="bot@example.com",
            from_name="Astra",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="bot@example.com",
            smtp_password="secret",
            smtp_ssl=True,
            smtp_timeout_seconds=15,
        )
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )

    user = make_user(email_verified=True)
    identity = user.identity
    background_tasks = BackgroundTasks()

    async def fake_create_password_reset_token(*_args, **_kwargs):
        return "raw-token", datetime.now(timezone.utc) + timedelta(minutes=30)

    async def fake_build_password_reset_url(*_args, **_kwargs):
        return "https://app.example.com/reset-password?token=raw-token"

    monkeypatch.setattr(password_reset_service, "create_password_reset_token", fake_create_password_reset_token)
    monkeypatch.setattr(password_reset_service, "build_password_reset_url", fake_build_password_reset_url)
    delivery_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.outbound_email_service.persist_template_email",
        AsyncMock(return_value=SimpleNamespace(id=delivery_id, status="queued")),
    )

    # Patch identity_dao.get_by_email to return our fake user
    from app.dao import identity_dao

    async def fake_get_by_email(email):
        return identity

    monkeypatch.setattr(identity_dao, "get_by_email", fake_get_by_email)
    monkeypatch.setattr(
        identity_dao,
        "get_for_update",
        AsyncMock(return_value=identity),
    )

    response = await auth_api.forgot_password(
        ForgotPasswordRequest(email=identity.email),
        background_tasks,
        SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"), headers={}),
    )

    assert response["ok"] is True
    assert len(background_tasks.tasks) == 1





def test_send_system_email_uses_configured_timeout(monkeypatch):
    captured = {}

    class DummySMTPSSL:
        def __init__(self, host: str, port: int, context=None, timeout: int | None = None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def login(self, username: str, password: str):
            captured["username"] = username
            captured["password"] = password

        def sendmail(self, from_address: str, to_addresses: list[str], message: str):
            captured["from"] = from_address
            captured["to"] = to_addresses
            captured["has_message"] = bool(message)

    config = system_email_service.SystemEmailConfig(
        from_address="bot@example.com",
        from_name="Astra",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="bot@example.com",
        smtp_password="secret",
        smtp_ssl=True,
        smtp_timeout_seconds=27,
    )
    monkeypatch.setattr(system_email_service.smtplib, "SMTP_SSL", DummySMTPSSL)
    monkeypatch.setattr(system_email_service, "force_ipv4", lambda: contextlib.nullcontext())

    system_email_service._send_email_with_config_sync(config, "alice@example.com", "subject", "body")

    assert captured["timeout"] == 27
    assert captured["to"] == ["alice@example.com"]


@pytest.mark.asyncio
async def test_reset_password_updates_user(monkeypatch):
    user = make_user(
        password_hash=hash_password("old-password"),
        password_login_enabled=False,
        email_verified=True,
        auth_version=5,
        is_active=True,
    )
    identity = user.identity
    db = RecordingDB([DummyResult(identity)])

    async def fake_consume_password_reset_token(*_args, **_kwargs):
        return {
            "identity_id": identity.id,
            "email": identity.email,
            "auth_version": 5,
        }

    monkeypatch.setattr(password_reset_service, "consume_password_reset_token", fake_consume_password_reset_token)

    response = await run_with_db(
        db,
        auth_api.reset_password,
        ResetPasswordRequest(token="t" * 20, new_password="new-password"),
    )

    assert response == {"ok": True}
    assert verify_password("new-password", user.password_hash)
    assert user.password_login_enabled is True
    assert user.email_verified is True
    assert user.identity.auth_version == 6
    assert user.is_active is True
    assert db.flushed is True


@pytest.mark.asyncio
async def test_broadcast_notification_rejects_missing_system_email_config(monkeypatch):
    current_user = make_user(role="org_admin")

    async def fake_resolve_email_config_async(db):
        return None

    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )

    with pytest.raises(HTTPException) as excinfo:
        await broadcast_notification(
            BroadcastRequest(title="Maintenance", body="Tonight", send_email=True),
            background_tasks=BackgroundTasks(),
            current_user=current_user,
            db=RecordingDB(),
        )

    assert excinfo.value.status_code == 400
    assert "System email is not configured" in excinfo.value.detail


@pytest.mark.asyncio
async def test_broadcast_notification_queues_email_delivery(monkeypatch):
    current_user = make_user(role="org_admin")
    target_user = make_user(email="bob@example.com", tenant_id=current_user.tenant_id)
    db = RecordingDB([
        DummyResult(values=[target_user]),
        DummyResult(values=[]),
    ])
    background_tasks = BackgroundTasks()

    async def fake_resolve_email_config_async(db):
        return system_email_service.SystemEmailConfig(
            from_address="bot@example.com",
            from_name="Astra",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="bot@example.com",
            smtp_password="secret",
            smtp_ssl=True,
            smtp_timeout_seconds=15,
        )
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        fake_resolve_email_config_async,
    )
    notifications = []

    async def fake_send_notification(*_args, **kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send_notification)

    response = await broadcast_notification(
        BroadcastRequest(title="Maintenance", body="Tonight", send_email=True),
        background_tasks=background_tasks,
        current_user=current_user,
        db=db,
    )

    assert response["ok"] is True
    assert response["emails_sent"] == 1
    assert db.committed is True
    assert len(notifications) == 1
    assert len(background_tasks.tasks) == 1


@pytest.mark.asyncio
async def test_deliver_broadcast_emails_continues_after_single_failure(monkeypatch):
    from app.services.system_email_service import BroadcastEmailRecipient, deliver_broadcast_emails

    delivered = []

    async def fake_send_system_email(email: str, subject: str, body: str) -> None:
        if email == "bad@example.com":
            raise RuntimeError("smtp down")
        delivered.append((email, subject, body))

    monkeypatch.setattr("app.services.system_email_service.send_system_email", fake_send_system_email)

    await deliver_broadcast_emails([
        BroadcastEmailRecipient(email="bad@example.com", subject="s1", body="b1"),
        BroadcastEmailRecipient(email="good@example.com", subject="s2", body="b2"),
    ])

    assert delivered == [("good@example.com", "s2", "b2")]
