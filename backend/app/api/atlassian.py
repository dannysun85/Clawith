"""Atlassian Rovo MCP Channel API routes.

Provides per-agent Atlassian integration configuration.
Unlike Slack/Discord (messaging channels), Atlassian is a tool-access channel:
the agent uses Jira, Confluence, and Compass via the Atlassian Rovo MCP server.
"""

import uuid
import hmac

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User

router = APIRouter(tags=["atlassian"])

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"


async def lock_atlassian_agent(agent_id: uuid.UUID, db: AsyncSession) -> None:
    """Serialize configure/sync/revoke with one stable lock order."""

    from app.models.agent import Agent

    await db.execute(
        select(Agent.id).where(Agent.id == agent_id).with_for_update()
    )


async def revoke_atlassian_tool_grants(agent_id: uuid.UUID, db: AsyncSession) -> None:
    """Disable Rovo assignments and erase every per-Agent fallback secret."""

    from app.models.tool import AgentTool, Tool

    await lock_atlassian_agent(agent_id, db)
    assignments = await db.execute(
        select(AgentTool)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.agent_id == agent_id,
            Tool.type == "mcp",
            Tool.mcp_server_name == "Atlassian Rovo",
        )
    )
    for assignment in assignments.scalars().all():
        assignment.enabled = False
        assignment.config = {}


async def _atlassian_credential_matches(
    agent_id: uuid.UUID,
    expected_key: str,
    db: AsyncSession,
) -> bool:
    from app.config import get_settings
    from app.core.security import decrypt_data

    await lock_atlassian_agent(agent_id, db)
    result = await db.execute(
        select(ChannelConfig)
        .where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "atlassian",
            ChannelConfig.is_configured.is_(True),
        )
        .with_for_update()
    )
    config = result.scalar_one_or_none()
    if not config or not config.app_secret:
        return False
    try:
        current = decrypt_data(config.app_secret, get_settings().SECRET_KEY)
    except Exception as exc:
        logger.error(
            "[AtlassianChannel] Credential comparison failed closed "
            "agent_id={} error_type={}",
            agent_id,
            type(exc).__name__,
        )
        return False
    return bool(
        current
        and expected_key
        and hmac.compare_digest(current, expected_key)
    )


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/atlassian-channel", status_code=201)
async def configure_atlassian_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Atlassian Rovo MCP for an agent.

    Required field: api_key (Bearer token starting with ATSTT, or Basic base64(email:token)).
    Optional: cloud_id (Atlassian cloud site ID for multi-site setups).
    """
    await check_agent_access(
        db,
        current_user,
        agent_id,
        required_level="manage",
        lock_authority=True,
    )

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=422, detail="api_key is required")

    cloud_id = (data.get("cloud_id") or "").strip()

    from app.core.security import encrypt_data
    from app.config import get_settings
    encrypted_key = encrypt_data(api_key, get_settings().SECRET_KEY)

    await lock_atlassian_agent(agent_id, db)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "atlassian",
        ).with_for_update()
    )
    existing = result.scalar_one_or_none()
    await revoke_atlassian_tool_grants(agent_id, db)
    if existing:
        existing.app_secret = encrypted_key
        existing.is_configured = True
        existing.extra_config = {
            **(existing.extra_config or {}),
            "cloud_id": cloud_id,
            "tool_sync_status": "syncing",
            "tool_count": 0,
            "tool_sync_error_code": None,
        }
        await db.commit()
        await _complete_atlassian_tool_sync(agent_id, api_key)
        await db.refresh(existing)
        return _serialize(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="atlassian",
        app_id="atlassian",
        app_secret=encrypted_key,
        is_configured=True,
        extra_config={
            "cloud_id": cloud_id,
            "tool_sync_status": "syncing",
            "tool_count": 0,
            "tool_sync_error_code": None,
        },
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    await _complete_atlassian_tool_sync(agent_id, api_key)
    await db.refresh(config)
    return _serialize(config)


@router.get("/agents/{agent_id}/atlassian-channel")
async def get_atlassian_channel(
    agent_id: uuid.UUID,
    missing_ok: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id, required_level="manage")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "atlassian",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        if missing_ok:
            return None
        raise HTTPException(status_code=404, detail="Atlassian not configured")
    return _serialize(config)


@router.delete("/agents/{agent_id}/atlassian-channel", status_code=204)
async def delete_atlassian_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(
        db,
        current_user,
        agent_id,
        required_level="manage",
        lock_authority=True,
    )
    await lock_atlassian_agent(agent_id, db)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "atlassian",
        ).with_for_update()
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Atlassian not configured")
    await revoke_atlassian_tool_grants(agent_id, db)
    await db.delete(config)
    await db.commit()


@router.post("/agents/{agent_id}/atlassian-channel/test")
async def test_atlassian_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity to Atlassian Rovo MCP and list available tools."""
    await check_agent_access(db, current_user, agent_id, required_level="manage")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "atlassian",
        )
    )
    config = result.scalar_one_or_none()
    if not config or not config.app_secret:
        raise HTTPException(status_code=400, detail="Atlassian not configured")

    from app.services.mcp_client import MCPClient
    try:
        api_key = await get_atlassian_api_key_for_agent(agent_id, db)
        if not api_key:
            raise HTTPException(status_code=400, detail="Atlassian credential is unavailable")
        client = MCPClient(ATLASSIAN_MCP_URL, api_key=api_key)
        tools = await client.list_tools()
        return {
            "ok": True,
            "tool_count": len(tools),
            "tools": [{"name": t["name"], "description": t.get("description", "")[:100]} for t in tools[:10]],
            "message": f"✅ Connected to Atlassian Rovo MCP — {len(tools)} tools available",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Atlassian connection failed ({type(exc).__name__})",
        }


