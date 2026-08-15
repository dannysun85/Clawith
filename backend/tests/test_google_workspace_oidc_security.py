"""Security contracts for tenant-managed Google Workspace OIDC login."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse
import uuid

from fastapi import HTTPException
import pytest

from app.api.enterprise import validate_provider_config
from app.models.user import Identity
from app.services.auth_provider import ExternalUserInfo, GoogleWorkspaceAuthProvider
from app.services import google_workspace_oauth as google_oauth


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, ex=None, nx=False):
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, _script, _key_count, key, expected):
        if self.values.get(key) != expected:
            return 0
        del self.values[key]
        return 1


@pytest.mark.asyncio
async def test_google_sso_state_wrong_browser_does_not_burn_then_consumes_once(monkeypatch):
    redis = _Redis()
    tenant_id = uuid.uuid4()
    sid = uuid.uuid4()
    provider = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))

    authorization = await google_oauth.create_google_sso_state(
        session_id=sid,
        provider=provider,
        redirect_uri="https://company.example/api/auth/google_workspace/callback",
    )
    state_key = (
        google_oauth.GOOGLE_SSO_STATE_PREFIX
        + authorization.state.removeprefix(google_oauth.GOOGLE_SSO_STATE_VALUE_PREFIX)
    )
    assert state_key in redis.values

    session = SimpleNamespace(id=sid, tenant_id=tenant_id)
    monkeypatch.setattr(
        "app.services.sso_scan_session_service.get_pending_sso_session",
        AsyncMock(return_value=session),
    )
    browser_check = Mock(
        side_effect=[
            HTTPException(status_code=403, detail="wrong browser"),
            None,
            None,
        ]
    )
    monkeypatch.setattr(
        "app.services.sso_scan_session_service.verify_sso_callback_initiator",
        browser_check,
    )

    with pytest.raises(HTTPException) as exc:
        await google_oauth.consume_google_sso_state(
            authorization.state,
            request=SimpleNamespace(cookies={}),
            db=SimpleNamespace(),
        )
    assert exc.value.status_code == 403
    assert state_key in redis.values

    context = await google_oauth.consume_google_sso_state(
        authorization.state,
        request=SimpleNamespace(cookies={}),
        db=SimpleNamespace(),
    )
    assert context["session_id"] == sid
    assert context["provider_id"] == provider.id
    assert context["tenant_id"] == tenant_id
    assert context["code_verifier"]
    assert context["oidc_nonce"] == authorization.nonce
    assert state_key not in redis.values
    assert await google_oauth.consume_google_sso_state(
        authorization.state,
        request=SimpleNamespace(cookies={}),
        db=SimpleNamespace(),
    ) is None


@pytest.mark.asyncio
async def test_google_authorization_code_claim_is_one_use(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr("app.core.events.get_redis", AsyncMock(return_value=redis))
    provider_id = uuid.uuid4()

    assert await google_oauth.claim_google_sso_authorization_code(
        provider_id=provider_id,
        code="provider-code",
    )
    assert not await google_oauth.claim_google_sso_authorization_code(
        provider_id=provider_id,
        code="provider-code",
    )
    assert await google_oauth.claim_google_sso_authorization_code(
        provider_id=provider_id,
        code="different-code",
    )


@pytest.mark.asyncio
async def test_google_sso_authorization_url_requires_pkce_and_nonce(monkeypatch):
    monkeypatch.setattr(
        google_oauth,
        "get_settings",
        Mock(
            return_value=SimpleNamespace(
                ALLOW_LOCAL_OIDC_EMULATOR=True,
                ENVIRONMENT="development",
            )
        ),
    )
    provider = GoogleWorkspaceAuthProvider(
        config={
            "client_id": "local-client",
            "client_secret": "local-secret",
            "local_oidc_emulator_base_url": "http://127.0.0.1:8911",
        }
    )
    url = await provider.get_sso_authorization_url(
        "http://127.0.0.1:3008/api/auth/google_workspace/callback",
        "gwsso.state",
        code_challenge="pkce-challenge",
        nonce="oidc-nonce",
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}" == "http://127.0.0.1:8911"
    assert parsed.path == "/authorize"
    assert query["state"] == ["gwsso.state"]
    assert query["code_challenge"] == ["pkce-challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == ["oidc-nonce"]


def test_local_oidc_override_is_loopback_and_development_only(monkeypatch):
    monkeypatch.setattr(
        google_oauth,
        "get_settings",
        Mock(
            return_value=SimpleNamespace(
                ALLOW_LOCAL_OIDC_EMULATOR=True,
                ENVIRONMENT="development",
            )
        ),
    )
    assert google_oauth.local_oidc_emulator_base_url(
        {"local_oidc_emulator_base_url": "http://localhost:8911"}
    ) == "http://localhost:8911"
    with pytest.raises(ValueError):
        google_oauth.local_oidc_emulator_base_url(
            {"local_oidc_emulator_base_url": "https://idp.example.com"}
        )

    monkeypatch.setattr(
        google_oauth,
        "get_settings",
        Mock(
            return_value=SimpleNamespace(
                ALLOW_LOCAL_OIDC_EMULATOR=True,
                ENVIRONMENT="production",
            )
        ),
    )
    with pytest.raises(ValueError):
        google_oauth.local_oidc_emulator_base_url(
            {"local_oidc_emulator_base_url": "http://127.0.0.1:8911"}
        )


def test_google_workspace_jit_requires_exact_verified_hd_and_email_domain():
    provider = GoogleWorkspaceAuthProvider(
        config={
            "client_id": "client",
            "client_secret": "secret",
            "jit_provisioning_enabled": True,
            "jit_allowed_domains": ["example.com"],
        }
    )
    valid = ExternalUserInfo(
        provider_type="google_workspace",
        provider_user_id="stable-subject",
        email="member@example.com",
        email_verified=True,
        raw_data={"hd": "example.com"},
    )
    assert provider._tenant_jit_provisioning_allowed(valid)

    for invalid in (
        ExternalUserInfo(**{**valid.__dict__, "email_verified": False}),
        ExternalUserInfo(**{**valid.__dict__, "email": "member@evil.example"}),
        ExternalUserInfo(**{**valid.__dict__, "raw_data": {"hd": "evil.example"}}),
        ExternalUserInfo(**{**valid.__dict__, "provider_user_id": ""}),
    ):
        assert not provider._tenant_jit_provisioning_allowed(invalid)


@pytest.mark.asyncio
async def test_jit_membership_keeps_uuid_tenant_type_before_identity_link(monkeypatch):
    """A freshly flushed JIT User is compared before any ORM refresh."""
    tenant_id = uuid.uuid4()
    isolated_identity = Identity(id=uuid.uuid4())
    monkeypatch.setattr(
        "app.services.auth_provider.create_isolated_external_identity",
        AsyncMock(return_value=isolated_identity),
    )
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    provider = GoogleWorkspaceAuthProvider(
        config={"client_id": "client", "client_secret": "secret"}
    )

    user = await provider._create_new_user(
        db,
        ExternalUserInfo(
            provider_type="google_workspace",
            provider_user_id="stable-subject",
            name="Workspace Member",
        ),
        str(tenant_id),
    )

    assert user.tenant_id == tenant_id
    assert isinstance(user.tenant_id, uuid.UUID)


def test_google_workspace_jit_config_normalizes_domains_and_rejects_unbounded_policy(monkeypatch):
    monkeypatch.setattr(
        google_oauth,
        "get_settings",
        Mock(
            return_value=SimpleNamespace(
                ALLOW_LOCAL_OIDC_EMULATOR=False,
                ENVIRONMENT="test",
            )
        ),
    )
    config = {
        "client_id": "client",
        "client_secret": "secret",
        "jit_provisioning_enabled": True,
        "jit_allowed_domains": "Example.COM, subsidiary.example.com",
    }
    validate_provider_config("google_workspace", config, sso_login_enabled=True)
    assert config["jit_allowed_domains"] == ["example.com", "subsidiary.example.com"]

    with pytest.raises(HTTPException) as exc:
        validate_provider_config(
            "google_workspace",
            {
                "client_id": "client",
                "client_secret": "secret",
                "jit_provisioning_enabled": True,
                "jit_allowed_domains": [],
            },
            sso_login_enabled=True,
        )
    assert exc.value.status_code == 422
