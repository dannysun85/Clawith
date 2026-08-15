"""Durable delivery and retry orchestration for system-owned emails."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import smtplib
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.identity_canonicalization import canonicalize_email
from app.core.outbound_email_crypto import open_outbound_email_payload, seal_outbound_email_payload
from app.database import async_session
from app.models.outbound_email import OutboundEmailDelivery
from app.services.system_email_service import (
    SystemEmailConfigResolutionError,
    _send_email_with_config_sync,
    render_email_template,
    resolve_email_config_async,
)


ACTIVE_DELIVERY_STATUSES = {"queued", "sending", "retry_wait", "blocked_configuration"}
TERMINAL_DELIVERY_STATUSES = {"smtp_accepted", "permanent_failed", "cancelled"}
_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 14400)
_CONFIGURATION_RECHECK_SECONDS = 300


def _now() -> datetime:
    return datetime.now(UTC)


def _recipient_hash(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def mask_email_address(email: str) -> str:
    """Return a stable user-facing mask without storing another credential."""

    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def delivery_public_payload(delivery: OutboundEmailDelivery | None) -> dict[str, Any] | None:
    if delivery is None:
        return None
    return {
        "id": str(delivery.id),
        "purpose": delivery.purpose,
        "status": delivery.status,
        "recipient_mask": delivery.recipient_mask,
        "attempt_count": delivery.attempt_count,
        "max_attempts": delivery.max_attempts,
        "next_attempt_at": delivery.next_attempt_at,
        "last_error_code": delivery.last_error_code,
        "smtp_accepted_at": delivery.smtp_accepted_at,
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
    }


async def enqueue_template_email(
    db: AsyncSession,
    *,
    purpose: str,
    to: str,
    scenario_key: str,
    variables: dict[str, str],
    tenant_id: uuid.UUID | None = None,
    identity_id: uuid.UUID | None = None,
    invitation_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> OutboundEmailDelivery:
    """Persist one rendered, encrypted delivery before any SMTP side effect."""

    email = canonicalize_email(to)
    if not email:
        raise ValueError("A valid recipient email is required")
    if purpose not in {"email_verification", "password_reset", "company_invitation"}:
        raise ValueError("Unsupported outbound email purpose")
    subject, body = await render_email_template(scenario_key, variables, db=db)
    status = "queued"
    error_code = None
    try:
        config = await resolve_email_config_async(raise_on_error=True)
    except SystemEmailConfigResolutionError:
        config = None
        status = "retry_wait"
        error_code = "email_config_lookup_failed"
    if config is None and status == "queued":
        status = "blocked_configuration"
        error_code = "email_configuration_unavailable"
    now = _now()
    delivery = OutboundEmailDelivery(
        purpose=purpose,
        tenant_id=tenant_id,
        identity_id=identity_id,
        invitation_id=invitation_id,
        requested_by_user_id=requested_by_user_id,
        recipient_hash=_recipient_hash(email),
        recipient_mask=mask_email_address(email),
        payload_envelope=seal_outbound_email_payload(
            {"to": email, "subject": subject, "body": body}
        ),
        status=status,
        attempt_count=0,
        max_attempts=max(1, min(max_attempts, 10)),
        next_attempt_at=(
            now + timedelta(seconds=_CONFIGURATION_RECHECK_SECONDS)
            if status == "blocked_configuration"
            else now
        ),
        last_error_code=error_code,
        last_error_at=now if error_code else None,
        idempotency_key=idempotency_key.strip()[:160] if idempotency_key else None,
    )
    db.add(delivery)
    await db.flush()
    return delivery


async def persist_template_email(**kwargs: Any) -> OutboundEmailDelivery:
    """Persist a delivery in its own short transaction for auth flows."""

    async with async_session() as db:
        delivery = await enqueue_template_email(db, **kwargs)
        await db.commit()
        return delivery


async def cancel_invitation_deliveries(
    db: AsyncSession,
    invitation_id: uuid.UUID,
    *,
    error_code: str = "invitation_rotated_or_revoked",
) -> int:
    result = await db.execute(
        update(OutboundEmailDelivery)
        .where(
            OutboundEmailDelivery.invitation_id == invitation_id,
            OutboundEmailDelivery.status.in_(tuple(ACTIVE_DELIVERY_STATUSES)),
        )
        .values(
            status="cancelled",
            claim_token=None,
            claimed_at=None,
            next_attempt_at=None,
            last_error_code=error_code,
            last_error_at=_now(),
        )
    )
    return int(result.rowcount or 0)


def _error_outcome(exc: Exception, attempt_count: int, max_attempts: int) -> tuple[str, str]:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "blocked_configuration", "smtp_authentication_failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "permanent_failed", "smtp_recipient_refused"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "blocked_configuration", "smtp_sender_refused"
    if isinstance(exc, smtplib.SMTPResponseException):
        if 400 <= int(exc.smtp_code) < 500 and attempt_count < max_attempts:
            return "retry_wait", "smtp_transient_response"
        return "permanent_failed", "smtp_permanent_response"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, smtplib.SMTPServerDisconnected)):
        if attempt_count < max_attempts:
            return "retry_wait", "smtp_transport_unavailable"
        return "permanent_failed", "smtp_retry_exhausted"
    return "permanent_failed", "email_delivery_internal_error"


async def dispatch_outbound_email(delivery_id: uuid.UUID) -> str:
    """Claim, send and finalize one delivery with a fenced claim token."""

    try:
        config = await resolve_email_config_async(raise_on_error=True)
    except SystemEmailConfigResolutionError:
        config = None
        config_error = "email_config_lookup_failed"
    else:
        config_error = "email_configuration_unavailable"

    claim_token = uuid.uuid4()
    payload_envelope = ""
    attempt_count = 0
    max_attempts = 0
    async with async_session() as db:
        async with db.begin():
            result = await db.execute(
                select(OutboundEmailDelivery)
                .where(OutboundEmailDelivery.id == delivery_id)
                .with_for_update(skip_locked=True)
            )
            delivery = result.scalar_one_or_none()
            if delivery is None:
                return "not_found"
            if delivery.status in TERMINAL_DELIVERY_STATUSES or delivery.status == "sending":
                return delivery.status
            if config is None:
                delivery.status = "blocked_configuration" if config_error == "email_configuration_unavailable" else "retry_wait"
                delivery.last_error_code = config_error
                delivery.last_error_at = _now()
                delivery.next_attempt_at = (
                    _now() + timedelta(seconds=_RETRY_DELAYS_SECONDS[0])
                    if delivery.status == "retry_wait"
                    else _now() + timedelta(seconds=_CONFIGURATION_RECHECK_SECONDS)
                )
                return delivery.status
            if delivery.attempt_count >= delivery.max_attempts:
                delivery.status = "permanent_failed"
                delivery.last_error_code = "smtp_retry_exhausted"
                delivery.last_error_at = _now()
                delivery.next_attempt_at = None
                return delivery.status
            delivery.status = "sending"
            delivery.attempt_count += 1
            delivery.claim_token = claim_token
            delivery.claimed_at = _now()
            delivery.next_attempt_at = None
            payload_envelope = delivery.payload_envelope
            attempt_count = delivery.attempt_count
            max_attempts = delivery.max_attempts

    try:
        payload = open_outbound_email_payload(payload_envelope)
        await asyncio.to_thread(
            _send_email_with_config_sync,
            config,
            payload["to"],
            payload["subject"],
            payload["body"],
        )
    except Exception as exc:
        next_status, error_code = _error_outcome(exc, attempt_count, max_attempts)
        async with async_session() as db:
            async with db.begin():
                result = await db.execute(
                    select(OutboundEmailDelivery)
                    .where(
                        OutboundEmailDelivery.id == delivery_id,
                        OutboundEmailDelivery.claim_token == claim_token,
                    )
                    .with_for_update()
                )
                delivery = result.scalar_one_or_none()
                if delivery is None:
                    return "claim_lost"
                delivery.status = next_status
                delivery.claim_token = None
                delivery.claimed_at = None
                delivery.last_error_code = error_code
                delivery.last_error_at = _now()
                if next_status in {"retry_wait", "blocked_configuration"}:
                    delay_index = min(max(attempt_count - 1, 0), len(_RETRY_DELAYS_SECONDS) - 1)
                    delay_seconds = (
                        _CONFIGURATION_RECHECK_SECONDS
                        if next_status == "blocked_configuration"
                        else _RETRY_DELAYS_SECONDS[delay_index]
                    )
                    delivery.next_attempt_at = _now() + timedelta(seconds=delay_seconds)
                else:
                    delivery.next_attempt_at = None
        logger.warning(
            "System email delivery failed delivery_id={} error_code={}",
            delivery_id,
            error_code,
        )
        return next_status

    accepted_at = _now()
    async with async_session() as db:
        async with db.begin():
            result = await db.execute(
                select(OutboundEmailDelivery)
                .where(
                    OutboundEmailDelivery.id == delivery_id,
                    OutboundEmailDelivery.claim_token == claim_token,
                )
                .with_for_update()
            )
            delivery = result.scalar_one_or_none()
            if delivery is None:
                return "claim_lost"
            delivery.status = "smtp_accepted"
            delivery.claim_token = None
            delivery.claimed_at = None
            delivery.next_attempt_at = None
            delivery.last_error_code = None
            delivery.last_error_at = None
            delivery.smtp_accepted_at = accepted_at
            delivery.transport_receipt = {
                "evidence_level": "smtp_accepted",
                "accepted_at": accepted_at.isoformat(),
                "smtp_host_sha256": hashlib.sha256(config.smtp_host.encode("utf-8")).hexdigest(),
                "smtp_port": config.smtp_port,
                "tls_mode": "implicit_tls" if config.smtp_ssl else "starttls_if_advertised",
            }
    return "smtp_accepted"


async def dispatch_pending_outbound_emails(*, limit: int = 25) -> int:
    """Recover stale claims and dispatch a bounded due batch."""

    now = _now()
    stale_before = now - timedelta(minutes=5)
    async with async_session() as db:
        async with db.begin():
            await db.execute(
                update(OutboundEmailDelivery)
                .where(
                    OutboundEmailDelivery.status == "sending",
                    OutboundEmailDelivery.claimed_at < stale_before,
                )
                .values(
                    status="retry_wait",
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=now,
                    last_error_code="stale_delivery_claim_recovered",
                    last_error_at=now,
                )
            )
            result = await db.execute(
                select(OutboundEmailDelivery.id)
                .where(
                    OutboundEmailDelivery.status.in_(
                        ("queued", "retry_wait", "blocked_configuration")
                    ),
                    or_(
                        OutboundEmailDelivery.next_attempt_at.is_(None),
                        OutboundEmailDelivery.next_attempt_at <= now,
                    ),
                )
                .order_by(OutboundEmailDelivery.created_at.asc())
                .limit(max(1, min(limit, 100)))
            )
            ids = list(result.scalars().all())
    for delivery_id in ids:
        await dispatch_outbound_email(delivery_id)
    return len(ids)


async def start_outbound_email_daemon() -> None:
    """Run the durable email worker for the lifetime of the worker process."""

    interval = max(1, int(get_settings().OUTBOUND_EMAIL_POLL_SECONDS))
    logger.info("[outbound_email] daemon started")
    while True:
        try:
            await dispatch_pending_outbound_emails()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[outbound_email] daemon iteration failed")
        await asyncio.sleep(interval)


__all__ = [
    "ACTIVE_DELIVERY_STATUSES",
    "cancel_invitation_deliveries",
    "delivery_public_payload",
    "dispatch_outbound_email",
    "dispatch_pending_outbound_emails",
    "enqueue_template_email",
    "mask_email_address",
    "persist_template_email",
    "start_outbound_email_daemon",
]
