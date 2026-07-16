import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.activity_log import AgentActivityLog
from app.models.audit import ChatMessage
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.services import media_generation
from app.services import tool_seeder
from app.services.media_assets import OverlayReceipt, VideoInfo


def test_media_generation_task_has_durable_recovery_identity():
    columns = MediaGenerationTask.__table__.columns

    assert "provider_task_id" in columns
    assert "reservation_id" in columns
    assert "metadata_path" in columns
    assert "output_path" in columns
    assert "next_poll_at" in columns
    assert "consecutive_error_count" in columns
    assert "origin_session_id" in columns
    assert "completion_message_id" in columns
    assert "completion_delivery_status" in columns
    assert "realtime_next_attempt_at" in columns
    assert "realtime_published_at" in columns


def test_worker_registers_media_generation_reconciliation_daemon():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert "start_media_generation_daemon" in main_source
    assert '("media_generation", start_media_generation_daemon())' in main_source


def test_video_tools_do_not_sync_temp_workspace_over_durable_storage():
    source = (Path(__file__).parents[1] / "app" / "services" / "agent_tools.py").read_text(
        encoding="utf-8"
    )
    generate_branch = source.split('elif tool_name == "generate_video_minimax":', 1)[1].split(
        'elif tool_name == "check_video_minimax":',
        1,
    )[0]
    check_branch = source.split('elif tool_name == "check_video_minimax":', 1)[1].split(
        'elif tool_name == "discover_resources":',
        1,
    )[0]

    assert "session_id=session_id" in generate_branch
    assert "sync_back=False" in generate_branch
    assert "sync_back=False" in check_branch


def test_manual_video_check_is_available_to_every_agent_by_default():
    definition = next(tool for tool in tool_seeder.BUILTIN_TOOLS if tool["name"] == "check_video_minimax")

    assert definition["is_default"] is True
    assert "check_video_minimax" in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES


def test_unknown_persisted_builtin_names_fail_closed():
    assert tool_seeder.is_registered_builtin_tool_name("generate_video_minimax") is True
    assert tool_seeder.is_registered_builtin_tool_name("check_video_minimax") is True
    assert tool_seeder.is_registered_builtin_tool_name("media_video_generate") is False
    assert tool_seeder.is_registered_builtin_tool_name("media_video_edit") is False


