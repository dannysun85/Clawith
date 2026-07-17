"""Fail-closed, privacy-safe rate limits for public authentication work."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.config import get_settings


_AUTH_RATE_LIMIT_SCRIPT = """
local client_count = tonumber(redis.call('GET', KEYS[1]) or '0')
local identity_count = tonumber(redis.call('GET', KEYS[2]) or '0')
local global_count = tonumber(redis.call('GET', KEYS[3]) or '0')

if client_count >= tonumber(ARGV[1]) then
    return 1
end
if identity_count >= tonumber(ARGV[2]) then
    return 2
end
if global_count + tonumber(ARGV[7]) > tonumber(ARGV[3]) then
    return 3
end

for index = 1, 2 do
    local count = redis.call('INCR', KEYS[index])
    local ttl = redis.call('TTL', KEYS[index])
    if count == 1 or ttl < 0 then
        redis.call('EXPIRE', KEYS[index], ARGV[index + 3])
    end
end
local global_count_after = redis.call('INCRBY', KEYS[3], ARGV[7])
local global_ttl = redis.call('TTL', KEYS[3])
if global_count_after == tonumber(ARGV[7]) or global_ttl < 0 then
    redis.call('EXPIRE', KEYS[3], ARGV[6])
end
return 0
"""


@dataclass(frozen=True)
class AuthRateLimitPolicy:
    """Three independent quotas claimed together by one Redis operation."""

    operation: str
    client_limit: int
    identity_limit: int
    global_limit: int
    client_window_seconds: int
    identity_window_seconds: int
    global_window_seconds: int
    global_namespace: str | None = None
    global_cost: int = 1


def auth_rate_limit_client_key(request: Request) -> str:
    """Return a non-reversible bucket for the verified client address.

    The public socket peer is authoritative. ``X-Real-IP`` is accepted only
    from a local/private reverse proxy; direct clients cannot choose another
    bucket with spoofed forwarding headers.
    """

    peer = str(getattr(getattr(request, "client", None), "host", "") or "unknown")
    headers = getattr(request, "headers", {}) or {}
    real_ip = (headers.get("x-real-ip") or "").strip()

    trusted_proxy = False
    try:
        peer_address = ipaddress.ip_address(peer)
        if peer_address.version == 4:
            trusted_proxy = peer_address.is_loopback or any(
                peer_address in network
                for network in (
                    ipaddress.ip_network("10.0.0.0/8"),
                    ipaddress.ip_network("172.16.0.0/12"),
                    ipaddress.ip_network("192.168.0.0/16"),
                )
            )
        else:
            trusted_proxy = peer_address.is_loopback or peer_address.is_private
    except ValueError:
        pass

    source = peer
    if trusted_proxy and real_ip:
        try:
            source = str(ipaddress.ip_address(real_ip))
        except ValueError:
            source = peer
    return hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        f"auth-rate-limit-client:v1:{source[:256]}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]


def _auth_rate_limit_identity_key(operation: str, identity: str) -> str:
    """HMAC an attacker-controlled login target before using it as a key."""

    settings = get_settings()
    normalized = identity.strip().lower() or "<empty>"
    message = f"auth-rate-limit:v1:{operation}:{normalized}".encode("utf-8")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()[:32]


async def enforce_auth_rate_limit(
    request: Request,
    *,
    identity: str,
    policy: AuthRateLimitPolicy,
) -> None:
    """Atomically claim client, identity and global authentication capacity.

    Every bucket is inspected before any bucket is incremented. A rejected
    client therefore cannot poison the target/global quota, and a targeted
    identity attack cannot consume global capacity after its own quota fills.
    Redis failure is fail-closed because all callers are anonymous credential,
    token, email or provider-I/O entry points.
    """

    from app.core.events import get_redis

    operation = policy.operation
    client_key = auth_rate_limit_client_key(request)
    identity_key = _auth_rate_limit_identity_key(operation, identity)
    keys = (
        f"auth:rate:v1:{operation}:client:{client_key}",
        f"auth:rate:v1:{operation}:identity:{identity_key}",
        f"auth:rate:v1:{policy.global_namespace or operation}:global",
    )
    try:
        redis = await get_redis()
        rejected_bucket = int(
            await redis.eval(
                _AUTH_RATE_LIMIT_SCRIPT,
                3,
                *keys,
                max(1, policy.client_limit),
                max(1, policy.identity_limit),
                max(1, policy.global_limit),
                max(1, policy.client_window_seconds),
                max(1, policy.identity_window_seconds),
                max(1, policy.global_window_seconds),
                max(1, policy.global_cost),
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable. Please try again later.",
        ) from exc

    if rejected_bucket:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts. Please try again later.",
            headers={
                "Retry-After": str(
                    (
                        policy.client_window_seconds,
                        policy.identity_window_seconds,
                        policy.global_window_seconds,
                    )[rejected_bucket - 1]
                )
            },
        )


def login_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="password-login",
        client_limit=settings.AUTH_LOGIN_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_LOGIN_IDENTITY_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_BCRYPT_GLOBAL_WORK_UNITS_PER_10_SECONDS,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=10,
        global_namespace="bcrypt-work",
    )


def login_lookup_rate_limit_policy() -> AuthRateLimitPolicy:
    """Bound anonymous namespace probes before any Identity database query."""

    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="password-login-lookup",
        client_limit=settings.AUTH_LOGIN_LOOKUP_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_LOGIN_LOOKUP_IDENTIFIER_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_LOGIN_LOOKUP_GLOBAL_QUERY_UNITS_PER_MINUTE,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=60,
        global_namespace="auth-read-work",
        # Email and phone shapes take one indexed query; an ordinary username
        # can take three. Reserve the worst case so the global cap is real.
        global_cost=3,
    )


def password_registration_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="password-register",
        client_limit=settings.AUTH_PASSWORD_REGISTER_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_PASSWORD_REGISTER_IDENTITY_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_BCRYPT_GLOBAL_WORK_UNITS_PER_10_SECONDS,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=10,
        global_namespace="bcrypt-work",
        global_cost=2,
    )


def password_change_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="password-change",
        client_limit=settings.AUTH_PASSWORD_CHANGE_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_PASSWORD_CHANGE_IDENTITY_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_BCRYPT_GLOBAL_WORK_UNITS_PER_10_SECONDS,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=10,
        global_namespace="bcrypt-work",
        global_cost=2,
    )


def password_reauth_rate_limit_policy() -> AuthRateLimitPolicy:
    """Bound password proof for sensitive authenticated profile changes."""

    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="password-reauth",
        client_limit=settings.AUTH_PASSWORD_REAUTH_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_PASSWORD_REAUTH_IDENTITY_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_BCRYPT_GLOBAL_WORK_UNITS_PER_10_SECONDS,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=10,
        global_namespace="bcrypt-work",
    )


def email_action_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="email-action",
        client_limit=settings.AUTH_EMAIL_ACTION_CLIENT_LIMIT_PER_15_MINUTES,
        identity_limit=settings.AUTH_EMAIL_ACTION_IDENTITY_LIMIT_PER_15_MINUTES,
        global_limit=settings.AUTH_EMAIL_ACTION_GLOBAL_LIMIT_PER_MINUTE,
        client_window_seconds=15 * 60,
        identity_window_seconds=15 * 60,
        global_window_seconds=60,
    )


def discovery_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="identity-discovery",
        client_limit=settings.AUTH_DISCOVERY_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_DISCOVERY_IDENTITY_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_DISCOVERY_GLOBAL_LIMIT_PER_MINUTE,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=60,
    )


def oauth_start_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="oauth-start",
        client_limit=settings.AUTH_OAUTH_START_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_OAUTH_START_PROVIDER_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_OAUTH_START_GLOBAL_LIMIT_PER_MINUTE,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=60,
    )


def oauth_exchange_rate_limit_policy() -> AuthRateLimitPolicy:
    settings = get_settings()
    return AuthRateLimitPolicy(
        operation="oauth-exchange",
        client_limit=settings.AUTH_OAUTH_EXCHANGE_CLIENT_LIMIT_PER_MINUTE,
        identity_limit=settings.AUTH_OAUTH_EXCHANGE_PROVIDER_LIMIT_PER_MINUTE,
        global_limit=settings.AUTH_OAUTH_EXCHANGE_GLOBAL_LIMIT_PER_MINUTE,
        client_window_seconds=60,
        identity_window_seconds=60,
        global_window_seconds=60,
    )
