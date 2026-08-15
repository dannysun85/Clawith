"""TOTP MFA, recovery codes, and database-fenced authentication ceremonies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from jose import JWTError, jwt
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.mfa_crypto import open_mfa_secret, recovery_code_digest, seal_mfa_secret
from app.models.identity_mfa import IdentityMfaChallenge, IdentityMfaRecoveryCode


TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_WINDOW_STEPS = 1
MFA_CHALLENGE_MINUTES = 5
MFA_MAX_FAILED_ATTEMPTS = 8
RECOVERY_CODE_COUNT = 10
_MFA_CHALLENGE_TYPE = "identity_mfa_challenge"


class MfaChallengeError(ValueError):
    """Stable MFA ceremony failure which never includes a submitted factor."""

    def __init__(self, code: str, message: str = "MFA challenge is invalid or expired"):
        self.code = code
        super().__init__(message)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def identity_requires_mfa(user: object) -> bool:
    """Return whether one membership/global role must enroll in MFA."""

    identity = getattr(user, "identity", None)
    return bool(
        getattr(identity, "is_platform_admin", False)
        or getattr(user, "role", None) in {"platform_admin", "org_owner", "org_admin"}
    )


def generate_totp_secret() -> str:
    """Generate a 160-bit RFC 4226/6238 base32 seed without padding."""

    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_base32(secret: str) -> bytes:
    normalized = "".join(secret.strip().upper().split())
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise ValueError("Invalid TOTP secret") from exc


def totp_code(secret: str, *, at_time: datetime | int | float | None = None) -> str:
    """Compute one RFC 6238 SHA-1 TOTP code."""

    if at_time is None:
        timestamp = utc_now().timestamp()
    elif isinstance(at_time, datetime):
        if at_time.tzinfo is None:
            at_time = at_time.replace(tzinfo=timezone.utc)
        timestamp = at_time.timestamp()
    else:
        timestamp = float(at_time)
    step = int(timestamp // TOTP_PERIOD_SECONDS)
    digest = hmac.new(
        _decode_base32(secret),
        struct.pack(">Q", step),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def matching_totp_step(
    secret: str,
    code: str,
    *,
    at_time: datetime | int | float | None = None,
    window_steps: int = TOTP_WINDOW_STEPS,
) -> int | None:
    """Return the matching time step, accepting only a small clock window."""

    submitted = code.strip()
    if len(submitted) != TOTP_DIGITS or not submitted.isdigit():
        return None
    if at_time is None:
        timestamp = utc_now().timestamp()
    elif isinstance(at_time, datetime):
        if at_time.tzinfo is None:
            at_time = at_time.replace(tzinfo=timezone.utc)
        timestamp = at_time.timestamp()
    else:
        timestamp = float(at_time)
    current_step = int(timestamp // TOTP_PERIOD_SECONDS)
    for offset in range(-window_steps, window_steps + 1):
        step = current_step + offset
        expected = totp_code(secret, at_time=step * TOTP_PERIOD_SECONDS)
        if hmac.compare_digest(expected, submitted):
            return step
    return None


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Generate 80-bit, human-readable one-time recovery codes."""

    codes: list[str] = []
    for _ in range(count):
        raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
        codes.append("-".join(raw[index : index + 4] for index in range(0, 16, 4)))
    return codes


def provisioning_uri(secret: str, *, account_name: str) -> str:
    """Build a standards-compatible otpauth URI for authenticator apps."""

    issuer = get_settings().APP_NAME.strip() or "Astra"
    label = quote(f"{issuer}:{account_name}", safe="")
    return (
        f"otpauth://totp/{label}?secret={quote(secret, safe='')}"
        f"&issuer={quote(issuer, safe='')}&algorithm=SHA1"
        f"&digits={TOTP_DIGITS}&period={TOTP_PERIOD_SECONDS}"
    )


