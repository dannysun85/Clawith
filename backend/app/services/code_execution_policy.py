"""Fail-closed authorization policy for every code-execution tool."""

from __future__ import annotations

import uuid
from collections.abc import Iterable


CODE_EXECUTION_TOOL_NAMES = frozenset({
    "execute_code",
    "execute_code_e2b",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
})

LOCAL_SANDBOX_TYPES = frozenset({"subprocess", "docker"})
NETWORKED_SANDBOX_TYPES = frozenset({"codesandbox", "e2b"})
DEFAULT_PRODUCTION_SANDBOX_TYPES = frozenset({
    "aio_sandbox",
    "codesandbox",
    "e2b",
    "judge0",
    "self_hosted",
})


def is_code_execution_tool(tool_name: str) -> bool:
    return tool_name in CODE_EXECUTION_TOOL_NAMES


def _csv_values(raw: str | Iterable[str] | None) -> set[str]:
    if raw is None:
        return set()
    values = raw if not isinstance(raw, str) else raw.split(",")
    return {str(value).strip() for value in values if str(value).strip()}


def code_execution_tenant_authorized(settings, tenant_id: uuid.UUID | str | None) -> bool:
    """Require both the platform kill switch and an explicit tenant grant."""

    if not bool(getattr(settings, "CODE_EXECUTION_ENABLED", False)) or tenant_id is None:
        return False
    allowlist = _csv_values(getattr(settings, "CODE_EXECUTION_ALLOWED_TENANT_IDS", ""))
    return str(tenant_id) in allowlist


def code_execution_denial_reason(
    settings,
    tenant_id: uuid.UUID | str | None,
    *,
    sandbox_type: str | None = None,
    allow_network: bool | None = None,
) -> str | None:
    """Return a safe operator-facing reason, or ``None`` when authorized."""

    if not bool(getattr(settings, "CODE_EXECUTION_ENABLED", False)):
        return "Code execution is disabled by the platform safety switch"
    if tenant_id is None:
        return "Code execution requires a tenant-scoped agent"
    if not code_execution_tenant_authorized(settings, tenant_id):
        return "Code execution is not authorized for this company"

    environment = str(getattr(settings, "ENVIRONMENT", "development")).strip().lower()
    if sandbox_type and environment in {"production", "prod"}:
        normalized_type = str(sandbox_type).strip().lower()
        allowed_types = _csv_values(
            getattr(
                settings,
                "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES",
                ",".join(sorted(DEFAULT_PRODUCTION_SANDBOX_TYPES)),
            )
        )
        if normalized_type in LOCAL_SANDBOX_TYPES:
            return "Production Code execution requires an isolated external sandbox"
        if normalized_type not in allowed_types:
            return "The configured Code sandbox backend is not approved for production"
        if normalized_type in NETWORKED_SANDBOX_TYPES and allow_network is not True:
            return "This Code sandbox requires explicit platform approval for network access"
    return None
