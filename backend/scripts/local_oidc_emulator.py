#!/usr/bin/env python3
"""Loopback-only OIDC provider for local browser and HTTP acceptance tests.

This process uses generated signing keys and fixed dummy credentials.  It is
not a production IdP and deliberately accepts redirect URIs only on loopback
hosts at Astra's Google Workspace callback path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import os
import secrets
import time
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlencode, urlparse

import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt


ISSUER = os.environ.get("OIDC_EMULATOR_ISSUER", "http://127.0.0.1:8911").rstrip("/")
CLIENT_ID = os.environ.get("OIDC_EMULATOR_CLIENT_ID", "clawith-local-oidc")
CLIENT_SECRET = os.environ.get("OIDC_EMULATOR_CLIENT_SECRET", "clawith-local-secret")
SUBJECT = os.environ.get("OIDC_EMULATOR_SUBJECT", "local-workspace-member-001")
EMAIL = os.environ.get("OIDC_EMULATOR_EMAIL", "jit.member@example.test")
HOSTED_DOMAIN = os.environ.get("OIDC_EMULATOR_HOSTED_DOMAIN", "example.test")
KEY_ID = "clawith-local-oidc-key"
CODE_TTL_SECONDS = 120
TOKEN_TTL_SECONDS = 300


def _b64url_uint(value: int) -> str:
    width = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")


def _is_allowed_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path != "/api/auth/google_workspace/callback" or not parsed.hostname:
        return False
    hostname = parsed.hostname.casefold()
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class AuthorizationCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    nonce: str
    expires_at: float


private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
public_numbers = private_key.public_key().public_numbers()
jwks = {
    "keys": [
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": KEY_ID,
            "n": _b64url_uint(public_numbers.n),
            "e": _b64url_uint(public_numbers.e),
        }
    ]
}
authorization_codes: dict[str, AuthorizationCode] = {}
access_tokens: dict[str, float] = {}
state_lock = asyncio.Lock()
app = FastAPI(title="Clawith Local OIDC Emulator")


def _validated_authorization_request(request: Request) -> dict[str, str]:
    query = request.query_params
    values = {
        "client_id": str(query.get("client_id") or ""),
        "redirect_uri": str(query.get("redirect_uri") or ""),
        "response_type": str(query.get("response_type") or ""),
        "scope": str(query.get("scope") or ""),
        "state": str(query.get("state") or ""),
        "code_challenge": str(query.get("code_challenge") or ""),
        "code_challenge_method": str(query.get("code_challenge_method") or ""),
        "nonce": str(query.get("nonce") or ""),
    }
    if values["client_id"] != CLIENT_ID:
        raise HTTPException(status_code=400, detail="invalid_client")
    if not _is_allowed_redirect_uri(values["redirect_uri"]):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    if values["response_type"] != "code" or "openid" not in values["scope"].split():
        raise HTTPException(status_code=400, detail="unsupported_authorization_request")
    if (
        not values["state"]
        or not values["nonce"]
        or not values["code_challenge"]
        or values["code_challenge_method"] != "S256"
    ):
        raise HTTPException(status_code=400, detail="pkce_state_and_nonce_required")
    return values


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "provider": "local_idp_emulated"}


@app.get("/.well-known/openid-configuration")
async def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
    }


@app.get("/jwks")
async def get_jwks():
    return jwks


@app.get("/authorize", response_class=HTMLResponse)
async def authorize(request: Request):
    values = _validated_authorization_request(request)
    approve_url = f"{ISSUER}/approve?{urlencode(values)}"
    deny_params = urlencode({"error": "access_denied", "state": values["state"]})
    deny_url = f"{values['redirect_uri']}?{deny_params}"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>Local OIDC</title></head>
        <body style="font-family:system-ui;max-width:620px;margin:64px auto;padding:24px">
          <p style="color:#8a5b00;font-weight:700">LOCAL IDP EMULATOR · NOT A REAL PROVIDER</p>
          <h1>Approve company sign-in</h1>
          <p>Account: <strong>{html.escape(EMAIL)}</strong></p>
          <p>Hosted domain: <strong>{html.escape(HOSTED_DOMAIN)}</strong></p>
          <div style="display:flex;gap:12px;margin-top:28px">
            <a id="approve-login" href="{html.escape(approve_url)}" style="padding:10px 16px;background:#111;color:white;text-decoration:none;border-radius:8px">Approve local login</a>
            <a id="deny-login" href="{html.escape(deny_url)}" style="padding:10px 16px;border:1px solid #aaa;color:#333;text-decoration:none;border-radius:8px">Deny</a>
          </div>
        </body></html>"""
    )


@app.get("/approve")
async def approve(request: Request):
    values = _validated_authorization_request(request)
    code = secrets.token_urlsafe(32)
    async with state_lock:
        authorization_codes[code] = AuthorizationCode(
            client_id=values["client_id"],
            redirect_uri=values["redirect_uri"],
            code_challenge=values["code_challenge"],
            nonce=values["nonce"],
            expires_at=time.time() + CODE_TTL_SECONDS,
        )
    callback_query = urlencode({"code": code, "state": values["state"]})
    return RedirectResponse(f"{values['redirect_uri']}?{callback_query}", status_code=302)


@app.post("/token")
async def token(request: Request):
    form = await request.form()
    code = str(form.get("code") or "")
    client_id = str(form.get("client_id") or "")
    client_secret = str(form.get("client_secret") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    code_verifier = str(form.get("code_verifier") or "")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    async with state_lock:
        grant = authorization_codes.pop(code, None)
    if (
        grant is None
        or grant.expires_at <= time.time()
        or grant.client_id != client_id
        or grant.redirect_uri != redirect_uri
        or not code_verifier
        or not secrets.compare_digest(_pkce_challenge(code_verifier), grant.code_challenge)
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    now = int(time.time())
    access_token = secrets.token_urlsafe(32)
    async with state_lock:
        access_tokens[access_token] = time.time() + TOKEN_TTL_SECONDS
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "nonce": grant.nonce,
        "email": EMAIL,
        "email_verified": True,
        "name": "Local Workspace Member",
        "hd": HOSTED_DOMAIN,
    }
    id_token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "id_token": id_token,
    }


@app.get("/userinfo")
async def userinfo(request: Request):
    authorization = str(request.headers.get("authorization") or "")
    token_value = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    async with state_lock:
        expires_at = access_tokens.get(token_value)
    if not expires_at or expires_at <= time.time():
        raise HTTPException(status_code=401, detail="invalid_token")
    return {
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "name": "Local Workspace Member",
        "hd": HOSTED_DOMAIN,
    }


if __name__ == "__main__":
    parsed_issuer = urlparse(ISSUER)
    uvicorn.run(
        app,
        host=parsed_issuer.hostname or "127.0.0.1",
        port=parsed_issuer.port or 8911,
        log_level="warning",
    )
