"""Secret-free capability contracts for Agent templates and runtime Agents."""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool import AgentTool, Tool
from app.services.agent_template_contract import TEMPLATE_LIFECYCLE_ENABLED
from app.services.agent_tools import (
    RUNTIME_TYPED_APPLICATION_TOOL_NAMES,
    get_runtime_agent_tools_for_llm,
)
from app.services.builtin_tool_definitions import (
    BUILTIN_TOOL_NAMES,
    builtin_policy,
    builtin_readiness,
)
from app.services.skill_seeder import BUILTIN_SKILLS


def _value(template: object, name: str, default: Any) -> Any:
    if isinstance(template, Mapping):
        return template.get(name, default)
    return getattr(template, name, default)


def template_capability_contract(template: object) -> dict[str, object]:
    """Describe whether a template's declared routes exist in local registries."""
    known_skills = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    required_skills = list(_value(template, "default_skills", []) or [])
    required_tools = list(_value(template, "default_tools", []) or [])
    required_mcp = list(_value(template, "default_mcp_servers", []) or [])

    skills = [
        {
            "name": name,
            "status": "registered" if name in known_skills else "missing_registration",
        }
        for name in required_skills
    ]
    tools = []
    for name in required_tools:
        registered = name in BUILTIN_TOOL_NAMES
        typed = name in RUNTIME_TYPED_APPLICATION_TOOL_NAMES
        readiness = builtin_readiness(name) if registered else None
        tools.append(
            {
                "name": name,
                "status": ("contract_ready" if registered and typed and readiness else "missing_runtime_contract"),
                "registered": registered,
                "typed_adapter": typed,
                "readiness": readiness,
                "execution_policy": builtin_policy(name) if registered else None,
            }
        )
    mcp_servers = [
        {
            "server_id": server_id,
            "status": "import_on_hire",
            "readiness": "requires local registry credential, import, assignment, and MCP schema",
        }
        for server_id in required_mcp
    ]
    contract_ready = all(item["status"] == "registered" for item in skills) and all(
        item["status"] == "contract_ready" for item in tools
    )
    lifecycle = str(_value(template, "lifecycle_status", TEMPLATE_LIFECYCLE_ENABLED))
    return {
        "role_key": _value(template, "role_key", None),
        "role_revision": _value(template, "role_revision", 1),
        "lifecycle_status": lifecycle,
        "contract_ready": contract_ready,
        "activation_ready": contract_ready and lifecycle == TEMPLATE_LIFECYCLE_ENABLED,
        "skills": skills,
        "tools": tools,
        "mcp_servers": mcp_servers,
    }


def _mcp_prefix(server_id: str) -> str:
    return "mcp_" + server_id.replace("/", "_").replace("@", "") + "_"


async def agent_runtime_capability_readiness(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    template: object,
) -> dict[str, object]:
    """Compare template requirements with the Agent's locally ready workset.

    This check is intentionally local-only. It never pings a Provider, imports
    an MCP server, or reveals credentials.
    """
    contract = template_capability_contract(template)
    runtime_tools = await get_runtime_agent_tools_for_llm(agent_id)
    ready_tool_names = {
        str(item.get("function", {}).get("name") or "") for item in runtime_tools if isinstance(item, dict)
    }
    tool_rows = []
    for item in contract["tools"]:
        tool_rows.append(
            {
                **item,
                "runtime_status": ("available" if item["name"] in ready_tool_names else "unavailable"),
            }
        )

    required_mcp = list(_value(template, "default_mcp_servers", []) or [])
    assigned_mcp_names: set[str] = set()
    if required_mcp:
        result = await db.execute(
            select(Tool.name)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(
                AgentTool.agent_id == agent_id,
                AgentTool.enabled.is_(True),
                Tool.enabled.is_(True),
                Tool.type == "mcp",
            )
        )
        assigned_mcp_names = {str(name) for name in result.scalars().all()}
    mcp_rows = []
    for server_id in required_mcp:
        prefix = _mcp_prefix(server_id)
        assigned_names = sorted(name for name in assigned_mcp_names if name.startswith(prefix))
        runtime_names = sorted(set(assigned_names) & ready_tool_names)
        mcp_rows.append(
            {
                "server_id": server_id,
                "status": "available" if runtime_names else "unavailable",
                "assigned_tool_count": len(assigned_names),
                "runtime_tool_count": len(runtime_names),
            }
        )

    blockers = [f"tool:{item['name']}" for item in tool_rows if item["runtime_status"] != "available"] + [
        f"mcp:{item['server_id']}" for item in mcp_rows if item["status"] != "available"
    ]
    return {
        **contract,
        "agent_id": str(agent_id),
        "tools": tool_rows,
        "mcp_servers": mcp_rows,
        "runtime_status": "available" if not blockers else "degraded",
        "blockers": blockers,
        "next_action": (
            None
            if not blockers
            else "Configure/import the missing capability, reconcile template grants, and re-run readiness."
        ),
    }


__all__ = ["agent_runtime_capability_readiness", "template_capability_contract"]
