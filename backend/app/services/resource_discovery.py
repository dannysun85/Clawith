"""Resource discovery — search Smithery & ModelScope registries and import MCP servers."""

import asyncio
import hashlib
import json
import re
import uuid
import httpx
from loguru import logger
from sqlalchemy import select, text, update
from app.database import async_session
from app.models.agent import Agent
from app.models.tool import Tool, AgentTool
from app.services.tool_config import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    get_tenant_tool_config,
    tenant_scoped_tool_name,
)
from app.services.mcp_security import (
    MCPURLPolicyError,
    mcp_server_namespace,
    normalized_mcp_endpoint,
    smithery_connect_url,
    split_mcp_url_secrets,
    validate_public_mcp_url,
)
from app.services.agent_tool_assignments import (
    lock_agent_tool_owner,
    upsert_agent_tool,
)


# ── Smithery Registry Search ────────────────────────────────────

SMITHERY_API_BASE = "https://registry.smithery.ai"
MODELSCOPE_API_BASE = "https://modelscope.cn"
MAX_REGISTRY_RESPONSE_BYTES = 2 * 1024 * 1024


async def _bounded_json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> tuple[int, dict]:
    """Read a fixed-origin registry response without unbounded buffering."""

    body = bytearray()
    async with client.stream(method, url, **kwargs) as response:
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_REGISTRY_RESPONSE_BYTES:
                raise ValueError("Registry response exceeded the 2 MiB limit")
        status_code = response.status_code
    parsed = json.loads(bytes(body)) if body else {}
    if not isinstance(parsed, dict):
        raise ValueError("Registry response must be a JSON object")
    return status_code, parsed


