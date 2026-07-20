"""Race-safe writes for the unique Agent-to-Tool grant."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.tool import AgentTool


AgentToolConflictMode = Literal[
    "preserve",
    "enabled",
    "selection",
    "template",
    "config",
    "reauthorize",
]


async def lock_agent_tool_owner(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Serialize config read/merge/write operations for one Agent."""

    result = await db.execute(
        select(Agent.id).where(Agent.id == agent_id).with_for_update()
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Agent not found while writing tool assignment")


async def upsert_agent_tool(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    enabled: bool,
    config: dict[str, Any] | None = None,
    source: str = "system",
    installed_by_agent_id: uuid.UUID | None = None,
    on_conflict: AgentToolConflictMode = "preserve",
) -> None:
    """Insert once and apply only the caller-owned fields on conflict.

    Defaults/backfills preserve every existing user choice. An enable action
    updates only ``enabled``; a config save updates only ``config``; an
    explicit MCP reauthorization owns config, enabled, source, and installer.
    """

    values = {
        "id": uuid.uuid4(),
        "agent_id": agent_id,
        "tool_id": tool_id,
        "enabled": enabled,
        "config": dict(config or {}),
        "source": source,
        "installed_by_agent_id": installed_by_agent_id,
    }
    statement = pg_insert(AgentTool).values(**values)
    if on_conflict == "preserve":
        statement = statement.on_conflict_do_nothing(
            index_elements=[AgentTool.agent_id, AgentTool.tool_id],
        )
    else:
        update_fields: dict[str, Any]
        if on_conflict == "enabled":
            update_fields = {"enabled": statement.excluded.enabled}
        elif on_conflict == "selection":
            update_fields = {
                "enabled": statement.excluded.enabled,
                "source": statement.excluded.source,
            }
        elif on_conflict == "template":
            update_fields = {
                "enabled": statement.excluded.enabled,
                "source": statement.excluded.source,
            }
        elif on_conflict == "config":
            update_fields = {"config": statement.excluded.config}
        elif on_conflict == "reauthorize":
            update_fields = {
                "enabled": statement.excluded.enabled,
                "config": statement.excluded.config,
                "source": statement.excluded.source,
                "installed_by_agent_id": statement.excluded.installed_by_agent_id,
            }
        else:  # pragma: no cover - Literal + defensive runtime boundary
            raise ValueError(f"unsupported AgentTool conflict mode: {on_conflict}")
        statement = statement.on_conflict_do_update(
            index_elements=[AgentTool.agent_id, AgentTool.tool_id],
            set_=update_fields,
            where=(
                AgentTool.source == "template"
                if on_conflict == "template"
                else None
            ),
        )
    await db.execute(statement)