@pytest.mark.asyncio
async def test_media_failure_issue_keeps_provider_quota_out_of_error_severity(monkeypatch):
    captured: list[dict] = []

    async def capture_issue(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(
        "app.services.production_issue_monitor.record_production_issue",
        capture_issue,
    )
    task = SimpleNamespace(
        id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        modality="video",
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-Hailuo-2.3",
        provider_task_id="provider-task",
        attempt_count=1,
        consecutive_error_count=1,
    )

    await media_generation._record_media_failure_issue(
        task,
        "MiniMax API error (2056): resource limit",
    )
    await media_generation._record_media_failure_issue(task, "provider socket closed")

    assert captured[0]["error_code"] == "2056"
    assert captured[0]["severity"] == "warning"
    assert captured[1]["severity"] == "error"


def test_legacy_backfill_claims_reserved_tasks_without_cross_worker_races():
    statement = media_generation._legacy_reserved_video_reservations_query()
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "NOT (EXISTS" in sql
    assert "LIMIT" in sql
    assert set(statement.compile().params["status_1"]) == {"reserved", "finalized"}


def test_legacy_backfill_never_accepts_paths_outside_workspace():
    fallback = media_generation._safe_legacy_workspace_video_path(
        "workspace/../secrets.mp4",
        "provider/task",
    )

    assert fallback == "workspace/videos/minimax_video_providertask.mp4"
    assert media_generation._safe_legacy_workspace_video_path(
        "workspace/videos/demo clip.mp4",
        "provider-task",
    ) == "workspace/videos/demo clip.mp4"


@pytest.mark.asyncio
async def test_origin_session_must_match_agent_and_authenticated_user():
    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(ValueError, match="not authorized"):
        await media_generation._validated_origin_session_id(
            Session(),
            origin_session_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )


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

    record_issue = AsyncMock()
    monkeypatch.setattr(media_generation, "_record_media_failure_issue", record_issue)

    with pytest.raises(media_generation.ProviderTaskIdentityCollision):
        await media_generation.mark_minimax_video_task_submitted(
            duplicate.id,
            provider_task_id="provider-task-1",
            metadata={"reservation_id": str(reservation_id)},
        )

    assert duplicate.status == "submission_ambiguous"
    assert duplicate.provider_task_id is None
    release.assert_not_awaited()
    write_metadata.assert_not_awaited()
    record_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_submission_failure_reports_missing_task_so_caller_can_release_hold(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(media_generation, "async_session", lambda: Session())

    closed = await media_generation.mark_media_generation_submission_failed(
        uuid.uuid4(),
        RuntimeError("insert failed"),
    )

    assert closed is False


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
        request_metadata={
            "overlay_text": "精确中文",
            "overlay_text_sha256": hashlib.sha256("精确中文".encode("utf-8")).hexdigest(),
            "overlay_position": "bottom",
        },
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

    async def mark_settlement_ready(*_args, **_kwargs):
        task.status = "settlement_ready"
        return task

    async def overlay(
        data,
        text,
        *,
        text_position,
        brand_asset,
        brand_position,
        brand_scale,
    ):
        events.append("overlay")
        assert text == "精确中文"
        assert text_position == "bottom"
        assert brand_asset is None
        assert brand_position == "center"
        assert brand_scale == 0.42
        return data + b"overlay", OverlayReceipt(
            rendered_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: FakeStorage())
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "_claim_success_download", AsyncMock(return_value=("claimed", task)))
    monkeypatch.setattr(
        media_generation,
        "_mark_settlement_ready",
        AsyncMock(side_effect=mark_settlement_ready),
    )
    monkeypatch.setattr(media_generation, "_finalize_success", AsyncMock(side_effect=finalize))
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr(media_generation, "apply_video_brand_overlays", overlay)
    monkeypatch.setattr(
        media_generation,
        "validate_generated_video",
        AsyncMock(return_value=VideoInfo(640, 360, 1.0, "h264", "yuv420p", "aac", True)),
    )
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
async def test_succeeded_task_with_missing_object_redownloads_without_duplicate_settlement(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        completion_message_id=uuid.uuid4(),
        provider="minimax",
        modality="video",
        model="MiniMax-Hailuo-2.3",
        provider_task_id="provider-task-repair",
        status="succeeded",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={},
        last_response={"status": "Success", "file_id": "file-repair"},
        last_error=None,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    brand_key = media_generation.minimax_video_brand_asset_key(
        task.agent_id,
        task.id,
        "png",
    )
    brand_raw = b"frozen-brand"
    brand_sha256 = hashlib.sha256(brand_raw).hexdigest()
    task.request_metadata = {
        "brand_asset_storage_key": brand_key,
        "brand_asset_sha256": brand_sha256,
        "brand_position": "center",
        "brand_scale": 0.42,
    }
    objects: dict[str, bytes] = {brand_key: brand_raw}

    class Storage:
        async def exists(self, key):
            return key in objects

        async def write_bytes(self, key, data, content_type=None):
            assert content_type == "video/mp4"
            objects[key] = data

        async def read_bytes(self, key):
            return objects[key]

        async def delete(self, key):
            objects.pop(key, None)

    async def begin_repair(_record_id):
        task.status = "asset_repairing"
        return task

    async def mark_ready(*_args, **_kwargs):
        task.status = "settlement_ready"
        return task

    async def finalize(*_args, **_kwargs):
        task.status = "succeeded"
        return task

    download = AsyncMock(return_value=b"\x00\x00\x00\x18ftypmp42repaired")
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_begin_missing_asset_repair", begin_repair)
    monkeypatch.setattr(
        media_generation,
        "_claim_success_download",
        AsyncMock(return_value=("claimed", task)),
    )
    monkeypatch.setattr(media_generation, "_mark_settlement_ready", mark_ready)
    finalize_success = AsyncMock(side_effect=finalize)
    monkeypatch.setattr(media_generation, "_finalize_success", finalize_success)
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr(
        media_generation,
        "validate_generated_video",
        AsyncMock(return_value=VideoInfo(640, 360, 1.0, "h264", "yuv420p", "aac", True)),
    )
    monkeypatch.setattr(
        media_generation,
        "image_asset_from_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(sha256=brand_sha256),
    )
    overlay = AsyncMock(
        return_value=(
            b"\x00\x00\x00\x18ftypmp42branded",
            OverlayReceipt(brand_asset_sha256=brand_sha256),
        )
    )
    monkeypatch.setattr(media_generation, "apply_video_brand_overlays", overlay)
    monkeypatch.setattr(
        "app.services.agent_tools._load_minimax_tool_credential_by_id",
        AsyncMock(return_value=SimpleNamespace(api_key="key", base_url="https://minimax.test")),
    )
    monkeypatch.setattr(
        "app.services.agent_tools._minimax_retrieve_file_download_url",
        AsyncMock(return_value="https://files.test/repaired"),
    )
    monkeypatch.setattr("app.services.agent_tools._minimax_download_file", download)

    outcome = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data=task.last_response,
    )

    assert outcome.status == "succeeded"
    download.assert_awaited_once()
    finalize_success.assert_awaited_once()
    overlay.assert_awaited_once()
    output_key = media_generation.agent_storage_key(task.agent_id, task.output_path)
    assert output_key in objects
    assert objects[brand_key] == brand_raw

    second = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data=task.last_response,
    )
    assert second.status == "succeeded"
    assert objects[brand_key] == brand_raw


