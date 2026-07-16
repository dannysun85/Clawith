import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import agents
from app.services.agent_manager import AgentManager


def test_format_agent_setup_error_redacts_credentials():
    error = RuntimeError(
        "postgresql://alice:db-password@example.test/app "
        "token=token-value api_key: key-value password = password-value"
    )

    message = agents._format_agent_setup_error("container_start", error)

    assert message.startswith("container_start: RuntimeError:")
    assert "db-password" not in message
    assert "token-value" not in message
    assert "key-value" not in message
    assert "password-value" not in message
    assert message.count("***") >= 4


def test_agent_manager_reconciles_stale_error_with_running_container(monkeypatch):
    manager = AgentManager()
    agent = SimpleNamespace(
        status="error",
        container_id="container-1",
        last_error="old failure",
        last_error_at=object(),
        last_active_at=None,
    )
    monkeypatch.setattr(
        manager,
        "get_container_status",
        lambda _agent: {"running": True, "status": "running"},
    )

    assert manager.reconcile_error_status(agent) is True
    assert agent.status == "running"
    assert agent.last_error is None
    assert agent.last_error_at is None
    assert agent.last_active_at is not None


@pytest.mark.asyncio
async def test_recover_agent_persists_redacted_failure(monkeypatch):
    agent = SimpleNamespace(
        container_id=None,
        status="error",
        last_error=None,
        last_error_at=None,
        deletion_requested_at=None,
    )

    async def allow_manage(*_args, **_kwargs):
        return agent, "manage"

    monkeypatch.setattr(agents, "check_agent_access", allow_manage)
    monkeypatch.setattr(
        agents.agent_manager,
        "initialize_agent_files",
        AsyncMock(side_effect=RuntimeError("api_key=super-secret")),
    )
    db = SimpleNamespace(commit=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await agents.recover_agent(
            uuid.uuid4(),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=db,
        )

    assert raised.value.status_code == 503
    assert "super-secret" not in str(raised.value.detail)
    assert "api_key=***" in str(raised.value.detail)
    assert agent.status == "error"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_agent_requires_manage_access(monkeypatch):
    async def allow_use(*_args, **_kwargs):
        return SimpleNamespace(), "use"

    initialize = AsyncMock()
    monkeypatch.setattr(agents, "check_agent_access", allow_use)
    monkeypatch.setattr(agents.agent_manager, "initialize_agent_files", initialize)

    with pytest.raises(HTTPException) as raised:
        await agents.recover_agent(
            uuid.uuid4(),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=SimpleNamespace(),
        )

    assert raised.value.status_code == 403
    initialize.assert_not_awaited()
