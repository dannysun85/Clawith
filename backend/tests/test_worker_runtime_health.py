import pytest
from unittest.mock import AsyncMock
from fastapi import HTTPException

import app.main as main
from app.services import production_issue_monitor


@pytest.fixture(autouse=True)
def restore_worker_runtime_state(monkeypatch):
    original_role = main.settings.PROCESS_ROLE
    original_started = main._worker_runtime_tracking_started
    original_ready = main._worker_runtime_ready
    original_failed = main._worker_runtime_failed
    original_names = set(main._critical_background_task_names)
    original_monitor_enabled = main.settings.PRODUCTION_ISSUE_MONITOR_ENABLED
    monkeypatch.setattr(main.settings, "PRODUCTION_ISSUE_MONITOR_ENABLED", False)
    yield
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", original_role)
    main._worker_runtime_tracking_started = original_started
    main._worker_runtime_ready = original_ready
    main._worker_runtime_failed = original_failed
    main._critical_background_task_names = original_names
    monkeypatch.setattr(
        main.settings,
        "PRODUCTION_ISSUE_MONITOR_ENABLED",
        original_monitor_enabled,
    )


@pytest.mark.asyncio
async def test_dedicated_worker_health_fails_before_runtime_is_ready(monkeypatch):
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", "worker,connector")
    main._worker_runtime_tracking_started = False
    main._worker_runtime_ready = False
    main._worker_runtime_failed = False

    with pytest.raises(HTTPException) as exc_info:
        await main.health_check()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "worker runtime unavailable"


@pytest.mark.asyncio
async def test_dedicated_worker_health_is_ok_after_critical_tasks_start(monkeypatch):
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", "worker,connector")
    main._begin_worker_runtime_tracking({"trigger_daemon", "media_generation"})
    main._mark_worker_runtime_ready()

    response = await main.health_check()

    assert response.status == "ok"

@pytest.mark.asyncio
async def test_critical_task_exit_makes_dedicated_worker_unhealthy(monkeypatch):
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", "worker,connector")
    main._begin_worker_runtime_tracking({"trigger_daemon", "media_generation"})
    main._mark_worker_runtime_ready()

    main._mark_worker_runtime_failed("media_generation")

    with pytest.raises(HTTPException) as exc_info:
        await main.health_check()
    assert exc_info.value.status_code == 503
    assert main._worker_runtime_failed is True
    assert main._worker_runtime_ready is False


@pytest.mark.asyncio
async def test_api_health_does_not_depend_on_worker_runtime(monkeypatch):
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", "api,bootstrap")
    main._worker_runtime_tracking_started = False
    main._worker_runtime_ready = False
    main._worker_runtime_failed = True

    response = await main.health_check()

    assert response.status == "ok"


@pytest.mark.asyncio
async def test_dedicated_worker_health_fails_when_monitor_db_loop_is_stale(
    monkeypatch,
):
    monkeypatch.setattr(main.settings, "PROCESS_ROLE", "worker,connector")
    monkeypatch.setattr(main.settings, "PRODUCTION_ISSUE_MONITOR_ENABLED", True)
    main._begin_worker_runtime_tracking({"production_issue_monitor"})
    main._mark_worker_runtime_ready()
    monkeypatch.setattr(
        production_issue_monitor,
        "production_issue_monitor_health",
        lambda: {"healthy": False},
    )

    with pytest.raises(HTTPException) as exc_info:
        await main.health_check()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "production issue monitor database loop unavailable"
    )


@pytest.mark.asyncio
async def test_fatal_worker_daemon_records_issue_before_requesting_restart(monkeypatch):
    recorder = AsyncMock(return_value=None)
    calls = []

    monkeypatch.setattr(main.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    await main._terminate_failed_worker(
        "media_generation",
        "RuntimeError",
        issue_recorder=recorder,
    )

    recorder.assert_awaited_once()
    assert calls == [(main.os.getpid(), main.signal.SIGTERM)]
