"""Finalize Agent template revisions after local capability synchronization."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate
from app.services.template_capabilities import TemplateToolReconcileReport


async def finalize_template_revision_sync(
    db: AsyncSession,
    *,
    tool_report: TemplateToolReconcileReport,
    skill_sync_state: dict | None,
) -> dict[str, int]:
    """Advance only Agents whose reviewed local capability contract is ready.

    The synchronizer deliberately owns only revision bookkeeping. Agent name,
    role description, SOUL, autonomy policy, user-selected Skills, and
    user-selected Tool assignments are outside its write boundary.
    """
    result = await db.execute(
        select(Agent, AgentTemplate)
        .join(AgentTemplate, Agent.template_id == AgentTemplate.id)
        .where(
            Agent.deleted_at.is_(None),
            AgentTemplate.is_builtin.is_(True),
            AgentTemplate.lifecycle_status == "enabled",
        )
    )
    rows = result.all()
    missing_tools = set(tool_report.missing_tool_names)
    missing_mcp = set(tool_report.missing_mcp_servers)
    skill_conflicts = int((skill_sync_state or {}).get("conflicts") or 0)
    conflict_agent_ids = {
        str(agent_id)
        for agent_id in (skill_sync_state or {}).get("conflict_agent_ids", [])
    }
    raw_conflict_folders = (skill_sync_state or {}).get(
        "conflict_skill_folders_by_agent", {}
    )
    conflict_folders_by_agent = {
        str(agent_id): sorted(
            {
                str(folder)
                for folder in folders
                if isinstance(folder, str) and folder.strip()
            }
        )
        for agent_id, folders in raw_conflict_folders.items()
        if isinstance(folders, (list, tuple, set))
    } if isinstance(raw_conflict_folders, dict) else {}
    global_skill_conflict = skill_conflicts > 0 and not conflict_agent_ids
    counts = {
        "reviewed": len(rows),
        "advanced": 0,
        "current": 0,
        "pending": 0,
        "conflict": 0,
    }
    now = datetime.now(timezone.utc)

    for agent, template in rows:
        blockers: dict[str, object] = {}
        required_tools = set(template.default_tools or [])
        required_mcp = set(template.default_mcp_servers or [])
        unresolved_tools = sorted(required_tools & missing_tools)
        unresolved_mcp = sorted(required_mcp & missing_mcp)
        if unresolved_tools:
            blockers["missing_tools"] = unresolved_tools
        if unresolved_mcp:
            blockers["missing_mcp_servers"] = unresolved_mcp
        agent_conflict_folders = conflict_folders_by_agent.get(str(agent.id), [])
        if agent_conflict_folders:
            blockers["managed_skill_conflicts"] = len(agent_conflict_folders)
            blockers["managed_skill_conflict_folders"] = agent_conflict_folders
        elif global_skill_conflict or str(agent.id) in conflict_agent_ids:
            # Preserve compatibility with older sync-state records that only
            # persisted the aggregate count and scoped Agent IDs.
            blockers["managed_skill_conflicts"] = skill_conflicts

        if blockers:
            agent.template_sync_status = (
                "conflict" if "managed_skill_conflicts" in blockers else "pending"
            )
            agent.template_sync_details = blockers
            counts[agent.template_sync_status] += 1
            continue

        changed = agent.template_revision_applied != template.role_revision
        agent.template_revision_applied = template.role_revision
        agent.template_sync_status = "current"
        agent.template_sync_details = {}
        agent.template_synced_at = now
        counts["advanced" if changed else "current"] += 1

    return counts
