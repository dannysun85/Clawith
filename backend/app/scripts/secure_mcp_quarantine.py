"""Sanitize a deployment MCP quarantine snapshot before safe restoration."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.database import engine
from app.services.mcp_security import (
    MCPURLPolicyError,
    is_sensitive_mcp_query_key,
    split_mcp_url_secrets,
)
from app.services.tool_config import get_sensitive_keys


_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _strict_encrypt_config(
    config: dict[str, Any] | None,
    config_schema: dict[str, Any] | None,
    *,
    secret_key: str | None = None,
) -> dict[str, Any]:
    """Encrypt every credential-like string and verify the round trip."""

    secured = dict(config or {})
    sensitive_keys = get_sensitive_keys(config_schema)
    key = secret_key or get_settings().SECRET_KEY

    def encrypt_value(value: Any, *, sensitive: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                field: encrypt_value(
                    nested,
                    sensitive=(
                        sensitive
                        or str(field) in sensitive_keys
                        or is_sensitive_mcp_query_key(str(field))
                    ),
                )
                for field, nested in value.items()
            }
        if isinstance(value, list):
            return [encrypt_value(item, sensitive=sensitive) for item in value]
        if not sensitive or not isinstance(value, str) or not value:
            return value
        try:
            decrypt_data(value, key)
            return value
        except Exception:
            pass
        encrypted = encrypt_data(value, key)
        if decrypt_data(encrypted, key) != value:
            raise RuntimeError("MCP snapshot credential encryption verification failed")
        return encrypted

    return encrypt_value(secured)


def _secure_tool_snapshot(
    *,
    enabled: bool,
    server_url: str | None,
    config: dict[str, Any] | None,
    config_schema: dict[str, Any] | None,
    server_name: str | None,
    source: str | None,
    tenant_id: object | None,
    secret_key: str | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Return a restorable Tool snapshot or a fail-closed disabled row."""

    valid_source = source in {"builtin", "admin", "agent"}
    if not valid_source or (source == "agent" and tenant_id is None):
        return False, None, {}

    secured_config = dict(config or {})
    secured_url = server_url
    if secured_url:
        try:
            secured_url, query_secrets = split_mcp_url_secrets(secured_url)
        except MCPURLPolicyError:
            return False, None, {}
        if query_secrets:
            # A credential in a global endpoint has no safe company owner.
            if tenant_id is None:
                return False, None, {}
            secured_config["mcp_url_query_secrets"] = query_secrets

    # Platform/global definitions may retain an endpoint, but credentials must
    # live only on the exact Agent assignment. Atlassian is always JIT-loaded
    # from ChannelConfig even for a tenant-scoped legacy row.
    if tenant_id is None or server_name == "Atlassian Rovo":
        secured_config = {}
    else:
        secured_config = _strict_encrypt_config(
            secured_config,
            config_schema,
            secret_key=secret_key,
        )
    return bool(enabled), secured_url, secured_config


def _snapshot_tool_tenant_id(
    *,
    source: str | None,
    actual_tenant_id: object | None,
    assigned_tenant_id: object | None,
    assigned_tenant_count: int,
) -> object | None:
    """Resolve only a legacy Agent-installed Tool with one exact owner.

    Global builtin/admin definitions must never inherit credential ownership
    from whichever Agent happens to reference them.  A tenantless historical
    ``source=agent`` row is different: migration 095 adopts it only when all
    assignments resolve to one company, so the deployment snapshot must apply
    the same narrow rule before that migration runs.
    """

    if actual_tenant_id is not None:
        return actual_tenant_id
    if source == "agent" and assigned_tenant_count == 1:
        return assigned_tenant_id
    return None


