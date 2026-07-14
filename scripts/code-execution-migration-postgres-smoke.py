#!/usr/bin/env python3
"""Seed and verify fail-closed Code grants in a disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import insert, select

from app.database import async_session
from app.models.agent import Agent
from app.models.tenant_setting import TenantSetting
from app.models.tool import AgentTool, Tool
from app.models.user import User


TENANT_ID = uuid.UUID("07500000-0000-4000-8000-000000000002")
USER_ID = uuid.UUID("07500000-0000-4000-8000-000000000060")
AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000061")
CODE_TOOL_NAMES = (
    "execute_code",
    "execute_code_e2b",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
)


async def seed_unsafe_history() -> None:
    """Reproduce the permissive Code state observed before revision 095."""

    async with async_session() as db:
        await db.execute(
            insert(User.__table__).values(
                id=USER_ID,
                tenant_id=TENANT_ID,
                display_name="Code Migration Smoke",
                role="member",
                is_active=True,
                registration_source="migration-smoke",
            )
        )
        await db.execute(
            insert(Agent.__table__).values(
                id=AGENT_ID,
                name="Code Migration Smoke Agent",
                creator_id=USER_ID,
                tenant_id=TENANT_ID,
                status="idle",
            )
        )
        for index, name in enumerate(CODE_TOOL_NAMES, start=80):
            config: dict = {}
            if name == "execute_code":
                config = {
                    "sandbox_type": "subprocess",
                    "allow_network": True,
                    "allow_unsafe_fallback_when_bwrap_missing": True,
                }
            elif name == "execute_code_e2b":
                config = {
                    "sandbox_type": "e2b",
                    "allow_network": True,
                    "allow_unsafe_fallback_when_bwrap_missing": True,
                }
            tool_id = uuid.UUID(f"07500000-0000-4000-8000-{index:012d}")
            await db.execute(
                insert(Tool.__table__).values(
                    id=tool_id,
                    name=name,
                    display_name=name,
                    description="migration smoke",
                    type="builtin",
                    category="code",
                    icon="C",
                    parameters_schema={},
                    config=config,
                    config_schema={},
                    enabled=True,
                    is_default=True,
                    source="builtin",
                )
            )
            await db.execute(
                insert(AgentTool.__table__).values(
                    agent_id=AGENT_ID,
                    tool_id=tool_id,
                    enabled=True,
                    config={
                        "allow_network": True,
                        "allow_unsafe_fallback_when_bwrap_missing": True,
                    },
                )
            )
        for tool_name in ("execute_code", "execute_code_e2b"):
            await db.execute(
                insert(TenantSetting.__table__).values(
                    tenant_id=TENANT_ID,
                    key=f"tool_config:{tool_name}",
                    value={
                        "config": {
                            "allow_network": True,
                            "allow_unsafe_fallback_when_bwrap_missing": True,
                        }
                    },
                )
            )
        await db.commit()


async def assert_secured() -> None:
    """Verify upgrades and downgrades never restore an implicit grant."""

    async with async_session() as db:
        tools = (
            await db.execute(
                select(
                    Tool.__table__.c.name,
                    Tool.__table__.c.is_default,
                    Tool.__table__.c.config,
                ).where(Tool.__table__.c.name.in_(CODE_TOOL_NAMES))
            )
        ).all()
        if len(tools) != len(CODE_TOOL_NAMES):
            raise SystemExit("Code migration smoke fixtures are incomplete")
        if any(tool.is_default for tool in tools):
            raise SystemExit("Code migration left an implicit default tool enabled")
        for tool in tools:
            if tool.name in {"execute_code", "execute_code_e2b"}:
                if (tool.config or {}).get("allow_network") is not False:
                    raise SystemExit("Code migration left network access enabled")
                if (
                    (tool.config or {}).get(
                        "allow_unsafe_fallback_when_bwrap_missing"
                    )
                    is not False
                ):
                    raise SystemExit("Code migration left unsafe fallback enabled")

        assignments = (
            await db.execute(
                select(
                    AgentTool.__table__.c.enabled,
                    AgentTool.__table__.c.config,
                ).where(
                    AgentTool.__table__.c.agent_id == AGENT_ID
                )
            )
        ).all()
        if len(assignments) != len(CODE_TOOL_NAMES):
            raise SystemExit("Code migration assignment fixtures are incomplete")
        if any(assignment.enabled for assignment in assignments):
            raise SystemExit("Code migration left a historical Agent grant enabled")
        if any(
            (assignment.config or {}).get("allow_network") is not False
            or (assignment.config or {}).get(
                "allow_unsafe_fallback_when_bwrap_missing"
            )
            is not False
            for assignment in assignments
        ):
            raise SystemExit("Code migration left an unsafe Agent override enabled")

        tenant_settings = (
            await db.execute(
                select(TenantSetting.__table__.c.value).where(
                    TenantSetting.__table__.c.tenant_id == TENANT_ID,
                    TenantSetting.__table__.c.key.in_(
                        (
                            "tool_config:execute_code",
                            "tool_config:execute_code_e2b",
                        )
                    ),
                )
            )
        ).all()
        if len(tenant_settings) != 2:
            raise SystemExit("Code migration tenant fixtures are incomplete")
        if any(
            (setting.value or {}).get("config", {}).get("allow_network")
            is not False
            or (setting.value or {})
            .get("config", {})
            .get("allow_unsafe_fallback_when_bwrap_missing")
            is not False
            for setting in tenant_settings
        ):
            raise SystemExit("Code migration left an unsafe company override enabled")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "assert-secured"))
    args = parser.parse_args()
    asyncio.run(seed_unsafe_history() if args.mode == "seed" else assert_secured())


if __name__ == "__main__":
    main()
