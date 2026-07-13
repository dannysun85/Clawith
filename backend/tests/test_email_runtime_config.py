import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_tools


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, responses):
        self.responses = list(responses)

    async def execute(self, _statement):
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_email_runtime_uses_tenant_company_config_then_agent_override(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    tool = SimpleNamespace(
        id=uuid.uuid4(),
        source="builtin",
        config={"auth_code": "legacy-global-secret"},
        config_schema={
            "fields": [{"key": "auth_code", "type": "password"}],
        },
    )
    assignment = SimpleNamespace(config={"email_address": "agent@example.com"})
    db = _DB([
        _Result(tool),
        _Result(tenant_id),
        _Result(assignment),
    ])

    @asynccontextmanager
    async def session():
        yield db

    company_config = {
        "email_provider": "qq_enterprise",
        "email_address": "company@example.com",
        "auth_code": "tenant-secret",
    }
    get_company_config = AsyncMock(return_value=company_config)
    monkeypatch.setattr(agent_tools, "async_session", session)
    monkeypatch.setattr(
        "app.services.tool_config.get_tool_company_config",
        get_company_config,
    )

    result = await agent_tools._get_email_config(agent_id)

    assert result == {
        "email_provider": "qq_enterprise",
        "email_address": "agent@example.com",
        "auth_code": "tenant-secret",
    }
    get_company_config.assert_awaited_once_with(db, tool, tenant_id)
    assert result["auth_code"] != tool.config["auth_code"]
