from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.services.template_capabilities import TemplateToolReconcileReport
from app.services.template_revision_sync import finalize_template_revision_sync


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _Result(self.rows)


def _report(*, missing_tools=(), missing_mcp=()):
    return TemplateToolReconcileReport(
        agents_reviewed=1,
        granted=0,
        removed=0,
        missing_tool_names=tuple(missing_tools),
        migrated_to_template=0,
        disabled_ambient=0,
        preserved_opt_out=0,
        preserved_ambiguous=0,
        missing_mcp_servers=tuple(missing_mcp),
    )


def _row(*, revision=2, tools=None, mcp=None):
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="User-renamed specialist",
        role_description="User-authored remit",
        autonomy_policy={"send_external_message": "L3"},
        template_revision_applied=1,
        template_sync_status="pending",
        template_sync_details={"stage": "seed"},
        template_synced_at=None,
    )
    template = SimpleNamespace(
        role_revision=revision,
        default_tools=tools or [],
        default_mcp_servers=mcp or [],
    )
    return agent, template


@pytest.mark.asyncio
async def test_revision_advance_preserves_user_owned_agent_fields() -> None:
    agent, template = _row()
    user_owned = deepcopy(
        (agent.name, agent.role_description, agent.autonomy_policy)
    )

    result = await finalize_template_revision_sync(
        _Db([(agent, template)]),
        tool_report=_report(),
        skill_sync_state={"conflicts": 0},
    )

    assert result == {
        "reviewed": 1,
        "advanced": 1,
        "current": 0,
        "pending": 0,
        "conflict": 0,
    }
    assert agent.template_revision_applied == 2
    assert agent.template_sync_status == "current"
    assert agent.template_sync_details == {}
    assert agent.template_synced_at is not None
    assert (agent.name, agent.role_description, agent.autonomy_policy) == user_owned


@pytest.mark.asyncio
async def test_missing_required_mcp_keeps_revision_pending() -> None:
    agent, template = _row(mcp=["vendor/research"])

    result = await finalize_template_revision_sync(
        _Db([(agent, template)]),
        tool_report=_report(missing_mcp=["vendor/research"]),
        skill_sync_state={"conflicts": 0},
    )

    assert result["pending"] == 1
    assert agent.template_revision_applied == 1
    assert agent.template_sync_status == "pending"
    assert agent.template_sync_details == {
        "missing_mcp_servers": ["vendor/research"]
    }


@pytest.mark.asyncio
async def test_managed_skill_conflict_blocks_revision_without_overwrite() -> None:
    agent, template = _row()

    result = await finalize_template_revision_sync(
        _Db([(agent, template)]),
        tool_report=_report(),
        skill_sync_state={"conflicts": 2},
    )

    assert result["conflict"] == 1
    assert agent.template_revision_applied == 1
    assert agent.template_sync_status == "conflict"
    assert agent.template_sync_details == {"managed_skill_conflicts": 2}


@pytest.mark.asyncio
async def test_scoped_skill_conflict_does_not_block_unrelated_agent() -> None:
    blocked_agent, blocked_template = _row()
    ready_agent, ready_template = _row()

    result = await finalize_template_revision_sync(
        _Db(
            [
                (blocked_agent, blocked_template),
                (ready_agent, ready_template),
            ]
        ),
        tool_report=_report(),
        skill_sync_state={
            "conflicts": 1,
            "conflict_agent_ids": [str(blocked_agent.id)],
        },
    )

    assert result["conflict"] == 1
    assert result["advanced"] == 1
    assert blocked_agent.template_revision_applied == 1
    assert ready_agent.template_revision_applied == 2


@pytest.mark.asyncio
async def test_scoped_skill_conflict_reports_only_that_agents_folders() -> None:
    agent, template = _row()

    result = await finalize_template_revision_sync(
        _Db([(agent, template)]),
        tool_report=_report(),
        skill_sync_state={
            "conflicts": 46,
            "conflict_agent_ids": [str(agent.id)],
            "conflict_skill_folders_by_agent": {
                str(agent.id): ["vercel-full-stack-deploy", "mcp-installer"]
            },
        },
    )

    assert result["conflict"] == 1
    assert agent.template_sync_details == {
        "managed_skill_conflicts": 2,
        "managed_skill_conflict_folders": [
            "mcp-installer",
            "vercel-full-stack-deploy",
        ],
    }
