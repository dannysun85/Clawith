"""Email verification token lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.core.events import get_redis

# Key prefixes for Redis
TOKEN_PREFIX = "email_verify:token:"
USER_PREFIX = "email_verify:user:"

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


class EmailVerificationService:
    """Email verification token lifecycle helpers."""

    def _hash_token(self, token: str) -> str:
        """Hash a raw verification token before persistence or lookup."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create_email_verification_token(self, identity_id: uuid.UUID, email: str) -> tuple[str, datetime]:
        """Create a high-entropy, Identity-scoped verification token."""
        redis = await get_redis()
        user_key = f"{USER_PREFIX}{identity_id}"

        # The UUID scopes the token to exactly one Identity while the 256-bit
        # nonce makes online enumeration infeasible.  This also prevents two
        # users from colliding in a global six-digit code namespace.
        raw_token = f"{identity_id}.{secrets.token_urlsafe(32)}"
        token_hash = self._hash_token(raw_token)

        now = datetime.now(timezone.utc)
        expiry_minutes = get_settings().EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES
        expires_at = now + timedelta(minutes=expiry_minutes)

        # Store the new token with Identity ID and email.
        token_key = f"{TOKEN_PREFIX}{token_hash}"
        ttl_seconds = int(expiry_minutes * 60)

        # Store as JSON with identity_id and email
        token_data = json.dumps({"identity_id": str(identity_id), "email": email})

        # Invalidation plus both sides of the new mapping must be one Redis
        # operation; otherwise concurrent resend requests can leave two live
        # verification tokens for the same Identity.
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

    async def build_email_verification_url(self, base_url: str, raw_token: str) -> str:
        """Build the user-facing URL for a high-entropy verification token."""
        base = base_url.strip().rstrip("/")
        return f"{base}/verify-email?code={raw_token}"

    async def invalidate_email_verification_tokens(self, identity_id: uuid.UUID) -> None:
        """Invalidate every currently issued verification token for an Identity."""
        redis = await get_redis()
        await redis.eval(
            _INVALIDATE_TOKEN_SCRIPT,
            1,
            f"{USER_PREFIX}{identity_id}",
            TOKEN_PREFIX,
        )

    async def consume_email_verification_token(self, raw_token: str) -> dict | None:
        """Atomically consume one Identity-scoped verification token."""
        try:
            identity_prefix, nonce = raw_token.split(".", 1)
            expected_identity_id = uuid.UUID(identity_prefix)
        except (AttributeError, ValueError):
            # Fail closed for legacy six-digit global codes.  Users can request
            # a replacement token; accepting them would retain the enumeration
            # and cross-account collision vulnerability during rollout.
            return None
        if len(nonce) < 32:
            return None

        redis = await get_redis()
        token_hash = self._hash_token(raw_token)
        token_key = f"{TOKEN_PREFIX}{token_hash}"

        # Consume is exactly-once.  The script also deletes the reverse pointer
        # only when it still refers to this code, preserving a concurrently
        # issued replacement.
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
            email = token_data["email"]
        except (json.JSONDecodeError, KeyError, ValueError):
            return None
        if identity_id != expected_identity_id:
            return None

        return {"identity_id": identity_id, "email": email}

    async def send_verification_email(
        self,
        to: str,
        display_name: str,
        verification_code: str,
        expiry_minutes: int,
    ) -> None:
        """Send an email verification token using the configured template."""
        from app.services.system_email_service import send_system_email, render_email_template
        from app.database import async_session
        from app.services.platform_service import platform_service

        async with async_session() as db:
            base_url = await platform_service.get_public_base_url(db)
        verification_url = await self.build_email_verification_url(base_url, verification_code)

        variables = {
            "display_name": display_name,
            "verification_url": verification_url,
            "verification_code": verification_code,
            "expiry_minutes": str(expiry_minutes),
        }
        subject, body = await render_email_template("email_verification", variables)
        await send_system_email(to, subject, body)

# Global Instance
email_verification_service = EmailVerificationService()
