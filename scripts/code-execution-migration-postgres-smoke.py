#!/usr/bin/env python3
"""Seed and verify fail-closed Code grants in a disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.tool import AgentTool, Tool
from app.models.user import User


TENANT_ID = uuid.UUID("07500000-0000-4000-8000-000000000002")
USER_ID = uuid.UUID("07500000-0000-4000-8000-000000000060")
AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000061")
SECOND_TENANT_ID = uuid.UUID("07500000-0000-4000-8000-000000000062")
SECOND_USER_ID = uuid.UUID("07500000-0000-4000-8000-000000000063")
SECOND_AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000064")
CODE_TOOL_NAMES = (
    "execute_code",
    "execute_code_e2b",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
)
MISSING_CODE_HELPERS = frozenset({
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
})
MISSING_CODE_TOOL_ROWS = frozenset({"agentbay_command_exec"})
FILE_TRANSFER_TOOL_ID = uuid.UUID("07500000-0000-4000-8000-000000000079")
LEGACY_MCP_SINGLE_ID = uuid.UUID("07500000-0000-4000-8000-000000000070")
LEGACY_MCP_SHARED_ID = uuid.UUID("07500000-0000-4000-8000-000000000071")
LEGACY_MCP_ORPHAN_ID = uuid.UUID("07500000-0000-4000-8000-000000000072")
DUPLICATE_ASSIGNMENT_TOOL_ID = uuid.UUID("07500000-0000-4000-8000-000000000075")
CONCURRENT_ASSIGNMENT_TOOL_ID = uuid.UUID("07500000-0000-4000-8000-000000000076")


async def seed_unsafe_history() -> None:
    """Reproduce the permissive Code state observed before revision 095."""

    async with async_session() as db:
        await db.execute(
            insert(Tenant.__table__).values(
                id=SECOND_TENANT_ID,
                name="Code Migration Smoke Two",
                slug="migration-smoke-two",
                im_provider="web_only",
                is_active=True,
            )
        )
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
        await db.execute(
            insert(User.__table__).values(
                id=SECOND_USER_ID,
                tenant_id=SECOND_TENANT_ID,
                display_name="Code Migration Smoke Two",
                role="member",
                is_active=True,
                registration_source="migration-smoke",
            )
        )
        await db.execute(
            insert(Agent.__table__).values(
                id=SECOND_AGENT_ID,
                name="Code Migration Smoke Agent Two",
                creator_id=SECOND_USER_ID,
                tenant_id=SECOND_TENANT_ID,
                status="idle",
            )
        )
        # This non-Code anchor was historically enough for the startup seeder
        # to recreate missing Code helper assignments as enabled. Keep it on
        # while deliberately omitting those helper rows below.
        await db.execute(
            pg_insert(Tool.__table__).values(
                id=FILE_TRANSFER_TOOL_ID,
                name="agentbay_file_transfer",
                display_name="agentbay_file_transfer",
                description="migration smoke non-Code anchor",
                type="builtin",
                category="agentbay",
                icon="F",
                parameters_schema={},
                config={},
                config_schema={},
                enabled=True,
                is_default=False,
                source="builtin",
            )
        )
        await db.execute(
            insert(AgentTool.__table__).values(
                agent_id=AGENT_ID,
                tool_id=FILE_TRANSFER_TOOL_ID,
                enabled=True,
                config={},
            )
        )
        await db.execute(
            insert(Tool.__table__).values(
                id=DUPLICATE_ASSIGNMENT_TOOL_ID,
                name="migration_duplicate_assignment",
                display_name="Migration duplicate assignment",
                description="historical SELECT-then-INSERT race fixture",
                type="builtin",
                category="test",
                icon="D",
                parameters_schema={},
                config={},
                config_schema={},
                enabled=True,
                is_default=False,
                source="builtin",
            )
        )
        for enabled, marker in ((True, "first-secret"), (False, "second-secret")):
            await db.execute(
                insert(AgentTool.__table__).values(
                    agent_id=AGENT_ID,
                    tool_id=DUPLICATE_ASSIGNMENT_TOOL_ID,
                    enabled=enabled,
                    config={"api_key": marker},
                    source="user_installed",
                    installed_by_agent_id=AGENT_ID,
                )
            )
        for index, name in enumerate(CODE_TOOL_NAMES, start=80):
            if name in MISSING_CODE_TOOL_ROWS:
                continue
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
            if name not in MISSING_CODE_HELPERS:
                await db.execute(
                    insert(AgentTool.__table__).values(
                        agent_id=AGENT_ID,
                        tool_id=tool_id,
                        enabled=True,
                        config={
                            "api_key": "legacy-code-secret",
                            "cpu_limit": 99,
                            "memory_limit": "99g",
                            "default_timeout": 999,
                            "max_timeout": 999,
                            "language_mapping": {"python": "unsafe"},
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
                            "api_key": "legacy-code-secret",
                            "cpu_limit": 99,
                            "memory_limit": "99g",
                            "default_timeout": 999,
                            "max_timeout": 999,
                            "language_mapping": {"python": "unsafe"},
                            "allow_network": True,
                            "allow_unsafe_fallback_when_bwrap_missing": True,
                        }
                    },
                )
            )
        for tool_id, name in (
            (LEGACY_MCP_SINGLE_ID, "legacy_mcp_single"),
            (LEGACY_MCP_SHARED_ID, "legacy_mcp_shared"),
            (LEGACY_MCP_ORPHAN_ID, "legacy_mcp_orphan"),
        ):
            await db.execute(
                insert(Tool.__table__).values(
                    id=tool_id,
                    name=name,
                    display_name=name,
                    description="legacy tenantless MCP",
                    type="mcp",
                    category="mcp",
                    icon="M",
                    parameters_schema={},
                    config={"api_key": "legacy-mcp-secret"},
                    config_schema={},
                    mcp_server_url="https://mcp.example.test/api",
                    mcp_server_name="Legacy",
                    mcp_tool_name=name,
                    enabled=True,
                    is_default=False,
                    source="agent",
                    tenant_id=None,
                )
            )
        await db.execute(
            insert(AgentTool.__table__).values(
                agent_id=AGENT_ID,
                tool_id=LEGACY_MCP_SINGLE_ID,
                enabled=True,
                config={"api_key": "single-secret"},
            )
        )
        for agent_id in (AGENT_ID, SECOND_AGENT_ID):
            await db.execute(
                insert(AgentTool.__table__).values(
                    agent_id=agent_id,
                    tool_id=LEGACY_MCP_SHARED_ID,
                    enabled=True,
                    config={"api_key": "shared-secret"},
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
                    Tool.__table__.c.name,
                    AgentTool.__table__.c.enabled,
                    AgentTool.__table__.c.config,
                )
                .join(
                    Tool.__table__,
                    Tool.__table__.c.id == AgentTool.__table__.c.tool_id,
                )
                .where(
                    AgentTool.__table__.c.agent_id == AGENT_ID,
                    Tool.__table__.c.name.in_(CODE_TOOL_NAMES),
                )
            )
        ).all()
        # Missing rows must be materialized disabled so rolling application
        # code back to the historical "create if missing" seeder cannot
        # resurrect helper grants.
        expected_assignment_names = set(CODE_TOOL_NAMES)
        if {assignment.name for assignment in assignments} != expected_assignment_names:
            raise SystemExit("Code migration did not materialize every disabled grant")
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
        controlled_override_keys = {
            "sandbox_type",
            "api_url",
            "api_key",
            "cpu_limit",
            "memory_limit",
            "default_timeout",
            "max_timeout",
            "language_mapping",
        }
        if any(
            controlled_override_keys.intersection(assignment.config or {})
            for assignment in assignments
        ):
            raise SystemExit("Code migration retained a platform-controlled Agent override")

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
        if any(
            controlled_override_keys.intersection(
                (setting.value or {}).get("config", {})
            )
            for setting in tenant_settings
        ):
            raise SystemExit("Code migration retained a platform-controlled company override")

        legacy_tools = (
            await db.execute(
                select(
                    Tool.__table__.c.id,
                    Tool.__table__.c.tenant_id,
                    Tool.__table__.c.enabled,
                    Tool.__table__.c.config,
                    Tool.__table__.c.mcp_server_url,
                ).where(
                    Tool.__table__.c.id.in_(
                        {
                            LEGACY_MCP_SINGLE_ID,
                            LEGACY_MCP_SHARED_ID,
                            LEGACY_MCP_ORPHAN_ID,
                        }
                    )
                )
            )
        ).all()
        legacy_by_id = {row.id: row for row in legacy_tools}
        single = legacy_by_id[LEGACY_MCP_SINGLE_ID]
        if single.tenant_id != TENANT_ID or single.enabled is not True:
            raise SystemExit("MCP migration did not preserve the single-tenant legacy tool")
        if not single.mcp_server_url or not single.config:
            raise SystemExit("MCP migration erased the safe single-tenant legacy tool")
        for tool_id in (LEGACY_MCP_SHARED_ID, LEGACY_MCP_ORPHAN_ID):
            quarantined = legacy_by_id[tool_id]
            if (
                quarantined.tenant_id is not None
                or quarantined.enabled is not False
                or quarantined.config
                or quarantined.mcp_server_url is not None
            ):
                raise SystemExit("MCP migration did not quarantine an ambiguous legacy tool")

        shared_assignments = (
            await db.execute(
                select(
                    AgentTool.__table__.c.enabled,
                    AgentTool.__table__.c.config,
                ).where(AgentTool.__table__.c.tool_id == LEGACY_MCP_SHARED_ID)
            )
        ).all()
        if not shared_assignments or any(
            assignment.enabled or assignment.config
            for assignment in shared_assignments
        ):
            raise SystemExit("MCP migration retained a shared cross-tenant grant")

        file_transfer_state = (
            await db.execute(
                select(
                    Tool.__table__.c.enabled.label("tool_enabled"),
                    AgentTool.__table__.c.enabled.label("assignment_enabled"),
                )
                .join(
                    AgentTool.__table__,
                    AgentTool.__table__.c.tool_id == Tool.__table__.c.id,
                )
                .where(
                    AgentTool.__table__.c.agent_id == AGENT_ID,
                    Tool.__table__.c.id == FILE_TRANSFER_TOOL_ID,
                )
            )
        ).one()
        if (
            file_transfer_state.tool_enabled is not False
            or file_transfer_state.assignment_enabled is not False
        ):
            raise SystemExit(
                "AgentBay file-transfer release policy did not converge"
            )

        duplicate_rows = (
            await db.execute(
                select(
                    AgentTool.__table__.c.enabled,
                    AgentTool.__table__.c.config,
                    AgentTool.__table__.c.source,
                    AgentTool.__table__.c.installed_by_agent_id,
                ).where(
                    AgentTool.__table__.c.agent_id == AGENT_ID,
                    AgentTool.__table__.c.tool_id == DUPLICATE_ASSIGNMENT_TOOL_ID,
                )
            )
        ).all()
        if len(duplicate_rows) != 1:
            raise SystemExit("AgentTool migration did not collapse duplicate grants")
        duplicate = duplicate_rows[0]
        if (
            duplicate.enabled
            or duplicate.config
            or duplicate.source != "system"
            or duplicate.installed_by_agent_id is not None
        ):
            raise SystemExit("AgentTool duplicate quarantine was not fail-closed")


async def assert_concurrent_agent_tool_upsert() -> None:
    """Prove the production lock/upsert path converges under concurrent writes."""

    from app.services.agent_tool_assignments import (
        lock_agent_tool_owner,
        upsert_agent_tool,
    )

    async with async_session() as db:
        await db.execute(
            pg_insert(Tool.__table__).values(
                id=CONCURRENT_ASSIGNMENT_TOOL_ID,
                name="migration_concurrent_assignment",
                display_name="Migration concurrent assignment",
                description="real PostgreSQL upsert race fixture",
                type="builtin",
                category="test",
                icon="C",
                parameters_schema={},
                config={},
                config_schema={},
                enabled=True,
                is_default=False,
                source="builtin",
            ).on_conflict_do_nothing(index_elements=[Tool.__table__.c.id])
        )
        await db.execute(
            AgentTool.__table__.delete().where(
                AgentTool.__table__.c.agent_id == AGENT_ID,
                AgentTool.__table__.c.tool_id == CONCURRENT_ASSIGNMENT_TOOL_ID,
            )
        )
        await db.commit()

    async def writer(enabled: bool, marker: str) -> None:
        async with async_session() as db:
            async with db.begin():
                await lock_agent_tool_owner(db, AGENT_ID)
                await upsert_agent_tool(
                    db,
                    agent_id=AGENT_ID,
                    tool_id=CONCURRENT_ASSIGNMENT_TOOL_ID,
                    enabled=enabled,
                    config={"writer": marker},
                    source="system",
                    on_conflict="enabled",
                )

    await asyncio.gather(
        writer(True, "first"),
        writer(False, "second"),
    )

    async with async_session() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(AgentTool.__table__)
                .where(
                    AgentTool.__table__.c.agent_id == AGENT_ID,
                    AgentTool.__table__.c.tool_id == CONCURRENT_ASSIGNMENT_TOOL_ID,
                )
            )
        ).scalar_one()
        row = (
            await db.execute(
                select(
                    AgentTool.__table__.c.enabled,
                    AgentTool.__table__.c.config,
                ).where(
                    AgentTool.__table__.c.agent_id == AGENT_ID,
                    AgentTool.__table__.c.tool_id == CONCURRENT_ASSIGNMENT_TOOL_ID,
                )
            )
        ).one()
        if count != 1:
            raise SystemExit("Concurrent AgentTool writes created duplicate grants")
        if row.config not in ({"writer": "first"}, {"writer": "second"}):
            raise SystemExit("AgentTool conflict update overwrote unrelated config")


async def run_seeder_and_assert() -> None:
    """Exercise the real startup seeder before checking grant non-resurrection."""

    from app.services.tool_seeder import seed_builtin_tools

    await seed_builtin_tools()
    await assert_secured()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "seed",
            "assert-secured",
            "run-seeder-and-assert",
            "assert-agent-tool-upsert",
        ),
    )
    args = parser.parse_args()
    if args.mode == "seed":
        coroutine = seed_unsafe_history()
    elif args.mode == "assert-agent-tool-upsert":
        coroutine = assert_concurrent_agent_tool_upsert()
    elif args.mode == "run-seeder-and-assert":
        coroutine = run_seeder_and_assert()
    else:
        coroutine = assert_secured()
    asyncio.run(coroutine)


if __name__ == "__main__":
    main()
