"""Tool configuration helpers.

Builtin tools are global capability records, so tenant/company configuration
must not live in ``tools.config`` for those rows. Tenant-specific values are
stored in ``tenant_settings`` under ``tool_config:<tool_name>``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.tenant_setting import TenantSetting
from app.models.tool import Tool


SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret"}
TENANT_TOOL_CONFIG_PREFIX = "tool_config:"
TOOL_FUNCTION_NAME_MAX_LENGTH = 64


def tenant_scoped_tool_name(name: str, tenant_id: uuid.UUID | str | None) -> str:
    """Return a deterministic, globally unique internal name for tenant tools.

    ``tools.name`` remains globally unique because older application versions
    query it as a scalar.  Tenant-created tools therefore use an internal name
    that cannot shadow a builtin or another tenant's tool.  ``display_name``
    and ``mcp_tool_name`` retain the user-facing/upstream names.

    Keep the generated name within the conservative 64-character function
    calling limit even though the database column allows 100 characters.
    """

    logical_name = (name or "").strip()
    if tenant_id is None:
        return logical_name[:100]

    resolved_tenant_id = (
        tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    )
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", logical_name).strip("_-") or "tool"
    digest = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:12]
    suffix = f"__t_{resolved_tenant_id.hex}_{digest}"
    prefix_length = TOOL_FUNCTION_NAME_MAX_LENGTH - len(suffix)
    prefix = safe_name[:prefix_length].rstrip("_-") or "tool"
    return f"{prefix}{suffix}"


def tenant_tool_config_key(tool_name: str) -> str:
    return f"{TENANT_TOOL_CONFIG_PREFIX}{tool_name}"


def get_sensitive_keys(config_schema: dict | None = None) -> set[str]:
    keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                keys.add(field.get("key", ""))
    keys.discard("")
    return keys


def encrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    if not config:
        return config

    settings = get_settings()
    result = dict(config)
    for key in get_sensitive_keys(config_schema):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            decrypt_data(value, settings.SECRET_KEY)
            continue
        except Exception:
            pass
        try:
            result[key] = encrypt_data(value, settings.SECRET_KEY)
        except Exception:
            pass
    return result


def decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    if not config:
        return config

    settings = get_settings()
    result = dict(config)
    for key in get_sensitive_keys(config_schema):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            result[key] = decrypt_data(value, settings.SECRET_KEY)
        except Exception:
            pass
    return result


def meaningful_config(config: dict | None) -> dict:
    """Drop empty form values while preserving booleans/numbers."""
    if not config:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned


async def get_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    tool_name: str,
    config_schema: dict | None = None,
) -> dict:
    if not tenant_id:
        return {}
    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == tenant_tool_config_key(tool_name),
        )
    )
    setting = result.scalar_one_or_none()
    raw = (setting.value or {}).get("config", {}) if setting else {}
    return decrypt_sensitive_fields(raw, config_schema)


async def set_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_name: str,
    config: dict,
    config_schema: dict | None = None,
) -> None:
    encrypted = encrypt_sensitive_fields(meaningful_config(config), config_schema)
    key = tenant_tool_config_key(tool_name)
    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = {"config": encrypted}
    else:
        db.add(TenantSetting(tenant_id=tenant_id, key=key, value={"config": encrypted}))


async def delete_tenant_tool_config(db: AsyncSession, tenant_id: uuid.UUID, tool_name: str) -> None:
    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == tenant_tool_config_key(tool_name),
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        await db.delete(existing)


async def get_tool_company_config(db: AsyncSession, tool: Tool, tenant_id: uuid.UUID | None) -> dict:
    """Return company config for a tool without leaking builtin config across tenants."""
    if tool.source == "builtin":
        return await get_tenant_tool_config(db, tenant_id, tool.name, tool.config_schema)
    return decrypt_sensitive_fields(tool.config or {}, tool.config_schema)


def mask_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    masked = dict(config or {})
    for key in get_sensitive_keys(config_schema):
        value = masked.get(key)
        if value and isinstance(value, str):
            suffix = value[-4:] if len(value) > 4 else value
            masked[key] = f"****{suffix}"
    return masked


def merge_config_preserving_sensitive(
    existing: dict | None,
    incoming: dict | None,
    config_schema: dict | None = None,
) -> dict:
    """Merge a UI update without replacing secrets with masks or blanks.

    An explicitly empty object still means "reset this override". For a
    non-empty update, omitted/blank/masked sensitive values preserve the
    existing secret while ordinary fields follow the submitted document.
    """

    if not incoming:
        return {}
    current = dict(existing or {})
    merged = dict(incoming)
    for key in get_sensitive_keys(config_schema):
        value = incoming.get(key)
        is_placeholder = isinstance(value, str) and value.startswith("****")
        if key not in incoming or value in (None, "") or is_placeholder:
            if key in current:
                merged[key] = current[key]
            else:
                merged.pop(key, None)
    return meaningful_config(merged)
