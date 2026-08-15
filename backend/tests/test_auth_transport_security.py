from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from app.api import auth as auth_api
from app.core import security as security_module
from app.core.security import (
    BROWSER_SESSION_COOKIE,
    WEBSOCKET_APP_PROTOCOL,
    access_token_matches_identity,
    extract_websocket_access_token,
    identity_auth_version,
    set_browser_session_cookie,
    websocket_response_subprotocol,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, value, *, tenant=None):
        self.value = value
        self.tenant = tenant

    async def execute(self, _statement):
        return _ScalarResult(self.value)

    async def get(self, _model, _identifier):
        return self.tenant

    async def commit(self):
        return None


def _websocket(protocols: str = "", cookie: str | None = None):
    cookies = {BROWSER_SESSION_COOKIE: cookie} if cookie else {}
    return SimpleNamespace(
        headers={"sec-websocket-protocol": protocols},
        cookies=cookies,
    )


def test_websocket_token_uses_secret_protocol_without_echoing_it():
    websocket = _websocket("astra-chat, astra-token.header.payload.signature")

    assert extract_websocket_access_token(websocket, "legacy") == "header.payload.signature"
    assert websocket_response_subprotocol(websocket) == WEBSOCKET_APP_PROTOCOL


def test_websocket_cookie_and_legacy_query_are_compatibility_fallbacks():
    assert extract_websocket_access_token(_websocket(cookie="cookie-jwt"), "legacy") == "cookie-jwt"
    assert extract_websocket_access_token(_websocket(), "legacy") == "legacy"
    assert extract_websocket_access_token(_websocket(), None) is None


def test_browser_session_cookie_is_httponly_secure_and_same_site():
    response = Response()
    request = SimpleNamespace(
        headers={"x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )

    set_browser_session_cookie(response, "opaque-jwt", request)

    cookie = response.headers["set-cookie"]
    assert f"{BROWSER_SESSION_COOKIE}=opaque-jwt" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


@pytest.mark.asyncio
async def test_browser_session_endpoints_return_explicit_204_responses():
    request = SimpleNamespace(
        headers={
            "authorization": "Bearer opaque-jwt",
            "x-forwarded-proto": "https",
        },
        url=SimpleNamespace(scheme="http"),
    )

    created = await auth_api.create_browser_session(
        request=request,
        response=Response(),
        current_user=SimpleNamespace(id="user-id"),
    )
    assert created.status_code == 204
    assert f"{BROWSER_SESSION_COOKIE}=opaque-jwt" in created.headers["set-cookie"]

    deleted = await auth_api.delete_browser_session(request=request, response=Response())
    assert deleted.status_code == 204
    assert f"{BROWSER_SESSION_COOKIE}=\"\"" in deleted.headers["set-cookie"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    [security_module.get_current_user, security_module.get_authenticated_user],
)
async def test_bearer_dependencies_reject_globally_disabled_identity(
    monkeypatch,
    dependency,
):
    user = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        is_active=True,
        identity=SimpleNamespace(is_active=False, auth_version=0),
    )
    monkeypatch.setattr(
        security_module,
        "decode_access_token",
        lambda _token: {"sub": user.id, "av": 0},
    )

    with pytest.raises(HTTPException) as exc:
        await dependency(
            credentials=SimpleNamespace(credentials="opaque-jwt"),
            db=_DB(user),
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verification_user_allows_explicit_email_pending_membership(monkeypatch):
    user = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000002",
        is_active=False,
        activation_pending_email_verification=True,
        identity=SimpleNamespace(is_active=True, auth_version=0),
    )
    monkeypatch.setattr(
        security_module,
        "decode_access_token",
        lambda _token: {"sub": user.id, "av": 0},
    )

    result = await security_module.get_verification_user(
        credentials=SimpleNamespace(credentials="opaque-jwt"),
        db=_DB(user),
    )

    assert result is user


@pytest.mark.asyncio
async def test_authenticated_user_rejects_email_pending_membership(monkeypatch):
    user = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000003",
        is_active=False,
        activation_pending_email_verification=True,
        identity=SimpleNamespace(is_active=True, auth_version=0),
    )
    monkeypatch.setattr(
        security_module,
        "decode_access_token",
        lambda _token: {"sub": user.id, "av": 0},
    )

    with pytest.raises(HTTPException) as exc:
        await security_module.get_authenticated_user(
            credentials=SimpleNamespace(credentials="opaque-jwt"),
            db=_DB(user),
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_suspended_tenant_keeps_recovery_auth_but_blocks_business_auth(monkeypatch):
    user = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000004",
        tenant_id="00000000-0000-0000-0000-000000000005",
        is_active=True,
        identity=SimpleNamespace(is_active=True, auth_version=0),
    )
    suspended_tenant = SimpleNamespace(is_active=False)
    monkeypatch.setattr(
        security_module,
        "decode_access_token",
        lambda _token: {"sub": user.id, "av": 0},
    )

    recovered = await security_module.get_authenticated_user(
        credentials=SimpleNamespace(credentials="opaque-jwt"),
        db=_DB(user, tenant=suspended_tenant),
    )
    assert recovered is user

    with pytest.raises(HTTPException) as exc:
        await security_module.get_current_user(
            credentials=SimpleNamespace(credentials="opaque-jwt"),
            db=_DB(user, tenant=suspended_tenant),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Organization is unavailable"


@pytest.mark.parametrize(
    ("payload", "identity"),
    [
        ({"sub": "user-id"}, SimpleNamespace(auth_version=0)),
        ({"sub": "user-id", "av": 0}, SimpleNamespace()),
        ({"sub": "user-id", "av": 0}, SimpleNamespace(auth_version=1)),
        ({"sub": "user-id", "av": -1}, SimpleNamespace(auth_version=-1)),
    ],
)
def test_access_token_requires_an_exact_explicit_non_negative_auth_version(
    payload,
    identity,
):
    assert access_token_matches_identity(payload, identity) is False


def test_token_issuance_rejects_identity_without_auth_version():
    with pytest.raises(ValueError, match="auth_version"):
        identity_auth_version(SimpleNamespace())
