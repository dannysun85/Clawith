import uuid

import pytest

from app.services import agent_tools as agent_tools_module
from app.services.mcp_client import (
    MAX_MCP_TOOL_DESCRIPTION_CHARS,
    MAX_MCP_TOOL_NAME_CHARS,
    MAX_MCP_TOOL_SCHEMA_BYTES,
    MAX_MCP_TOOLS,
    MCPClient,
)


@pytest.mark.asyncio
async def test_mcp_transport_error_does_not_expose_provider_messages(monkeypatch):
    client = MCPClient("https://example.test/mcp")

    async def fail_streamable(_method, _params=None):
        raise RuntimeError("streamable returned 401")

    async def fail_sse(_method, _params=None):
        raise RuntimeError("sse endpoint returned 404")

    monkeypatch.setattr(client, "_streamable_request", fail_streamable)
    monkeypatch.setattr(client, "_sse_request", fail_sse)

    with pytest.raises(Exception) as exc_info:
        await client._detect_and_request("tools/list")

    message = str(exc_info.value)
    assert message == (
        "Both MCP transports failed "
        "(streamable=RuntimeError, sse=RuntimeError)"
    )
    assert "returned 401" not in message
    assert "returned 404" not in message


@pytest.mark.asyncio
async def test_smithery_recovery_does_not_store_auth_required_connection(monkeypatch):
    async def fake_ensure_connection(_api_key, _mcp_url, _display_name):
        return {
            "namespace": "shadowsseven",
            "connection_id": "new-auth-required",
            "auth_url": "https://smithery.run/shadowsseven/new-auth-required/setup",
        }

    def fail_if_db_touched():
        raise AssertionError("auth-required Smithery connections must not overwrite stored config")

    async def allow_public_url(url):
        return url

    monkeypatch.setattr(
        "app.services.resource_discovery._ensure_smithery_connection",
        fake_ensure_connection,
    )
    monkeypatch.setattr(agent_tools_module, "async_session", fail_if_db_touched)
    monkeypatch.setattr(
        "app.services.mcp_security.validate_public_mcp_url",
        allow_public_url,
    )

    result = await agent_tools_module._smithery_auto_recover(
        "smithery-key",
        "https://twitter.run.tools",
        "shadowsseven",
        "old-working-connection",
        agent_id=uuid.uuid4(),
    )

    assert "Re-authorization needed" in result
    assert "https://smithery.run/shadowsseven/new-auth-required/setup" in result


def test_mcp_catalog_rejects_count_name_schema_and_duplicate_abuse():
    valid = {
        "name": "search",
        "description": "Search",
        "inputSchema": {"type": "object"},
    }
    with pytest.raises(ValueError, match="tool count"):
        MCPClient._validated_tool_catalog([valid] * (MAX_MCP_TOOLS + 1))
    with pytest.raises(ValueError, match="missing or too long"):
        MCPClient._validated_tool_catalog([
            {**valid, "name": "x" * (MAX_MCP_TOOL_NAME_CHARS + 1)},
        ])
    with pytest.raises(ValueError, match="size limit"):
        MCPClient._validated_tool_catalog([
            {
                **valid,
                "inputSchema": {"description": "x" * MAX_MCP_TOOL_SCHEMA_BYTES},
            },
        ])
    with pytest.raises(ValueError, match="duplicate"):
        MCPClient._validated_tool_catalog([valid, dict(valid)])


def test_mcp_catalog_truncates_description_without_mutating_schema():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    catalog = MCPClient._validated_tool_catalog([
        {
            "name": "search",
            "description": "x" * (MAX_MCP_TOOL_DESCRIPTION_CHARS + 10),
            "inputSchema": schema,
        },
    ])

    assert catalog[0]["description"].endswith("...[description truncated]")
    assert catalog[0]["inputSchema"] is schema
