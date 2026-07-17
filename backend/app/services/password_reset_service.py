"""Password reset token lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.core.events import get_redis
from app.database import async_session
from app.services.platform_service import platform_service

# Key prefixes for Redis
TOKEN_PREFIX = "pwd_reset:token:"
USER_PREFIX = "pwd_reset:user:"

_CREATE_TOKEN_SCRIPT = """
local old_token_hash = redis.call('GET', KEYS[1])
if old_token_hash then
    redis.call('DEL', ARGV[1] .. old_token_hash)
end
redis.call('SETEX', KEYS[2], ARGV[2], ARGV[3])
redis.call('SETEX', KEYS[1], ARGV[2], ARGV[4])
return 1
"""

_CONSUME_TOKEN_SCRIPT = """
local token_data = redis.call('GET', KEYS[1])
if not token_data then
    return false
end
redis.call('DEL', KEYS[1])
local decoded_ok, decoded = pcall(cjson.decode, token_data)
if decoded_ok and decoded['identity_id'] then
    local user_key = ARGV[1] .. decoded['identity_id']
    if redis.call('GET', user_key) == ARGV[2] then
        redis.call('DEL', user_key)
    end
end
return token_data
"""

_INVALIDATE_TOKEN_SCRIPT = """
local token_hash = redis.call('GET', KEYS[1])
if token_hash then
    redis.call('DEL', ARGV[1] .. token_hash)
end
redis.call('DEL', KEYS[1])
return 1
"""


def _hash_token(token: str) -> str:
    """Hash a raw reset token before persistence or lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_password_reset_token(
    identity_id: uuid.UUID,
    email: str,
    auth_version: int,
) -> tuple[str, datetime]:
    """Create an email-bound single-use token and invalidate older tokens."""
    redis = await get_redis()
    user_key = f"{USER_PREFIX}{identity_id}"

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    
    now = datetime.now(timezone.utc)
    expiry_minutes = get_settings().PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    expires_at = now + timedelta(minutes=expiry_minutes)
    
    # Store the new token (bi-directional mapping for easy invalidation).
    token_key = f"{TOKEN_PREFIX}{token_hash}"
    ttl_seconds = int(expiry_minutes * 60)
    token_data = json.dumps(
        {
            "identity_id": str(identity_id),
            "email": email,
            "auth_version": max(0, int(auth_version)),
        }
    )

    # Issuance is one Redis operation: concurrent requests cannot leave two
    # valid reset tokens for the same identity.
    await redis.eval(
        _CREATE_TOKEN_SCRIPT,
        2,
        user_key,
        token_key,
        TOKEN_PREFIX,
        ttl_seconds,
        token_data,
        token_hash,
    )
        
    return raw_token, expires_at


async def get_public_base_url() -> str:
    """Resolve the public base URL used for user-facing links."""
    async with async_session() as db:
        return await platform_service.get_public_base_url(db)


async def build_password_reset_url(raw_token: str) -> str:
    """Build the user-facing reset URL."""
    base_url = await get_public_base_url()
    return f"{base_url}/reset-password?token={raw_token}"


async def invalidate_password_reset_tokens(identity_id: uuid.UUID) -> None:
    """Invalidate every currently issued reset token for an Identity."""
    redis = await get_redis()
    await redis.eval(
        _INVALIDATE_TOKEN_SCRIPT,
        1,
        f"{USER_PREFIX}{identity_id}",
        TOKEN_PREFIX,
    )


async def consume_password_reset_token(raw_token: str) -> dict | None:
    """Atomically consume a valid reset token from Redis exactly once."""
    redis = await get_redis()
    token_hash = _hash_token(raw_token)
    token_key = f"{TOKEN_PREFIX}{token_hash}"

    # The Lua script is atomic at the Redis server.  It also removes the
    # identity-to-token pointer only when it still points at this token, so a
    # concurrent replacement token cannot be accidentally invalidated.
    token_data_str = await redis.eval(
        _CONSUME_TOKEN_SCRIPT,
        1,
        token_key,
        USER_PREFIX,
        token_hash,
    )
    if not token_data_str:
        return None

    try:
        token_data = json.loads(token_data_str)
        identity_id = uuid.UUID(token_data["identity_id"])
        email = str(token_data["email"])
        auth_version = int(token_data["auth_version"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Legacy identity-only tokens cannot prove ownership of the current
        # email address and therefore fail closed after this release.
        return None
    if not email:
        return None
    if auth_version < 0:
        return None
    return {
        "identity_id": identity_id,
        "email": email,
        "auth_version": auth_version,
    }
