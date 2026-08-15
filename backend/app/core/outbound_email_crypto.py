"""Authenticated envelope for retryable system-email payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data


OUTBOUND_EMAIL_ENVELOPE_PREFIX = "enc:outbound-email:v1:"
_PURPOSE = "outbound_email_payload"


def _authentication_key(secret_key: str) -> bytes:
    return hashlib.sha256(
        f"astra-outbound-email-envelope-v1\0{_PURPOSE}\0{secret_key}".encode("utf-8")
    ).digest()


def seal_outbound_email_payload(value: dict[str, Any]) -> str:
    """Encrypt and authenticate an immutable email payload."""

    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    secret_key = get_settings().SECRET_KEY
    ciphertext = encrypt_data(plaintext, secret_key)
    signature = hmac.new(
        _authentication_key(secret_key),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{OUTBOUND_EMAIL_ENVELOPE_PREFIX}{signature}:{ciphertext}"


def open_outbound_email_payload(value: str) -> dict[str, Any]:
    """Verify and decrypt one email payload; malformed values fail closed."""

    if not isinstance(value, str) or not value.startswith(OUTBOUND_EMAIL_ENVELOPE_PREFIX):
        raise ValueError("Outbound email payload envelope is required")
    raw = value[len(OUTBOUND_EMAIL_ENVELOPE_PREFIX) :]
    try:
        signature, ciphertext = raw.split(":", 1)
    except ValueError as exc:
        raise ValueError("Malformed outbound email payload envelope") from exc
    secret_key = get_settings().SECRET_KEY
    expected = hmac.new(
        _authentication_key(secret_key),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Outbound email payload authentication failed")
    decoded = json.loads(decrypt_data(ciphertext, secret_key))
    if not isinstance(decoded, dict):
        raise ValueError("Outbound email payload must be an object")
    required = {"to", "subject", "body"}
    if not required.issubset(decoded) or any(not isinstance(decoded[key], str) for key in required):
        raise ValueError("Outbound email payload is incomplete")
    return decoded


__all__ = [
    "OUTBOUND_EMAIL_ENVELOPE_PREFIX",
    "open_outbound_email_payload",
    "seal_outbound_email_payload",
]
