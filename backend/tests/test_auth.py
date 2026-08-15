"""Unit tests for the authentication API (app/api/auth.py)."""

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException, Response

from app.api import auth as auth_api
from app.config import unverified_local_signup_allowed
from app.core.security import hash_password
from app.dao.identity_dao import IdentityDAO
from app.dao.user_dao import UserDAO
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

    def scalar(self):
        return self.scalar_one_or_none()

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
        self.statements = []

    async def execute(self, _statement, _params=None):
        self.statements.append(_statement)
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


class OAuthStateRedis:
    def __init__(self, state_token: str, payload: dict):
        self.key = f"{auth_api._OAUTH_STATE_PREFIX}{state_token}"
        self.values = {self.key: json.dumps(payload, separators=(",", ":"))}

    async def get(self, key: str):
        await asyncio.sleep(0)
        return self.values.get(key)

    async def eval(self, _script: str, _numkeys: int, key: str, raw: str):
        if self.values.get(key) != raw:
            return 0
        del self.values[key]
        return 1

def _make_identity(
    *,
    email="test@example.com",
    username="testuser",
    password="correctpassword",
    password_login_enabled=True,
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
        password_login_enabled=password_login_enabled,
        auth_version=0,
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


def _request(host: str = "203.0.113.10"):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers={},
        cookies={},
        url=SimpleNamespace(scheme="http"),
    )


@pytest.fixture(autouse=True)
def _stub_auth_rate_limit(monkeypatch):
    """Existing auth behavior tests do not require a live Redis instance."""
    monkeypatch.setattr(auth_api, "enforce_auth_rate_limit", AsyncMock())
    monkeypatch.setattr(
        auth_api,
        "validate_identity_login_namespace",
        AsyncMock(),
    )
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_username",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_phone",
        AsyncMock(return_value=None),
    )


@pytest.mark.parametrize(
    ("environment", "enabled", "expected"),
    [
        ("development", True, True),
        ("test", True, True),
        ("production", True, False),
        ("prod", True, False),
        ("development", False, False),
    ],
)
def test_unverified_local_signup_requires_explicit_non_production_opt_in(
    environment,
    enabled,
    expected,
):
    settings = SimpleNamespace(
        ENVIRONMENT=environment,
        ALLOW_UNVERIFIED_LOCAL_SIGNUP=enabled,
    )

    assert unverified_local_signup_allowed(settings) is expected


@pytest.mark.asyncio
async def test_signup_organization_invitation_stays_pending_and_never_promotes():
    tenant_id = uuid.uuid4()
    db = RecordingDB()

    resolved_tenant, role = await auth_api._resolve_signup_tenant(
        db,
        SimpleNamespace(tenant_id=tenant_id),
    )

    assert resolved_tenant is None
    assert role == "member"
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["", " ", "\t\n"])
async def test_identity_login_lookup_rejects_blank_identifier_without_query(
    monkeypatch,
    identifier,
):
    dao = IdentityDAO()

    @asynccontextmanager
    async def forbidden_session():
        raise AssertionError("blank login identifiers must not query nullable identities")
        yield

    monkeypatch.setattr(dao, "session", forbidden_session)

    assert await dao.get_by_login_identifier(identifier) is None