def _challenge_token(challenge: IdentityMfaChallenge) -> str:
    settings = get_settings()
    payload = {
        "typ": _MFA_CHALLENGE_TYPE,
        "cid": str(challenge.id),
        "iid": str(challenge.identity_id),
        "uid": str(challenge.user_id),
        "purpose": challenge.purpose,
        "av": challenge.auth_version,
        "exp": challenge.expires_at,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_challenge_token(token: str) -> dict[str, object]:
    """Validate and normalize a signed MFA challenge token."""

    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("typ") != _MFA_CHALLENGE_TYPE:
            raise MfaChallengeError("challenge_type_invalid")
        normalized = {
            "challenge_id": uuid.UUID(str(payload["cid"])),
            "identity_id": uuid.UUID(str(payload["iid"])),
            "user_id": uuid.UUID(str(payload["uid"])),
            "purpose": str(payload["purpose"]),
            "auth_version": int(payload["av"]),
        }
    except MfaChallengeError:
        raise
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise MfaChallengeError("challenge_invalid") from exc
    return normalized


async def create_mfa_challenge(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
    user_id: uuid.UUID,
    auth_version: int,
    purpose: str,
    secret: str | None = None,
    now: datetime | None = None,
) -> tuple[IdentityMfaChallenge, str]:
    """Persist a short-lived challenge before returning its signed handle."""

    if purpose not in {"login", "bootstrap", "setup"}:
        raise ValueError("Unsupported MFA challenge purpose")
    issued_at = now or utc_now()
    challenge = IdentityMfaChallenge(
        id=uuid.uuid4(),
        identity_id=identity_id,
        user_id=user_id,
        purpose=purpose,
        auth_version=max(0, int(auth_version)),
        secret_envelope=seal_mfa_secret(secret) if secret else None,
        failed_attempts=0,
        expires_at=issued_at + timedelta(minutes=MFA_CHALLENGE_MINUTES),
    )
    db.add(challenge)
    await db.flush()
    return challenge, _challenge_token(challenge)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def require_live_challenge(
    db: AsyncSession,
    token: str,
    *,
    purposes: set[str],
    now: datetime | None = None,
) -> IdentityMfaChallenge:
    """Lock and validate the database half of a signed challenge."""

    claims = decode_challenge_token(token)
    result = await db.execute(
        select(IdentityMfaChallenge)
        .where(IdentityMfaChallenge.id == claims["challenge_id"])
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()
    current = now or utc_now()
    if (
        challenge is None
        or challenge.purpose not in purposes
        or challenge.purpose != claims["purpose"]
        or challenge.identity_id != claims["identity_id"]
        or challenge.user_id != claims["user_id"]
        or challenge.auth_version != claims["auth_version"]
        or challenge.consumed_at is not None
        or challenge.failed_attempts >= MFA_MAX_FAILED_ATTEMPTS
        or _aware(challenge.expires_at) <= current
    ):
        raise MfaChallengeError("challenge_invalid")
    return challenge


def record_challenge_failure(
    challenge: IdentityMfaChallenge,
    *,
    now: datetime | None = None,
) -> None:
    """Consume a challenge after the bounded number of failed attempts."""

    challenge.failed_attempts = min(
        MFA_MAX_FAILED_ATTEMPTS,
        int(challenge.failed_attempts or 0) + 1,
    )
    if challenge.failed_attempts >= MFA_MAX_FAILED_ATTEMPTS:
        challenge.consumed_at = now or utc_now()


async def replace_recovery_codes(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
) -> list[str]:
    """Revoke every prior code and persist a fresh one-time set."""

    await db.execute(
        delete(IdentityMfaRecoveryCode).where(
            IdentityMfaRecoveryCode.identity_id == identity_id
        )
    )
    raw_codes = generate_recovery_codes()
    db.add_all(
        [
            IdentityMfaRecoveryCode(
                id=uuid.uuid4(),
                identity_id=identity_id,
                code_hash=recovery_code_digest(identity_id, code),
            )
            for code in raw_codes
        ]
    )
    return raw_codes


async def recovery_codes_remaining(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
) -> int:
    result = await db.execute(
        select(func.count(IdentityMfaRecoveryCode.id)).where(
            IdentityMfaRecoveryCode.identity_id == identity_id,
            IdentityMfaRecoveryCode.used_at.is_(None),
        )
    )
    return int(result.scalar_one() or 0)


async def verify_identity_factor(
    db: AsyncSession,
    *,
    identity: object,
    code: str,
    now: datetime | None = None,
) -> str | None:
    """Verify TOTP or consume one recovery code under an Identity row lock."""

    submitted = code.strip()
    envelope = getattr(identity, "mfa_secret_envelope", None)
    if submitted.isdigit() and len(submitted) == TOTP_DIGITS and envelope:
        step = matching_totp_step(open_mfa_secret(envelope), submitted, at_time=now)
        last_step = getattr(identity, "mfa_last_totp_step", None)
        if step is None or (last_step is not None and step <= int(last_step)):
            return None
        identity.mfa_last_totp_step = step
        return "totp"

    digest = recovery_code_digest(getattr(identity, "id"), submitted)
    result = await db.execute(
        select(IdentityMfaRecoveryCode)
        .where(
            IdentityMfaRecoveryCode.identity_id == getattr(identity, "id"),
            IdentityMfaRecoveryCode.code_hash == digest,
            IdentityMfaRecoveryCode.used_at.is_(None),
        )
        .with_for_update()
    )
    recovery = result.scalar_one_or_none()
    if recovery is None:
        return None
    recovery.used_at = now or utc_now()
    return "recovery_code"


__all__ = [
    "MFA_CHALLENGE_MINUTES",
    "MFA_MAX_FAILED_ATTEMPTS",
    "MfaChallengeError",
    "create_mfa_challenge",
    "decode_challenge_token",
    "generate_recovery_codes",
    "generate_totp_secret",
    "identity_requires_mfa",
    "matching_totp_step",
    "open_mfa_secret",
    "provisioning_uri",
    "record_challenge_failure",
    "recovery_codes_remaining",
    "replace_recovery_codes",
    "require_live_challenge",
    "seal_mfa_secret",
    "totp_code",
    "utc_now",
    "verify_identity_factor",
]
