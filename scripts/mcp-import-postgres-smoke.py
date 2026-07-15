#!/usr/bin/env python3
"""Real PostgreSQL cross-Agent MCP import concurrency smoke."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, insert, select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.services import mcp_client, resource_discovery
from app.services.mcp_security import mcp_server_namespace


TENANT_ID = uuid.UUID("07500000-0000-4000-8000-000000000002")
USER_ID = uuid.UUID("07500000-0000-4000-8000-000000000060")
PRIMARY_AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000061")
SECOND_AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000066")
SERVER_URL = "https://mcp-concurrency.example.test/api"
SERVER_NAME = "Concurrency MCP"
UPSTREAM_TOOL_NAME = "search"


class _FakeMCPClient:
    def __init__(self, _url: str, api_key: str | None = None):
        assert api_key is None

    async def list_tools(self) -> list[dict]:
        await asyncio.sleep(0.02)
        return [
            {
                "name": UPSTREAM_TOOL_NAME,
                "description": "PostgreSQL concurrency smoke",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]


async def _allow_test_url(url: str) -> str:
    return url


async def main() -> None:
    namespace = mcp_server_namespace(SERVER_NAME, SERVER_URL)
    original_client = mcp_client.MCPClient
    original_validator = resource_discovery.validate_public_mcp_url
    try:
        async with async_session() as db:
            await db.execute(
                delete(AgentTool).where(
                    AgentTool.tool_id.in_(
                        select(Tool.id).where(
                            Tool.tenant_id == TENANT_ID,
                            Tool.type == "mcp",
                            Tool.mcp_server_name == SERVER_NAME,
                        )
                    )
                )
            )
            await db.execute(
                delete(Tool).where(
                    Tool.tenant_id == TENANT_ID,
                    Tool.type == "mcp",
                    Tool.mcp_server_name == SERVER_NAME,
                )
            )
            await db.execute(delete(Agent).where(Agent.id == SECOND_AGENT_ID))
            await db.execute(
                insert(Agent.__table__).values(
                    id=SECOND_AGENT_ID,
                    name="MCP Import Concurrency Agent",
                    creator_id=USER_ID,
                    tenant_id=TENANT_ID,
                    status="idle",
                )
            )
            await db.commit()

        mcp_client.MCPClient = _FakeMCPClient
        resource_discovery.validate_public_mcp_url = _allow_test_url
        results = await asyncio.wait_for(
            asyncio.gather(
                resource_discovery.import_mcp_direct(
                    SERVER_URL,
                    PRIMARY_AGENT_ID,
                    server_name=SERVER_NAME,
                ),
                resource_discovery.import_mcp_direct(
                    SERVER_URL,
                    SECOND_AGENT_ID,
                    server_name=SERVER_NAME,
                ),
            ),
            timeout=10,
        )
        assert all("Imported MCP server" in result for result in results)

        async with async_session() as db:
            tools = (
                await db.execute(
                    select(Tool).where(
                        Tool.tenant_id == TENANT_ID,
                        Tool.type == "mcp",
                        Tool.mcp_server_name == SERVER_NAME,
                        Tool.mcp_tool_name == UPSTREAM_TOOL_NAME,
                    )
                )
            ).scalars().all()
            assert len(tools) == 1
            assert (
                mcp_server_namespace(
                    tools[0].mcp_server_name,
                    tools[0].mcp_server_url,
                )
                == namespace
            )
            assignment_count = await db.scalar(
                select(func.count())
                .select_from(AgentTool)
                .where(
                    AgentTool.tool_id == tools[0].id,
                    AgentTool.agent_id.in_(
                        [PRIMARY_AGENT_ID, SECOND_AGENT_ID]
                    ),
                    AgentTool.enabled.is_(True),
                )
            )
            assert assignment_count == 2
    finally:
        mcp_client.MCPClient = original_client
        resource_discovery.validate_public_mcp_url = original_validator
        async with async_session() as db:
            await db.execute(
                delete(AgentTool).where(
                    AgentTool.agent_id == SECOND_AGENT_ID
                )
            )
            await db.execute(
                delete(AgentTool).where(
                    AgentTool.tool_id.in_(
                        select(Tool.id).where(
                            Tool.tenant_id == TENANT_ID,
                            Tool.type == "mcp",
                            Tool.mcp_server_name == SERVER_NAME,
                        )
                    )
                )
            )
            await db.execute(
                delete(Tool).where(
                    Tool.tenant_id == TENANT_ID,
                    Tool.type == "mcp",
                    Tool.mcp_server_name == SERVER_NAME,
                )
            )
            await db.execute(delete(Agent).where(Agent.id == SECOND_AGENT_ID))
            await db.commit()

    print("MCP import PostgreSQL cross-Agent concurrency smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
