"""Unit tests for the authentication API (app/api/auth.py)."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import auth as auth_api
from app.core.security import hash_password
from app.database import _session_ctx
from app.services import registration_service as registration_service_module
from app.services.system_email_service import SystemEmailConfigResolutionError


async def run_with_db(db, func, *args, **kwargs):
    token = _session_ctx.set(db)
    try:
        return await func(*args, **kwargs)
    finally:
        _session_ctx.reset(token)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, _statement, _params=None):
        if not self.responses:
            return DummyResult()
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)

    async def flush(self):
        pass


def _make_identity(
    *,
    email="test@example.com",
    username="testuser",
    password="correctpassword",
    is_active=True,
    email_verified=True,
):
    """Create a fake Identity object with hashed password."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        username=username,
        phone=None,
        password_hash=hash_password(password),
        is_active=is_active,
        email_verified=email_verified,
    )


def _make_user(identity_id, *, role="member", tenant_id=None):
    """Create a fake User object."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        role=role,
        tenant_id=tenant_id or uuid.uuid4(),
        identity=_make_identity(),
        is_active=True,
    )


def _make_login_data(login_identifier="test@example.com", password="correctpassword"):
    return SimpleNamespace(
        login_identifier=login_identifier,
        password=password,
        tenant_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_verified", "service_active", "email_config"),
    [
        (True, True, None),
        (False, False, SimpleNamespace(smtp_host="smtp.example.com")),
    ],
    ids=["no-smtp-auto-verified", "smtp-verification-required"],
)
async def test_register_init_preserves_service_activation_decision(
    monkeypatch,
    identity_verified,
    service_active,
    email_config,
):
    """The API must not overwrite the registration service's activation policy."""

    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="new-user@example.com",
        username="new-user",
        email_verified=identity_verified,
        is_platform_admin=False,
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        tenant_id=None,
        role="member",
        is_active=service_active,
        email_verified=identity_verified,
    )
    db = RecordingDB()

    @asynccontextmanager
    async def registration_transaction():
        yield db

    registration_service = SimpleNamespace(
        find_or_create_identity=AsyncMock(return_value=identity),
        create_user_with_identity=AsyncMock(return_value=user),
    )
    monkeypatch.setattr(auth_api, "transaction", registration_transaction)
    monkeypatch.setattr(auth_api, "hash_password_async", AsyncMock(return_value="password-hash"))
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_and_tenant",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_api,
        "_prepare_signup_code_if_required",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_api,
        "_send_verification_email_task",
        AsyncMock(),
    )
    monkeypatch.setattr(auth_api, "create_access_token", lambda *_args: "access-token")
    monkeypatch.setattr(auth_api, "serialize_user", lambda value: value)
    monkeypatch.setattr(
        auth_api,
        "RegisterInitResponse",
        lambda **values: SimpleNamespace(**values),
    )

    data = SimpleNamespace(
        email=identity.email,
        username=identity.username,
        password="correctpassword",
        display_name="New User",
        invitation_code="REGISTER-CODE",
        target_tenant_id=None,
    )
    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=AsyncMock(return_value=email_config),
    ), patch(
        "app.services.registration_service.registration_service",
        new=registration_service,
    ):
        result = await auth_api.register_init(data, AsyncMock())

    assert result.access_token == "access-token"
    assert user.is_active is service_active
    assert registration_service.find_or_create_identity.await_args.kwargs["email_config"] is email_config
    assert registration_service.create_user_with_identity.await_args.kwargs["email_config"] is email_config


@pytest.mark.asyncio
async def test_auth_email_policy_lookup_failure_is_service_unavailable():
    resolver = AsyncMock(
        side_effect=SystemEmailConfigResolutionError("temporary settings failure")
    )

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=resolver,
    ), pytest.raises(HTTPException) as exc:
        await auth_api._resolve_auth_email_config()

    assert exc.value.status_code == 503
    assert "temporarily unavailable" in str(exc.value.detail).lower()
    resolver.assert_awaited_once_with(raise_on_error=True)