async def _get_agent_tenant_id(agent_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve the tenant before creating or mutating an Agent-owned tool."""

    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        return result.scalar_one_or_none()


async def _find_tenant_mcp_tool(
    db,
    logical_name: str,
    tenant_id: uuid.UUID,
    server_namespace: str | None,
    server_name: str,
    upstream_tool_name: str | None,
) -> tuple[str, Tool | None]:
    """Find a namespaced MCP row while remaining compatible with RC1 names."""

    storage_name = tenant_scoped_tool_name(
        logical_name,
        tenant_id,
        namespace=server_namespace,
    )
    legacy_name = tenant_scoped_tool_name(logical_name, tenant_id)
    result = await db.execute(
        select(Tool).where(
            (
                Tool.name.in_({storage_name, legacy_name})
                | (
                    (Tool.mcp_server_name == server_name)
                    & (Tool.mcp_tool_name == upstream_tool_name)
                )
            ),
            Tool.type == "mcp",
            Tool.tenant_id == tenant_id,
        )
    )
    candidates = result.scalars().all()
    for tool in candidates:
        if tool.name == storage_name:
            return storage_name, tool
    for tool in candidates:
        try:
            identity = mcp_server_namespace(tool.mcp_server_name, tool.mcp_server_url)
        except MCPURLPolicyError:
            continue
        if identity == server_namespace and (
            tool.name == legacy_name
            or (
                tool.mcp_server_name == server_name
                and tool.mcp_tool_name == upstream_tool_name
            )
        ):
            return storage_name, tool
    return storage_name, None


async def _lock_tenant_mcp_import(
    db,
    tenant_id: uuid.UUID,
    server_namespace: str,
) -> None:
    """Serialize one company's imports of the same MCP server on PostgreSQL.

    Agent-row locks protect config merges for one Agent, but two Agents in the
    same company can still discover and insert the same deterministic Tool row
    concurrently.  A transaction-scoped advisory lock closes that cross-Agent
    check/insert race without widening ownership or retaining a process-local
    lock across workers.
    """

    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is None or bind.dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {
            "lock_key": (
                f"astra:mcp-import:{tenant_id}:{server_namespace}"
            )
        },
    )


async def _quarantine_legacy_generic_mcp_tools(
    db,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_namespace: str,
    server_name: str,
) -> dict:
    """Disable unusable legacy generic rows and preserve current Agent config.

    Old imports could create an Agent-owned MCP Tool without mcp_tool_name.
    Such a row cannot produce a valid tools/call request. It remains stored so
    encrypted per-Agent credentials are recoverable, while both the Tool and
    every assignment are quarantined. The importing Agent's legacy config is
    returned for migration to newly discovered named tools.
    """

    result = await db.execute(
        select(Tool).where(
            Tool.mcp_server_name == server_name,
            Tool.type == "mcp",
            Tool.source == "agent",
            Tool.tenant_id == tenant_id,
            Tool.mcp_tool_name.is_(None),
        )
    )
    legacy_tools = []
    for tool in result.scalars().all():
        try:
            identity = mcp_server_namespace(
                tool.mcp_server_name,
                tool.mcp_server_url,
            )
        except MCPURLPolicyError:
            continue
        if identity == server_namespace:
            legacy_tools.append(tool)
    if not legacy_tools:
        return {}

    legacy_ids = [tool.id for tool in legacy_tools]
    assignment_result = await db.execute(
        select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id.in_(legacy_ids),
        )
    )
    migrated_config: dict = {}
    for assignment in assignment_result.scalars().all():
        migrated_config.update(decrypt_sensitive_fields(assignment.config or {}))

    for tool in legacy_tools:
        tool.enabled = False
    await db.execute(
        update(AgentTool)
        .where(AgentTool.tool_id.in_(legacy_ids))
        .values(enabled=False)
    )
    return migrated_config


async def _get_smithery_api_key(agent_id: uuid.UUID | None = None) -> str:
    """Read Smithery API key.

    Priority: 1) per-agent AgentTool config, 2) system-level tool config.

    Sensitive fields in tool/AgentTool config are stored encrypted (see
    api.tools._encrypt_sensitive_fields). We must decrypt here before
    handing the value to httpx — otherwise Smithery rejects with 401.
    Falls back to raw value when decrypt fails (e.g. legacy plaintext keys).
    """
    def _maybe_decrypt(raw: str) -> str:
        if not raw:
            return ""
        return decrypt_sensitive_fields({"value": raw}, {"fields": [{"key": "value", "type": "password"}]}).get("value", raw)

    try:
        async with async_session() as db:
            agent_tenant_id = None
            if agent_id:
                from app.models.agent import Agent as AgentModel
                tenant_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                agent_tenant_id = tenant_r.scalar_one_or_none()

            # 1) Per-agent: check AgentTool configs for any MCP tool with a smithery_api_key
            if agent_id:
                at_r = await db.execute(
                    select(AgentTool).where(AgentTool.agent_id == agent_id)
                )
                for at in at_r.scalars().all():
                    if at.config and at.config.get("smithery_api_key"):
                        return _maybe_decrypt(at.config["smithery_api_key"])
            # 2) Tenant/company fallback for builtin discovery tools
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                tenant_config = await get_tenant_tool_config(db, agent_tenant_id, tool.name, tool.config_schema)
                if tenant_config.get("smithery_api_key"):
                    return tenant_config["smithery_api_key"]
                if tool.config and tool.config.get("smithery_api_key") and not agent_tenant_id:
                    return _maybe_decrypt(tool.config["smithery_api_key"])
    except Exception:
        pass
    return ""


async def _search_smithery_api(query: str, max_results: int, api_key: str) -> list[dict]:
    """Search Smithery registry, returns normalized results."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            status_code, data = await _bounded_json_request(
                client,
                "GET",
                f"{SMITHERY_API_BASE}/servers",
                params={"q": query, "pageSize": max_results},
                headers=headers,
            )
            if status_code != 200:
                return []
        results = []
        for srv in data.get("servers", [])[:max_results]:
            results.append({
                "name": srv.get("qualifiedName", ""),
                "display_name": srv.get("displayName", ""),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("remote", False),
                "verified": srv.get("verified", False),
                "use_count": srv.get("useCount", 0),
                "homepage": srv.get("homepage", ""),
                "source": "Smithery",
            })
        return results
    except Exception:
        return []


async def _get_modelscope_api_token(agent_id: uuid.UUID | None = None) -> str:
    """Read ModelScope API token from discover_resources tool config."""
    try:
        async with async_session() as db:
            agent_tenant_id = None
            if agent_id:
                from app.models.agent import Agent as AgentModel
                tenant_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
                agent_tenant_id = tenant_r.scalar_one_or_none()
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                tenant_config = await get_tenant_tool_config(db, agent_tenant_id, tool.name, tool.config_schema)
                if tenant_config.get("modelscope_api_token"):
                    return tenant_config["modelscope_api_token"]
                if tool.config and tool.config.get("modelscope_api_token") and not agent_tenant_id:
                    return tool.config["modelscope_api_token"]
    except Exception:
        pass
    return ""