@pytest.mark.asyncio
async def test_user_email_lookup_rejects_blank_email_without_query(monkeypatch):
    dao = UserDAO()

    @asynccontextmanager
    async def forbidden_session():
        raise AssertionError("blank emails must not query nullable identities")
        yield

    monkeypatch.setattr(dao, "session", forbidden_session)

    assert await dao.get_by_email_and_tenant("  ", uuid.uuid4()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_verified", "service_active", "email_config"),
    [
        (True, True, None),
        (False, False, SimpleNamespace(smtp_host="smtp.example.com")),
    ],
    ids=["verified-local-policy", "smtp-verification-required"],
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
        password_hash=hash_password("correctpassword"),
        password_login_enabled=True,
        auth_version=0,
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
        activation_pending_email_verification=not service_active,
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
    monkeypatch.setattr(
        auth_api,
        "create_access_token",
        lambda *_args, **_kwargs: "access-token",
    )
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
    with patch.object(
        auth_api,
        "_resolve_password_registration_email_config",
        new=AsyncMock(return_value=email_config),
    ), patch(
        "app.services.registration_service.registration_service",
        new=registration_service,
    ):
        result = await auth_api.register_init(data, AsyncMock(), _request())

    assert result.access_token == "access-token"
    assert user.is_active is service_active
    assert registration_service.find_or_create_identity.await_args.kwargs["email_config"] is email_config
    assert registration_service.create_user_with_identity.await_args.kwargs["email_config"] is email_config


@pytest.mark.asyncio
async def test_register_init_rejects_sso_identity_created_after_preflight(monkeypatch):
    """A concurrent SSO create cannot be converted into an authenticated Web user."""
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="race@example.com",
        username="race",
        password_hash=None,
        password_login_enabled=False,
        email_verified=False,
        is_platform_admin=False,
    )
    db = RecordingDB()

    @asynccontextmanager
    async def registration_transaction():
        yield db

    registration_service = SimpleNamespace(
        find_or_create_identity=AsyncMock(return_value=identity),
        create_user_with_identity=AsyncMock(),
    )
    token_factory = AsyncMock()
    monkeypatch.setattr(auth_api, "transaction", registration_transaction)
    monkeypatch.setattr(auth_api, "hash_password_async", AsyncMock(return_value="attacker-hash"))
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api, "_prepare_signup_code_if_required", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api, "create_access_token", token_factory)
    data = SimpleNamespace(
        email=identity.email,
        username="attacker",
        password="attacker-password",
        display_name="Attacker",
        invitation_code="REGISTER-CODE",
        target_tenant_id=None,
    )

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=AsyncMock(return_value=SimpleNamespace(smtp_host="smtp.example.com")),
    ), patch(
        "app.services.registration_service.registration_service",
        new=registration_service,
    ), pytest.raises(HTTPException) as exc:
        await auth_api.register_init(data, AsyncMock(), _request())

    assert exc.value.status_code == 400
    registration_service.create_user_with_identity.assert_not_awaited()
    token_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_register_is_an_exact_deprecated_delegate(monkeypatch):
    canonical_result = SimpleNamespace(access_token="canonical-token")
    canonical_register = AsyncMock(return_value=canonical_result)
    monkeypatch.setattr(auth_api, "register_init", canonical_register)
    response = Response()
    request = _request()
    background_tasks = AsyncMock()

    result = await auth_api.register(
        SimpleNamespace(
            email="legacy@example.com",
            username="legacy-user",
            password="correct-password",
            display_name="Legacy User",
            invitation_code="REGISTER-CODE",
            provider=None,
            provider_code=None,
        ),
        background_tasks,
        request,
        response,
    )

    assert result is canonical_result
    delegated = canonical_register.await_args.args[0]
    assert delegated.model_dump() == {
        "username": "legacy-user",
        "email": "legacy@example.com",
        "password": "correct-password",
        "display_name": "Legacy User",
        "invitation_code": "REGISTER-CODE",
        "target_tenant_id": None,
    }
    assert canonical_register.await_args.args[1:] == (background_tasks, request)
    assert response.headers["deprecation"] == "true"
    assert response.headers["sunset"] == "Mon, 16 Nov 2026 00:00:00 GMT"
    assert response.headers["link"] == '</api/auth/register/init>; rel="successor-version"'


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
        await auth_api.register_init(
            SimpleNamespace(
                email="blocked@example.com",
                username="blocked",
                password="not-hashed",
                display_name="Blocked",
                invitation_code="REGISTER-CODE",
                target_tenant_id=None,
            ),
            AsyncMock(),
            _request(),
        )

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
async def test_retired_public_social_registration_fails_without_side_effects(
    monkeypatch,
    legacy_entrypoint,
):
    resolver = AsyncMock(side_effect=AssertionError("SSO must not read SMTP policy"))
    provider_lookup = AsyncMock(return_value=None)
    limiter = AsyncMock()

    with patch(
        "app.services.system_email_service.resolve_email_config_async",
        new=resolver,
    ), patch(
        "app.services.auth_registry.auth_provider_registry.get_provider",
        new=provider_lookup,
    ), patch(
        "app.api.auth.enforce_auth_rate_limit",
        new=limiter,
    ), pytest.raises(HTTPException) as exc:
        if legacy_entrypoint:
            await auth_api.register(
                SimpleNamespace(
                    provider="google",
                    provider_code="provider-code",
                    invitation_code=None,
                ),
                AsyncMock(),
                _request(),
                Response(),
            )
        else:
            await auth_api.register_sso(
                SimpleNamespace(
                    provider="google",
                    code="provider-code",
                    invitation_code=None,
                ),
                _request(),
            )

    assert exc.value.status_code == 410
    resolver.assert_not_awaited()
    provider_lookup.assert_not_awaited()
    limiter.assert_not_awaited()


