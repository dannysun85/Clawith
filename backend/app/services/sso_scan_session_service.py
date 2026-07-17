"""Atomic lifecycle helpers for same-browser SSO relay sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.identity import SSOScanSession


SSO_INITIATOR_COOKIE_PREFIX = "astra_sso_initiator_"


def sso_initiator_cookie_name(sid: uuid.UUID) -> str:
    """Return a per-session cookie name so parallel login tabs cannot collide."""
    return f"{SSO_INITIATOR_COOKIE_PREFIX}{sid.hex}"


def hash_sso_initiator_nonce(nonce: str) -> str:
    """Hash the initiating browser's secret for one SSO relay session."""
    return hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_sso_initiator_nonce(session: SSOScanSession, nonce: str) -> None:
    """Require the browser secret that owns this login session."""
    expected_nonce_hash = str(session.initiator_nonce_hash or "")
    supplied_nonce_hash = hash_sso_initiator_nonce(nonce) if nonce else ""
    if (
        not expected_nonce_hash
        or not supplied_nonce_hash
        or not hmac.compare_digest(expected_nonce_hash, supplied_nonce_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO session belongs to a different browser",
        )


def verify_sso_callback_initiator(
    session: SSOScanSession,
    request: Request | None,
) -> None:
    """Bind provider authorization to the browser that created the relay.

    A signed OAuth state proves that Astra created the relay URL, but it does
    not prove which browser followed that URL.  Requiring the per-session,
    HttpOnly browser nonce before provider lookup or code exchange prevents a
    third party from relaying its URL through another user's provider login.
    """
    cookies = getattr(request, "cookies", {}) if request is not None else {}
    nonce = cookies.get(sso_initiator_cookie_name(session.id)) or ""
    verify_sso_initiator_nonce(session, nonce)


def sign_sso_scan_state(
    provider_type: str,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
) -> str:
    """Bind a callback to the exact relay session and provider configuration."""
    payload = f"sso:{provider_type}:{session_id}:{provider_id}"
    signature = hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def parse_sso_scan_state(
    state_value: str | None,
    *,
    provider_type: str,
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Validate a signed relay callback state for one provider type."""
    parts = str(state_value or "").split(":")
    if len(parts) != 5 or parts[0] != "sso" or parts[1] != provider_type:
        return None
    payload = ":".join(parts[:-1])
    expected = hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(parts[-1], expected):
        return None
    try:
        return uuid.UUID(parts[2]), uuid.UUID(parts[3])
    except ValueError:
        return None


def sso_session_is_expired(session: SSOScanSession) -> bool:
    """Return whether a scan session is past its absolute expiry."""
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


async def get_pending_sso_session(
    db: AsyncSession,
    sid: uuid.UUID,
    *,
    for_update: bool = False,
) -> SSOScanSession:
    """Load a non-expired session that is still eligible for authorization."""
    query = (
        select(SSOScanSession)
        .where(SSOScanSession.id == sid)
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    session = result.scalar_one_or_none()
    if (
        not session
        or sso_session_is_expired(session)
        or session.status not in {"pending", "scanned"}
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO session is invalid or expired",
        )
    return session


async def authorize_sso_session(
    db: AsyncSession,
    *,
    sid: uuid.UUID,
    provider_type: str,
    user_id: uuid.UUID,
    access_token: str,
) -> None:
    """Atomically transition one pending session to authorized exactly once."""
    encrypted_token = encrypt_data(access_token, get_settings().JWT_SECRET_KEY)
    result = await db.execute(
        update(SSOScanSession)
        .where(
            SSOScanSession.id == sid,
            SSOScanSession.status.in_(("pending", "scanned")),
            SSOScanSession.expires_at > datetime.now(timezone.utc),
        )
        .values(
            status="authorized",
            provider_type=provider_type,
            user_id=user_id,
            access_token=encrypted_token,
            error_msg=None,
        )
        .returning(SSOScanSession.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SSO session was already completed or expired",
        )


async def consume_authorized_sso_session(
    db: AsyncSession,
    sid: uuid.UUID,
    *,
    initiator_nonce: str,
) -> tuple[SSOScanSession, str] | None:
    """Atomically consume an authorized JWT and clear it from the database."""
    result = await db.execute(
        select(SSOScanSession)
        .where(SSOScanSession.id == sid)
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    verify_sso_initiator_nonce(session, initiator_nonce)
    if sso_session_is_expired(session):
        session.status = "expired"
        session.access_token = None
        await db.flush()
        return None
    if session.status != "authorized" or not session.access_token:
        return None

    encrypted_token = session.access_token
    session.status = "completed"
    session.access_token = None
    await db.flush()
    try:
        token = decrypt_data(encrypted_token, get_settings().JWT_SECRET_KEY)
    except ValueError:
        session.error_msg = "SSO session credential is invalid"
        await db.flush()
        return None
    return session, token


async def cleanup_expired_sso_sessions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    retention_minutes: int | None = None,
) -> tuple[int, int]:
    """Clear expired credentials and delete old terminal session rows.

    Expired rows remain briefly so an initiating browser can receive a stable
    terminal status. Encrypted JWTs do not share that retention window: they
    are cleared as soon as the session expires.
    """
    settings = get_settings()
    effective_now = now or datetime.now(timezone.utc)
    retention = max(
        1,
        retention_minutes
        if retention_minutes is not None
        else settings.SSO_SESSION_RETENTION_MINUTES,
    )
    expired_result = await db.execute(
        update(SSOScanSession)
        .where(
            SSOScanSession.expires_at <= effective_now,
            SSOScanSession.status.in_(("pending", "scanned", "authorized")),
        )
        .values(status="expired", access_token=None)
        .execution_options(synchronize_session=False)
    )
    # Defensive cleanup also strips any credential from a terminal row whose
    # status was written by older code.
    await db.execute(
        update(SSOScanSession)
        .where(
            SSOScanSession.expires_at <= effective_now,
            SSOScanSession.access_token.is_not(None),
        )
        .values(access_token=None)
        .execution_options(synchronize_session=False)
    )
    delete_before = effective_now - timedelta(minutes=retention)
    deleted_result = await db.execute(
        delete(SSOScanSession)
        .where(SSOScanSession.expires_at <= delete_before)
        .execution_options(synchronize_session=False)
    )
    return (
        max(0, int(getattr(expired_result, "rowcount", 0) or 0)),
        max(0, int(getattr(deleted_result, "rowcount", 0) or 0)),
    )


async def start_sso_session_cleanup_daemon() -> None:
    """Continuously bound temporary SSO session storage in the worker role."""
    from app.database import async_session

    settings = get_settings()
    interval = max(10, settings.SSO_SESSION_CLEANUP_INTERVAL_SECONDS)
    logger.info("SSO session cleanup service started interval_seconds={}", interval)
    while True:
        try:
            async with async_session() as db:
                expired, deleted = await cleanup_expired_sso_sessions(db)
                await db.commit()
            if expired or deleted:
                logger.info(
                    "SSO session cleanup completed expired={} deleted={}",
                    expired,
                    deleted,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "SSO session cleanup failed error_type={}",
                type(exc).__name__,
            )
        await asyncio.sleep(interval)