@pytest.mark.asyncio
async def test_register_init_fails_before_mutation_when_email_policy_is_unavailable(monkeypatch):
    resolver = AsyncMock(
        side_effect=SystemEmailConfigResolutionError("temporary settings failure")
    )
    hash_password_call = AsyncMock(return_value="password-hash")
    is_empty = AsyncMock(return_value=False)
    monkeypatch.setattr(auth_api, "hash_password_async", hash_password_call)
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", is_empty)

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=resolver,
    ), pytest.raises(HTTPException) as exc:
        await auth_api.register_init(SimpleNamespace(), AsyncMock())

    assert exc.value.status_code == 503
    hash_password_call.assert_not_awaited()
    is_empty.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_service_does_not_reread_confirmed_no_smtp_policy(monkeypatch):
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        username="no-smtp-user",
        email_verified=True,
        is_platform_admin=False,
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        display_name="No SMTP User",
        avatar_url=None,
        is_active=True,
    )
    resolver = AsyncMock(side_effect=AssertionError("email policy was read twice"))
    create_user = AsyncMock(return_value=user)
    bind_org_member = AsyncMock()
    create_participant = AsyncMock()

    monkeypatch.setattr(registration_service_module, "resolve_email_config_async", resolver)
    monkeypatch.setattr(registration_service_module.user_dao, "create", create_user)
    monkeypatch.setattr(
        registration_service_module.participant_dao,
        "create_for_user",
        create_participant,
    )
    service = registration_service_module.RegistrationService()
    monkeypatch.setattr(service, "bind_org_member", bind_org_member)

    result = await service.create_user_with_identity(
        identity=identity,
        display_name=user.display_name,
        email_config=None,
    )

    assert result is user
    assert create_user.await_args.kwargs["obj_in"]["is_active"] is True
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_entrypoint", [False, True], ids=["register-sso", "legacy-register"])
async def test_sso_registration_entrypoints_fail_closed_when_policy_is_unavailable(
    monkeypatch,
    legacy_entrypoint,
):
    resolver = AsyncMock(
        side_effect=SystemEmailConfigResolutionError("temporary settings failure")
    )
    is_empty = AsyncMock(return_value=False)
    provider_lookup = AsyncMock()
    monkeypatch.setattr(auth_api.user_dao, "is_empty", is_empty)

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=resolver,
    ), patch(
        "app.services.auth_registry.auth_provider_registry.get_provider",
        new=provider_lookup,
    ), pytest.raises(HTTPException) as exc:
        if legacy_entrypoint:
            await auth_api.register(
                SimpleNamespace(
                    provider="google",
                    provider_code="provider-code",
                    invitation_code=None,
                ),
                AsyncMock(),
            )
        else:
            await auth_api.register_sso(
                SimpleNamespace(
                    provider="google",
                    code="provider-code",
                    invitation_code=None,
                )
            )

    assert exc.value.status_code == 503
    is_empty.assert_not_awaited()
    provider_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_sso_service_propagates_one_email_policy_snapshot(monkeypatch):
    service = registration_service_module.RegistrationService()
    email_config = SimpleNamespace(smtp_host="smtp.example.com")
    provider_user = SimpleNamespace(
        name="SSO User",
        email="sso@example.com",
        avatar_url=None,
        mobile=None,
        raw_data={},
        provider_union_id="provider-union-id",
        provider_user_id="provider-user-id",
    )
    provider = SimpleNamespace(
        exchange_code_for_token=AsyncMock(return_value={"access_token": "provider-token"}),
        get_user_info=AsyncMock(return_value=provider_user),
    )
    user = _make_user(uuid.uuid4())
    handle_registration = AsyncMock(return_value=(user, True))

    @asynccontextmanager
    async def identity_session():
        yield RecordingDB()

    monkeypatch.setattr(service, "detect_tenant_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "handle_sso_registration", handle_registration)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr(registration_service_module.identity_dao, "session", identity_session)
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "resolve_user_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "match_user_by_email",
        AsyncMock(return_value=None),
    )

    result = await service.register_with_sso(
        "google",
        "provider-code",
        provider,
        email_config=email_config,
    )

    assert result == (user, True, None)
    assert handle_registration.await_args.kwargs["email_config"] is email_config