@pytest.mark.asyncio
async def test_sso_service_does_not_resolve_email_policy(monkeypatch):
    service = registration_service_module.RegistrationService()
    provider_user = SimpleNamespace(
        name="SSO User",
        email="sso@example.com",
        avatar_url=None,
        mobile=None,
        raw_data={},
        provider_union_id="provider-union-id",
        provider_user_id="provider-user-id",
    )
    provider_record = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider_type="google",
        config={},
    )

    class Provider:
        def __init__(self, provider, config):
            self.provider = provider
            self.config = config

        def _identity_payload(self, _user_info):
            return {"raw_data": {"sub": "provider-user-id"}}

    provider = Provider(provider_record, {})
    identity = _make_identity(email_verified=True)
    user = _make_user(identity.id)
    user.identity = identity
    user.tenant_id = None
    resolver = AsyncMock(side_effect=AssertionError("SSO must not read SMTP policy"))
    monkeypatch.setattr(registration_service_module, "resolve_email_config_async", resolver)
    monkeypatch.setattr(
        registration_service_module,
        "get_login_identity_provider_by_id",
        AsyncMock(return_value=provider_record),
    )
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "resolve_user_identity",
        AsyncMock(return_value=user),
    )

    result = await service.register_with_sso(
        RecordingDB(),
        "google",
        provider,
        provider_user,
        membership_tenant_id=None,
        membership_role="member",
        signup_capacity_available=True,
    )

    assert result == (user, False, None)
    resolver.assert_not_awaited()


@pytest.mark.asyncio
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
        await run_with_db(db, auth_api.login, data, bg, _request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_invalid_credentials_wrong_password():
    """Login with wrong password returns 401."""
    identity = _make_identity(password="correctpassword")
    db = RecordingDB(responses=[DummyResult(values=[identity])])
    data = _make_login_data(password="wrongpassword")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, data, bg, _request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_disabled_sso_derived_password_before_bcrypt():
    """A historical provider ID hash must never reach password verification."""
    identity = _make_identity(
        password="public-provider-id",
        password_login_enabled=False,
        email_verified=False,
    )
    verifier = AsyncMock(side_effect=AssertionError("disabled password was verified"))

    with patch.object(auth_api, "verify_password_async", verifier), pytest.raises(
        HTTPException
    ) as exc:
        await run_with_db(
            RecordingDB(responses=[DummyResult(values=[identity])]),
            auth_api.login,
            _make_login_data(password="public-provider-id"),
            AsyncMock(),
            _request(),
        )

    assert exc.value.status_code == 401
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_reset_reenables_sso_password_and_login(monkeypatch):
    identity = _make_identity(
        password="public-provider-id",
        password_login_enabled=False,
        email_verified=True,
    )
    identity.is_platform_admin = False
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        tenant_id=None,
        role="member",
        is_active=True,
        activation_pending_email_verification=False,
    )

    @asynccontextmanager
    async def auth_transaction():
        yield RecordingDB()

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        "app.services.password_reset_service.consume_password_reset_token",
        AsyncMock(
            return_value={
                "identity_id": identity.id,
                "email": identity.email,
                "auth_version": identity.auth_version,
            }
        ),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    membership_lookup = AsyncMock(return_value=[user])
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_id",
        membership_lookup,
    )

    reset = await auth_api.reset_password(
        SimpleNamespace(token="t" * 20, new_password="new-local-password")
    )

    assert reset == {"ok": True}
    assert identity.password_login_enabled is True
    assert identity.email_verified is True
    assert user.is_active is True
    membership_lookup.assert_not_awaited()

    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_login_identifier",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        auth_api,
        "create_access_token",
        lambda *_args, **_kwargs: "reset-login-token",
    )
    monkeypatch.setattr(auth_api, "serialize_user", lambda value: {"id": str(value.id)})
    monkeypatch.setattr(
        auth_api,
        "TokenResponse",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        auth_api.IdentityOut,
        "model_validate",
        lambda _value: {"id": str(identity.id)},
    )

    result = await auth_api.login(
        _make_login_data(password="new-local-password"),
        AsyncMock(),
        _request(),
    )

    assert result.access_token == "reset-login-token"