async def _search_modelscope_api(query: str, max_results: int, agent_id: uuid.UUID | None = None) -> list[dict]:
    """Search ModelScope MCP Hub via official OpenAPI (no WAF issues)."""
    api_token = await _get_modelscope_api_token(agent_id)
    if not api_token:
        return []  # Silently skip if no token configured

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
        "Cookie": f"m_session_id={api_token}",
        "User-Agent": "modelscope-mcp-server/1.0",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            status_code, data = await _bounded_json_request(
                client,
                "PUT",
                f"{MODELSCOPE_API_BASE}/openapi/v1/mcp/servers",
                json={"page_size": max_results, "page_number": 1, "search": query, "filter": {}},
                headers=headers,
            )
            if status_code != 200:
                return []
            if not data.get("success"):
                return []

        servers_data = data.get("data", {}).get("mcp_server_list", [])
        if not servers_data:
            return []

        results = []
        for srv in servers_data[:max_results]:
            server_id = srv.get("id", "")
            results.append({
                "name": server_id,
                "display_name": srv.get("name", server_id),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("is_hosted", False),
                "verified": True,
                "use_count": 0,
                "homepage": f"https://modelscope.cn/mcp/servers/{server_id}",
                "source": "ModelScope",
            })
        return results
    except Exception as exc:
        logger.warning(
            "[ResourceDiscovery] ModelScope search failed error_type={}",
            type(exc).__name__,
        )
        return []


