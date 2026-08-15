"""Authenticated envelope and keyed digests for Identity MFA secrets."""

from __future__ import annotations

import hashlib
import hmac

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data


MFA_SECRET_ENVELOPE_PREFIX = "enc:identity-mfa:v1:"


def _key(purpose: str) -> bytes:
    settings = get_settings()
    return hashlib.sha256(
        f"astra-identity-mfa-v1\0{purpose}\0{settings.SECRET_KEY}".encode()
    ).digest()


def seal_mfa_secret(secret: str) -> str:
    """Encrypt and authenticate a base32 TOTP seed."""

    normalized = secret.strip().upper()
    if not normalized:
        raise ValueError("MFA secret is required")
    ciphertext = encrypt_data(normalized, get_settings().SECRET_KEY)
    signature = hmac.new(
        _key("totp-secret"), ciphertext.encode(), hashlib.sha256
    ).hexdigest()
    return f"{MFA_SECRET_ENVELOPE_PREFIX}{signature}:{ciphertext}"


def open_mfa_secret(envelope: str) -> str:
    """Verify and decrypt a TOTP seed; malformed values fail closed."""

    if not isinstance(envelope, str) or not envelope.startswith(
        MFA_SECRET_ENVELOPE_PREFIX
    ):
        raise ValueError("Authenticated MFA secret envelope is required")
    raw = envelope[len(MFA_SECRET_ENVELOPE_PREFIX) :]
    try:
        signature, ciphertext = raw.split(":", 1)
    except ValueError as exc:
        raise ValueError("Malformed MFA secret envelope") from exc
    expected = hmac.new(
        _key("totp-secret"), ciphertext.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("MFA secret authentication failed")
    secret = decrypt_data(ciphertext, get_settings().SECRET_KEY).strip().upper()
    if not secret:
        raise ValueError("MFA secret is empty")
    return secret


def recovery_code_digest(identity_id: object, code: str) -> str:
    """Return a non-reversible, Identity-bound recovery-code digest."""

    normalized = "".join(character for character in code.upper() if character.isalnum())
    return hmac.new(
        _key("recovery-code"),
        f"{identity_id}:{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()


__all__ = [
    "MFA_SECRET_ENVELOPE_PREFIX",
    "open_mfa_secret",
    "recovery_code_digest",
    "seal_mfa_secret",
]