@pytest.mark.asyncio
async def test_password_reset_does_not_reactivate_manually_disabled_membership(monkeypatch):
    identity = _make_identity(password_login_enabled=False, email_verified=True)
    membership = SimpleNamespace(is_active=False)

    @asynccontextmanager
    async def auth_transaction():
        yield RecordingDB()

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        "app.services.password_reset_service.consume_password_reset_token",
        AsyncMock(
            return_value={
                "identity_id": identity.id,
                "email": identity.email,
                "auth_version": identity.auth_version,
            }
        ),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    membership_lookup = AsyncMock(return_value=[membership])
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", membership_lookup)

    result = await auth_api.reset_password(
        SimpleNamespace(token="t" * 20, new_password="new-local-password")
    )

    assert result == {"ok": True}
    assert identity.password_login_enabled is True
    assert membership.is_active is False
    membership_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_reset_does_not_complete_email_verification_or_activate_membership(
    monkeypatch,
):
    identity = _make_identity(password_login_enabled=False, email_verified=False)
    pending_member = SimpleNamespace(
        role="member",
        is_active=False,
        activation_pending_email_verification=True,
    )
    disabled_admin = SimpleNamespace(
        role="org_admin",
        is_active=False,
        activation_pending_email_verification=False,
    )
    active_member = SimpleNamespace(
        role="member",
        is_active=True,
        activation_pending_email_verification=False,
    )

    @asynccontextmanager
    async def auth_transaction():
        yield RecordingDB()

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        "app.services.password_reset_service.consume_password_reset_token",
        AsyncMock(
            return_value={
                "identity_id": identity.id,
                "email": identity.email,
                "auth_version": identity.auth_version,
            }
        ),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    membership_lookup = AsyncMock(
        return_value=[pending_member, disabled_admin, active_member]
    )
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_id",
        membership_lookup,
    )

    with pytest.raises(HTTPException) as exc:
        await auth_api.reset_password(
            SimpleNamespace(token="t" * 20, new_password="new-local-password")
        )

    assert exc.value.status_code == 400
    assert identity.email_verified is False
    assert identity.password_login_enabled is False
    assert pending_member.is_active is False
    assert pending_member.activation_pending_email_verification is True
    assert disabled_admin.is_active is False
    assert active_member.is_active is True
    membership_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_email_activates_only_explicitly_pending_membership(monkeypatch):
    from app.services.email_verification_service import email_verification_service

    identity = _make_identity(email_verified=False)
    pending_member = SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        tenant_id=uuid.uuid4(),
        is_active=False,
        activation_pending_email_verification=True,
    )
    disabled_admin = SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=pending_member.tenant_id,
        is_active=False,
        activation_pending_email_verification=False,
    )
    db = RecordingDB()

    @asynccontextmanager
    async def auth_transaction():
        yield db

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": identity.id, "email": identity.email}),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_id",
        AsyncMock(return_value=[pending_member, disabled_admin]),
    )
    legacy_representative_lookup = AsyncMock(return_value=disabled_admin)
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_representative_user_for_identity",
        legacy_representative_lookup,
    )
    create_token = Mock(return_value="verified-token")
    monkeypatch.setattr(auth_api, "create_access_token", create_token)
    monkeypatch.setattr(auth_api, "serialize_user", lambda value: {"id": str(value.id)})
    monkeypatch.setattr(
        auth_api.IdentityOut,
        "model_validate",
        lambda value: {"id": str(value.id)},
    )
    monkeypatch.setattr(
        auth_api,
        "TokenResponse",
        lambda **values: SimpleNamespace(**values),
    )

    result = await auth_api.verify_email(SimpleNamespace(token="secure-token"))

    assert result.access_token == "verified-token"
    assert pending_member.is_active is True
    assert pending_member.activation_pending_email_verification is False
    assert disabled_admin.is_active is False
    create_token.assert_called_once_with(
        str(pending_member.id),
        "member",
        auth_version=identity.auth_version,
    )
    legacy_representative_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_email_cannot_reactivate_disabled_identity(monkeypatch):
    from app.services.email_verification_service import email_verification_service

    identity = _make_identity(is_active=False, email_verified=False)
    db = RecordingDB()

    @asynccontextmanager
    async def auth_transaction():
        yield db

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": identity.id, "email": identity.email}),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    membership_lookup = AsyncMock()
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", membership_lookup)
    create_token = Mock()
    monkeypatch.setattr(auth_api, "create_access_token", create_token)

    with pytest.raises(HTTPException) as exc:
        await auth_api.verify_email(SimpleNamespace(token="secure-token"))

    assert exc.value.status_code == 400
    assert identity.is_active is False
    assert identity.email_verified is False
    membership_lookup.assert_not_awaited()
    create_token.assert_not_called()