def _secure_assignment_snapshot(
    *,
    enabled: bool,
    config: dict[str, Any] | None,
    config_schema: dict[str, Any] | None,
    server_name: str | None,
    source: str | None,
    tool_tenant_id: object | None,
    agent_tenant_id: object | None,
    secret_key: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Return a restorable assignment only when ownership is unambiguous."""

    same_tenant = (
        tool_tenant_id is not None
        and agent_tenant_id is not None
        and str(tool_tenant_id) == str(agent_tenant_id)
    )
    visible = (
        source == "agent" and same_tenant
    ) or (
        source in {"builtin", "admin"}
        and (tool_tenant_id is None or same_tenant)
    )
    if not visible:
        return False, {}
    if server_name == "Atlassian Rovo":
        return bool(enabled), {}
    return bool(enabled), _strict_encrypt_config(
        config,
        config_schema,
        secret_key=secret_key,
    )


async def secure_snapshot(snapshot_id: str) -> None:
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("invalid MCP quarantine snapshot identity")

    tool_count = 0
    assignment_count = 0
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        await connection.execute(text("SET LOCAL statement_timeout = '90s'"))
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('astra-deploy-mcp-quarantine-v1'))")
        )
        state = await connection.execute(
            text(
                """
                SELECT snapshot_id
                FROM astra_deploy_mcp_quarantine_state
                WHERE singleton = true AND snapshot_id = :snapshot_id
                FOR UPDATE
                """
            ),
            {"snapshot_id": snapshot_id},
        )
        if state.scalar_one_or_none() != snapshot_id:
            raise RuntimeError("MCP quarantine snapshot is no longer pending")

        tools = await connection.execute(
            text(
                """
                WITH ownership AS (
                    SELECT owned_assignment.tool_id,
                           (array_agg(DISTINCT owned_agent.tenant_id)
                               FILTER (WHERE owned_agent.tenant_id IS NOT NULL))[1]
                               AS tenant_id,
                           count(DISTINCT owned_agent.tenant_id) AS tenant_count
                    FROM agent_tools AS owned_assignment
                    JOIN agents AS owned_agent
                      ON owned_agent.id = owned_assignment.agent_id
                    GROUP BY owned_assignment.tool_id
                )
                SELECT snapshot.tool_id, snapshot.enabled,
                       snapshot.mcp_server_url, snapshot.config,
                       tool.config_schema, tool.mcp_server_name,
                       tool.source, tool.tenant_id AS actual_tenant_id,
                       ownership.tenant_id AS assigned_tenant_id,
                       COALESCE(ownership.tenant_count, 0) AS assigned_tenant_count
                FROM astra_deploy_mcp_quarantine_tools AS snapshot
                JOIN tools AS tool ON tool.id = snapshot.tool_id
                LEFT JOIN ownership ON ownership.tool_id = tool.id
                WHERE snapshot.snapshot_id = :snapshot_id
                  AND tool.type = 'mcp'
                """
            ),
            {"snapshot_id": snapshot_id},
        )
        for row in tools.mappings():
            tenant_id = _snapshot_tool_tenant_id(
                source=row["source"],
                actual_tenant_id=row["actual_tenant_id"],
                assigned_tenant_id=row["assigned_tenant_id"],
                assigned_tenant_count=row["assigned_tenant_count"],
            )
            enabled, server_url, config = _secure_tool_snapshot(
                enabled=row["enabled"],
                server_url=row["mcp_server_url"],
                config=row["config"],
                config_schema=row["config_schema"],
                server_name=row["mcp_server_name"],
                source=row["source"],
                tenant_id=tenant_id,
            )
            await connection.execute(
                text(
                    """
                    UPDATE astra_deploy_mcp_quarantine_tools
                    SET enabled = :enabled,
                        mcp_server_url = :server_url,
                        config = CAST(:config AS jsonb)
                    WHERE snapshot_id = :snapshot_id AND tool_id = :tool_id
                    """
                ),
                {
                    "enabled": enabled,
                    "server_url": server_url,
                    "config": json.dumps(config, separators=(",", ":")),
                    "snapshot_id": snapshot_id,
                    "tool_id": row["tool_id"],
                },
            )
            tool_count += 1

        assignments = await connection.execute(
            text(
                """
                WITH ownership AS (
                    SELECT owned_assignment.tool_id,
                           (array_agg(DISTINCT owned_agent.tenant_id)
                               FILTER (WHERE owned_agent.tenant_id IS NOT NULL))[1]
                               AS tenant_id,
                           count(DISTINCT owned_agent.tenant_id) AS tenant_count
                    FROM agent_tools AS owned_assignment
                    JOIN agents AS owned_agent
                      ON owned_agent.id = owned_assignment.agent_id
                    GROUP BY owned_assignment.tool_id
                )
                SELECT snapshot.agent_tool_id, snapshot.enabled, snapshot.config,
                       tool.config_schema, tool.mcp_server_name, tool.source,
                       COALESCE(
                           tool.tenant_id,
                           CASE WHEN ownership.tenant_count = 1
                                THEN ownership.tenant_id END
                       ) AS effective_tool_tenant_id,
                       agent.tenant_id AS agent_tenant_id
                FROM astra_deploy_mcp_quarantine_assignments AS snapshot
                JOIN agent_tools AS assignment
                  ON assignment.id = snapshot.agent_tool_id
                JOIN tools AS tool ON tool.id = assignment.tool_id
                JOIN agents AS agent ON agent.id = assignment.agent_id
                LEFT JOIN ownership ON ownership.tool_id = tool.id
                WHERE snapshot.snapshot_id = :snapshot_id
                  AND tool.type = 'mcp'
                """
            ),
            {"snapshot_id": snapshot_id},
        )
        for row in assignments.mappings():
            enabled, config = _secure_assignment_snapshot(
                enabled=row["enabled"],
                config=row["config"],
                config_schema=row["config_schema"],
                server_name=row["mcp_server_name"],
                source=row["source"],
                tool_tenant_id=row["effective_tool_tenant_id"],
                agent_tenant_id=row["agent_tenant_id"],
            )
            await connection.execute(
                text(
                    """
                    UPDATE astra_deploy_mcp_quarantine_assignments
                    SET enabled = :enabled,
                        config = CAST(:config AS jsonb)
                    WHERE snapshot_id = :snapshot_id
                      AND agent_tool_id = :agent_tool_id
                    """
                ),
                {
                    "enabled": enabled,
                    "config": json.dumps(config, separators=(",", ":")),
                    "snapshot_id": snapshot_id,
                    "agent_tool_id": row["agent_tool_id"],
                },
            )
            assignment_count += 1

    print(
        "[mcp-quarantine] secured "
        f"tool_count={tool_count} assignment_count={assignment_count}",
        flush=True,
    )


async def _main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.scripts.secure_mcp_quarantine SNAPSHOT_ID")
    try:
        async with asyncio.timeout(120):
            await secure_snapshot(sys.argv[1])
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
