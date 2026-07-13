from types import SimpleNamespace

import pytest
from fastapi import Response

from app.api import auth as auth_api
from app.core.security import (
    BROWSER_SESSION_COOKIE,
    WEBSOCKET_APP_PROTOCOL,
    extract_websocket_access_token,
    set_browser_session_cookie,
    websocket_response_subprotocol,
)


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
