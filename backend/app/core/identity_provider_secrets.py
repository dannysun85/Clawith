"""Authenticated encryption for complete IdentityProvider configuration objects."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data


IDENTITY_PROVIDER_CONFIG_PREFIX = "enc:idp:v1:"
_PURPOSE = "identity_provider_config"


def _authentication_key(secret_key: str) -> bytes:
    return hashlib.sha256(
        f"astra-identity-provider-envelope-v1\0{_PURPOSE}\0{secret_key}".encode(
            "utf-8"
        )
    ).digest()


def _normalize_config(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Identity provider config must be a JSON object")


def is_identity_provider_config_envelope(value: object) -> bool:
    return isinstance(value, str) and value.startswith(
        IDENTITY_PROVIDER_CONFIG_PREFIX
    )


def seal_identity_provider_config(value: Any) -> str:
    """Serialize and authenticate the complete provider config object."""
    if is_identity_provider_config_envelope(value):
        # Validate already-encrypted values instead of ever double-wrapping or
        # silently persisting a malformed envelope.
        open_identity_provider_config(value)
        return value
    plaintext = json.dumps(
        _normalize_config(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    secret_key = get_settings().SECRET_KEY
    ciphertext = encrypt_data(plaintext, secret_key)
    signature = hmac.new(
        _authentication_key(secret_key),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{IDENTITY_PROVIDER_CONFIG_PREFIX}{signature}:{ciphertext}"


def open_identity_provider_config(value: Any) -> dict:
    """Open an encrypted config while dual-reading legacy JSON during rollout."""
    if isinstance(value, dict):
        return value
    if not is_identity_provider_config_envelope(value):
        return _normalize_config(value)
    payload = value[len(IDENTITY_PROVIDER_CONFIG_PREFIX) :]
    try:
        signature, ciphertext = payload.split(":", 1)
    except ValueError as exc:
        raise ValueError("Malformed identity provider config envelope") from exc
    secret_key = get_settings().SECRET_KEY
    expected = hmac.new(
        _authentication_key(secret_key),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Identity provider config authentication failed")
    return _normalize_config(decrypt_data(ciphertext, secret_key))


def seal_legacy_identity_provider_config(value: Any) -> str | None:
    """Backfill one nullable legacy JSON value without exposing its contents."""
    if value is None:
        return None
    return seal_identity_provider_config(value)


class EncryptedIdentityProviderJSON(TypeDecorator[dict]):
    """Transparent encrypted-at-rest JSON with legacy JSON dual-read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return seal_identity_provider_config(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return open_identity_provider_config(value)