@pytest.mark.asyncio
async def test_sso_service_does_not_collapse_policy_resolution_error(monkeypatch):
    service = registration_service_module.RegistrationService()
    resolver = AsyncMock(
        side_effect=SystemEmailConfigResolutionError("temporary settings failure")
    )
    provider = SimpleNamespace(
        exchange_code_for_token=AsyncMock(),
        get_user_info=AsyncMock(),
    )
    monkeypatch.setattr(registration_service_module, "resolve_email_config_async", resolver)

    with pytest.raises(SystemEmailConfigResolutionError):
        await service.register_with_sso("google", "provider-code", provider)

    provider.exchange_code_for_token.assert_not_awaited()


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_invalid_credentials_no_identity():
    """Login with a nonexistent user returns 401."""
    db = RecordingDB(responses=[DummyResult()])  # no identity found
    data = _make_login_data(login_identifier="nobody@example.com", password="whatever")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, data, bg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_invalid_credentials_wrong_password():
    """Login with wrong password returns 401."""
    identity = _make_identity(password="correctpassword")
    db = RecordingDB(responses=[DummyResult(values=[identity])])
    data = _make_login_data(password="wrongpassword")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, data, bg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_disabled_account():
    """Login with a disabled account returns 403."""
    identity = _make_identity(is_active=False)
    db = RecordingDB(responses=[DummyResult(values=[identity])])
    data = _make_login_data()
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, data, bg)
    assert exc.value.status_code == 403
    assert "disabled" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_login_unverified_email():
    """Login with unverified email returns 403 with verification info."""
    identity = _make_identity(email_verified=False)
    user = _make_user(identity.id)
    db = RecordingDB(responses=[
        DummyResult(values=[identity]),  # identity lookup
        DummyResult(values=[user]),       # user lookup for email task
    ])
    data = _make_login_data()
    bg = AsyncMock()

    with patch("app.services.system_email_service.resolve_email_config_async", new_callable=AsyncMock, return_value={"host": "localhost"}):
        with patch.object(auth_api, "_send_verification_email_task", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc:
                await run_with_db(db, auth_api.login, data, bg)
    assert exc.value.status_code == 403
    assert exc.value.detail["needs_verification"] is True


@pytest.mark.asyncio
async def test_login_unverified_email_fails_closed_when_policy_is_unavailable():
    identity = _make_identity(email_verified=False)
    db = RecordingDB(responses=[DummyResult(values=[identity])])
    resolver = AsyncMock(
        side_effect=SystemEmailConfigResolutionError("temporary settings failure")
    )

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=resolver,
    ), pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, _make_login_data(), AsyncMock())

    assert exc.value.status_code == 503
    assert identity.email_verified is False
    resolver.assert_awaited_once_with(raise_on_error=True)