@pytest.mark.asyncio
async def test_unrepairable_missing_asset_is_not_written_back_as_succeeded(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="asset_repairing",
        last_response=None,
        last_error=None,
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

    record_issue = AsyncMock()
    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(media_generation, "_record_media_failure_issue", record_issue)

    await media_generation._record_unrepairable_asset(
        task.id,
        "provider no longer retains the file",
        {"status": "Fail"},
    )

    assert task.status == "asset_delivery_failed"
    assert task.next_poll_at is None
    assert "provider no longer retains the file" in task.last_error
    record_issue.assert_awaited_once_with(task, task.last_error)


@pytest.mark.asyncio
async def test_tampered_exact_copy_holds_provider_debt_for_asset_repair(monkeypatch):
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
        provider_task_id="provider-task-tampered-copy",
        status="submitted",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={
            "overlay_text": "被篡改的文案",
            "overlay_text_sha256": hashlib.sha256("原始文案".encode("utf-8")).hexdigest(),
        },
        last_error=None,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
    )

    class Storage:
        async def exists(self, _key):
            return False

    async def record_asset_failure(_record_id, error, _status_data):
        task.status = "asset_repairing"
        task.last_error = str(error)
        return task

    asset_failure_mock = AsyncMock(side_effect=record_asset_failure)
    finalize_failure_mock = AsyncMock()
    overlay = AsyncMock()
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(
        media_generation,
        "_claim_success_download",
        AsyncMock(return_value=("claimed", task)),
    )
    monkeypatch.setattr(media_generation, "_finalize_failure", finalize_failure_mock)
    monkeypatch.setattr(
        media_generation,
        "_record_provider_success_asset_failure",
        asset_failure_mock,
    )
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr(media_generation, "apply_video_brand_overlays", overlay)
    monkeypatch.setattr(
        media_generation,
        "validate_generated_video",
        AsyncMock(return_value=VideoInfo(640, 360, 1.0, "h264", "yuv420p", "aac", True)),
    )
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

    outcome = await media_generation.reconcile_minimax_video_task(
        task.id,
        status_data={"status": "Success", "file_id": "file-1"},
    )

    assert outcome.status == "retrying"
    assert outcome.retryable is True
    assert "Frozen video copy hash" in (outcome.error or "")
    asset_failure_mock.assert_awaited_once()
    finalize_failure_mock.assert_not_awaited()
    overlay.assert_not_awaited()


