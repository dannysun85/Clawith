"""Tool management API — CRUD for tools and per-agent assignments."""

import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import String, cast, select, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.tool import Tool, AgentTool
from app.models.user import User
from app.services.tool_config import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    get_sensitive_keys,
    get_tool_company_config,
    mask_sensitive_fields,
    merge_config_preserving_sensitive,
    set_tenant_tool_config,
    tenant_scoped_tool_name,
)

router = APIRouter(prefix="/tools", tags=["tools"])


CATEGORY_CONFIG_PRIMARY_TOOL = {
    "agentbay": "agentbay_browser_navigate",
}

_SENSITIVE_URL_QUERY_MARKERS = (
    "apikey",
    "api_key",
    "auth",
    "password",
    "secret",
    "token",
)


def _is_sensitive_url_query_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_URL_QUERY_MARKERS)


def _mask_mcp_server_url(server_url: str | None) -> str | None:
    """Keep an MCP endpoint useful to the UI without returning URL credentials."""

    if not server_url:
        return server_url
    parts = urlsplit(server_url)
    netloc = parts.netloc
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        netloc = f"****@{host}"
    query = urlencode(
        [
            (key, "****" if _is_sensitive_url_query_key(key) and value else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    fragment = "****" if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def _merge_masked_mcp_server_url(
    existing_url: str | None,
    incoming_url: str | None,
) -> str | None:
    """Preserve stored URL credentials when an admin resubmits a masked URL."""

    if not incoming_url or not existing_url:
        return incoming_url
    if incoming_url == _mask_mcp_server_url(existing_url):
        return existing_url

    existing = urlsplit(existing_url)
    incoming = urlsplit(incoming_url)
    existing_values: dict[str, list[str]] = {}
    for key, value in parse_qsl(existing.query, keep_blank_values=True):
        existing_values.setdefault(key, []).append(value)
    positions: dict[str, int] = {}
    merged_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(incoming.query, keep_blank_values=True):
        position = positions.get(key, 0)
        positions[key] = position + 1
        candidates = existing_values.get(key, [])
        if value.startswith("****") and position < len(candidates):
            value = candidates[position]
        merged_query.append((key, value))

    netloc = incoming.netloc
    if netloc.startswith("****@") and "@" in existing.netloc:
        credentials, _ = existing.netloc.rsplit("@", 1)
        _, host = netloc.rsplit("@", 1)
        netloc = f"{credentials}@{host}"
    fragment = existing.fragment if incoming.fragment == "****" else incoming.fragment
    return urlunsplit(
        (
            incoming.scheme,
            netloc,
            incoming.path,
            urlencode(merged_query, doseq=True),
            fragment,
        )
    )


def _is_platform_admin(user: User) -> bool:
    return user.role == "platform_admin" or bool(
        getattr(getattr(user, "identity", None), "is_platform_admin", False)
    )


async def _require_agent_tool_access(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    *,
    manage: bool = False,
):
    """Apply the canonical agent/tenant boundary to every tool endpoint."""

    from app.core.permissions import check_agent_access

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if manage and access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access to this agent is required")
    return agent


async def _load_agent_tool_assignments(db: AsyncSession, agent_id: uuid.UUID) -> dict[str, AgentTool]:
    """Return explicit tool assignments for one agent keyed by tool ID string."""
    agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
    return {str(at.tool_id): at for at in agent_tools_r.scalars().all()}


def _agent_visible_tool_clause(agent_tenant_id: uuid.UUID | None, assignments: dict[str, AgentTool]):
    """Build the DB filter for tools visible to an agent.

    Visibility rules:
    - builtin tools are global platform capabilities
    - admin tools belong only to the agent's company or are platform-wide (tenant_id is NULL)
    - agent-installed tools require an explicit assignment
    """
    clauses = [Tool.source == "builtin"]
    admin_cond = Tool.tenant_id.is_(None)
    if agent_tenant_id:
        admin_cond = admin_cond | (Tool.tenant_id == agent_tenant_id)
    clauses.append((Tool.source == "admin") & admin_cond)

    assigned_tool_ids = [uuid.UUID(tool_id) for tool_id in assignments]
    if assigned_tool_ids:
        clauses.append(
            (Tool.source == "agent") & Tool.id.in_(assigned_tool_ids)
        )

    return or_(*clauses)


def _tool_record_visible_to_agent(
    tool: Tool,
    agent_tenant_id: uuid.UUID | None,
    assignments: dict[str, AgentTool],
) -> bool:
    """Pure visibility check mirroring _agent_visible_tool_clause."""
    if tool.source == "builtin":
        return True
    if tool.source == "admin":
        return tool.tenant_id is None or (agent_tenant_id is not None and tool.tenant_id == agent_tenant_id)
    if tool.source == "agent":
        return str(tool.id) in assignments
    return False


def _resolve_target_tenant_id(current_user: User, tenant_id: str | None = None) -> uuid.UUID | None:
    if tenant_id:
        try:
            target_tenant_id = uuid.UUID(tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format")
        if target_tenant_id != current_user.tenant_id and not _is_platform_admin(current_user):
            raise HTTPException(status_code=403, detail="Cross-tenant tool access is not allowed")
        return target_tenant_id
    return current_user.tenant_id


def _authorize_tool_record(current_user: User, tool: Tool, target_tenant_id: uuid.UUID | None) -> None:
    """Reject cross-tenant/global mutations unless the caller is platform admin."""

    if _is_platform_admin(current_user):
        return
    if target_tenant_id is None or target_tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tool tenant access denied")
    if tool.source == "builtin":
        return
    if tool.tenant_id != target_tenant_id:
        raise HTTPException(status_code=403, detail="Tool tenant access denied")


def _require_platform_admin(current_user: User) -> None:
    if not _is_platform_admin(current_user):
        raise HTTPException(status_code=403, detail="Platform admin access required")


CODE_PLATFORM_CONTROLLED_CONFIG_KEYS = frozenset({
    "sandbox_type",
    "api_url",
    "api_key",
    "allow_network",
    "allow_unsafe_fallback_when_bwrap_missing",
    "cpu_limit",
    "memory_limit",
    "default_timeout",
    "max_timeout",
    "language_mapping",
})


def _enforce_code_control_permission(
    current_user: User,
    tool: Tool,
    incoming_config: dict,
) -> None:
    """Keep Code provider, credential, egress and limits platform-controlled."""

    from app.services.code_execution_policy import is_code_execution_tool

    controlled = CODE_PLATFORM_CONTROLLED_CONFIG_KEYS.intersection(incoming_config)
    if (
        controlled
        and is_code_execution_tool(tool.name)
        and not _is_platform_admin(current_user)
    ):
        raise HTTPException(
            status_code=403,
            detail="Only a platform admin can modify Code isolation settings",
        )


async def _code_tool_availability(
    db: AsyncSession,
    tool: Tool,
    tenant_id: uuid.UUID | None,
) -> tuple[bool, str | None]:
    from app.config import get_settings
    from app.services.code_execution_policy import (
        code_execution_denial_reason,
        is_code_execution_tool,
    )

    if not is_code_execution_tool(tool.name):
        return True, None
    company_config = await get_tool_company_config(db, tool, tenant_id)
    effective_config = {**(tool.config or {}), **company_config}
    sandbox_type = effective_config.get("sandbox_type")
    denial = code_execution_denial_reason(
        get_settings(),
        tenant_id,
        tool_name=tool.name,
        sandbox_type=str(sandbox_type) if sandbox_type else None,
        allow_network=effective_config.get("allow_network"),
        api_url=effective_config.get("api_url"),
    )
    return denial is None, denial


def _get_sensitive_keys(config_schema: dict | None = None) -> set[str]:
    return get_sensitive_keys(config_schema)


def _encrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    return encrypt_sensitive_fields(config, config_schema)


def _decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    return decrypt_sensitive_fields(config, config_schema)


# ─── Schemas ────────────────────────────────────────────────
class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    type: str = "mcp"
    category: str = "custom"
    icon: str = "🔧"
    parameters_schema: dict = {}
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    is_default: bool = False
    # Optional: platform admins can specify target tenant (e.g. when managing
    # another company's tools via the Enterprise Settings page).
    tenant_id: str | None = None


class ToolUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    parameters_schema: dict | None = None
    is_default: bool | None = None
    config: dict | None = None
    tenant_id: str | None = None


class AgentToolUpdate(BaseModel):
    tool_id: str
    enabled: bool


class CategoryConfigUpdate(BaseModel):
    config: dict


# ─── Global Tool CRUD ──────────────────────────────────────
@router.get("")
async def list_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List platform tools scoped by tenant (builtin + tenant-specific)."""
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    # Builtins are global capability definitions. Admin tools are visible only
    # inside the selected tenant (or platform-global when no tenant is selected).
    query = (
        select(Tool)
        .where(
            or_(
                Tool.source == "builtin",
                (Tool.source == "admin")
                & or_(
                    Tool.tenant_id.is_(None),
                    Tool.tenant_id == target_tenant_id,
                ),
            )
        )
        .order_by(Tool.category, Tool.name)
    )
    result = await db.execute(query)
    tools = result.scalars().all()
    response = []
    for t in tools:
        company_config = await get_tool_company_config(db, t, target_tenant_id)
        available, availability_reason = await _code_tool_availability(
            db,
            t,
            target_tenant_id,
        )
        response.append({
            "id": str(t.id),
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "parameters_schema": t.parameters_schema,
            "mcp_server_url": _mask_mcp_server_url(t.mcp_server_url),
            "mcp_server_name": t.mcp_server_name,
            "mcp_tool_name": t.mcp_tool_name,
            "enabled": t.enabled,
            "available": available,
            "availability_reason": availability_reason,
            "is_default": t.is_default,
            "source": t.source,
            "config": mask_sensitive_fields(company_config, t.config_schema),
            "config_schema": t.config_schema or {},
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return response


@router.post("")
async def create_tool(
    data: ToolCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tool (typically MCP).

    The tool is scoped to the target tenant, which defaults to the caller's
    own tenant but can be overridden via data.tenant_id. This allows platform
    admins to import MCP tools while viewing another company's settings page.
    """
    # Resolve target tenant: explicit payload value takes priority so that
    # platform admins importing tools for another company work correctly.
    target_tenant_id = _resolve_target_tenant_id(current_user, data.tenant_id)

    # Keep Tool.name globally unique for old-application rollback safety. The
    # upstream MCP name remains in mcp_tool_name, while this internal name is
    # deterministically namespaced so tenant tools cannot shadow builtins.
    storage_name = tenant_scoped_tool_name(data.name, target_tenant_id)
    existing = await db.execute(
        select(Tool).where(Tool.name == storage_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Tool '{data.name}' already exists")

    tool = Tool(
        name=storage_name,
        display_name=data.display_name,
        description=data.description,
        type=data.type,
        category=data.category,
        icon=data.icon,
        parameters_schema=data.parameters_schema,
        mcp_server_url=data.mcp_server_url,
        mcp_server_name=data.mcp_server_name,
        mcp_tool_name=data.mcp_tool_name,
        is_default=data.is_default,
        tenant_id=target_tenant_id,
        source="admin",
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return {"id": str(tool.id), "name": tool.name}


# UUID path converters keep later literal routes such as /mcp-server from
# being captured by the generic per-tool update/delete endpoints.

class BulkToolUpdateItem(BaseModel):
    tool_id: str
    enabled: bool

@router.put("/bulk")
async def update_tools_bulk(
    updates: list[BulkToolUpdateItem],
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bulk update the enabled status of multiple tools."""
    # ``enabled`` lives on the shared Tool row. Tenant admins configure their
    # own assignments/config instead of mutating a platform-wide switch.
    _require_platform_admin(current_user)
    tool_ids = [uuid.UUID(u.tool_id) for u in updates]
    result = await db.execute(select(Tool).where(Tool.id.in_(tool_ids)))
    tools_map = {str(t.id): t for t in result.scalars().all()}
    
    for update in updates:
        if update.tool_id in tools_map:
            tools_map[update.tool_id].enabled = update.enabled
            
    await db.commit()
    return {"ok": True}


@router.put("/{tool_id:uuid}")
async def update_tool(
    tool_id: uuid.UUID,
    data: ToolUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a tool."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    update_data = data.model_dump(exclude_unset=True)
    target_tenant_id = _resolve_target_tenant_id(current_user, update_data.pop("tenant_id", None))
    _authorize_tool_record(current_user, tool, target_tenant_id)
    if "mcp_server_url" in update_data:
        update_data["mcp_server_url"] = _merge_masked_mcp_server_url(
            tool.mcp_server_url,
            update_data["mcp_server_url"],
        )

    tenant_tool_config_update: dict | None = None
    if "config" in update_data:
        incoming_config = update_data.pop("config") or {}
        _enforce_code_control_permission(current_user, tool, incoming_config)
        if tool.source == "builtin":
            if not target_tenant_id:
                raise HTTPException(status_code=400, detail="tenant_id is required to configure builtin tools")
            existing_config = await get_tool_company_config(db, tool, target_tenant_id)
            config_value = merge_config_preserving_sensitive(
                existing_config,
                incoming_config,
                tool.config_schema,
            )
            await set_tenant_tool_config(db, target_tenant_id, tool.name, config_value, tool.config_schema)
        else:
            existing_config = _decrypt_sensitive_fields(tool.config or {}, tool.config_schema)
            config_value = merge_config_preserving_sensitive(
                existing_config,
                incoming_config,
                tool.config_schema,
            )
            tenant_tool_config_update = _encrypt_sensitive_fields(
                config_value,
                tool.config_schema,
            )

    # Shared metadata/global availability can only be changed by platform
    # admins. Organization admins are limited to their tenant config above.
    if update_data and not _is_platform_admin(current_user):
        raise HTTPException(status_code=403, detail="Platform admin access required for shared tool metadata")

    if tenant_tool_config_update is not None:
        tool.config = tenant_tool_config_update
    for field, value in update_data.items():
        setattr(tool, field, value)
    await db.commit()
    return {"ok": True}


@router.delete("/{tool_id:uuid}")
async def delete_tool(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tool (only non-builtin)."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    _authorize_tool_record(current_user, tool, current_user.tenant_id)
    if tool.type == "builtin":
        raise HTTPException(status_code=400, detail="Cannot delete builtin tools")

    await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool_id))
    await db.delete(tool)
    await db.commit()
    return {"ok": True}


# ─── Per-Agent Tool Assignment ─────────────────────────────
@router.get("/agents/{agent_id}")
async def get_agent_tools(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tools for a specific agent with their enabled status."""
    agent_obj = await _require_agent_tool_access(db, current_user, agent_id)
    from app.services.agent_tools import _agent_has_feishu
    has_feishu = await _agent_has_feishu(agent_id)

    # Determine if this is a system agent (e.g. OKR Agent).
    # System agents can see all tools; regular agents cannot see okr_agent_only tools.
    is_system_agent = bool(agent_obj and agent_obj.is_system)

    # Agent-specific assignments
    assignments = await _load_agent_tool_assignments(db, agent_id)

    # All tools visible within this agent's tenant boundary
    all_tools_r = await db.execute(
        select(Tool)
        .where(Tool.enabled.is_(True), _agent_visible_tool_clause(agent_obj.tenant_id, assignments))
        .order_by(Tool.category, Tool.name)
    )
    all_tools = all_tools_r.scalars().all()

    # ── Backfill: create missing AgentTool records ──────────────────────
    # For agents that already have at least one AgentTool assignment (i.e.
    # the tool panel has been configured), create AgentTool records for any
    # visible tool that doesn't have one yet.  The initial `enabled` value
    # is taken from `is_default`.
    #
    # This keeps the UI state and `get_agent_tools_for_llm` in sync: both
    # now rely on explicit AgentTool records instead of the implicit
    # `is_default` fallback.
    if assignments:
        backfilled = 0
        for t in all_tools:
            tid = str(t.id)
            if tid not in assignments:
                new_at = AgentTool(
                    agent_id=agent_id,
                    tool_id=t.id,
                    enabled=t.is_default,
                )
                db.add(new_at)
                assignments[tid] = new_at
                backfilled += 1
        if backfilled:
            await db.commit()
            logger.info(
                f"[Tools] Backfilled {backfilled} AgentTool records for "
                f"agent={agent_id}"
            )

    result = []
    for t in all_tools:
        # Hide feishu tools for agents without Feishu channel
        if t.category == "feishu" and not has_feishu:
            continue
        # Hide OKR Agent-exclusive tools from regular agents.
        # These tools (create_objective, collect_okr_progress, etc.) should only
        # appear in the tool panel of system agents such as the OKR Agent.
        if (t.config or {}).get("okr_agent_only") and not is_system_agent:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj.tenant_id, assignments):
            continue
        # If no explicit assignment, use is_default
        available, availability_reason = await _code_tool_availability(
            db,
            t,
            agent_obj.tenant_id,
        )
        enabled = (at.enabled if at else t.is_default) and available
        result.append({
            "id": tid,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "available": available,
            "availability_reason": availability_reason,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_url": _mask_mcp_server_url(t.mcp_server_url),
            "source": t.source,
        })
    return result


@router.put("/agents/{agent_id}")
async def update_agent_tools(
    agent_id: uuid.UUID,
    updates: list[AgentToolUpdate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update tool assignments for an agent."""
    agent_obj = await _require_agent_tool_access(
        db,
        current_user,
        agent_id,
        manage=True,
    )
    assignments = await _load_agent_tool_assignments(db, agent_id)
    for u in updates:
        tool_id = uuid.UUID(u.tool_id)
        tool_r = await db.execute(
            select(Tool).where(
                Tool.id == tool_id,
                _agent_visible_tool_clause(agent_obj.tenant_id, assignments),
            )
        )
        tool_obj = tool_r.scalar_one_or_none()
        if not tool_obj:
            raise HTTPException(status_code=404, detail="Tool not found")
        available, availability_reason = await _code_tool_availability(
            db,
            tool_obj,
            agent_obj.tenant_id,
        )
        if u.enabled and not available:
            raise HTTPException(
                status_code=403,
                detail=availability_reason or "Code execution is not authorized",
            )

        # System-category tools (e.g. finish) are protocol-level and
        # must always remain enabled — reject any attempt to disable them.
        if tool_obj.category == "system" and not u.enabled:
            continue

        # Upsert
        result = await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
        )
        at = result.scalar_one_or_none()
        if at:
            at.enabled = u.enabled
        else:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=u.enabled))
    await db.commit()
    return {"ok": True}


# ─── MCP Server Testing ────────────────────────────────────
class MCPTestRequest(BaseModel):
    server_url: str
    # Optional standalone API Key. If provided, it is sent as
    # 'Authorization: Bearer {api_key}' and is NOT embedded in the URL.
    api_key: str | None = None


@router.post("/test-mcp")
async def test_mcp_connection(
    data: MCPTestRequest,
    current_user: User = Depends(get_current_admin),
):
    """Test connection to an MCP server and list available tools.

    Supports two authentication modes:
    - URL-embedded key (e.g. ?tavilyApiKey=xxx) — include in server_url.
    - Bearer token — pass via api_key field; sent as Authorization header.
    """
    from app.services.mcp_client import MCPClient

    try:
        client = MCPClient(data.server_url, api_key=data.api_key or None)
        tools = await client.list_tools()
        return {"ok": True, "tools": tools}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ─── MCP Server-level Credential Management ────────────────
class MCPServerUpdate(BaseModel):
    server_name: str            # Identifies which server's tools to update
    server_url: str             # New MCP server URL (may contain embedded key)
    api_key: str | None = None  # Optional standalone Bearer key
    # Target tenant (platform admins may manage another company's tools)
    tenant_id: str | None = None


@router.put("/mcp-server")
async def update_mcp_server(
    data: MCPServerUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-update the Server URL and API Key for all tools from an MCP server.

    All tools sharing the same mcp_server_name under the target tenant are
    updated atomically. The API Key is stored encrypted in tool.config so
    the agent runner can resolve it at execution time without re-configuring
    each tool individually.

    Authentication priority at runtime (handled by MCPClient):
    1. tool.config['api_key'] — sent as Authorization: Bearer header.
    2. URL query param (e.g. ?tavilyApiKey=xxx) — extracted from the URL
       and converted to Bearer by MCPClient automatically.
    """
    # Resolve target tenant
    target_tenant_id = _resolve_target_tenant_id(current_user, data.tenant_id)
    if not target_tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    # Load all tools from this server under the target tenant
    result = await db.execute(
        select(Tool).where(
            Tool.mcp_server_name == data.server_name,
            Tool.tenant_id == target_tenant_id,
        )
    )
    tools = result.scalars().all()
    if not tools:
        raise HTTPException(
            status_code=404,
            detail=f"No tools found for server '{data.server_name}'",
        )

    for tool in tools:
        tool.mcp_server_url = _merge_masked_mcp_server_url(
            tool.mcp_server_url,
            data.server_url,
        )
        if data.api_key is not None:
            # Decrypt before merging so existing encrypted fields are never
            # encrypted a second time. A mask/blank round-trip preserves the
            # stored secret just like the per-tool configuration endpoint.
            current_config = _decrypt_sensitive_fields(
                tool.config or {},
                tool.config_schema,
            )
            merged_config = merge_config_preserving_sensitive(
                current_config,
                {**current_config, "api_key": data.api_key},
                tool.config_schema,
            )
            tool.config = _encrypt_sensitive_fields(
                merged_config,
                tool.config_schema,
            )
        # If api_key is None (not provided), preserve the existing encrypted key

    await db.commit()
    return {"ok": True, "updated": len(tools)}




# ─── Agent-installed Tools Management (admin) ───────────────

@router.get("/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint: list user-installed tools scoped by tenant."""
    from app.models.agent import Agent
    query = (
        select(AgentTool, Tool, Agent)
        .join(Tool, cast(AgentTool.tool_id, String) == cast(Tool.id, String))
        .outerjoin(Agent, cast(AgentTool.installed_by_agent_id, String) == cast(Agent.id, String))
        .where(or_(AgentTool.source == "user_installed", Tool.source == "agent"))
        .order_by(AgentTool.created_at.desc())
    )
    # Scope by tenant: only show tools installed by agents in this tenant
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    if target_tenant_id:
        from app.models.agent import Agent as Ag
        # Some local/prod databases still have agents.tenant_id as varchar from
        # older migrations, while newer models bind tenant_id as UUID. Cast the
        # column to text so this admin listing works across both schemas.
        tenant_agent_ids = select(cast(Ag.id, String)).where(
            cast(Ag.tenant_id, String) == str(target_tenant_id)
        )
        query = query.where(cast(AgentTool.agent_id, String).in_(tenant_agent_ids))
    else:
        # A platform identity without a selected tenant must not receive a
        # cross-tenant aggregate accidentally.
        query = query.where(False)
    result = await db.execute(query)
    rows = result.all()
    return [
        {
            "agent_tool_id": str(at.id),
            "agent_id": str(at.agent_id),
            "tool_id": str(t.id),
            "tool_name": t.name,
            "tool_display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "source": t.source,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_url": _mask_mcp_server_url(t.mcp_server_url),
            "mcp_tool_name": t.mcp_tool_name,
            "installed_by_agent_id": str(at.installed_by_agent_id) if at.installed_by_agent_id else None,
            "installed_by_agent_name": a.name if a else None,
            "enabled": at.enabled,
            "configured": bool(at.config and len(at.config) > 0),
            "installed_at": at.created_at.isoformat() if at.created_at else None,
        }
        for at, t, a in rows
    ]


@router.delete("/agent-tool/{agent_tool_id}")
async def delete_agent_tool(
    agent_tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: remove an agent-tool assignment. Also deletes the tool record if no other agents use it."""
    at_r = await db.execute(select(AgentTool).where(AgentTool.id == agent_tool_id))
    at = at_r.scalar_one_or_none()
    if not at:
        raise HTTPException(status_code=404, detail="Agent tool assignment not found")
    await _require_agent_tool_access(
        db,
        current_user,
        at.agent_id,
        manage=True,
    )
    tool_id = at.tool_id
    await db.delete(at)
    await db.flush()
    # If no other Agent uses a user-installed MCP tool, remove that private
    # definition too. A manager uninstalling an assignment must never delete a
    # company/platform-owned MCP definition.
    remaining_r = await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id).limit(1))
    if not remaining_r.scalar_one_or_none():
        tool_r = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = tool_r.scalar_one_or_none()
        if tool and tool.type == "mcp" and tool.source == "agent":
            await db.delete(tool)
    await db.commit()
    return {"ok": True}


# ─── Per-Agent Tool Config ───────────────────────────────────

class AgentToolConfigUpdate(BaseModel):
    config: dict


@router.get("/agents/{agent_id}/tool-config/{tool_id}")
async def get_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get merged tool config (global defaults + agent overrides) and config_schema.

    Configs are decrypted only in memory; every sensitive field is masked in
    the response so the frontend can show that a value exists without exposing it.
    """
    agent = await _require_agent_tool_access(db, current_user, agent_id)
    assignments = await _load_agent_tool_assignments(db, agent_id)
    tool_r = await db.execute(
        select(Tool).where(
            Tool.id == tool_id,
            _agent_visible_tool_clause(agent.tenant_id, assignments),
        )
    )
    tool = tool_r.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()

    # Decrypt both configs using the tool's config_schema for field type awareness
    schema = tool.config_schema
    raw_global = await get_tool_company_config(db, tool, agent.tenant_id)
    raw_agent = _decrypt_sensitive_fields(at.config if at else {}, schema)

    # Mask sensitive fields in global config for display
    masked_global = mask_sensitive_fields(raw_global, schema)

    # Merged: agent overrides take precedence over global defaults.
    # Use raw (non-masked) global as the base so the agent inherits actual values
    # at runtime, but the UI will show masked_global for display hints.
    masked_agent = mask_sensitive_fields(raw_agent or {}, schema)
    merged = mask_sensitive_fields({**raw_global, **(raw_agent or {})}, schema)
    return {
        "global_config": masked_global,
        "agent_config": masked_agent,
        "merged_config": merged,
        "config_schema": tool.config_schema or {},
    }


@router.put("/agents/{agent_id}/tool-config/{tool_id}")
async def update_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    data: AgentToolConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save per-agent config override for a tool."""
    agent = await _require_agent_tool_access(
        db,
        current_user,
        agent_id,
        manage=True,
    )
    assignments = await _load_agent_tool_assignments(db, agent_id)
    # The tool itself must be visible inside this agent's tenant boundary.
    tool_r2 = await db.execute(
        select(Tool).where(
            Tool.id == tool_id,
            _agent_visible_tool_clause(agent.tenant_id, assignments),
        )
    )
    tool_for_schema = tool_r2.scalar_one_or_none()
    if not tool_for_schema:
        raise HTTPException(status_code=404, detail="Tool not found")

    # Code provider, endpoint, credential, egress and resource limits are a
    # separate platform privilege. Organization admins may still configure
    # ordinary tools within their tenant.
    if "allow_network" in data.config:
        if current_user.role not in ("platform_admin", "org_admin") and not _is_platform_admin(
            current_user
        ):
            raise HTTPException(
                status_code=403,
                detail="Only platform or organization admins can modify network access",
            )
    _enforce_code_control_permission(
        current_user,
        tool_for_schema,
        data.config,
    )

    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()
    existing_plain = _decrypt_sensitive_fields(
        (at.config if at else {}) or {},
        tool_for_schema.config_schema,
    )
    merged_config = merge_config_preserving_sensitive(
        existing_plain,
        data.config,
        tool_for_schema.config_schema,
    )
    encrypted_config = _encrypt_sensitive_fields(
        merged_config,
        tool_for_schema.config_schema,
    )
    if at:
        at.config = encrypted_config
    else:
        # Saving configuration is not an authorization action. The Agent must
        # still be enabled explicitly through the assignment endpoint.
        db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=False, config=encrypted_config))
    await db.commit()
    return {"ok": True}


@router.get("/agents/{agent_id}/with-config")
async def get_agent_tools_with_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent's enabled tools with per-agent config info and config_schema for settings UI.

    Configs are decrypted only in memory. Sensitive fields in both company and
    Agent values are masked before the response leaves the API.

    Special handling: some tools (Jina) store their API key in system_settings
    rather than Tool.config. We resolve those as part of the global config so
    the agent-level UI can show the inherited key hint.
    """
    agent_obj2 = await _require_agent_tool_access(db, current_user, agent_id)
    from app.services.agent_tools import _agent_has_feishu
    has_feishu = await _agent_has_feishu(agent_id)

    # Determine if this is a system agent (e.g. OKR Agent).
    is_system_agent2 = bool(agent_obj2 and agent_obj2.is_system)

    assignments = await _load_agent_tool_assignments(db, agent_id)
    all_tools_r = await db.execute(
        select(Tool)
        .where(Tool.enabled.is_(True), _agent_visible_tool_clause(agent_obj2.tenant_id, assignments))
        .order_by(Tool.category, Tool.name)
    )
    all_tools = all_tools_r.scalars().all()

    # Pre-fetch system_settings keys that some tools use as an alternative
    # config storage (e.g. Jina stores its API key in system_settings.jina_api_key)
    system_keys_cache: dict[str, str] = {}
    SYSTEM_SETTINGS_TOOL_MAP = {
        # tool_name -> system_settings key + value path
        "jina_search": ("jina_api_key", "api_key"),
        "jina_read": ("jina_api_key", "api_key"),
    }

    result = []
    for t in all_tools:
        # Hide feishu tools for agents without Feishu channel
        if t.category == "feishu" and not has_feishu:
            continue
        # Hide OKR Agent-exclusive tools from regular agents.
        if (t.config or {}).get("okr_agent_only") and not is_system_agent2:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj2.tenant_id, assignments):
            continue
        available, availability_reason = await _code_tool_availability(
            db,
            t,
            agent_obj2.tenant_id,
        )
        enabled = (at.enabled if at else t.is_default) and available

        # Decrypt tenant/company config for the frontend. Builtin tool configs
        # are tenant-scoped via tenant_settings, not shared Tool.config.
        raw_global = await get_tool_company_config(db, t, agent_obj2.tenant_id)

        # Fallback: resolve api_key from system_settings for tools that store
        # their key there (e.g. Jina). Only if Tool.config doesn't have it.
        if t.name in SYSTEM_SETTINGS_TOOL_MAP and not raw_global.get("api_key"):
            ss_key, ss_field = SYSTEM_SETTINGS_TOOL_MAP[t.name]
            if ss_key not in system_keys_cache:
                try:
                    from app.models.system_settings import SystemSetting
                    ss_r = await db.execute(
                        select(SystemSetting).where(SystemSetting.key == ss_key)
                    )
                    ss = ss_r.scalar_one_or_none()
                    system_keys_cache[ss_key] = (
                        ss.value.get(ss_field, "") if ss and ss.value else ""
                    )
                except Exception:
                    system_keys_cache[ss_key] = ""
            if system_keys_cache[ss_key]:
                raw_global["api_key"] = system_keys_cache[ss_key]

        raw_agent = _decrypt_sensitive_fields((at.config if at else {}) or {}, t.config_schema)

        # Mask sensitive fields in global_config so users can see that a key
        # is configured at the company level without exposing the full value.
        masked_global = mask_sensitive_fields(raw_global, t.config_schema)
        masked_agent = mask_sensitive_fields(raw_agent, t.config_schema)

        result.append({
            "id": tid,
            "agent_tool_id": str(at.id) if at else None,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "available": available,
            "availability_reason": availability_reason,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_url": _mask_mcp_server_url(t.mcp_server_url),
            "config_schema": t.config_schema or {},
            "global_config": masked_global,
            "agent_config": masked_agent,
            "source": t.source,
        })
    return result


# ─── Email Connection Testing ──────────────────────────────

class EmailTestRequest(BaseModel):
    config: dict


@router.post("/test-email")
async def test_email_connection(
    data: EmailTestRequest,
    current_user: User = Depends(get_current_user),
):
    """Test IMAP and SMTP email connections with provided config."""
    from app.services.email_service import test_connection

    try:
        result = await test_connection(data.config)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/email-providers")
async def get_email_providers(
    current_user: User = Depends(get_current_user),
):
    """Get list of supported email provider presets with help text."""
    from app.services.email_service import EMAIL_PROVIDERS

    return {
        key: {
            "label": p["label"],
            "help_url": p.get("help_url", ""),
            "help_text": p.get("help_text", ""),
        }
        for key, p in EMAIL_PROVIDERS.items()
    }
# ─── Tool Category Sharing Config (Generic ChannelConfig) ───

@router.get("/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get shared configuration for a tool category.

    Returns both global_config (company-level, from Tool.config) and
    agent_config (agent-level override, from ChannelConfig) separately.
    Sensitive fields in global_config are masked for display.
    Company-level values always take precedence at runtime.
    """
    from app.models.channel_config import ChannelConfig

    agent = await _require_agent_tool_access(db, current_user, agent_id)

    # ── 1. Load company-level (global) config from Tool.config ──────────────
    # Find a tool in this category that actually has config data.
    # We cannot just LIMIT 1 because most tools may have empty config.
    primary_tool_name = CATEGORY_CONFIG_PRIMARY_TOOL.get(category)
    all_cat_tools = await db.execute(
        select(Tool).where(
            Tool.category == category,
            Tool.enabled.is_(True),
            _agent_visible_tool_clause(agent.tenant_id, await _load_agent_tool_assignments(db, agent_id)),
        ).order_by((Tool.name != primary_tool_name) if primary_tool_name else Tool.name, Tool.name)
    )
    raw_global: dict = {}
    cat_schema: dict | None = None
    for ct in all_cat_tools.scalars():
        company_config = await get_tool_company_config(db, ct, agent.tenant_id)
        if company_config:
            cat_schema = ct.config_schema
            raw_global = company_config
            break

    # Mask sensitive fields for UI display
    masked_global = mask_sensitive_fields(raw_global, cat_schema)

    # ── 2. Load agent-level config from ChannelConfig ───────────────────────
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    config = result.scalar_one_or_none()

    config_id = None
    is_configured = bool(raw_global) or config is not None
    raw_agent: dict = {}

    if config:
        config_id = str(config.id)
        full_agent = {
            "api_key": config.app_secret,
            **(config.extra_config or {}),
        }
        raw_agent = _decrypt_sensitive_fields(full_agent)
        # Remove None values produced by missing app_secret
        raw_agent = {k: v for k, v in raw_agent.items() if v is not None}

    # ── 3. Build effective config ───────────────────────────────────────────
    # Priority: Agent config > Company config > Default
    # Agent can override company values by setting their own.
    masked_agent = mask_sensitive_fields(raw_agent, cat_schema)
    effective_config = mask_sensitive_fields({**raw_global, **raw_agent}, cat_schema)

    return {
        "id": config_id,
        "agent_id": str(agent_id),
        "category": category,
        "is_configured": is_configured,
        # Legacy field (backward-compat): full effective config for display
        "config": effective_config,
        # New fields for richer UI: show global and agent configs separately
        "global_config": masked_global,
        "agent_config": masked_agent,
    }


@router.post("/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID,
    category: str,
    data: CategoryConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update or create shared configuration for a tool category."""
    from app.models.channel_config import ChannelConfig

    await _require_agent_tool_access(db, current_user, agent_id, manage=True)

    # Encrypt sensitive fields
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    existing = result.scalar_one_or_none()
    existing_plain: dict = {}
    if existing:
        existing_plain = _decrypt_sensitive_fields({
            "api_key": existing.app_secret,
            **(existing.extra_config or {}),
        })
    merged_config = merge_config_preserving_sensitive(existing_plain, data.config)
    encrypted_config = _encrypt_sensitive_fields(merged_config)
    app_secret = encrypted_config.get("api_key") or encrypted_config.get("api_secret") or encrypted_config.get("app_secret")
    extra = {
        key: value
        for key, value in encrypted_config.items()
        if key not in ("api_key", "api_secret", "app_secret")
    }
    if existing:
        existing.app_secret = app_secret
        existing.extra_config = extra
        existing.is_configured = True
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            channel_type=category,
            app_id=category,
            app_secret=app_secret,
            extra_config=extra,
            is_configured=True,
        )
        db.add(config)

    await db.commit()

    # Special logic for Atlassian: trigger sync
    if category == "atlassian":
        from app.api.atlassian import _sync_atlassian_tools_for_agent
        import asyncio
        # Use the preserved plaintext value when a masked/omitted secret was
        # resubmitted with unrelated settings.
        plaintext_key = merged_config.get("api_key") or merged_config.get("api_secret") or merged_config.get("app_secret")
        asyncio.create_task(_sync_atlassian_tools_for_agent(agent_id, plaintext_key))

    return {"ok": True}


@router.delete("/agents/{agent_id}/category-config/{category}", status_code=204)
async def delete_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove shared configuration for a tool category."""
    from app.models.channel_config import ChannelConfig

    await _require_agent_tool_access(db, current_user, agent_id, manage=True)

    await db.execute(
        delete(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    await db.commit()


@router.post("/agents/{agent_id}/category-config/{category}/test")
async def test_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity for a tool category."""
    await _require_agent_tool_access(db, current_user, agent_id, manage=True)
    if category == "atlassian":
        from app.api.atlassian import test_atlassian_channel
        return await test_atlassian_channel(agent_id, current_user, db)
    elif category == "agentbay":
        from app.services.agentbay_client import test_agentbay_channel
        return await test_agentbay_channel(agent_id, current_user, db)

    return {"ok": True, "message": f"Settings for {category} saved."}
