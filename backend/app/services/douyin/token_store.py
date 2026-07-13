"""Encrypted Douyin OAuth token persistence."""

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.douyin import DouyinAccount, DouyinToken
from app.services.douyin.client import DouyinOpenAPIClient
from app.services.douyin.errors import DouyinAuthError, DouyinPermissionError
from app.services.douyin.policy import capability_status


TOKEN_REFRESH_SKEW = timedelta(minutes=10)


def _encrypt(value: str) -> str:
    return encrypt_data(value, get_settings().SECRET_KEY)


def _decrypt(value: str) -> str:
    return decrypt_data(value, get_settings().SECRET_KEY)


async def store_oauth_tokens(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    token_payload: dict,
    profile: dict | None = None,
) -> DouyinAccount:
    """Create or update account + encrypted token pair from OAuth callback."""
    profile = profile or {}
    scopes = token_payload.get("scope") or []
    open_id = token_payload["open_id"]
    result = await db.execute(
        select(DouyinAccount).where(DouyinAccount.tenant_id == tenant_id, DouyinAccount.open_id == open_id)
    )
    account = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not account:
        account = DouyinAccount(
            tenant_id=tenant_id,
            created_by=user_id,
            open_id=open_id,
            authorized_at=now,
        )
        db.add(account)
        await db.flush()

    account.union_id = profile.get("union_id")
    account.nickname = profile.get("nickname") or account.nickname or f"Douyin {open_id[-6:]}"
    account.avatar_url = profile.get("avatar_url")
    account.account_type = profile.get("account_type")
    account.scopes = scopes
    account.permission_status = {row["key"]: row["status"] for row in capability_status(scopes)}
    account.status = "active"
    account.last_error = None
    account.authorized_at = account.authorized_at or now

    token_result = await db.execute(select(DouyinToken).where(DouyinToken.account_id == account.id))
    token = token_result.scalar_one_or_none()
    if not token:
        token = DouyinToken(account_id=account.id)
        db.add(token)

    token.access_token_encrypted = _encrypt(token_payload["access_token"])
    token.refresh_token_encrypted = _encrypt(token_payload["refresh_token"])
    token.access_token_expires_at = token_payload.get("access_token_expires_at")
    token.refresh_token_expires_at = token_payload.get("refresh_token_expires_at")
    token.status = "active"
    await db.flush()
    return account


async def get_valid_access_token(
    db: AsyncSession,
    account: DouyinAccount,
    *,
    client: DouyinOpenAPIClient | None = None,
) -> str:
    """Return a valid access token, refreshing when close to expiry."""
    result = await db.execute(select(DouyinToken).where(DouyinToken.account_id == account.id))
    token = result.scalar_one_or_none()
    if not token or token.status != "active":
        account.status = "needs_reauth"
        raise DouyinAuthError("Douyin account needs reauthorization", code="needs_reauth")

    now = datetime.now(timezone.utc)
    if token.refresh_token_expires_at and token.refresh_token_expires_at <= now:
        token.status = "expired"
        account.status = "needs_reauth"
        raise DouyinAuthError("Douyin refresh token expired", code="refresh_expired")

    if token.access_token_expires_at and token.access_token_expires_at > now + TOKEN_REFRESH_SKEW:
        return _decrypt(token.access_token_encrypted)

    refresh_token = _decrypt(token.refresh_token_encrypted)
    client = client or DouyinOpenAPIClient()
    payload = await client.refresh_access_token(refresh_token)
    token.access_token_encrypted = _encrypt(payload["access_token"])
    if payload.get("refresh_token"):
        token.refresh_token_encrypted = _encrypt(payload["refresh_token"])
    token.access_token_expires_at = payload.get("access_token_expires_at")
    token.refresh_token_expires_at = payload.get("refresh_token_expires_at") or token.refresh_token_expires_at
    token.refresh_count += 1
    token.last_refresh_at = now
    token.status = "active"
    account.status = "active" if account.status != "disabled" else account.status
    await db.flush()
    return payload["access_token"]


def assert_scope(account: DouyinAccount, capability: str) -> None:
    permissions = account.permission_status or {}
    if permissions.get(capability) != "ready":
        raise DouyinPermissionError(
            f"Douyin account missing required capability: {capability}",
            code="permission_missing",
        )