@pytest.mark.asyncio
async def test_settlement_ready_retries_local_settlement_without_provider_call(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        status="settlement_ready",
        output_path="workspace/videos/result.mp4",
        output_size=123,
        last_response={"status": "Success", "file_id": "file-1"},
        completed_at=None,
    )

    class Storage:
        async def exists(self, _key):
            return True

    async def finalize(*_args, **_kwargs):
        task.status = "succeeded"
        task.completed_at = datetime.now(timezone.utc)
        return task

    finalize_success = AsyncMock(side_effect=finalize)
    provider_load = AsyncMock()
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_finalize_success", finalize_success)
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr(
        "app.services.agent_tools._load_minimax_tool_credential_by_id",
        provider_load,
    )

    outcome = await media_generation.reconcile_minimax_video_task(task.id)

    assert outcome.status == "succeeded"
    finalize_success.assert_awaited_once_with(
        task.id,
        task.last_response,
        123,
        deliver_completion=True,
    )
    provider_load.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_completion_persists_one_message_and_exact_session_deep_link(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        reservation_id=uuid.uuid4(),
        origin_session_id=session_id,
        completion_message_id=None,
        completion_delivery_status="pending",
        realtime_published_at=None,
        realtime_next_attempt_at=None,
        status="settlement_ready",
        output_path="workspace/videos/demo clip.mp4",
        provider="minimax",
        provider_task_id="provider-task",
        completed_at=None,
        last_response=None,
        last_error="retry",
        output_size=None,
        consecutive_error_count=2,
        last_checked_at=None,
        next_poll_at=None,
    )
    chat_session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        is_group=False,
        last_message_at=None,
    )
    added: list[object] = []

    class Result:
        def scalar_one_or_none(self):
            return chat_session

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _record_id, **_kwargs):
            if model is MediaGenerationTask:
                return task
            return None

        async def execute(self, _statement):
            return Result()

        def add(self, value):
            added.append(value)

        async def flush(self):
            for value in added:
                if isinstance(value, ChatMessage) and value.id is None:
                    value.id = uuid.uuid4()

        async def commit(self):
            return None

    finalize_credits = AsyncMock()

    async def publish(_record_id):
        task.realtime_published_at = datetime.now(timezone.utc)
        return True

    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(
        media_generation,
        "finalize_reserved_credits_in_session",
        finalize_credits,
    )
    publish_completion = AsyncMock(side_effect=publish)
    monkeypatch.setattr(
        media_generation,
        "publish_media_completion_event",
        publish_completion,
    )
    monkeypatch.setattr(
        "app.api.websocket.maybe_mark_session_read_for_active_viewer",
        AsyncMock(),
    )

    first = await media_generation._finalize_success(
        task.id,
        {"status": "Success"},
        4096,
    )
    second = await media_generation._finalize_success(
        task.id,
        {"status": "Success"},
        4096,
    )

    messages = [value for value in added if isinstance(value, ChatMessage)]
    notifications = [value for value in added if isinstance(value, Notification)]
    activities = [value for value in added if isinstance(value, AgentActivityLog)]
    assert first is task and second is task
    assert len(messages) == len(notifications) == len(activities) == 1
    assert messages[0].conversation_id == str(session_id)
    assert "path=workspace%2Fvideos%2Fdemo+clip.mp4&inline=1" in messages[0].content
    assert f"session_id={session_id}" in notifications[0].link
    assert "workspace_path=workspace%2Fvideos%2Fdemo+clip.mp4" in notifications[0].link
    assert task.completion_message_id == messages[0].id
    finalize_credits.assert_awaited_once()
    publish_completion.assert_awaited_once_with(task.id)


