from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.models.agent import Agent
from app.schemas.schemas import AgentOut


def test_heartbeat_is_globally_disabled_by_default():
    settings = Settings(_env_file=None)

    assert settings.HEARTBEAT_ENABLED is False


def test_heartbeat_can_be_explicitly_enabled_by_operator():
    settings = Settings(_env_file=None, HEARTBEAT_ENABLED=True)

    assert settings.HEARTBEAT_ENABLED is True


def test_trigger_runtime_defaults_keep_explicit_work_bounded_and_okr_off():
    settings = Settings(_env_file=None)

    assert settings.TRIGGER_DAEMON_ENABLED is True
    assert settings.USER_AUTOMATION_EXECUTION_ENABLED is True
    assert settings.USER_SCHEDULE_EXECUTION_ENABLED is False
    assert settings.USER_TASK_EXECUTION_ENABLED is False
    assert settings.APPROVAL_EXECUTION_ENABLED is True
    assert settings.SUPERVISION_EXECUTION_ENABLED is False
    assert settings.OKR_AUTOMATION_ENABLED is False
    assert settings.TRIGGER_MAX_CONCURRENCY == 8
    assert settings.TRIGGER_CLAIM_BATCH_SIZE == 16


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
    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_DAEMON_ENABLED", True)
    monkeypatch.setattr(trigger_daemon, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", True)
    monkeypatch.setattr(trigger_daemon, "_tick", trigger_tick)
    monkeypatch.setattr(trigger_daemon.asyncio, "sleep", stop_after_first_tick)

    with pytest.raises(StopLoop):
        await trigger_daemon.start_trigger_daemon()

    trigger_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_daemon_kill_switch_stays_idle_without_ticking(monkeypatch):
    from app.services import trigger_daemon

    trigger_tick = AsyncMock()
    class StopIdleLoop(Exception):
        pass

    async def stop_idle_loop(_seconds):
        raise StopIdleLoop

    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_DAEMON_ENABLED", False)
    monkeypatch.setattr(trigger_daemon, "_tick", trigger_tick)
    monkeypatch.setattr(trigger_daemon.asyncio, "sleep", stop_idle_loop)

    with pytest.raises(StopIdleLoop):
        await trigger_daemon.start_trigger_daemon()

    trigger_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_policy_pause_stays_idle_without_starting_trigger_loop(monkeypatch):
    from app.services import trigger_daemon

    trigger_tick = AsyncMock()
    class StopIdleLoop(Exception):
        pass

    async def stop_idle_loop(_seconds):
        raise StopIdleLoop

    monkeypatch.setattr(trigger_daemon.settings, "TRIGGER_DAEMON_ENABLED", True)
    monkeypatch.setattr(trigger_daemon, "AUTOMATIC_TRIGGER_EXECUTION_ENABLED", False)
    monkeypatch.setattr(trigger_daemon, "_tick", trigger_tick)
    monkeypatch.setattr(trigger_daemon.asyncio, "sleep", stop_idle_loop)

    with pytest.raises(StopIdleLoop):
        await trigger_daemon.start_trigger_daemon()

    trigger_tick.assert_not_awaited()


def test_production_compose_exposes_disabled_heartbeat_kill_switch():
    repository_root = Path(__file__).parents[2]
    compose = (repository_root / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "HEARTBEAT_ENABLED: ${HEARTBEAT_ENABLED:-false}" in compose
    assert "TRIGGER_DAEMON_ENABLED: ${TRIGGER_DAEMON_ENABLED:-true}" in compose
    assert (
        "USER_AUTOMATION_EXECUTION_ENABLED: "
        "${USER_AUTOMATION_EXECUTION_ENABLED:-true}" in compose
    )
    assert (
        "USER_SCHEDULE_EXECUTION_ENABLED: "
        "${USER_SCHEDULE_EXECUTION_ENABLED:-false}" in compose
    )
    assert (
        "USER_TASK_EXECUTION_ENABLED: "
        "${USER_TASK_EXECUTION_ENABLED:-false}" in compose
    )
    assert "APPROVAL_EXECUTION_ENABLED: ${APPROVAL_EXECUTION_ENABLED:-true}" in compose
    assert "SUPERVISION_EXECUTION_ENABLED: ${SUPERVISION_EXECUTION_ENABLED:-false}" in compose
    assert "OKR_AUTOMATION_ENABLED: ${OKR_AUTOMATION_ENABLED:-false}" in compose
    assert "TRIGGER_MAX_CONCURRENCY: ${TRIGGER_MAX_CONCURRENCY:-8}" in compose
    assert "TRIGGER_CLAIM_BATCH_SIZE: ${TRIGGER_CLAIM_BATCH_SIZE:-16}" in compose


def test_production_deploy_splits_system_and_user_automation_controls():
    repository_root = Path(__file__).parents[2]
    deploy_script = (repository_root / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert '"HEARTBEAT_ENABLED": "false"' in deploy_script
    assert '"TRIGGER_DAEMON_ENABLED": "true"' in deploy_script
    assert '"USER_AUTOMATION_EXECUTION_ENABLED": "true"' in deploy_script
    assert '"USER_SCHEDULE_EXECUTION_ENABLED": "false"' in deploy_script
    assert '"USER_TASK_EXECUTION_ENABLED": "false"' in deploy_script
    assert '"APPROVAL_EXECUTION_ENABLED": "true"' in deploy_script
    assert '"SUPERVISION_EXECUTION_ENABLED": "false"' in deploy_script
    assert '"OKR_AUTOMATION_ENABLED": "false"' in deploy_script
    assert '"PRODUCTION_ISSUE_MONITOR_ENABLED": "true"' in deploy_script
    assert '"TRIGGER_MAX_CONCURRENCY": "8"' in deploy_script
    assert '"TRIGGER_CLAIM_BATCH_SIZE": "16"' in deploy_script
    assert '"COMPANY_ASSIGNMENT_RUNNER_ENABLED": "false"' in deploy_script
    assert 'git archive --format=tar --output="$PACKAGE_TAR" "$COMMIT"' in deploy_script
    assert 'git get-tar-commit-id < "$PACKAGE_TAR"' in deploy_script
    assert 'write_atomic_line "$RELEASE/PACKAGE_SHA256" "$PACKAGE_SHA256"' in deploy_script


def test_agent_capabilities_distinguish_user_automation_from_supervision():
    repository_root = Path(__file__).parents[2]
    source = (repository_root / "backend/app/api/agents.py").read_text(
        encoding="utf-8"
    )

    assert '"task_execution": AUTOMATIC_TASK_EXECUTION_ENABLED' in source
    assert '"schedule_execution": AUTOMATIC_SCHEDULE_EXECUTION_ENABLED' in source
    assert '"trigger_execution": AUTOMATIC_TRIGGER_EXECUTION_ENABLED' in source
    assert '"supervision_execution": SUPERVISION_EXECUTION_ENABLED' in source
    assert '"heartbeat_execution": bool(settings.HEARTBEAT_ENABLED)' in source


def test_agent_capabilities_expose_disabled_heartbeat(monkeypatch):
    from app.api import agents

    model = AgentOut.model_construct()
    monkeypatch.setattr(agents.settings, "HEARTBEAT_ENABLED", False)

    agents._apply_release_capabilities(model)

    assert model.execution_capabilities["heartbeat_execution"] is False


def test_disabled_heartbeat_allows_cleanup_but_rejects_new_configuration(monkeypatch):
    from app.api import agents

    monkeypatch.setattr(agents.settings, "HEARTBEAT_ENABLED", False)

    agents._validate_heartbeat_release_update({"heartbeat_enabled": False})

    with pytest.raises(HTTPException) as enable_error:
        agents._validate_heartbeat_release_update({"heartbeat_enabled": True})
    assert enable_error.value.status_code == 409
    assert enable_error.value.detail["code"] == "heartbeat_automation_unavailable"

    with pytest.raises(HTTPException) as config_error:
        agents._validate_heartbeat_release_update({"heartbeat_active_hours": "08:00-19:00"})
    assert config_error.value.status_code == 409
