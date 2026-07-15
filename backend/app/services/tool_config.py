"""Tool configuration helpers.

Builtin tools are global capability records, so tenant/company configuration
must not live in ``tools.config`` for those rows. Tenant-specific values are
stored in ``tenant_settings`` under ``tool_config:<tool_name>``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.tenant_setting import TenantSetting
from app.models.tool import Tool
from app.services.mcp_security import is_sensitive_mcp_query_key


SENSITIVE_FIELD_KEYS = {
    "api_key",
    "private_key",
    "auth_code",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "atlassian_api_key",
    "smithery_api_key",
    "modelscope_api_token",
    # JSON-encoded key/value pairs removed from persisted MCP URLs.
    "mcp_url_query_secrets",
}
TENANT_TOOL_CONFIG_PREFIX = "tool_config:"
TOOL_FUNCTION_NAME_MAX_LENGTH = 64


def tenant_scoped_tool_name(
    name: str,
    tenant_id: uuid.UUID | str | None,
    *,
    namespace: str | None = None,
) -> str:
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
    # Preserve the pre-namespace digest for every existing caller. Only MCP
    # paths that explicitly provide a server identity opt into the new digest.
    normalized_namespace = (namespace or "").strip()
    identity = (
        f"{normalized_namespace}\0{logical_name}"
        if normalized_namespace
        else logical_name
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
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
    sensitive_keys = get_sensitive_keys(config_schema)

    def encrypt_value(value: Any, *, sensitive: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                key: encrypt_value(
                    nested,
                    sensitive=(
                        sensitive
                        or str(key) in sensitive_keys
                        or is_sensitive_mcp_query_key(str(key))
                    ),
                )
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [encrypt_value(item, sensitive=sensitive) for item in value]
        if not sensitive or not isinstance(value, str) or not value:
            return value
        try:
            decrypt_data(value, settings.SECRET_KEY)
            return value
        except Exception:
            pass
        encrypted = encrypt_data(value, settings.SECRET_KEY)
        if decrypt_data(encrypted, settings.SECRET_KEY) != value:
            raise RuntimeError("tool credential encryption verification failed")
        return encrypted

    return encrypt_value(dict(config))


def decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    if not config:
        return config

    settings = get_settings()
    sensitive_keys = get_sensitive_keys(config_schema)

    def decrypt_value(value: Any, *, sensitive: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                key: decrypt_value(
                    nested,
                    sensitive=(
                        sensitive
                        or str(key) in sensitive_keys
                        or is_sensitive_mcp_query_key(str(key))
                    ),
                )
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [decrypt_value(item, sensitive=sensitive) for item in value]
        if not sensitive or not isinstance(value, str) or not value:
            return value
        try:
            return decrypt_data(value, settings.SECRET_KEY)
        except Exception:
            return value

    return decrypt_value(dict(config))


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
    if tool.type == "mcp" and tool.tenant_id is None:
        # Tenantless MCP rows are shared capability definitions. Their URL and
        # schema may be global, but credentials/options belong to an exact
        # Agent assignment and must never come from the shared Tool row.
        return {}
    return decrypt_sensitive_fields(tool.config or {}, tool.config_schema)


def mask_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    sensitive_keys = get_sensitive_keys(config_schema)

    def mask_value(value: Any, *, sensitive: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                key: mask_value(
                    nested,
                    sensitive=(
                        sensitive
                        or str(key) in sensitive_keys
                        or is_sensitive_mcp_query_key(str(key))
                    ),
                )
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [mask_value(item, sensitive=sensitive) for item in value]
        if sensitive and isinstance(value, str) and value:
            suffix = value[-4:] if len(value) > 4 else value
            return f"****{suffix}"
        return value

    return mask_value(dict(config or {}))


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
    current = deepcopy(existing or {})
    merged = deepcopy(incoming)
    sensitive_keys = get_sensitive_keys(config_schema)
    missing = object()

    def sensitive_leaves(
        value: Any,
        path: tuple[str | int, ...] = (),
        *,
        sensitive: bool = False,
    ):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield from sensitive_leaves(
                    nested,
                    (*path, key),
                    sensitive=(
                        sensitive
                        or str(key) in sensitive_keys
                        or is_sensitive_mcp_query_key(str(key))
                    ),
                )
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                yield from sensitive_leaves(
                    nested,
                    (*path, index),
                    sensitive=sensitive,
                )
        elif sensitive:
            yield path, value

    def get_path(value: Any, path: tuple[str | int, ...]) -> Any:
        current_value = value
        for part in path:
            if isinstance(part, int):
                if not isinstance(current_value, list) or part >= len(current_value):
                    return missing
                current_value = current_value[part]
            else:
                if not isinstance(current_value, dict) or part not in current_value:
                    return missing
                current_value = current_value[part]
        return current_value

    def set_path(value: dict, path: tuple[str | int, ...], replacement: Any) -> None:
        cursor: Any = value
        for index, part in enumerate(path[:-1]):
            next_part = path[index + 1]
            if isinstance(part, int):
                while len(cursor) <= part:
                    cursor.append({} if isinstance(next_part, str) else [])
                if not isinstance(cursor[part], (dict, list)):
                    cursor[part] = {} if isinstance(next_part, str) else []
                cursor = cursor[part]
            else:
                expected = {} if isinstance(next_part, str) else []
                if not isinstance(cursor.get(part), type(expected)):
                    cursor[part] = expected
                cursor = cursor[part]
        last = path[-1]
        if isinstance(last, int):
            while len(cursor) <= last:
                cursor.append(None)
            cursor[last] = replacement
        else:
            cursor[last] = replacement

    for path, existing_value in sensitive_leaves(current):
        incoming_value = get_path(incoming, path)
        placeholder = isinstance(incoming_value, str) and incoming_value.startswith("****")
        if incoming_value is missing or incoming_value in (None, "") or placeholder:
            set_path(merged, path, existing_value)
    return meaningful_config(merged)