@pytest.mark.asyncio
async def test_verify_email_rejects_token_issued_to_previous_email(monkeypatch):
    from app.services.email_verification_service import email_verification_service

    identity = _make_identity(email="new@example.com", email_verified=False)
    db = RecordingDB()

    @asynccontextmanager
    async def auth_transaction():
        yield db

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": identity.id, "email": "old@example.com"}),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    membership_lookup = AsyncMock()
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", membership_lookup)
    create_token = Mock()
    monkeypatch.setattr(auth_api, "create_access_token", create_token)

    with pytest.raises(HTTPException) as exc:
        await auth_api.verify_email(SimpleNamespace(token="secure-token"))

    assert exc.value.status_code == 400
    assert identity.email_verified is False
    membership_lookup.assert_not_awaited()
    create_token.assert_not_called()


@pytest.mark.asyncio
async def test_verify_email_without_active_membership_does_not_issue_user_token(monkeypatch):
    from app.services.email_verification_service import email_verification_service

    identity = _make_identity(email_verified=False)
    disabled_member = SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        tenant_id=uuid.uuid4(),
        is_active=False,
        activation_pending_email_verification=False,
    )
    db = RecordingDB()

    @asynccontextmanager
    async def auth_transaction():
        yield db

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": identity.id, "email": identity.email}),
    )
    monkeypatch.setattr(auth_api.identity_dao, "get_for_update", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_id",
        AsyncMock(return_value=[disabled_member]),
    )
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_active_representative_user_for_identity",
        AsyncMock(return_value=None),
    )
    create_token = Mock()
    monkeypatch.setattr(auth_api, "create_access_token", create_token)

    with pytest.raises(HTTPException) as exc:
        await auth_api.verify_email(SimpleNamespace(token="secure-token"))

    assert exc.value.status_code == 403
    assert identity.email_verified is True
    assert disabled_member.is_active is False
    create_token.assert_not_called()


