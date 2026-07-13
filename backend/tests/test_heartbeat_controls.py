from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.models.agent import Agent
from app.schemas.schemas import AgentOut


def test_heartbeat_is_globally_disabled_by_default():
    settings = Settings(_env_file=None)

    assert settings.HEARTBEAT_ENABLED is False


def test_heartbeat_can_be_explicitly_enabled_by_operator():
    settings = Settings(_env_file=None, HEARTBEAT_ENABLED=True)

    assert settings.HEARTBEAT_ENABLED is True


def test_new_agents_and_agent_responses_default_heartbeat_off():
    column_default = Agent.__table__.c.heartbeat_enabled.default

    assert column_default is not None
    assert column_default.arg is False
    assert AgentOut.model_fields["heartbeat_enabled"].default is False


@pytest.mark.asyncio
async def test_heartbeat_tick_does_not_open_database_when_globally_disabled(monkeypatch):
    from app.services import heartbeat

    monkeypatch.setattr(heartbeat.settings, "HEARTBEAT_ENABLED", False)
    with patch("app.database.async_session") as session_factory:
        await heartbeat._heartbeat_tick()

    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_trigger_tick_still_runs_when_heartbeat_is_disabled(monkeypatch):
    from app.services import trigger_daemon

    class StopLoop(Exception):
        pass

    trigger_tick = AsyncMock()

    async def stop_after_first_tick(_seconds):
        raise StopLoop

    monkeypatch.setattr(trigger_daemon.settings, "HEARTBEAT_ENABLED", False)
    monkeypatch.setattr(trigger_daemon, "_tick", trigger_tick)
    monkeypatch.setattr(trigger_daemon.asyncio, "sleep", stop_after_first_tick)

    with pytest.raises(StopLoop):
        await trigger_daemon.start_trigger_daemon()

    trigger_tick.assert_awaited_once()


def test_production_compose_exposes_disabled_heartbeat_kill_switch():
    repository_root = Path(__file__).parents[2]
    compose = (repository_root / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "HEARTBEAT_ENABLED: ${HEARTBEAT_ENABLED:-false}" in compose


def test_production_deploy_pins_autonomous_runners_off():
    repository_root = Path(__file__).parents[2]
    deploy_script = (repository_root / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert '"HEARTBEAT_ENABLED": "false"' in deploy_script
    assert '"COMPANY_ASSIGNMENT_RUNNER_ENABLED": "false"' in deploy_script
    assert "--exclude .omx" in deploy_script
    assert "--exclude '*/__pycache__'" in deploy_script
    assert "--exclude '*.pyc'" in deploy_script
    assert "--exclude frontend/dist" in deploy_script
