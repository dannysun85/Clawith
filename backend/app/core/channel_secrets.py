"""Authenticated, versioned encryption for ChannelConfig credentials."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data


CHANNEL_SECRET_ENVELOPE_PREFIX = "enc:channel:v1:"


def _authentication_key(secret_key: str, purpose: str) -> bytes:
    return hashlib.sha256(
        f"astra-channel-envelope-v1\0{purpose}\0{secret_key}".encode("utf-8")
    ).digest()


def seal_channel_secret(plaintext: str, *, purpose: str) -> str:
    """Encrypt and authenticate one value for a specific channel field."""
    if plaintext == "":
        return ""
    secret_key = get_settings().SECRET_KEY
    ciphertext = encrypt_data(plaintext, secret_key)
    signature = hmac.new(
        _authentication_key(secret_key, purpose),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{CHANNEL_SECRET_ENVELOPE_PREFIX}{signature}:{ciphertext}"


def is_channel_secret_envelope(value: object) -> bool:
    return isinstance(value, str) and value.startswith(CHANNEL_SECRET_ENVELOPE_PREFIX)


def open_channel_secret(value: str, *, purpose: str) -> str:
    """Open an envelope, while allowing pre-migration plaintext reads."""
    if value == "" or not is_channel_secret_envelope(value):
        return value
    payload = value[len(CHANNEL_SECRET_ENVELOPE_PREFIX):]
    try:
        signature, ciphertext = payload.split(":", 1)
    except ValueError as exc:
        raise ValueError("Malformed channel secret envelope") from exc
    secret_key = get_settings().SECRET_KEY
    expected = hmac.new(
        _authentication_key(secret_key, purpose),
        ciphertext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Channel secret envelope authentication failed")
    return decrypt_data(ciphertext, secret_key)


def seal_legacy_channel_secret(value: str | None, *, purpose: str) -> str | None:
    """Backfill one plaintext value without double-wrapping a valid envelope."""
    if value is None or value == "":
        return value
    if is_channel_secret_envelope(value):
        open_channel_secret(value, purpose=purpose)
        return value
    return seal_channel_secret(value, purpose=purpose)


def _normalize_json_object(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Channel extra_config must be a JSON object")


def seal_legacy_channel_json(value: Any, *, purpose: str) -> str:
    """Backfill a legacy JSON object into one authenticated ciphertext."""
    if is_channel_secret_envelope(value):
        plaintext = open_channel_secret(value, purpose=purpose)
        _normalize_json_object(plaintext)
        return value
    plaintext = json.dumps(
        _normalize_json_object(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return seal_channel_secret(plaintext, purpose=purpose)


class EncryptedChannelText(TypeDecorator[str]):
    """Transparent encrypted-at-rest text with legacy plaintext dual-read."""

    impl = Text
    cache_ok = True

    def __init__(self, *, purpose: str) -> None:
        super().__init__()
        self.purpose = purpose

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return seal_channel_secret(str(value), purpose=self.purpose)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return open_channel_secret(str(value), purpose=self.purpose)


class EncryptedChannelJSON(TypeDecorator[dict]):
    """Encrypt the complete JSON object so unknown future secrets stay safe."""

    impl = Text
    cache_ok = True

    def __init__(self, *, purpose: str) -> None:
        super().__init__()
        self.purpose = purpose

    def process_bind_param(self, value, dialect):
        plaintext = json.dumps(
            _normalize_json_object(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return seal_channel_secret(plaintext, purpose=self.purpose)

    def process_result_value(self, value, dialect):
        if isinstance(value, dict):
            # Compatibility with a database temporarily downgraded to JSON.
            return value
        plaintext = open_channel_secret(str(value or "{}"), purpose=self.purpose)
        return _normalize_json_object(plaintext)
