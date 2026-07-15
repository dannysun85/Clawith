"""Fail-closed authorization policy for every code-execution tool."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


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
EXTERNAL_SANDBOX_TYPES = frozenset({
    "aio_sandbox",
    "agentbay",
    "codesandbox",
    "e2b",
    "judge0",
    "self_hosted",
})
CUSTOM_ENDPOINT_SANDBOX_TYPES = frozenset({
    "aio_sandbox",
    "judge0",
    "self_hosted",
})
FIXED_TOOL_SANDBOX_TYPES = {
    "execute_code": "subprocess",
    "execute_code_e2b": "e2b",
    "agentbay_code_execute": "agentbay",
    "agentbay_code_write_file": "agentbay",
    "agentbay_code_read_file": "agentbay",
    "agentbay_code_edit_file": "agentbay",
    "agentbay_command_exec": "agentbay",
}
DEFAULT_ENDPOINTS = {"judge0": "https://ce.judge0.com"}


def is_code_execution_tool(tool_name: str) -> bool:
    return tool_name in CODE_EXECUTION_TOOL_NAMES


def _csv_values(raw: str | Iterable[str] | None) -> set[str]:
    if raw is None:
        return set()
    values = raw if not isinstance(raw, str) else raw.split(",")
    return {str(value).strip() for value in values if str(value).strip()}


def _normalized_endpoint(raw: str | None) -> str | None:
    """Return a comparable HTTP endpoint, rejecting credential-bearing URLs."""

    if not raw:
        return None
    try:
        parsed = urlsplit(str(raw).strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = parsed.hostname.lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), host, path, "", ""))
    except (TypeError, ValueError):
        return None


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
    tool_name: str | None = None,
    sandbox_type: str | None = None,
    allow_network: bool | None = None,
    api_url: str | None = None,
) -> str | None:
    """Return a safe operator-facing reason, or ``None`` when authorized."""

    if not bool(getattr(settings, "CODE_EXECUTION_ENABLED", False)):
        return "Code execution is disabled by the platform safety switch"
    if tenant_id is None:
        return "Code execution requires a tenant-scoped agent"
    if not code_execution_tenant_authorized(settings, tenant_id):
        return "Code execution is not authorized for this company"

    environment = str(getattr(settings, "ENVIRONMENT", "development")).strip().lower()
    if environment in {"production", "prod"}:
        if tool_name:
            allowed_tools = _csv_values(
                getattr(settings, "CODE_EXECUTION_ALLOWED_TOOL_NAMES", "")
            )
            if "*" in allowed_tools or tool_name not in allowed_tools:
                return "This Code tool is not approved for production"

        validate_sandbox = (
            sandbox_type is not None
            or allow_network is not None
            or api_url is not None
        )
        if not validate_sandbox:
            return None

        normalized_type = str(sandbox_type).strip().lower()
        allowed_types = _csv_values(
            getattr(settings, "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES", "")
        )
        if "*" in allowed_types:
            return "Wildcard Code sandbox authorization is not allowed"
        if normalized_type in LOCAL_SANDBOX_TYPES:
            return "Production Code execution requires an isolated external sandbox"
        if normalized_type not in EXTERNAL_SANDBOX_TYPES or normalized_type not in allowed_types:
            return "The configured Code sandbox backend is not approved for production"
        fixed_type = FIXED_TOOL_SANDBOX_TYPES.get(str(tool_name or ""))
        if fixed_type and normalized_type != fixed_type:
            return "This Code tool cannot be rerouted to a different sandbox provider"
        if allow_network is not True:
            return "This external Code sandbox requires explicit platform network approval"
        if normalized_type in CUSTOM_ENDPOINT_SANDBOX_TYPES:
            configured_endpoint = _normalized_endpoint(
                api_url or DEFAULT_ENDPOINTS.get(normalized_type)
            )
            allowed_endpoints = {
                normalized
                for value in _csv_values(
                    getattr(settings, "CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS", "")
                )
                if (normalized := _normalized_endpoint(value)) is not None
            }
            if configured_endpoint is None or configured_endpoint not in allowed_endpoints:
                return "The configured Code sandbox endpoint is not approved for production"
    return None