@pytest.mark.asyncio
async def test_completion_outbox_retries_when_exact_realtime_delivery_fails(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    task = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        origin_session_id=session_id,
        completion_message_id=message_id,
        status="succeeded",
        output_path="workspace/videos/result.mp4",
        realtime_attempt_count=0,
        realtime_next_attempt_at=None,
        realtime_published_at=None,
        realtime_last_error=None,
    )
    message = SimpleNamespace(
        id=message_id,
        agent_id=agent_id,
        role="assistant",
        content="video ready",
        conversation_id=str(session_id),
        created_at=datetime.now(timezone.utc),
    )
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        is_group=False,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _record_id, **_kwargs):
            if model is MediaGenerationTask:
                return task
            if model is ChatMessage:
                return message
            return session

        async def commit(self):
            return None

    send = AsyncMock(side_effect=ConnectionError("remote delivery failed"))
    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr("app.api.websocket.manager.send_to_session_user", send)

    published = await media_generation.publish_media_completion_event(task.id)

    assert published is False
    assert task.realtime_published_at is None
    assert task.realtime_attempt_count == 1
    assert task.realtime_next_attempt_at is not None
    assert task.realtime_last_error == "remote delivery failed"
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_keeps_accepted_task_recoverable_on_quota_poll_error(monkeypatch):
    from app.services import agent_tools

    provider_error = RuntimeError("MiniMax API error (2056): resource limit")
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
        provider_task_id="provider-task-quota",
        status="submitted",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={},
        last_response=None,
        last_error=None,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
    )

    class Storage:
        async def exists(self, _key):
            return False

    mark_failure = AsyncMock()
    task.status = "retrying"
    retry = AsyncMock(return_value=task)
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_finalize_failure", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "_write_task_metadata", AsyncMock())
    monkeypatch.setattr(media_generation, "record_media_generation_retry", retry)
    monkeypatch.setattr(
        agent_tools,
        "_load_minimax_tool_credential_by_id",
        AsyncMock(return_value=SimpleNamespace(api_key="key", base_url="https://minimax.test")),
    )
    monkeypatch.setattr(
        agent_tools,
        "_minimax_query_video_task",
        AsyncMock(side_effect=provider_error),
    )
    monkeypatch.setattr(agent_tools, "_mark_minimax_tool_credential_failure", mark_failure)

    result = await media_generation.reconcile_minimax_video_task(task.id)

    assert result.status == "retrying"
    assert result.retryable is True
    retry.assert_awaited_once_with(task.id, provider_error)
    mark_failure.assert_awaited_once_with(
        task.credential_id,
        provider_error,
        modality="video",
        model=task.model,
    )


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
async def test_accepted_provider_task_is_not_refunded_after_retry_threshold(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="retrying",
        provider_task_id="accepted-provider-task",
        reservation_id=uuid.uuid4(),
        user_id=None,
        agent_id=uuid.uuid4(),
        attempt_count=30,
        consecutive_error_count=2,
        last_error=None,
        last_checked_at=None,
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
        TimeoutError("provider poll timed out"),
    )

    assert result is task
    assert task.status == "retrying"
    assert task.consecutive_error_count == 3
    assert task.next_poll_at is not None
    release.assert_not_awaited()
    record_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_provider_inflight_submission_without_task_id_keeps_hold(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-Hailuo-2.3",
        provider_task_id=None,
        status="submitting",
        metadata_path="workspace/videos/task.json",
        output_path="workspace/videos/result.mp4",
        request_metadata={},
        last_response=None,
        last_error=None,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        completed_at=None,
    )
    reservation = SimpleNamespace(status="provider_inflight")

    class Storage:
        async def exists(self, _key):
            return False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _object_id, **_kwargs):
            return reservation

    mark_ambiguous = AsyncMock()
    finalize_failure = AsyncMock()
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "async_session", lambda: Session())
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS=60),
    )
    monkeypatch.setattr(
        media_generation,
        "mark_media_generation_submission_ambiguous",
        mark_ambiguous,
    )
    monkeypatch.setattr(media_generation, "_finalize_failure", finalize_failure)

    outcome = await media_generation.reconcile_minimax_video_task(task.id)

    assert outcome.status == "failed"
    assert "Credits remain held" in (outcome.error or "")
    mark_ambiguous.assert_awaited_once()
    finalize_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_threshold_does_not_release_provider_inflight_without_task_id(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="retrying",
        provider_task_id=None,
        reservation_id=uuid.uuid4(),
        user_id=None,
        agent_id=uuid.uuid4(),
        attempt_count=30,
        consecutive_error_count=2,
        last_error=None,
        last_checked_at=None,
        completed_at=None,
        next_poll_at=datetime.now(timezone.utc),
    )
    reservation = SimpleNamespace(status="provider_inflight")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _object_id, **_kwargs):
            if model is media_generation.MediaGenerationTask:
                return task
            return reservation

        async def commit(self):
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
        TimeoutError("provider response lost"),
    )

    assert result is task
    assert task.status == "submission_ambiguous"
    release.assert_not_awaited()
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
async def test_expired_accepted_task_keeps_polling_without_guessing_a_refund(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        credential_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-Hailuo-2.3",
        provider_task_id="old-provider-task",
        status="retrying",
        output_path="workspace/videos/expired.mp4",
        metadata_path="workspace/videos/expired.json",
        request_metadata={},
        last_response={"status": "Processing"},
        last_error=None,
        last_checked_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc) - media_generation.timedelta(hours=49),
    )

    class Storage:
        async def exists(self, _key):
            return False

    task.status = "processing"
    pending = AsyncMock(return_value=task)
    record_issue = AsyncMock()
    write_metadata = AsyncMock()
    monkeypatch.setattr(media_generation, "_load_task", AsyncMock(return_value=task))
    monkeypatch.setattr(media_generation, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(media_generation, "_record_provider_pending", pending)
    monkeypatch.setattr(media_generation, "_record_media_failure_issue", record_issue)
    monkeypatch.setattr(media_generation, "_write_task_metadata", write_metadata)
    monkeypatch.setattr(
        "app.services.agent_tools._load_minimax_tool_credential_by_id",
        AsyncMock(return_value=SimpleNamespace(api_key="key", base_url="https://minimax.test")),
    )
    query = AsyncMock(return_value={"status": "Processing"})
    monkeypatch.setattr("app.services.agent_tools._minimax_query_video_task", query)
    monkeypatch.setattr(
        media_generation,
        "get_settings",
        lambda: SimpleNamespace(MEDIA_GENERATION_MAX_AGE_SECONDS=48 * 3600),
    )

    result = await media_generation.reconcile_minimax_video_task(task.id)

    assert result.status == "processing"
    query.assert_awaited_once()
    pending.assert_awaited_once()
    record_issue.assert_awaited_once()
    write_metadata.assert_awaited_once()
