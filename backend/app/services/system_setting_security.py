"""Encryption and response masking for platform-owned system settings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data


CONFIGURED_SECRET_PLACEHOLDER = "••••••••"

# Keep this allowlist intentionally small. Tenant/company Tool credentials
# belong in tenant_settings and Agent-specific credentials belong in AgentTool.
SYSTEM_SETTING_SECRET_FIELDS: dict[str, frozenset[str]] = {
    "jina_api_key": frozenset({"api_key"}),  # legacy platform fallback only
    "system_email_platform": frozenset({"SYSTEM_SMTP_PASSWORD"}),
}


def is_sensitive_system_setting(key: str) -> bool:
    return key in SYSTEM_SETTING_SECRET_FIELDS or key.startswith(
        "legacy_tool_config_quarantine:"
    )


def _decrypt_secret(value: str) -> str:
    if not value:
        return value
    try:
        return decrypt_data(value, get_settings().SECRET_KEY)
    except Exception:
        # Compatibility for rows written by older releases. Startup migration
        # rewrites these values at rest; runtime must remain available during
        # a rolling deployment where an old plaintext row may still exist.
        return value


def decrypt_system_setting_value(key: str, value: Any) -> Any:
    """Return a runtime copy with known platform secrets decrypted in memory."""
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    for field in SYSTEM_SETTING_SECRET_FIELDS.get(key, frozenset()):
        raw = result.get(field)
        if isinstance(raw, str) and raw:
            result[field] = _decrypt_secret(raw)
    return result


def encrypt_system_setting_value(
    key: str,
    value: dict[str, Any],
    *,
    existing_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Encrypt known fields and preserve an existing secret placeholder.

    The settings API treats its JSON body as a replacement. Omitting a secret
    therefore clears it; sending the configured placeholder preserves the
    existing encrypted value without ever returning the plaintext to a browser.
    """
    result = deepcopy(value)
    existing = dict(existing_value or {})
    for field in SYSTEM_SETTING_SECRET_FIELDS.get(key, frozenset()):
        raw = result.get(field)
        if raw == CONFIGURED_SECRET_PLACEHOLDER:
            existing_secret = existing.get(field)
            if isinstance(existing_secret, str) and existing_secret:
                try:
                    decrypt_data(existing_secret, get_settings().SECRET_KEY)
                    result[field] = existing_secret
                except Exception:
                    result[field] = encrypt_data(
                        existing_secret,
                        get_settings().SECRET_KEY,
                    )
            else:
                result.pop(field, None)
            continue
        if not isinstance(raw, str) or not raw:
            result.pop(field, None)
            continue
        try:
            decrypt_data(raw, get_settings().SECRET_KEY)
            encrypted = raw
        except Exception:
            encrypted = encrypt_data(raw, get_settings().SECRET_KEY)
        if decrypt_data(encrypted, get_settings().SECRET_KEY) != _decrypt_secret(raw):
            raise RuntimeError("system setting secret encryption verification failed")
        result[field] = encrypted
    return result


def mask_system_setting_value(key: str, value: Any) -> Any:
    """Return an API-safe copy without plaintext or ciphertext secrets."""
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    configured: list[str] = []
    for field in SYSTEM_SETTING_SECRET_FIELDS.get(key, frozenset()):
        raw = result.pop(field, None)
        if isinstance(raw, str) and raw:
            result[field] = CONFIGURED_SECRET_PLACEHOLDER
            configured.append(field)
    if configured:
        result["_configured_secret_fields"] = sorted(configured)
    else:
        result.pop("_configured_secret_fields", None)
    if key.startswith("legacy_tool_config_quarantine:"):
        # Recovery records are intentionally not browsable through the generic
        # settings API. This fallback keeps accidental serialization harmless.
        return {
            "runtime_enabled": False,
            "quarantined": True,
        }
    return result


async def migrate_sensitive_system_settings() -> int:
    """Encrypt plaintext secrets left by releases before this boundary."""
    from sqlalchemy import select

    from app.database import async_session
    from app.models.system_settings import SystemSetting

    migrated = 0
    async with async_session() as db:
        result = await db.execute(
            select(SystemSetting).where(
                SystemSetting.key.in_(tuple(SYSTEM_SETTING_SECRET_FIELDS))
            )
        )
        for setting in result.scalars().all():
            secured = encrypt_system_setting_value(
                setting.key,
                dict(setting.value or {}),
                existing_value=dict(setting.value or {}),
            )
            if secured != (setting.value or {}):
                setting.value = secured
                migrated += 1
        if migrated:
            await db.commit()
    return migrated


__all__ = [
    "CONFIGURED_SECRET_PLACEHOLDER",
    "decrypt_system_setting_value",
    "encrypt_system_setting_value",
    "is_sensitive_system_setting",
    "mask_system_setting_value",
    "migrate_sensitive_system_settings",
]