@pytest.mark.asyncio
async def test_self_email_change_revokes_verification_and_old_token(monkeypatch):
    from app.services.email_verification_service import email_verification_service
    from app.services import password_reset_service
    from app.services.registration_service import registration_service

    identity = _make_identity(email="old@example.com", email_verified=True)
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        tenant_id=uuid.uuid4(),
        role="member",
        email="old@example.com",
        primary_mobile=None,
    )
    db = RecordingDB()

    @asynccontextmanager
    async def auth_transaction():
        yield db

    monkeypatch.setattr(auth_api, "transaction", auth_transaction)
    monkeypatch.setattr(auth_api.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_for_update",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        auth_api.identity_dao,
        "get_by_email",
        AsyncMock(return_value=None),
    )
    invalidate_email = AsyncMock()
    invalidate_reset = AsyncMock()
    monkeypatch.setattr(
        email_verification_service,
        "invalidate_email_verification_tokens",
        invalidate_email,
    )
    monkeypatch.setattr(
        password_reset_service,
        "invalidate_password_reset_tokens",
        invalidate_reset,
    )
    monkeypatch.setattr(
        registration_service,
        "sync_org_member_contact_from_user",
        AsyncMock(),
    )
    monkeypatch.setattr(auth_api, "serialize_user", lambda value: value)

    response = Response()
    result = await auth_api.update_me(
        auth_api.SelfUserUpdate(
            email="new@example.com",
            current_password="correctpassword",
        ),
        _request(),
        response,
        current_user=SimpleNamespace(id=user.id),
    )

    assert result is user
    assert identity.email == "new@example.com"
    assert identity.email_verified is False
    assert identity.auth_version == 1
    assert response.headers["X-Astra-Access-Token"]
    invalidate_email.assert_awaited_once_with(identity.id)
    invalidate_reset.assert_awaited_once_with(identity.id)


@pytest.mark.asyncio
async def test_login_disabled_account():
    """Login with a disabled account returns 403."""
    identity = _make_identity(is_active=False)
    db = RecordingDB(responses=[DummyResult(values=[identity])])
    data = _make_login_data()
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await run_with_db(db, auth_api.login, data, bg, _request())
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
                await run_with_db(db, auth_api.login, data, bg, _request())
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
        await run_with_db(
            db,
            auth_api.login,
            _make_login_data(),
            AsyncMock(),
            _request(),
        )

    assert exc.value.status_code == 503
    assert identity.email_verified is False
    resolver.assert_awaited_once_with(raise_on_error=True)


# ---------------------------------------------------------------------------
# Registration code gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email_config", "local_opt_in", "expected"),
    [
        (SimpleNamespace(smtp_host="smtp.example.com"), False, True),
        (None, True, True),
        (None, False, False),
    ],
)
async def test_password_registration_availability_mirrors_fail_closed_policy(
    monkeypatch,
    email_config,
    local_opt_in,
    expected,
):
    monkeypatch.setattr(
        auth_api,
        "_resolve_auth_email_config",
        AsyncMock(return_value=email_config),
    )
    monkeypatch.setattr(
        "app.config.unverified_local_signup_allowed",
        lambda: local_opt_in,
    )

    assert await auth_api._password_registration_available() is expected


