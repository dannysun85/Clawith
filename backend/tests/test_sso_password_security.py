"""Regression coverage for SSO/local-password credential separation."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException, Request
import pytest

from app.api import dingtalk as dingtalk_api
from app.api import feishu as feishu_api
from app.api import google_workspace as google_api
from app.api import wecom as wecom_api
from app.models.user import Identity
from app.services import registration_service as registration_service_module
from app.services import sso_scan_session_service as session_service
from app.services.auth_provider import ExternalUserInfo, GitHubAuthProvider
from app.services.google_workspace_oauth import (
    GOOGLE_SYNC_BROWSER_NONCE_COOKIE,
    GOOGLE_SYNC_STATE_PREFIX,
    sign_google_sso_state,
)


ROOT = Path(__file__).parents[1]


class _ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.added = []

    async def execute(self, _statement):
        return _ScalarResult(self.values.pop(0) if self.values else None)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _callback_request(
    sid: uuid.UUID,
    nonce: str | None,
    *,
    cookie_sid: uuid.UUID | None = None,
) -> Request:
    headers = [(b"host", b"astra.example")]
    if nonce is not None:
        name = session_service.sso_initiator_cookie_name(cookie_sid or sid)
        headers.append((b"cookie", f"{name}={nonce}".encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/api/auth/provider/callback",
            "raw_path": b"/api/auth/provider/callback",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.20", 443),
            "server": ("astra.example", 443),
        }
    )


def _google_callback_request(browser_nonce: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": google_api.GOOGLE_CALLBACK_PATH,
            "raw_path": google_api.GOOGLE_CALLBACK_PATH.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"host", b"astra.example"),
                (
                    b"cookie",
                    f"{GOOGLE_SYNC_BROWSER_NONCE_COOKIE}={browser_nonce}".encode("ascii"),
                ),
            ],
            "client": ("203.0.113.20", 443),
            "server": ("astra.example", 443),
        }
    )


class _GoogleSyncStateRedis:
    def __init__(self, state: str, payload: dict):
        self.key = f"{GOOGLE_SYNC_STATE_PREFIX}{state}"
        self.values = {self.key: json.dumps(payload, separators=(",", ":"))}

    async def get(self, key: str):
        return self.values.get(key)

    async def eval(self, _script: str, _numkeys: int, key: str, raw: str):
        if self.values.get(key) != raw:
            return 0
        del self.values[key]
        return 1


@pytest.mark.asyncio
async def test_base_oauth_user_creation_never_passes_provider_id_as_password(monkeypatch):
    provider = GitHubAuthProvider(config={})
    identity = Identity(
        id=uuid.uuid4(),
        email=None,
        username="github_isolated",
        password_hash=None,
        password_login_enabled=False,
        email_verified=False,
    )
    monkeypatch.setattr(
        "app.services.auth_provider.create_isolated_external_identity",
        AsyncMock(return_value=identity),
    )

    user = await provider._create_new_user(
        _DB(),
        ExternalUserInfo(
            provider_type="github",
            provider_user_id="12345678",
            name="Linked User",
            email=identity.email,
            raw_data={"id": 12345678},
        ),
        None,
    )

    assert user.identity is identity
    assert user.identity.email is None
    assert user.identity.password_hash is None
    assert user.identity.password_login_enabled is False


@pytest.mark.asyncio
async def test_existing_legacy_oauth_user_gets_isolated_identity_without_email_merge(monkeypatch):
    provider = GitHubAuthProvider(config={})
    legacy_user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=None,
        tenant_id=None,
        is_active=True,
    )
    identity = SimpleNamespace(id=uuid.uuid4())
    identity.email = None
    identity.password_hash = None
    identity.password_login_enabled = False

    monkeypatch.setattr(provider, "_ensure_provider", AsyncMock())
    monkeypatch.setattr(provider, "_update_existing_user", AsyncMock())
    monkeypatch.setattr(
        "app.services.auth_provider.create_isolated_external_identity",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        registration_service_module.registration_service,
        "ensure_web_org_member",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.sso_service.sso_service.resolve_user_identity",
        AsyncMock(return_value=legacy_user),
    )
    monkeypatch.setattr(
        "app.services.sso_service.sso_service.link_identity",
        AsyncMock(),
    )

    result, is_new = await provider.find_or_create_user(
        _DB(),
        ExternalUserInfo(
            provider_type="github",
            provider_user_id="12345678",
            name="Legacy User",
            email="legacy@example.com",
            raw_data={"id": 12345678},
        ),
    )

    assert result is legacy_user
    assert is_new is False
    assert legacy_user.identity_id == identity.id
    assert legacy_user.identity is identity
    assert identity.email is None
    assert identity.password_hash is None


@pytest.mark.asyncio
async def test_register_sso_user_creation_never_passes_provider_id_as_password(monkeypatch):
    service = registration_service_module.RegistrationService()
    identity = Identity(
        id=uuid.uuid4(),
        email="sso@example.com",
        username="sso",
        password_hash=None,
        password_login_enabled=False,
        email_verified=True,
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=None)
    tenant_id = uuid.uuid4()
    provider_record = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)

    @asynccontextmanager
    async def identity_session():
        yield _DB()

    monkeypatch.setattr(
        registration_service_module,
        "get_login_identity_provider",
        AsyncMock(return_value=provider_record),
    )
    monkeypatch.setattr(
        registration_service_module,
        "create_isolated_external_identity",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(service, "create_user_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(registration_service_module.identity_dao, "session", identity_session)
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "resolve_user_identity",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "find_identity_member",
        AsyncMock(return_value=SimpleNamespace(status="active")),
    )
    monkeypatch.setattr(
        registration_service_module.sso_service,
        "link_identity",
        AsyncMock(),
    )

    result, is_new = await service.handle_sso_registration(
        "github",
        "12345678",
        {"email": "claimed@example.com", "name": "SSO User", "tenant_id": tenant_id},
    )

    assert result is user
    assert is_new is True
    kwargs = service.create_user_with_identity.await_args.kwargs
    assert kwargs["identity"] is identity
    assert kwargs["require_email_verification_for_activation"] is False


def test_sso_services_have_no_provider_identifier_password_keyword():
    forbidden = {
        "app/services/registration_service.py": "password=effective_id",
        "app/services/auth_provider.py": "password=effective_id",
        "app/services/feishu_service.py": "password=open_id",
    }

    for relative_path, expression in forbidden.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8").replace(" ", "")
        assert expression not in source


def test_password_capability_backfill_does_not_treat_platform_membership_as_sso():
    migration = (
        ROOT / "alembic/versions/106_secure_sso_password_login.py"
    ).read_text(encoding="utf-8")

    assert migration.count("NOT IN ('web', 'platform')") == 2
    assert "LEFT JOIN identity_providers AS ip ON ip.id = om.provider_id" in migration


@pytest.mark.parametrize(
    "module",
    [feishu_api, dingtalk_api, wecom_api, google_api],
)
def test_dedicated_oauth_callbacks_do_not_depend_on_smtp(module):
    assert not hasattr(module, "resolve_email_config_async")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["dingtalk", "wecom", "google_workspace"])
async def test_dedicated_oauth_callbacks_do_not_echo_provider_exceptions(
    monkeypatch,
    provider,
):
    secret = "provider-client-secret-sentinel"
    failing_provider = SimpleNamespace(
        exchange_code_for_token=AsyncMock(side_effect=RuntimeError(secret)),
        config={},
    )
    fake_logger = SimpleNamespace(error=Mock(), warning=Mock())
    sid = uuid.uuid4()
    provider_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    initiator_nonce = "initiator-browser-secret"
    provider_record = SimpleNamespace(
        id=provider_id,
        tenant_id=tenant_id,
        config={},
        is_active=True,
        sso_login_enabled=True,
    )
    scan_session = SimpleNamespace(
        id=sid,
        tenant_id=tenant_id,
        initiator_nonce_hash=session_service.hash_sso_initiator_nonce(
            initiator_nonce
        ),
    )
    request = _callback_request(sid, initiator_nonce)

    if provider == "dingtalk":
        monkeypatch.setattr(dingtalk_api, "parse_sso_scan_state", Mock(return_value=(sid, provider_id)))
        monkeypatch.setattr(dingtalk_api, "get_pending_sso_session", AsyncMock(return_value=scan_session))
        monkeypatch.setattr(dingtalk_api, "get_login_identity_provider_by_id", AsyncMock(return_value=provider_record))
        monkeypatch.setattr(dingtalk_api, "DingTalkAuthProvider", Mock(return_value=failing_provider))
        monkeypatch.setattr(dingtalk_api, "logger", fake_logger)
        response = await dingtalk_api.dingtalk_callback(
            authCode="code",
            request=request,
            state="state",
            db=_DB(),
        )
    elif provider == "wecom":
        monkeypatch.setattr(wecom_api, "parse_sso_scan_state", Mock(return_value=(sid, provider_id)))
        monkeypatch.setattr(wecom_api, "get_pending_sso_session", AsyncMock(return_value=scan_session))
        monkeypatch.setattr(wecom_api, "get_login_identity_provider_by_id", AsyncMock(return_value=provider_record))
        monkeypatch.setattr(wecom_api, "WeComAuthProvider", Mock(return_value=failing_provider))
        monkeypatch.setattr(wecom_api, "logger", fake_logger)
        response = await wecom_api.wecom_callback(
            code="code",
            request=request,
            state="state",
            db=_DB(),
        )
    else:
        monkeypatch.setattr(google_api, "get_pending_sso_session", AsyncMock(return_value=scan_session))
        monkeypatch.setattr(google_api, "get_login_identity_provider_by_id", AsyncMock(return_value=provider_record))
        monkeypatch.setattr(google_api, "get_google_redirect_uri", AsyncMock(return_value="https://example.com/google/callback"))
        monkeypatch.setattr(google_api, "GoogleWorkspaceAuthProvider", Mock(return_value=failing_provider))
        monkeypatch.setattr(google_api, "logger", fake_logger)
        response = await google_api._handle_google_sso_callback(
            "code",
            sid,
            provider_id,
            request,
            _DB(),
        )

    assert response.status_code == 400
    assert secret not in response.body.decode("utf-8")
    assert secret not in repr(fake_logger.warning.call_args_list)
    assert secret not in repr(fake_logger.error.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    ["feishu", "dingtalk", "wecom", "google_workspace"],
)
@pytest.mark.parametrize("browser_proof", ["missing", "wrong", "other_session"])
async def test_attacker_relay_cannot_be_authorized_by_victim_browser(
    monkeypatch,
    provider,
    browser_proof,
):
    sid = uuid.uuid4()
    provider_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    initiator_nonce = "attacker-browser-secret"
    scan_session = SimpleNamespace(
        id=sid,
        tenant_id=tenant_id,
        initiator_nonce_hash=session_service.hash_sso_initiator_nonce(
            initiator_nonce
        ),
    )
    if browser_proof == "missing":
        request = _callback_request(sid, None)
    elif browser_proof == "wrong":
        request = _callback_request(sid, "victim-browser-secret")
    else:
        request = _callback_request(
            sid,
            initiator_nonce,
            cookie_sid=uuid.uuid4(),
        )

    if provider == "feishu":
        module = feishu_api
        monkeypatch.setattr(
            module,
            "parse_sso_scan_state",
            Mock(return_value=(sid, provider_id)),
        )
    elif provider == "dingtalk":
        module = dingtalk_api
        monkeypatch.setattr(
            module,
            "parse_sso_scan_state",
            Mock(return_value=(sid, provider_id)),
        )
    elif provider == "wecom":
        module = wecom_api
        monkeypatch.setattr(
            module,
            "parse_sso_scan_state",
            Mock(return_value=(sid, provider_id)),
        )
    else:
        module = google_api

    provider_lookup = AsyncMock()
    authorize = AsyncMock()
    monkeypatch.setattr(
        module,
        "get_pending_sso_session",
        AsyncMock(return_value=scan_session),
    )
    monkeypatch.setattr(
        module,
        "get_login_identity_provider_by_id",
        provider_lookup,
    )
    monkeypatch.setattr(module, "authorize_sso_session", authorize)

    with pytest.raises(HTTPException) as exc:
        if provider == "feishu":
            await feishu_api.feishu_oauth_callback(
                code="code",
                request=request,
                state="state",
                db=_DB(),
            )
        elif provider == "dingtalk":
            await dingtalk_api.dingtalk_callback(
                authCode="code",
                request=request,
                state="state",
                db=_DB(),
            )
        elif provider == "wecom":
            await wecom_api.wecom_callback(
                code="code",
                request=request,
                state="state",
                db=_DB(),
            )
        else:
            await google_api._handle_google_sso_callback(
                "code",
                sid,
                provider_id,
                request,
                _DB(),
            )

    assert exc.value.status_code == 403
    provider_lookup.assert_not_awaited()
    authorize.assert_not_awaited()


def test_sso_provider_callbacks_only_accept_top_level_get():
    routes = (
        (feishu_api.router, "/auth/feishu/callback"),
        (dingtalk_api.router, "/auth/dingtalk/callback"),
        (wecom_api.router, "/auth/wecom/callback"),
        (google_api.router, google_api.GOOGLE_CALLBACK_PATH),
    )
    for router, path in routes:
        methods = {
            method
            for route in router.routes
            if route.path == path
            for method in route.methods
        }
        assert methods == {"GET"}


@pytest.mark.asyncio
async def test_google_sso_callback_does_not_depend_on_redis(monkeypatch):
    sid = uuid.uuid4()
    provider_id = uuid.uuid4()
    expected = object()
    handler = AsyncMock(return_value=expected)
    redis_consume = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(google_api, "_handle_google_sso_callback", handler)
    monkeypatch.setattr(google_api, "consume_google_sync_state", redis_consume)

    result = await google_api.google_workspace_callback(
        code="oauth-code",
        state=sign_google_sso_state(sid, provider_id),
        request=_callback_request(sid, "browser-proof"),
        db=_DB(),
    )

    assert result is expected
    handler.assert_awaited_once()
    redis_consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_browser_cannot_consume_google_admin_sync_state(monkeypatch):
    state = "admin-state"
    browser_nonce = "correct-browser-proof"
    payload = {
        "provider_id": str(uuid.uuid4()),
        "admin_user_id": str(uuid.uuid4()),
        "tenant_id": None,
        "redirect_uri": "https://astra.example/api/auth/google_workspace/callback",
        "browser_nonce": browser_nonce,
    }
    redis = _GoogleSyncStateRedis(state, payload)
    admin_handler = AsyncMock(return_value=object())
    monkeypatch.setattr(
        "app.core.events.get_redis",
        AsyncMock(return_value=redis),
    )
    monkeypatch.setattr(
        google_api,
        "_handle_google_admin_sync_callback",
        admin_handler,
    )

    with pytest.raises(HTTPException) as exc:
        await google_api.google_workspace_callback(
            code="oauth-code",
            state=state,
            request=_google_callback_request("wrong-browser-proof"),
            db=_DB(),
        )

    assert exc.value.status_code == 400
    assert redis.key in redis.values
    admin_handler.assert_not_awaited()

    result = await google_api.google_workspace_callback(
        code="oauth-code",
        state=state,
        request=_google_callback_request(browser_nonce),
        db=_DB(),
    )

    assert result is admin_handler.return_value
    assert redis.key not in redis.values
    admin_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_google_admin_oauth_callback_does_not_echo_provider_exception(monkeypatch):
    secret = "google-admin-provider-secret-sentinel"
    provider_record = SimpleNamespace(
        config={},
        id=uuid.uuid4(),
        tenant_id=None,
        is_active=True,
    )
    failing_provider = SimpleNamespace(
        exchange_code_for_token=AsyncMock(side_effect=RuntimeError(secret)),
    )
    fake_logger = SimpleNamespace(error=Mock())
    admin = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        role="platform_admin",
        is_active=True,
        identity=SimpleNamespace(is_active=True, is_platform_admin=True),
    )
    db = _DB([admin])

    monkeypatch.setattr(
        google_api,
        "get_google_provider",
        AsyncMock(return_value=provider_record),
    )
    monkeypatch.setattr(
        google_api,
        "GoogleWorkspaceAuthProvider",
        Mock(return_value=failing_provider),
    )
    monkeypatch.setattr(google_api, "logger", fake_logger)

    response = await google_api._handle_google_admin_sync_callback(
        "oauth-code",
        provider_record.id,
        admin.id,
        None,
        "https://example.com/google/callback",
        db,
    )

    assert response.status_code == 400
    assert secret not in response.body.decode("utf-8")
    assert secret not in repr(fake_logger.error.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email_verified", "email_config", "is_platform_admin", "expected_active", "expected_pending"),
    [
        (False, SimpleNamespace(smtp_host="smtp.example.com"), False, False, True),
        (True, SimpleNamespace(smtp_host="smtp.example.com"), False, True, False),
        (False, None, False, False, True),
        (False, SimpleNamespace(smtp_host="smtp.example.com"), True, True, False),
    ],
)
async def test_registration_records_email_activation_provenance(
    monkeypatch,
    email_verified,
    email_config,
    is_platform_admin,
    expected_active,
    expected_pending,
):
    service = registration_service_module.RegistrationService()
    identity = Identity(
        id=uuid.uuid4(),
        email="activation@example.com",
        username="activation",
        email_verified=email_verified,
        is_platform_admin=is_platform_admin,
    )
    created_user = SimpleNamespace(
        id=uuid.uuid4(),
        identity=None,
        display_name="Activation User",
        avatar_url=None,
    )
    create_user = AsyncMock(return_value=created_user)
    monkeypatch.setattr(registration_service_module.user_dao, "create", create_user)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr(
        registration_service_module.participant_dao,
        "create_for_user",
        AsyncMock(),
    )

    result = await service.create_user_with_identity(
        identity,
        email_config=email_config,
    )

    values = create_user.await_args.kwargs["obj_in"]
    assert values["is_active"] is expected_active
    assert values["activation_pending_email_verification"] is expected_pending
    assert result.identity is identity
