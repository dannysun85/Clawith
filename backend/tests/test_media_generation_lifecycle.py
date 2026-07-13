import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.media_generation import MediaGenerationTask
from app.services import media_generation
from app.services import tool_seeder


def test_media_generation_task_has_durable_recovery_identity():
    columns = MediaGenerationTask.__table__.columns

    assert "provider_task_id" in columns
    assert "reservation_id" in columns
    assert "metadata_path" in columns
    assert "output_path" in columns
    assert "next_poll_at" in columns
    assert "consecutive_error_count" in columns


def test_worker_registers_media_generation_reconciliation_daemon():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert "start_media_generation_daemon" in main_source
    assert '("media_generation", start_media_generation_daemon())' in main_source


def test_manual_video_check_is_available_to_every_agent_by_default():
    definition = next(tool for tool in tool_seeder.BUILTIN_TOOLS if tool["name"] == "check_video_minimax")

    assert definition["is_default"] is True
    assert "check_video_minimax" in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES


def test_legacy_backfill_claims_reserved_tasks_without_cross_worker_races():
    statement = media_generation._legacy_reserved_video_reservations_query()
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert set(statement.compile().params["status_1"]) == {"reserved", "finalized"}


@pytest.mark.asyncio
async def test_duplicate_provider_task_releases_new_reservation_and_returns_canonical(monkeypatch):
    reservation_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    duplicate = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        reservation_id=reservation_id,
        provider_task_id=None,
        status="submitting",
        completed_at=None,
        next_poll_at=None,
        last_error=None,
    )
    canonical = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider_task_id="provider-task-1",
        output_path="workspace/videos/canonical.mp4",
        request_metadata={},
        provider="minimax",
        metadata_path="workspace/videos/canonical.json",
    )

    class Result:
        def scalar_one_or_none(self):
            return canonical

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return duplicate

        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    release = AsyncMock()
    write_metadata = AsyncMock()
    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(media_generation, "release_reserved_credits_in_session", release)
    monkeypatch.setattr(media_generation, "_write_task_metadata", write_metadata)

    canonical_id = await media_generation.mark_minimax_video_task_submitted(
        duplicate.id,
        provider_task_id="provider-task-1",
        metadata={"reservation_id": str(reservation_id)},
    )

    assert canonical_id == canonical.id
    assert duplicate.status == "failed"
    assert duplicate.provider_task_id is None
    assert release.await_args.args[1] == reservation_id
    assert write_metadata.await_args.args[0] is canonical
    assert write_metadata.await_args.args[1]["reservation_id"] == str(canonical.reservation_id)


@pytest.mark.asyncio
async def test_reconciliation_stores_valid_mp4_before_settlement_and_is_idempotent(monkeypatch):
    events: list[str] = []
    objects: dict[str, bytes] = {}
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider="minimax",
        modality="video",
        model="MiniMax-Hailuo-2.3",
        provider_task_id="provider-task-1",
        status="submitted",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={"overlay_text": "精确中文", "overlay_position": "bottom"},
        last_error=None,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
    )

    class FakeStorage:
        async def exists(self, key: str) -> bool:
            return key in objects

        async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
            assert content_type == "video/mp4"
            events.append("store")
            objects[key] = data

    async def finalize(*_args, **_kwargs):
        events.append("finalize")
        task.status = "succeeded"
        task.completed_at = datetime.now(timezone.utc)
        return task

    async def overlay(data, text, *, position):
        events.append("overlay")
        assert text == "精确中文"
        assert position == "bottom"
        return data + b"overlay"

    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: FakeStorage())
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "_claim_success_download", AsyncMock(return_value=("claimed", task)))
    monkeypatch.setattr(media_generation, "_finalize_success", AsyncMock(side_effect=finalize))
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr("app.services.media_assets.apply_video_text_overlay", overlay)
    monkeypatch.setattr(
        "app.services.agent_tools._load_minimax_tool_credential_by_id",
        AsyncMock(return_value=SimpleNamespace(api_key="key", base_url="https://minimax.test")),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._minimax_retrieve_file_download_url",
        AsyncMock(return_value="https://files.test/video"),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._minimax_download_file",
        AsyncMock(return_value=b"\x00\x00\x00\x18ftypmp42video"),
    )

    first = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data={"status": "Success", "file_id": "file-1"},
    )
    second = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data={"status": "Success", "file_id": "file-1"},
    )

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert events == ["overlay", "store", "finalize"]


@pytest.mark.asyncio
async def test_transient_recovery_errors_are_bounded_and_release_once(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="retrying",
        reservation_id=uuid.uuid4(),
        user_id=None,
        agent_id=uuid.uuid4(),
        attempt_count=30,
        consecutive_error_count=2,
        last_error=None,
        last_checked_at=None,
        last_response=None,
        completed_at=None,
        next_poll_at=datetime.now(timezone.utc),
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return task

        async def commit(self):
            return None

        def add(self, _value):
            return None

    release = AsyncMock()
    record_issue = AsyncMock()
    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(media_generation, "release_reserved_credits_in_session", release)
    monkeypatch.setattr(media_generation, "_record_media_failure_issue", record_issue)
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(
            MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS=3,
            MEDIA_GENERATION_POLL_INTERVAL_SECONDS=5,
        ),
    )

    result = await media_generation.record_media_generation_retry(
        task.id,
        RuntimeError("MiniMax API error (1000): unexpected error"),
    )

    assert result is task
    assert task.status == "failed"
    assert task.consecutive_error_count == 3
    assert task.next_poll_at is None
    assert release.await_count == 1
    assert release.await_args.args[1] == task.reservation_id
    record_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_provider_poll_resets_consecutive_errors(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="retrying",
        reservation_id=None,
        consecutive_error_count=7,
        last_response=None,
        last_error="network timeout",
        last_checked_at=None,
        next_poll_at=None,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return task

        async def commit(self):
            return None

    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_POLL_INTERVAL_SECONDS=5),
    )

    result = await media_generation._record_provider_pending(
        task.id,
        "Processing",
        {"status": "Processing"},
    )

    assert result.status == "processing"
    assert result.consecutive_error_count == 0
    assert result.last_error is None


@pytest.mark.asyncio
async def test_expired_task_fails_before_calling_provider(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider="minimax",
        provider_task_id="old-provider-task",
        status="retrying",
        output_path="workspace/videos/expired.mp4",
        metadata_path="workspace/videos/expired.json",
        request_metadata={},
        last_response={"status": "Processing"},
        last_error=None,
        created_at=datetime.now(timezone.utc) - media_generation.timedelta(hours=49),
    )

    class Storage:
        async def exists(self, _key):
            return False

    finalize = AsyncMock(side_effect=lambda *_args: task)
    write_metadata = AsyncMock()
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_finalize_failure", finalize)
    monkeypatch.setattr(media_generation, "_write_task_metadata", write_metadata)
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_MAX_AGE_SECONDS=48 * 3600),
    )

    result = await media_generation.reconcile_minimax_video_task(task.id)

    assert result.status == "failed"
    finalize.assert_awaited_once()
    write_metadata.assert_awaited_once()