@pytest.mark.asyncio
async def test_registration_config_allows_bootstrap_without_code(monkeypatch):
    is_empty = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", is_empty)
    availability = AsyncMock(return_value=True)
    monkeypatch.setattr(auth_api, "_password_registration_available", availability)

    result = await auth_api.get_registration_config()

    assert result == {
        "invitation_code_required": False,
        "password_registration_available": True,
    }
    is_empty.assert_awaited_once_with()
    availability.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_registration_config_requires_code_after_bootstrap(monkeypatch):
    is_empty = AsyncMock(return_value=False)
    monkeypatch.setattr(auth_api.identity_dao, "is_empty", is_empty)
    availability = AsyncMock(return_value=False)
    monkeypatch.setattr(auth_api, "_password_registration_available", availability)

    result = await auth_api.get_registration_config()

    assert result == {
        "invitation_code_required": True,
        "password_registration_available": False,
    }
    is_empty.assert_awaited_once_with()
    availability.assert_awaited_once_with()


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

    monkeypatch.setattr(
        "app.services.identity_governance.resolve_registration_grant",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.identity_governance.resolve_organization_credential",
        AsyncMock(return_value=None),
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
    from app.services.identity_governance import GovernanceCredentialError

    monkeypatch.setattr(
        "app.services.identity_governance.resolve_registration_grant",
        AsyncMock(
            side_effect=GovernanceCredentialError(
                "registration_grant_exhausted",
                "Registration grant has reached its usage limit",
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await auth_api._prepare_signup_code_if_required(
            RecordingDB(),
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
    code = SimpleNamespace(status="active", used_count=0, max_uses=2)
    monkeypatch.setattr(
        "app.services.identity_governance.resolve_registration_grant",
        AsyncMock(return_value=code),
    )

    result = await auth_api._prepare_signup_code_if_required(
        RecordingDB(),
        " open123 ",
        is_first_user=False,
    )
    auth_api._consume_signup_code_if_needed(result)

    assert result.record is code
    assert result.kind == "registration_grant"
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
async def test_oauth_callback_passes_redirect_uri_after_browser_bound_state(monkeypatch):
    """OAuth callback forwards redirect_uri only after browser-bound CSRF passes."""
    provider_record = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider_type="google",
        config={},
    )
    provider = SimpleNamespace(provider=provider_record)
    provider.exchange_code_for_token = AsyncMock(return_value={"access_token": "provider-token"})
    provider.get_user_info = AsyncMock(
        return_value=SimpleNamespace(
            provider_user_id="google-subject",
            provider_union_id=None,
            email="linked@example.com",
        )
    )
    provider._identity_payload = Mock(return_value={"raw_data": {"sub": "google-subject"}})
    data = SimpleNamespace(
        code="oauth-code",
        state="oauth-state",
        redirect_uri="https://example.com/oauth/callback/google",
        pending_token=None,
        tenant_id=None,
    )
    request = SimpleNamespace(cookies={auth_api._OAUTH_BROWSER_NONCE_COOKIE: "browser-nonce"})

    @asynccontextmanager
    async def oauth_transaction():
        yield RecordingDB()

    monkeypatch.setattr(auth_api, "transaction", oauth_transaction)
    consume_state = AsyncMock(
        return_value={
            "provider_type": "google",
            "redirect_uri": data.redirect_uri,
            "browser_nonce": "browser-nonce",
        }
    )
    monkeypatch.setattr(
        auth_api,
        "_consume_oauth_state",
        consume_state,
    )

    with patch(
        "app.services.auth_registry.auth_provider_registry.get_provider",
        new=AsyncMock(return_value=provider),
    ), patch(
        "app.services.identity_provider_lookup.get_login_identity_provider_by_id",
        new=AsyncMock(return_value=provider_record),
    ), patch(
        "app.services.external_identity_policy.acquire_external_subject_lock",
        new=AsyncMock(),
    ), patch(
        "app.services.sso_service.sso_service.resolve_user_identity",
        new=AsyncMock(return_value=None),
    ), pytest.raises(HTTPException) as exc:
        await auth_api.oauth_callback("google", data, request)

    provider.exchange_code_for_token.assert_awaited_once_with("oauth-code", "https://example.com/oauth/callback/google")
    consume_state.assert_awaited_once_with(
        "oauth-state",
        provider_type="google",
        redirect_uri="https://example.com/oauth/callback/google",
        browser_nonce="browser-nonce",
    )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_oauth_authorize_stores_only_browser_nonce_hash(monkeypatch):
    provider = SimpleNamespace(
        get_authorization_url=AsyncMock(
            return_value="https://accounts.example/authorize",
        )
    )
    cache_state = AsyncMock()
    monkeypatch.setattr(auth_api, "_cache_oauth_state", cache_state)
    monkeypatch.setattr(auth_api, "_request_is_secure", lambda _request: True)

    with patch(
        "app.services.auth_registry.auth_provider_registry.get_provider",
        new=AsyncMock(return_value=provider),
    ), patch.object(
        auth_api.secrets,
        "token_urlsafe",
        side_effect=["state-token", "browser-nonce"],
    ):
        result = await auth_api.authorize(
            "google",
            SimpleNamespace(cookies={}),
            Response(),
            "https://astra.example/oauth/callback/google",
        )

    assert result.authorization_url == "https://accounts.example/authorize"
    cache_state.assert_awaited_once()
    state_token, payload = cache_state.await_args.args
    assert state_token == "state-token"
    assert "browser_nonce" not in payload
    assert payload["browser_nonce_hash"] == hashlib.sha256(
        b"browser-nonce"
    ).hexdigest()


@pytest.mark.asyncio
async def test_oauth_callback_fails_before_provider_lookup_when_state_is_invalid():
    provider_lookup = AsyncMock()
    data = SimpleNamespace(
        code="oauth-code",
        state="oauth-state",
        redirect_uri="https://example.com/oauth/callback/github",
        pending_token=None,
        tenant_id=None,
    )
    request = SimpleNamespace(cookies={auth_api._OAUTH_BROWSER_NONCE_COOKIE: "browser-nonce"})

    with patch.object(
        auth_api,
        "_consume_oauth_state",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.auth_registry.auth_provider_registry.get_provider",
        new=provider_lookup,
    ), pytest.raises(HTTPException) as exc:
        await auth_api.oauth_callback("github", data, request)

    assert exc.value.status_code == 400
    provider_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_redirect_uri_before_state_consumption(
    monkeypatch,
):
    consume_state = AsyncMock()
    monkeypatch.setattr(auth_api, "_consume_oauth_state", consume_state)
    data = SimpleNamespace(
        code="oauth-code",
        state="oauth-state",
        redirect_uri=None,
        pending_token=None,
        tenant_id=None,
    )

    with pytest.raises(HTTPException) as exc:
        await auth_api.oauth_callback(
            "google",
            data,
            SimpleNamespace(cookies={}),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing OAuth callback parameters"
    consume_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_oauth_callback_binding_does_not_burn_state():
    state_token = "browser-bound-state"
    browser_nonce = "correct-browser-nonce"
    redirect_uri = "https://astra.example/oauth/callback/google"
    payload = {
        "provider_type": "google",
        "redirect_uri": redirect_uri,
        "browser_nonce_hash": hashlib.sha256(
            browser_nonce.encode("utf-8")
        ).hexdigest(),
    }
    redis = OAuthStateRedis(state_token, payload)

    with patch(
        "app.core.events.get_redis",
        new=AsyncMock(return_value=redis),
    ):
        invalid_bindings = (
            {
                "provider_type": "github",
                "redirect_uri": redirect_uri,
                "browser_nonce": browser_nonce,
            },
            {
                "provider_type": "google",
                "redirect_uri": "https://attacker.example/callback",
                "browser_nonce": browser_nonce,
            },
            {
                "provider_type": "google",
                "redirect_uri": redirect_uri,
                "browser_nonce": "wrong-browser-nonce",
            },
        )
        for binding in invalid_bindings:
            assert (
                await auth_api._consume_oauth_state(state_token, **binding)
                is None
            )
            assert redis.key in redis.values

        consumed = await auth_api._consume_oauth_state(
            state_token,
            provider_type="google",
            redirect_uri=redirect_uri,
            browser_nonce=browser_nonce,
        )

    assert consumed == payload
    assert redis.key not in redis.values


@pytest.mark.asyncio
async def test_concurrent_valid_oauth_callbacks_consume_state_exactly_once():
    state_token = "concurrent-state"
    browser_nonce = "browser-nonce"
    redirect_uri = "https://astra.example/oauth/callback/github"
    payload = {
        "provider_type": "github",
        "redirect_uri": redirect_uri,
        "browser_nonce_hash": hashlib.sha256(
            browser_nonce.encode("utf-8")
        ).hexdigest(),
    }
    redis = OAuthStateRedis(state_token, payload)

    async def consume():
        return await auth_api._consume_oauth_state(
            state_token,
            provider_type="github",
            redirect_uri=redirect_uri,
            browser_nonce=browser_nonce,
        )

    with patch(
        "app.core.events.get_redis",
        new=AsyncMock(return_value=redis),
    ):
        results = await asyncio.gather(consume(), consume())

    assert sum(result == payload for result in results) == 1
    assert sum(result is None for result in results) == 1


@pytest.mark.asyncio
async def test_oauth_pending_tenant_selection_is_bound_to_initiating_browser(monkeypatch):
    consume = AsyncMock(return_value={"memberships": []})
    monkeypatch.setattr(auth_api, "_consume_oauth_payload", consume)

    assert await auth_api._get_oauth_pending("pending", "") is None
    consume.assert_not_awaited()

    result = await auth_api._get_oauth_pending("pending", "browser-nonce")

    assert result == {"memberships": []}
    key = consume.await_args.args[1]
    assert key.startswith("pending:")
    assert "browser-nonce" not in key