async def search_registries(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    """Search both Smithery and ModelScope for MCP servers."""
    api_key = await _get_smithery_api_key(agent_id)

    # Search both registries in parallel
    smithery_task = _search_smithery_api(query, max_results, api_key)
    modelscope_task = _search_modelscope_api(query, max_results, agent_id)
    smithery_results, modelscope_results = await asyncio.gather(smithery_task, modelscope_task)

    # Merge: Smithery first, then ModelScope (deduplicate by name)
    seen_names = set()
    all_results = []
    for r in smithery_results + modelscope_results:
        if r["name"] not in seen_names:
            seen_names.add(r["name"])
            all_results.append(r)

    if not all_results:
        return f'🔍 No MCP servers found for "{query}" on Smithery or ModelScope. Try different keywords.'

    results = []
    for i, srv in enumerate(all_results[:max_results], 1):
        verified = " ✅" if srv["verified"] else ""
        source_tag = f"[{srv['source']}]"
        if srv["remote"]:
            deploy_info = "🌐 Remote (no local install needed)"
        else:
            deploy_info = "💻 Local install required"
        use_info = f" · 👥 {srv['use_count']:,} users" if srv["use_count"] else ""
        hp = srv['homepage']

        results.append(
            f"**{i}. {srv['display_name']}**{verified} {source_tag}\n"
            f"   ID: `{srv['name']}`\n"
            f"   {srv['description']}\n"
            f"   {deploy_info}{use_info}\n"
            f"   {'🔗 ' + hp if hp else ''}"
        )

    header = f'🔍 Found {len(results)} MCP server(s) for "{query}":\n\n'
    footer = (
        "\n\n---\n"
        "💡 To import a remote server, use `import_mcp_server` with the server ID.\n"
        '   Example: import_mcp_server(server_id="gmail")'
    )
    return header + "\n\n".join(results) + footer


# Keep backward-compatible alias
async def search_smithery(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    return await search_registries(query, max_results, agent_id=agent_id)


# ── Import MCP Server ───────────────────────────────────────────

async def _ensure_smithery_connection(api_key: str, mcp_url: str, display_name: str) -> dict:
    """Create or reuse a Smithery Connect namespace + connection.

    Returns dict with keys: namespace, connection_id, auth_url (if OAuth needed).
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            # Get or create namespace
            ns_status, ns_data = await _bounded_json_request(
                client,
                "GET",
                "https://api.smithery.ai/namespaces",
                headers=headers,
            )
            namespaces = ns_data.get("namespaces", []) if ns_status == 200 else []
            if namespaces:
                namespace = str(namespaces[0].get("name", ""))
            else:
                create_status, create_data = await _bounded_json_request(
                    client,
                    "POST",
                    "https://api.smithery.ai/namespaces",
                    json={"name": "astra"},
                    headers=headers,
                )
                if create_status not in (200, 201):
                    return {"error": "smithery_namespace_setup_failed"}
                namespace = str(create_data.get("name", ""))

            # Create connection
            conn_slug = re.sub(
                r"[^a-z0-9._-]+",
                "-",
                str(display_name or "").casefold(),
            ).strip("-._") or "astra-mcp"
            endpoint_digest = hashlib.sha256(
                normalized_mcp_endpoint(mcp_url).encode()
            ).hexdigest()[:10]
            conn_id = f"{conn_slug[:53]}-{endpoint_digest}"
            connection_collection_url = smithery_connect_url(namespace)
            conn_status, conn_data = await _bounded_json_request(
                client,
                "POST",
                connection_collection_url,
                json={"connectionId": conn_id, "mcpUrl": mcp_url, "name": display_name},
                headers=headers,
            )
            if conn_status not in (200, 201):
                return {"error": "smithery_connection_setup_failed"}

            returned_connection_id = str(conn_data.get("connectionId", conn_id))
            smithery_connect_url(namespace, returned_connection_id)
            result = {
                "namespace": namespace,
                "connection_id": returned_connection_id,
            }
            status = conn_data.get("status", {})
            if isinstance(status, dict) and status.get("state") == "auth_required":
                auth_url = str(status.get("authorizationUrl", ""))
                if auth_url:
                    result["auth_url"] = await validate_public_mcp_url(auth_url)
            return result
    except Exception as exc:
        logger.warning(
            "[ResourceDiscovery] Smithery connection setup failed error_type={}",
            type(exc).__name__,
        )
        return {"error": "smithery_connection_setup_failed"}


async def import_mcp_from_smithery(
    server_id: str,
    agent_id: uuid.UUID,
    config: dict | None = None,
    reauthorize: bool = False,
) -> str:
    """Import an MCP server from Smithery into the platform.

    Uses the Smithery Registry detail API to get tool definitions,
    and stores the deploymentUrl for runtime execution via Smithery Connect.
    If config contains 'smithery_api_key', it's stored per-agent for future use.
    """
    config = dict(config) if config else {}  # mutable copy
    agent_tenant_id = await _get_agent_tenant_id(agent_id)
    if agent_tenant_id is None:
        return "❌ MCP import requires an Agent that belongs to a company"

    # Extract smithery_api_key from config (user-provided) or fallback to stored
    api_key = config.pop("smithery_api_key", None) or await _get_smithery_api_key(agent_id)
    if not api_key:
        return (
            "❌ Smithery API key is required to import MCP servers.\n\n"
            "请提供你的 Smithery API Key，你可以通过以下步骤获取：\n"
            "1. 注册/登录 https://smithery.ai\n"
            "2. 前往 https://smithery.ai/account/api-keys 创建 API Key\n"
            "3. 将 Key 提供给我，例如：\n"
            '   `import_mcp_server(server_id="github", config={"smithery_api_key": "your-key"})`'
        )

    # Write key back to discover_resources / import_mcp_server AgentTool configs
    # so it shows up in the Config dialog
    try:
        async with async_session() as db:
            await lock_agent_tool_owner(db, agent_id)
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if not tool:
                    continue
                at_r = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                at = at_r.scalar_one_or_none()
                encrypted_config = encrypt_sensitive_fields(
                    {**(decrypt_sensitive_fields(at.config or {}) if at else {}), "smithery_api_key": api_key}
                )
                await upsert_agent_tool(
                    db,
                    agent_id=agent_id,
                    tool_id=tool.id,
                    enabled=True,
                    source="system",
                    config=encrypted_config,
                    on_conflict="reauthorize",
                )
            await db.commit()
    except Exception:
        pass  # non-critical — key is still usable from MCP tool configs

    # Step 1: Search for server by ID
    headers = {"Accept": "application/json"}

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            status_code, data = await _bounded_json_request(
                client,
                "GET",
                f"{SMITHERY_API_BASE}/servers",
                params={"q": server_id.lstrip("@"), "pageSize": 5},
                headers=headers,
            )
            if status_code != 200:
                return f"❌ Server '{server_id}' not found on Smithery"
            servers = data.get("servers", [])
            server_info = None
            clean_id = server_id.lstrip("@")
            for s in servers:
                if s.get("qualifiedName") == clean_id or s.get("qualifiedName") == server_id:
                    server_info = s
                    break
            if not server_info and servers:
                server_info = servers[0]
            if not server_info:
                return f"❌ Server '{server_id}' not found on Smithery."
    except Exception as exc:
        logger.warning(
            "[ResourceDiscovery] Smithery server lookup failed error_type={}",
            type(exc).__name__,
        )
        return "❌ Failed to fetch server info from Smithery"

    display_name = server_info.get("displayName", server_id.split("/")[-1])
    description = server_info.get("description", "")
    qualified_name = server_info.get("qualifiedName", server_id.lstrip("@"))

    # Check if server supports remote hosting
    if not server_info.get("remote"):
        return (
            f"⚠️ **{display_name}** (`{qualified_name}`) does not support remote hosting via Smithery Connect.\n"
            f"This server requires local installation and cannot be imported automatically.\n"
            f"🔗 {server_info.get('homepage', '')}"
        )

    # Step 2: Get full server details including tools from registry API
    tools_discovered = []
    deployment_url = None
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            detail_status, detail = await _bounded_json_request(
                client,
                "GET",
                f"{SMITHERY_API_BASE}/servers/{qualified_name}",
                headers=headers,
            )
            if detail_status == 200:
                deployment_url = detail.get("deploymentUrl")
                raw_tools = detail.get("tools", [])
                tools_discovered = [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                    for t in raw_tools if t.get("name")
                ]
                logger.info(
                    "[ResourceDiscovery] Got {} tools from registry",
                    len(tools_discovered),
                )
            else:
                logger.warning(
                    "[ResourceDiscovery] Could not fetch server detail status={}",
                    detail_status,
                )
    except Exception as exc:
        logger.warning(
            "[ResourceDiscovery] Could not fetch server detail error_type={}",
            type(exc).__name__,
        )

    # Step 3: Determine the MCP server URL for runtime execution
    raw_base_mcp_url = deployment_url or f"https://{qualified_name}.run.tools"
    try:
        await validate_public_mcp_url(raw_base_mcp_url)
        base_mcp_url, base_url_secret_payload = split_mcp_url_secrets(raw_base_mcp_url)
    except MCPURLPolicyError as exc:
        return f"❌ MCP server URL rejected by security policy: {exc}"

    # Step 3.5: Auto-create Smithery Connect namespace + connection
    smithery_config = {}  # will be merged into every AgentTool.config
    auth_message = ""
    conn_result = await _ensure_smithery_connection(api_key, raw_base_mcp_url, display_name)
    connection_ready = "error" not in conn_result
    authorization_pending = bool(conn_result.get("auth_url")) if connection_ready else False
    if "error" in conn_result:
        auth_message = "\n\n⚠️ Could not auto-create the Smithery connection. Please retry."
    else:
        smithery_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        if conn_result.get("auth_url"):
            auth_message = (
                f"\n\n🔐 **OAuth 授权需要**: 请在浏览器中访问以下链接完成授权：\n"
                f"{conn_result['auth_url']}\n"
                f"授权完成后，工具即可使用。"
            )

    # Step 3.6: Override registry-advertised schema with the runtime server's
    # actual tools/list. Smithery's registry detail can drift behind the live
    # server (we hit this with shibui/finance: registry said `sql`, server
    # required `user_prompt` + `query`). The truth is whatever tools/list
    # returns at call time, so prefer it whenever available.
    if smithery_config:
        ns_ = smithery_config["smithery_namespace"]
        conn_ = smithery_config["smithery_connection_id"]
        try:
            from app.services.mcp_client import MCPClient

            connect_url = smithery_connect_url(ns_, conn_)
            live_tools = await MCPClient(connect_url, api_key=api_key).list_tools()
            logger.info(
                "[ResourceDiscovery] Using live tools/list: {} tool(s) "
                "override registry's {}",
                len(live_tools),
                len(tools_discovered),
            )
            # A successful empty tools/list response is authoritative.  The
            # registry catalog is descriptive metadata and may be stale; using
            # it after the live server returned no callable names would create
            # tools that cannot execute.  Only transport/protocol exceptions
            # below retain the registry fallback.
            tools_discovered = live_tools
        except Exception as exc:
            logger.warning(
                "[ResourceDiscovery] Live tools/list failed; falling back to registry schema "
                "error_type={}",
                type(exc).__name__,
            )

    # Merge smithery_config + user config for AgentTool
    agent_tool_config = {**smithery_config, **config}
    if base_url_secret_payload:
        agent_tool_config["mcp_url_query_secrets"] = base_url_secret_payload
    server_namespace = mcp_server_namespace(display_name, base_mcp_url)

    async with async_session() as db:
        await lock_agent_tool_owner(db, agent_id)
        await _lock_tenant_mcp_import(
            db,
            agent_tenant_id,
            server_namespace,
        )
        imported_tools = []

        legacy_agent_config = await _quarantine_legacy_generic_mcp_tools(
            db,
            tenant_id=agent_tenant_id,
            agent_id=agent_id,
            server_namespace=server_namespace,
            server_name=display_name,
        )
        agent_tool_config = {**legacy_agent_config, **agent_tool_config}

        if not connection_ready:
            await db.commit()
            return (
                f"❌ Smithery connection for **{display_name}** could not be "
                "validated. Existing named tools, URLs, configurations, and "
                "enablement were left unchanged; legacy generic rows were "
                "quarantined. Retry after Smithery is available."
            )
        if authorization_pending:
            await db.commit()
            return (
                f"⚠️ **{display_name}** requires authorization before its tools "
                "can be enabled. Existing named tools and configurations were "
                "left unchanged; complete authorization and then retry."
                f"{auth_message}"
            )

        # Helper: ensure AgentTool link exists and save config
        async def _ensure_agent_tool(tool_id: uuid.UUID):
            agent_check = await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
            at = agent_check.scalar_one_or_none()
            existing_plain = decrypt_sensitive_fields(at.config or {}) if at else {}
            encrypted_config = encrypt_sensitive_fields(
                {**existing_plain, **agent_tool_config}
            )
            await upsert_agent_tool(
                db,
                agent_id=agent_id,
                tool_id=tool_id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
                config=encrypted_config,
                on_conflict="reauthorize",
            )

        existing_server_tools_r = await db.execute(
            select(Tool).where(
                Tool.mcp_server_name == display_name,
                Tool.type == "mcp",
                Tool.source == "agent",
                Tool.tenant_id == agent_tenant_id,
            )
        )
        existing_server_tools = []
        for existing in existing_server_tools_r.scalars().all():
            try:
                existing_namespace = mcp_server_namespace(
                    existing.mcp_server_name,
                    existing.mcp_server_url,
                )
            except MCPURLPolicyError:
                continue
            if existing_namespace != server_namespace:
                continue
            if existing.mcp_tool_name:
                existing_server_tools.append(existing)

        if tools_discovered:
            # Create one Tool record per MCP tool
            for mcp_tool in tools_discovered:
                logical_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}_{mcp_tool['name']}"
                tool_name, existing_tool = await _find_tenant_mcp_tool(
                    db,
                    logical_name,
                    agent_tenant_id,
                    server_namespace,
                    display_name,
                    mcp_tool["name"],
                )
                tool_display = f"{display_name}: {mcp_tool['name']}"

                if existing_tool:
                    existing_tool.mcp_server_url = base_mcp_url
                    await _ensure_agent_tool(existing_tool.id)
                    if reauthorize:
                        imported_tools.append(f"🔄 {tool_display} (reauthorized)")
                    elif config:
                        imported_tools.append(f"🔄 {tool_display} (config updated)")
                    else:
                        imported_tools.append(f"⏭️ {tool_display} (already imported)")
                    continue

                tool = Tool(
                    name=tool_name,
                    display_name=tool_display,
                    description=mcp_tool.get("description", description)[:500],
                    type="mcp",
                    category="mcp",
                    icon="🔌",
                    parameters_schema=mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
                    mcp_server_url=base_mcp_url,
                    mcp_server_name=display_name,
                    mcp_tool_name=mcp_tool["name"],
                    enabled=True,
                    is_default=False,
                    source="agent",
                    tenant_id=agent_tenant_id,
                )
                db.add(tool)
                await db.flush()
                await _ensure_agent_tool(tool.id)
                imported_tools.append(f"✅ {tool_display}")
        else:
            # Fail closed when neither the registry nor the live endpoint can
            # provide an executable tool name. Never turn transport failure
            # into a green "ready" tool that will call the wrong remote name.
            if existing_server_tools:
                await db.commit()
                result = (
                    f"⚠️ **{display_name}** did not return a live tool catalog. "
                    f"Left {len(existing_server_tools)} previously discovered named "
                    "tool(s), URLs, configurations, and enablement unchanged; no "
                    "generic tool was created. Retry discovery before treating "
                    "this server as ready."
                )
                return result + auth_message

            await db.commit()
            result = (
                f"❌ MCP import stopped: **{display_name}** did not return any "
                "named tools. No executable tool was created or enabled. Retry "
                "after the server and authorization are available."
            )
            return result + auth_message

        await db.commit()

    result = f"🔌 Imported MCP server: **{display_name}** (`{server_id}`)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{base_mcp_url}`"
    if auth_message:
        result += auth_message
    else:
        result += "\n\n💡 The imported tools are now available for use."
    return result


# ── Direct URL Import ───────────────────────────────────────────

async def import_mcp_direct(
    mcp_url: str,
    agent_id: uuid.UUID,
    server_name: str | None = None,
    api_key: str | None = None,
) -> str:
    """Import an MCP server by directly connecting to its HTTP/SSE endpoint.

    This bypasses Smithery entirely — useful for self-hosted or third-party
    MCP servers that provide their own public endpoint.
    """
    from app.services.mcp_client import MCPClient

    agent_tenant_id = await _get_agent_tenant_id(agent_id)
    if agent_tenant_id is None:
        return "❌ MCP import requires an Agent that belongs to a company"

    try:
        await validate_public_mcp_url(mcp_url)
        public_mcp_url, url_secret_payload = split_mcp_url_secrets(mcp_url)
    except MCPURLPolicyError as exc:
        return f"❌ MCP server URL rejected by security policy: {exc}"

    display_name = server_name or public_mcp_url.split("//")[-1].split("/")[0].split(":")[0]
    safe_name = display_name.replace(".", "_").replace("/", "_").replace(":", "_").replace("-", "_")
    server_namespace = mcp_server_namespace(display_name, public_mcp_url)

    # Try to list tools from the endpoint
    tools_discovered = []
    try:
        client = MCPClient(mcp_url, api_key=api_key)
        tools_discovered = await client.list_tools()
        logger.info(f"[DirectImport] Got {len(tools_discovered)} tools from configured server")
    except Exception as e:
        logger.error(
            "[DirectImport] Could not list tools error_type={}",
            type(e).__name__,
        )

    # Config to store in AgentTool
    agent_tool_config = {}
    if api_key:
        agent_tool_config["api_key"] = api_key
    if url_secret_payload:
        agent_tool_config["mcp_url_query_secrets"] = url_secret_payload

    async with async_session() as db:
        await lock_agent_tool_owner(db, agent_id)
        await _lock_tenant_mcp_import(
            db,
            agent_tenant_id,
            server_namespace,
        )
        imported_tools = []

        legacy_agent_config = await _quarantine_legacy_generic_mcp_tools(
            db,
            tenant_id=agent_tenant_id,
            agent_id=agent_id,
            server_namespace=server_namespace,
            server_name=display_name,
        )
        agent_tool_config = {**legacy_agent_config, **agent_tool_config}

        if not tools_discovered:
            await db.commit()
            return (
                f"❌ MCP import stopped: **{display_name}** did not return any "
                "named tools. No executable tool was created or enabled; legacy "
                "generic rows were quarantined. Check the endpoint and "
                "credentials, then retry."
            )

        async def _ensure_agent_tool(tool_id: uuid.UUID):
            agent_check = await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
            at = agent_check.scalar_one_or_none()
            existing_plain = decrypt_sensitive_fields(at.config or {}) if at else {}
            encrypted_config = encrypt_sensitive_fields(
                {**existing_plain, **agent_tool_config}
            )
            await upsert_agent_tool(
                db,
                agent_id=agent_id,
                tool_id=tool_id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
                config=encrypted_config,
                on_conflict="reauthorize",
            )

        if tools_discovered:
            for mcp_tool in tools_discovered:
                logical_name = f"mcp_{safe_name}_{mcp_tool['name']}"
                tool_name, existing_tool = await _find_tenant_mcp_tool(
                    db,
                    logical_name,
                    agent_tenant_id,
                    server_namespace,
                    display_name,
                    mcp_tool["name"],
                )
                tool_display = f"{display_name}: {mcp_tool['name']}"

                if existing_tool:
                    existing_tool.mcp_server_url = public_mcp_url
                    await _ensure_agent_tool(existing_tool.id)
                    imported_tools.append(f"⏭️ {tool_display} (already imported)")
                    continue

                tool = Tool(
                    name=tool_name,
                    display_name=tool_display,
                    description=mcp_tool.get("description", "")[:500],
                    type="mcp",
                    category="mcp",
                    icon="🔌",
                    parameters_schema=mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
                    mcp_server_url=public_mcp_url,
                    mcp_server_name=display_name,
                    mcp_tool_name=mcp_tool["name"],
                    enabled=True,
                    is_default=False,
                    source="agent",
                    tenant_id=agent_tenant_id,
                )
                db.add(tool)
                await db.flush()
                await _ensure_agent_tool(tool.id)
                imported_tools.append(f"✅ {tool_display}")
        await db.commit()

    result = f"🔌 Imported MCP server: **{display_name}**\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{public_mcp_url}`"
    result += "\n\n💡 The imported tools are now available for use."
    return result


# ── Atlassian Rovo MCP Auto-Seeding ─────────────────────────────────────────

ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
ATLASSIAN_ROVO_SERVER_NAME = "Atlassian Rovo"
ATLASSIAN_ROVO_TOOL_PREFIX = "atlassian_rovo_"


async def seed_atlassian_rovo_tools(api_key: str) -> None:
    """Connect to Atlassian Rovo MCP and seed all available tools as platform-level MCP tools.

    Called on startup when an API key is configured. Existing tools are updated in-place;
    new tools discovered from the server are created. The platform key is used
    only for schema discovery and is never persisted on tenantless Tool rows.
    """
    from app.services.mcp_client import MCPClient

    logger.info("[AtlassianRovo] Connecting to configured server")
    try:
        client = MCPClient(ATLASSIAN_ROVO_MCP_URL, api_key=api_key)
        tools_discovered = await client.list_tools()
    except Exception as e:
        logger.error(
            "[AtlassianRovo] Could not list tools error_type={}",
            type(e).__name__,
        )
        return

    if not tools_discovered:
        logger.warning("[AtlassianRovo] No tools returned from server")
        return

    logger.info(f"[AtlassianRovo] Discovered {len(tools_discovered)} tools")

    async with async_session() as db:
        upserted = 0
        for mcp_tool in tools_discovered:
            raw_name = mcp_tool.get("name", "")
            if not raw_name:
                continue

            tool_name = f"{ATLASSIAN_ROVO_TOOL_PREFIX}{raw_name}"
            tool_display = f"Atlassian: {raw_name}"
            tool_desc = mcp_tool.get("description", "")[:500]
            tool_schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})

            # Determine icon based on tool name hints
            if "jira" in raw_name.lower() or "issue" in raw_name.lower():
                icon = "🔵"
            elif "confluence" in raw_name.lower() or "page" in raw_name.lower():
                icon = "📘"
            elif "compass" in raw_name.lower() or "component" in raw_name.lower():
                icon = "🧭"
            else:
                icon = "🔷"

            existing_r = await db.execute(select(Tool).where(Tool.name == tool_name))
            existing_tool = existing_r.scalar_one_or_none()

            if existing_tool:
                # Update description and schema in case they changed
                existing_tool.description = tool_desc
                existing_tool.parameters_schema = tool_schema
                existing_tool.config = {}
            else:
                tool = Tool(
                    name=tool_name,
                    display_name=tool_display,
                    description=tool_desc,
                    type="mcp",
                    category="atlassian",
                    icon=icon,
                    parameters_schema=tool_schema,
                    mcp_server_url=ATLASSIAN_ROVO_MCP_URL,
                    mcp_server_name=ATLASSIAN_ROVO_SERVER_NAME,
                    mcp_tool_name=raw_name,
                    enabled=True,
                    is_default=False,
                    config={},
                    source="admin",
                )
                db.add(tool)
                upserted += 1

        await db.commit()

    logger.info(f"[AtlassianRovo] Seeded {upserted} new Atlassian Rovo tools")


async def refresh_atlassian_rovo_api_key(api_key: str) -> None:
    """Scrub legacy global keys; credentials are per Agent/channel only."""
    del api_key
    async with async_session() as db:
        from sqlalchemy import update as _update
        await db.execute(
            _update(Tool)
            .where(Tool.mcp_server_name == ATLASSIAN_ROVO_SERVER_NAME, Tool.type == "mcp")
            .values(config={})
        )
        await db.commit()
    logger.info("[AtlassianRovo] Removed global credentials from Rovo tools")
