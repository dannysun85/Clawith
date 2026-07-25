"""Apply executable capabilities declared by an Agent role template."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentTemplate
from app.models.tool import AgentTool, Tool
from app.services.agent_tool_assignments import upsert_agent_tool
from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


@dataclass(frozen=True)
class TemplateToolReconcileReport:
    """Auditable result of synchronizing role-owned Tool assignments."""

    agents_reviewed: int
    granted: int
    removed: int
    missing_tool_names: tuple[str, ...]
    migrated_to_template: int
    disabled_ambient: int
    preserved_opt_out: int
    preserved_ambiguous: int

    @property
    def changed(self) -> bool:
        return any(
            (
                self.granted,
                self.removed,
                self.migrated_to_template,
                self.disabled_ambient,
                self.preserved_opt_out,
                self.preserved_ambiguous,
            )
        )

    def as_log_dict(self) -> dict[str, object]:
        return asdict(self)


async def grant_template_tools(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_names: list[str],
) -> tuple[int, tuple[str, ...]]:
    """Add idempotent template grants without overwriting user choices.

    Existing grants are authoritative. Older releases used ``source=system``
    for both automatic placeholders and real user choices, so overwriting that
    provenance would risk re-enabling a Tool the user deliberately disabled.
    Only this template's own prior ``source=template`` grant may be refreshed.
    """
    requested = {name.strip() for name in tool_names if isinstance(name, str) and name.strip()}
    if not requested:
        return 0, ()

    result = await db.execute(
        select(Tool).where(
            Tool.name.in_(requested),
            Tool.source == "builtin",
        )
    )
    tools = list(result.scalars().all())
    found = {tool.name for tool in tools}
    for tool in tools:
        await upsert_agent_tool(
            db,
            agent_id=agent_id,
            tool_id=tool.id,
            enabled=True,
            source="template",
            on_conflict="template",
        )
    return len(tools), tuple(sorted(requested - found))


async def reconcile_template_tool_grants(
    db: AsyncSession,
) -> TemplateToolReconcileReport:
    """Reconcile reviewed role grants without touching user-owned choices.

    ``source=template`` is the only provenance this synchronizer owns.  When a
    reviewed template drops a capability, its stale grant must disappear or a
    role change would leave executable authority behind.  Rows rewritten by a
    user selection have ``source=user_selected`` and are therefore preserved.
    """
    result = await db.execute(
        select(Agent.id, AgentTemplate.default_tools)
        .join(AgentTemplate, Agent.template_id == AgentTemplate.id)
        .where(
            Agent.deletion_requested_at.is_(None),
            AgentTemplate.is_builtin.is_(True),
        )
    )
    desired_by_agent: dict[uuid.UUID, set[str]] = {}
    for agent_id, tool_names in result.all():
        desired_by_agent[agent_id] = {
            name.strip()
            for name in (tool_names or [])
            if isinstance(name, str) and name.strip()
        }
    # Older releases persisted every ambient default as ``source=system``.
    # Once a capability becomes role-specific, changing Tool.is_default does
    # not revoke those already-enabled AgentTool rows.  Migrate only that
    # historical automatic provenance:
    #
    # * enabled + required by the reviewed role -> template-owned grant;
    # * enabled + not required by that reviewed role -> disable but keep config;
    # * disabled + required by the role -> preserve the user's prior opt-out;
    # * no reviewed builtin role -> preserve the ambiguous legacy choice.
    #
    # New UI selections use ``source=user_selected`` and are never touched.
    legacy_result = await db.execute(
        select(AgentTool, Tool.name)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.source == "system",
            Tool.name.in_(EXPLICIT_GRANT_TOOL_NAMES),
        )
    )
    migrated_to_template = 0
    disabled_ambient = 0
    preserved_opt_out = 0
    preserved_ambiguous = 0
    for assignment, tool_name in legacy_result.all():
        reviewed_role = assignment.agent_id in desired_by_agent
        if not reviewed_role:
            assignment.source = "legacy_ambiguous"
            preserved_ambiguous += 1
            continue
        desired = tool_name in desired_by_agent[assignment.agent_id]
        if desired and assignment.enabled:
            assignment.source = "template"
            migrated_to_template += 1
        elif desired:
            assignment.source = "user_selected"
            preserved_opt_out += 1
        elif assignment.enabled:
            assignment.enabled = False
            disabled_ambient += 1

    granted = 0
    missing: set[str] = set()
    for agent_id, tool_names in desired_by_agent.items():
        count, unresolved = await grant_template_tools(
            db,
            agent_id=agent_id,
            tool_names=sorted(tool_names),
        )
        granted += count
        missing.update(unresolved)

    stale_result = await db.execute(
        select(AgentTool, Tool.name)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(AgentTool.source == "template")
    )
    removed = 0
    for assignment, tool_name in stale_result.all():
        if tool_name in desired_by_agent.get(assignment.agent_id, set()):
            continue
        await db.delete(assignment)
        removed += 1
    missing_tool_names = tuple(sorted(missing))
    if missing_tool_names:
        logger.warning(
            "[TemplateCapabilities] Ignored unknown builtin tools names={}",
            missing_tool_names,
        )
    if (
        migrated_to_template
        or disabled_ambient
        or preserved_opt_out
        or preserved_ambiguous
    ):
        logger.info(
            "[TemplateCapabilities] Reconciled legacy system grants "
            "template_count={} disabled_ambient_count={} "
            "preserved_opt_out_count={} preserved_ambiguous_count={}",
            migrated_to_template,
            disabled_ambient,
            preserved_opt_out,
            preserved_ambiguous,
        )
    return TemplateToolReconcileReport(
        agents_reviewed=len(desired_by_agent),
        granted=granted,
        removed=removed,
        missing_tool_names=missing_tool_names,
        migrated_to_template=migrated_to_template,
        disabled_ambient=disabled_ambient,
        preserved_opt_out=preserved_opt_out,
        preserved_ambiguous=preserved_ambiguous,
    )
