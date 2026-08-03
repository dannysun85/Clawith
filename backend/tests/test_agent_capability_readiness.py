from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_capability_readiness as readiness
from app.services.agent_candidate_templates import load_candidate_template_seeds
from app.services.template_seeder import _load_folder_templates


class _Result:
    def __init__(self, names):
        self._names = list(names)

    def scalars(self):
        return self

    def all(self):
        return self._names


class _Db:
    def __init__(self, names=()):
        self.names = names
        self.executions = 0

    async def execute(self, _statement):
        self.executions += 1
        return _Result(self.names)


def test_every_enabled_folder_template_has_registered_typed_capability_contract() -> None:
    contracts = [readiness.template_capability_contract(template) for template in _load_folder_templates()]

    assert len(contracts) == 30
    assert all(contract["contract_ready"] for contract in contracts)
    assert all(
        tool["registered"] and tool["typed_adapter"] and tool["readiness"]
        for contract in contracts
        for tool in contract["tools"]
    )


def test_disabled_candidates_cannot_become_activation_ready_from_prompt_metadata() -> None:
    contracts = [readiness.template_capability_contract(template) for template in load_candidate_template_seeds()]

    assert len(contracts) == 92
    assert all(contract["contract_ready"] for contract in contracts)
    assert all(contract["activation_ready"] is False for contract in contracts)


@pytest.mark.asyncio
async def test_runtime_readiness_reports_missing_tool_without_provider_call(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    template = SimpleNamespace(
        role_key="code-reviewer",
        role_revision=2,
        lifecycle_status="enabled",
        default_skills=[],
        default_tools=["execute_code"],
        default_mcp_servers=[],
    )

    async def _runtime_tools(_agent_id):
        return []

    monkeypatch.setattr(readiness, "get_runtime_agent_tools_for_llm", _runtime_tools)
    result = await readiness.agent_runtime_capability_readiness(_Db(), agent_id=agent_id, template=template)

    assert result["runtime_status"] == "degraded"
    assert result["blockers"] == ["tool:execute_code"]
    assert result["tools"][0]["runtime_status"] == "unavailable"


@pytest.mark.asyncio
async def test_runtime_mcp_readiness_requires_assignment_and_typed_visibility(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    tool_name = "mcp_shibui_finance_quote"
    template = SimpleNamespace(
        role_key="technical-analyst",
        role_revision=1,
        lifecycle_status="enabled",
        default_skills=[],
        default_tools=[],
        default_mcp_servers=["shibui/finance"],
    )

    async def _runtime_tools(_agent_id):
        return [{"type": "function", "function": {"name": tool_name, "parameters": {}}}]

    monkeypatch.setattr(readiness, "get_runtime_agent_tools_for_llm", _runtime_tools)
    result = await readiness.agent_runtime_capability_readiness(_Db([tool_name]), agent_id=agent_id, template=template)

    assert result["runtime_status"] == "available"
    assert result["mcp_servers"] == [
        {
            "server_id": "shibui/finance",
            "status": "available",
            "assigned_tool_count": 1,
            "runtime_tool_count": 1,
        }
    ]
