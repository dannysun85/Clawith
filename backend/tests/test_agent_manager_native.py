"""Agent lifecycle regressions for native versus OpenClaw runtimes."""

from types import SimpleNamespace

import pytest

from app.services.agent_manager import agent_manager


@pytest.mark.asyncio
async def test_native_agent_start_never_touches_docker(monkeypatch):
    class DockerTrap:
        @property
        def containers(self):
            raise AssertionError("native agents must not touch Docker")

    monkeypatch.setattr(agent_manager, "docker_client", DockerTrap())
    agent = SimpleNamespace(
        agent_type="native",
        status="creating",
        last_active_at=None,
        last_error="legacy container error",
        last_error_at=object(),
    )

    container_id = await agent_manager.start_container(db=None, agent=agent)

    assert container_id is None
    assert agent.status == "idle"
    assert agent.last_active_at is not None
    assert agent.last_error is None
    assert agent.last_error_at is None