# ─── Internal helper ────────────────────────────────────

def _serialize(config: ChannelConfig) -> dict:
    return {
        "id": str(config.id),
        "agent_id": str(config.agent_id),
        "channel_type": config.channel_type,
        "is_configured": config.is_configured,
        "is_connected": config.is_connected,
        "cloud_id": (config.extra_config or {}).get("cloud_id", ""),
        "extra_config": config.extra_config or {},
        "created_at": config.created_at.isoformat() if config.created_at else None,
    }


# ─── Utility for internal use ──────────────────────────

class AtlassianToolSyncError(RuntimeError):
    """Stable, credential-free reason for an incomplete tool sync."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def _mark_atlassian_sync_failed(
    agent_id: uuid.UUID,
    api_key: str,
    error_code: str,
) -> None:
    """Persist a retryable sync failure only for the still-current key."""

    from app.database import async_session

    async with async_session() as db:
        await lock_atlassian_agent(agent_id, db)
        if not await _atlassian_credential_matches(agent_id, api_key, db):
            await db.rollback()
            return
        result = await db.execute(
            select(ChannelConfig)
            .where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "atlassian",
            )
            .with_for_update()
        )
        config = result.scalar_one_or_none()
        if not config:
            await db.rollback()
            return
        config.extra_config = {
            **(config.extra_config or {}),
            "tool_sync_status": "failed",
            "tool_count": 0,
            "tool_sync_error_code": error_code,
        }
        await db.commit()


async def _complete_atlassian_tool_sync(
    agent_id: uuid.UUID,
    api_key: str,
) -> int:
    """Synchronously finish configuration or return an explicit failure."""

    try:
        return await _sync_atlassian_tools_for_agent(agent_id, api_key)
    except AtlassianToolSyncError as exc:
        error_code = exc.code
    except Exception as exc:
        error_code = "atlassian_tool_sync_failed"
        logger.error(
            "[AtlassianChannel] Tool sync failed agent_id={} error_type={}",
            agent_id,
            type(exc).__name__,
        )
    await _mark_atlassian_sync_failed(agent_id, api_key, error_code)
    raise HTTPException(
        status_code=502,
        detail={
            "code": error_code,
            "message": "Atlassian credential saved, but tool synchronization failed",
        },
    )


async def _sync_atlassian_tools_for_agent(agent_id: uuid.UUID, api_key: str) -> int:
    """Connect to Atlassian Rovo MCP and ensure all tools are seeded + assigned to this agent.

    Discovers tools from the MCP server, creates Tool records if needed,
    and creates AgentTool assignments for this specific agent.
    """
    from app.services.mcp_client import MCPClient
    from app.models.tool import Tool
    from app.database import async_session
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.services.agent_tool_assignments import (
        lock_agent_tool_owner,
        upsert_agent_tool,
    )

    logger.info(f"[AtlassianChannel] Syncing tools for agent {agent_id} ...")
    try:
        client = MCPClient(ATLASSIAN_MCP_URL, api_key=api_key)
        tools_discovered = await client.list_tools()
    except Exception as e:
        logger.error(
            "[AtlassianChannel] Could not list tools error_type={}",
            type(e).__name__,
        )
        raise AtlassianToolSyncError("atlassian_discovery_failed") from e

    if not tools_discovered:
        logger.warning("[AtlassianChannel] No tools returned from Atlassian MCP")
        raise AtlassianToolSyncError("atlassian_no_tools")

    logger.info(f"[AtlassianChannel] Found {len(tools_discovered)} tools, assigning to agent {agent_id}")

    async with async_session() as db:
        await lock_agent_tool_owner(db, agent_id)
        # A configure/delete or credential rotation may have completed while
        # discovery was in flight. A stale task must never resurrect its key.
        if not await _atlassian_credential_matches(agent_id, api_key, db):
            logger.info(
                "[AtlassianChannel] Ignoring stale sync for agent {}",
                agent_id,
            )
            raise AtlassianToolSyncError("atlassian_credential_changed")
        assigned = 0
        for mcp_tool in tools_discovered:
            raw_name = mcp_tool.get("name", "")
            if not raw_name:
                continue

            tool_name = f"atlassian_rovo_{raw_name}"
            tool_desc = mcp_tool.get("description", "")[:500]
            tool_schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})

            if "jira" in raw_name.lower() or "issue" in raw_name.lower():
                icon = "🔵"
            elif "confluence" in raw_name.lower() or "page" in raw_name.lower():
                icon = "📘"
            elif "compass" in raw_name.lower() or "component" in raw_name.lower():
                icon = "🧭"
            else:
                icon = "🔷"

            # The global name is shared, so creation must be race-safe. Never
            # repurpose an unrelated row that happens to have the same name.
            await db.execute(
                pg_insert(Tool)
                .values(
                    id=uuid.uuid4(),
                    name=tool_name,
                    display_name=f"Atlassian: {raw_name}",
                    description=tool_desc,
                    type="mcp",
                    category="atlassian",
                    icon=icon,
                    parameters_schema=tool_schema,
                    config={},
                    config_schema={},
                    mcp_server_url=ATLASSIAN_MCP_URL,
                    mcp_server_name="Atlassian Rovo",
                    mcp_tool_name=raw_name,
                    enabled=True,
                    is_default=False,
                    source="admin",
                    tenant_id=None,
                )
                .on_conflict_do_nothing(index_elements=[Tool.name])
            )
            tool_r = await db.execute(
                sa_select(Tool).where(Tool.name == tool_name).with_for_update()
            )
            tool = tool_r.scalar_one()
            if not (
                tool.type == "mcp"
                and tool.mcp_server_name == "Atlassian Rovo"
                and tool.mcp_tool_name == raw_name
                and tool.source == "admin"
                and tool.tenant_id is None
            ):
                raise RuntimeError("Atlassian tool name conflicts with an unrelated Tool")
            tool.display_name = f"Atlassian: {raw_name}"
            tool.description = tool_desc
            tool.category = "atlassian"
            tool.icon = icon
            tool.parameters_schema = tool_schema
            tool.mcp_server_url = ATLASSIAN_MCP_URL
            tool.enabled = True
            tool.config = {}

            # Assign to this specific agent. The credential remains exclusively
            # in ChannelConfig and is resolved just in time at execution.
            await upsert_agent_tool(
                db,
                agent_id=agent_id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
                config={},
                on_conflict="reauthorize",
            )
            assigned += 1

        config_result = await db.execute(
            sa_select(ChannelConfig)
            .where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "atlassian",
            )
            .with_for_update()
        )
        config = config_result.scalar_one_or_none()
        if not config:
            raise AtlassianToolSyncError("atlassian_configuration_removed")
        config.extra_config = {
            **(config.extra_config or {}),
            "tool_sync_status": "ready",
            "tool_count": assigned,
            "tool_sync_error_code": None,
        }
        await db.commit()
    logger.info(f"[AtlassianChannel] Synced {assigned} tool assignments for agent {agent_id}")
    return assigned


async def get_atlassian_api_key_for_agent(agent_id: uuid.UUID, db=None) -> str | None:
    """Return the configured Atlassian API key for the given agent, or None."""
    from app.database import async_session

    async def _fetch(session):
        from app.core.security import decrypt_data
        from app.config import get_settings
        result = await session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "atlassian",
                ChannelConfig.is_configured.is_(True),
            )
        )
        config = result.scalar_one_or_none()
        if not config or not config.app_secret:
            return None
        
        try:
            return decrypt_data(config.app_secret, get_settings().SECRET_KEY)
        except Exception:
            logger.error(
                "[AtlassianChannel] Refusing undecryptable credential for agent %s",
                agent_id,
            )
            return None

    if db is not None:
        return await _fetch(db)
    async with async_session() as session:
        return await _fetch(session)
