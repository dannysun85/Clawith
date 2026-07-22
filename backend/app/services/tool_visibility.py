"""Canonical tenant boundary for tools exposed to an Agent.

The management API and the LLM Runtime must use the same visibility rule.
Assignments are grants, not ownership overrides: a stale AgentTool row may not
make another tenant's custom tool visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import or_

from app.models.tool import AgentTool, Tool
from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


def agent_visible_tool_clause(
    agent_tenant_id: uuid.UUID | None,
    assignments: Mapping[str, AgentTool],
):
    """Build the database predicate for tools visible to one Agent."""

    clauses = [Tool.source == "builtin"]

    admin_owner = Tool.tenant_id.is_(None)
    if agent_tenant_id is not None:
        admin_owner = admin_owner | (Tool.tenant_id == agent_tenant_id)
    clauses.append((Tool.source == "admin") & admin_owner)

    assigned_tool_ids = [uuid.UUID(tool_id) for tool_id in assignments]
    if assigned_tool_ids and agent_tenant_id is not None:
        clauses.append(
            (Tool.source == "agent")
            & (Tool.tenant_id == agent_tenant_id)
            & Tool.id.in_(assigned_tool_ids)
        )

    return or_(*clauses)


def tool_record_visible_to_agent(
    tool: Tool,
    agent_tenant_id: uuid.UUID | None,
    assignments: Mapping[str, AgentTool],
) -> bool:
    """Pure check mirroring :func:`agent_visible_tool_clause`."""

    # ``source`` predates some historical Tool rows and a few runtime adapters
    # still materialize the old record shape. The database query has already
    # applied the canonical ownership predicate, so a missing attribute here
    # means a legacy builtin record rather than permission to reinterpret an
    # explicitly owned tool. Real ORM Tool objects always expose ``source``.
    source = getattr(tool, "source", "builtin")
    tenant_id = getattr(tool, "tenant_id", None)
    if source == "builtin":
        return True
    if source == "admin":
        return tenant_id is None or (
            agent_tenant_id is not None and tenant_id == agent_tenant_id
        )
    if source == "agent":
        return (
            agent_tenant_id is not None
            and tenant_id is not None
            and tenant_id == agent_tenant_id
            and str(tool.id) in assignments
        )
    return False


def tool_enabled_for_agent(
    tool: Tool,
    assignment: AgentTool | None,
) -> bool:
    """Resolve an Agent grant without reviving explicit-grant capabilities.

    ``Tool.is_default`` remains the product-policy grant for ordinary core
    capabilities, including the reviewed global image, speech, and video
    tools. Capabilities retained in ``EXPLICIT_GRANT_TOOL_NAMES`` always
    require an ``AgentTool`` row, even if a stale database row still carries
    ``is_default=True`` before the canonical seeder repairs it. Agent-owned
    tools likewise require their exact assignment.
    """

    if assignment is not None:
        return bool(assignment.enabled)
    if getattr(tool, "source", "builtin") == "agent":
        return False
    if str(getattr(tool, "name", "")) in EXPLICIT_GRANT_TOOL_NAMES:
        return False
    return bool(getattr(tool, "is_default", False))