# ---------------------------------------------------------------------------
# Registration code gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_config_allows_bootstrap_without_code(monkeypatch):
    is_empty = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", is_empty)

    result = await auth_api.get_registration_config()

    assert result == {"invitation_code_required": False}
    is_empty.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_registration_config_requires_code_after_bootstrap(monkeypatch):
    is_empty = AsyncMock(return_value=False)
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", is_empty)

    result = await auth_api.get_registration_config()

    assert result == {"invitation_code_required": True}
    is_empty.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_signup_code_gate_allows_bootstrap_without_code(monkeypatch):
    enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_api.system_setting_dao, "is_invitation_code_enabled", enabled)

    result = await auth_api._prepare_signup_code_if_required(
        RecordingDB(),
        None,
        is_first_user=True,
    )

    assert result is None
    enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_signup_code_gate_rejects_missing_code(monkeypatch):
    monkeypatch.setattr(
        auth_api.system_setting_dao,
        "is_invitation_code_enabled",
        AsyncMock(return_value=True),
    )

    with pytest.raises(HTTPException) as exc:
        await auth_api._prepare_signup_code_if_required(
            RecordingDB(),
            "",
            is_first_user=False,
        )

    assert exc.value.status_code == 400
    assert "required" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_signup_code_gate_rejects_invalid_code(monkeypatch):
    monkeypatch.setattr(
        auth_api.system_setting_dao,
        "is_invitation_code_enabled",
        AsyncMock(return_value=True),
    )

    with pytest.raises(HTTPException) as exc:
        await auth_api._prepare_signup_code_if_required(
            RecordingDB(responses=[DummyResult()]),
            "bad-code",
            is_first_user=False,
        )

    assert exc.value.status_code == 400
    assert "invalid" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_signup_code_gate_rejects_exhausted_code(monkeypatch):
    monkeypatch.setattr(
        auth_api.system_setting_dao,
        "is_invitation_code_enabled",
        AsyncMock(return_value=True),
    )
    code = SimpleNamespace(code="FULL", tenant_id=None, used_count=3, max_uses=3)

    with pytest.raises(HTTPException) as exc:
        await auth_api._prepare_signup_code_if_required(
            RecordingDB(responses=[DummyResult(values=[code])]),
            "full",
            is_first_user=False,
        )

    assert exc.value.status_code == 400
    assert "usage limit" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_signup_code_gate_consumes_valid_code(monkeypatch):
    monkeypatch.setattr(
        auth_api.system_setting_dao,
        "is_invitation_code_enabled",
        AsyncMock(return_value=True),
    )
    code = SimpleNamespace(code="OPEN123", tenant_id=None, used_count=0, max_uses=2)

    result = await auth_api._prepare_signup_code_if_required(
        RecordingDB(responses=[DummyResult(values=[code])]),
        " open123 ",
        is_first_user=False,
    )
    auth_api._consume_signup_code_if_needed(result)

    assert result is code
    assert code.used_count == 1


# ---------------------------------------------------------------------------
# /me tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_user():
    """GET /me with an authenticated user returns user data."""
    identity = _make_identity()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        role="member",
        tenant_id=uuid.uuid4(),
        username=identity.username,
        email=identity.email,
        avatar_url=None,
        identity=identity,
    )

    class DummyUserOut:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
        @classmethod
        def model_validate(cls, obj):
            return cls(id=str(obj.id), email=obj.email)

    with patch("app.api.auth.UserOut", new=DummyUserOut):
        result = await auth_api.get_me(current_user=user)
    assert result.id == str(user.id)
    assert result.email == user.email
    assert result.is_platform_admin is False


@pytest.mark.asyncio
async def test_oauth_callback_passes_redirect_uri():
    """OAuth callback should forward redirect_uri for providers like Google."""
    identity = _make_identity()
    user = _make_user(identity.id)
    provider = AsyncMock()
    provider.exchange_code_for_token = AsyncMock(return_value={"access_token": "provider-token"})
    provider.get_user_info = AsyncMock(return_value=SimpleNamespace())
    provider.find_or_create_user = AsyncMock(return_value=(user, False))
    data = SimpleNamespace(
        code="oauth-code",
        state="oauth-state",
        redirect_uri="https://example.com/oauth/callback/google",
        pending_token=None,
        tenant_id=None,
    )

    with patch("app.services.auth_registry.auth_provider_registry.get_provider", new=AsyncMock(return_value=provider)):
        class DummyTokenResponse:
            def __init__(self, access_token, **kwargs):
                self.access_token = access_token
        with patch("app.api.auth.TokenResponse", new=DummyTokenResponse):
            with patch("app.api.auth.UserOut") as MockUserOut:
                MockUserOut.model_validate.return_value = {"id": str(user.id)}
                with patch.object(auth_api, "create_access_token", return_value="jwt-token"):
                    result = await run_with_db(RecordingDB(), auth_api.oauth_callback, "google", data)

    provider.exchange_code_for_token.assert_awaited_once_with("oauth-code", "https://example.com/oauth/callback/google")
    assert result.access_token == "jwt-token"
