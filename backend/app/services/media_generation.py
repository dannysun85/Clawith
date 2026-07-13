"""Durable recovery for asynchronous media-generation provider tasks.

Provider jobs routinely outlive an Agent tool-call timeout.  This service makes
their lifecycle independent from the originating LLM turn, stores completed
assets before settling Credits, and keeps settlement idempotent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.logging_config import get_trace_id
from app.database import async_session
from app.models.activity_log import AgentActivityLog
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditReservation
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    release_reserved_credits_in_session,
)
from app.services.llm.failover import (
    MINIMAX_QUOTA_CODES,
    FailoverErrorType,
    classify_error,
    extract_minimax_code,
)
from app.services.storage import agent_storage_key, get_storage_backend


ACTIVE_MEDIA_STATUSES = ("submitting", "submitted", "processing", "retrying", "downloading")
TERMINAL_MEDIA_STATUSES = ("succeeded", "failed")


class ProviderTaskIdentityCollision(RuntimeError):
    """A provider task identity was already owned by another security scope."""


@dataclass(slots=True, frozen=True)
class MediaGenerationOutcome:
    status: str
    output_path: str | None = None
    error: str | None = None
    retryable: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider_status(data: dict) -> str:
    return str(data.get("status") or (data.get("data") or {}).get("status") or "Unknown")


def _valid_mp4(data: bytes) -> bool:
    return len(data) >= 12 and b"ftyp" in data[:64]


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:1000]


def _media_task_age(task: MediaGenerationTask) -> timedelta:
    created_at = task.created_at or _utcnow()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return _utcnow() - created_at


def _media_task_expiry_reason(task: MediaGenerationTask) -> str | None:
    max_age = max(int(get_settings().MEDIA_GENERATION_MAX_AGE_SECONDS), 3600)
    if _media_task_age(task) > timedelta(seconds=max_age):
        return f"MiniMax video task exceeded the {max_age}-second recovery window"
    return None


async def _record_media_failure_issue(task: MediaGenerationTask, reason: str) -> None:
    from app.services.production_issue_monitor import record_production_issue

    task_id = getattr(task, "id", None)
    reservation_id = getattr(task, "reservation_id", None)
    error_code = extract_minimax_code(reason) or "media_task_failed"
    await record_production_issue(
        source="media_generation",
        category="media",
        summary="Media generation task failed before a usable asset was delivered",
        severity="warning" if error_code in MINIMAX_QUOTA_CODES else "error",
        error_code=error_code,
        operation=getattr(task, "modality", None),
        tenant_id=getattr(task, "tenant_id", None),
        user_id=getattr(task, "user_id", None),
        agent_id=getattr(task, "agent_id", None),
        trace_id=get_trace_id(),
        metadata={
            "provider": getattr(task, "provider", None),
            "model": getattr(task, "model", None),
            "modality": getattr(task, "modality", None),
            "task_id": str(task_id) if task_id else None,
            "provider_task_id": getattr(task, "provider_task_id", None),
            "reservation_id": str(reservation_id) if reservation_id else None,
            "attempt_count": getattr(task, "attempt_count", 0),
            "consecutive_error_count": getattr(task, "consecutive_error_count", 0),
        },
    )


async def create_minimax_video_task_record(
    *,
    record_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    credential_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
    model: str,
    metadata_path: str,
    output_path: str,
    request_metadata: dict,
) -> MediaGenerationTask:
    """Create the durable row before asking the paid provider to start work."""
    task = MediaGenerationTask(
        id=record_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        credential_id=credential_id,
        reservation_id=reservation_id,
        provider="minimax",
        modality="video",
        model=model,
        status="submitting",
        metadata_path=metadata_path,
        output_path=output_path,
        request_metadata=request_metadata,
        next_poll_at=_utcnow(),
    )
    async with async_session() as db:
        db.add(task)
        await db.commit()
    return task


async def mark_minimax_video_task_submitted(
    record_id: uuid.UUID,
    *,
    provider_task_id: str,
    metadata: dict,
    poll_after_seconds: int = 0,
) -> uuid.UUID:
    """Attach the provider identity and persist compatibility metadata."""
    normalized_provider_task_id = str(provider_task_id or "").strip()
    if not normalized_provider_task_id:
        raise ValueError("Provider task identity is empty")

    attached_task: MediaGenerationTask | None = None
    for attempt in range(2):
        try:
            async with async_session() as db:
                task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
                if not task:
                    raise ValueError("Media generation task not found")
                existing_result = await db.execute(
                    select(MediaGenerationTask)
                    .where(
                        MediaGenerationTask.provider == "minimax",
                        MediaGenerationTask.provider_task_id == normalized_provider_task_id,
                        MediaGenerationTask.id != record_id,
                    )
                    .with_for_update()
                )
                existing = existing_result.scalar_one_or_none()
                if existing:
                    same_scope = (
                        existing.agent_id == task.agent_id
                        and existing.tenant_id == task.tenant_id
                    )
                    if task.reservation_id:
                        await release_reserved_credits_in_session(db, task.reservation_id)
                    task.provider_task_id = None
                    task.status = "failed"
                    task.completed_at = _utcnow()
                    task.next_poll_at = None
                    task.last_error = (
                        f"Deduplicated provider task; canonical media task is {existing.id}"
                        if same_scope
                        else "Provider task identity collision across security scopes"
                    )
                    await db.commit()
                    if not same_scope:
                        raise ProviderTaskIdentityCollision(task.last_error)
                    attached_task = existing
                else:
                    task.provider_task_id = normalized_provider_task_id
                    task.status = "submitted"
                    task.last_error = None
                    task.consecutive_error_count = 0
                    task.next_poll_at = _utcnow() + timedelta(seconds=max(int(poll_after_seconds), 0))
                    await db.commit()
                    attached_task = task
            break
        except IntegrityError:
            if attempt:
                raise
            # Another replica attached the same provider identity between the
            # lookup and commit. Retry once and converge on its canonical row.
            continue

    if attached_task is None:
        raise RuntimeError("Unable to attach provider task identity")
    canonical_metadata = dict(metadata)
    canonical_metadata.update({
        "task_record_id": str(attached_task.id),
        "task_id": attached_task.provider_task_id or normalized_provider_task_id,
        "credential_id": str(attached_task.credential_id) if attached_task.credential_id else "",
        "reservation_id": str(attached_task.reservation_id) if attached_task.reservation_id else "",
        "save_path": attached_task.output_path,
    })
    await _write_task_metadata(attached_task, canonical_metadata)
    return attached_task.id


async def mark_media_generation_submission_failed(record_id: uuid.UUID, error: BaseException) -> None:
    """Close a provider submission that never produced a recoverable task id."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status in TERMINAL_MEDIA_STATUSES:
            return
        if task.reservation_id:
            await release_reserved_credits_in_session(db, task.reservation_id)
        task.status = "failed"
        task.last_error = _safe_error(error)
        task.completed_at = _utcnow()
        task.next_poll_at = None
        await db.commit()
    await _record_media_failure_issue(task, task.last_error or "submission_failed")


async def record_media_generation_retry(record_id: uuid.UUID, error: BaseException) -> MediaGenerationTask | None:
    """Keep a transient provider task recoverable, but never retry forever."""
    settings = get_settings()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status in TERMINAL_MEDIA_STATUSES:
            return task
        task.consecutive_error_count = (getattr(task, "consecutive_error_count", 0) or 0) + 1
        max_errors = max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1)
        if task.consecutive_error_count >= max_errors:
            reason = (
                f"Media generation stopped after {task.consecutive_error_count} "
                "consecutive recovery errors"
            )
            await _finalize_failure_in_session(db, task, reason, None)
            await db.commit()
            await _record_media_failure_issue(task, reason)
            return task
        task.status = "retrying"
        task.last_error = _safe_error(error)
        task.last_checked_at = _utcnow()
        backoff = min(
            max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5) * (2 ** min(task.attempt_count, 5)),
            300,
        )
        task.next_poll_at = _utcnow() + timedelta(seconds=backoff)
        await db.commit()
        return task


async def find_media_generation_task(
    *,
    agent_id: uuid.UUID,
    provider_task_id: str,
) -> MediaGenerationTask | None:
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask).where(
                MediaGenerationTask.agent_id == agent_id,
                MediaGenerationTask.provider == "minimax",
                MediaGenerationTask.provider_task_id == provider_task_id,
            )
        )
        return result.scalar_one_or_none()


async def reconcile_minimax_video_task(
    record_id: uuid.UUID,
    *,
    status_data: dict | None = None,
) -> MediaGenerationOutcome:
    """Poll and settle one task. Safe to run repeatedly and concurrently."""
    task = await _load_task(record_id)
    if not task:
        return MediaGenerationOutcome(status="failed", error="Media generation task not found")

    storage = get_storage_backend()
    output_key = agent_storage_key(task.agent_id, task.output_path)
    if task.status == "succeeded" and await storage.exists(output_key):
        return MediaGenerationOutcome(status="succeeded", output_path=task.output_path)
    if task.status == "failed":
        return MediaGenerationOutcome(status="failed", error=task.last_error)
    if not task.provider_task_id or not task.credential_id:
        created_at = task.created_at or _utcnow()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        submission_timeout = max(int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS), 60)
        if _utcnow() - created_at > timedelta(seconds=submission_timeout):
            reason = "Provider submission did not produce a recoverable task identity"
            await _finalize_failure(record_id, reason, None)
            return MediaGenerationOutcome(status="failed", error=reason)
        error = ValueError("Provider task identity is not available yet")
        retry_task = await record_media_generation_retry(record_id, error)
        if retry_task and retry_task.status == "failed":
            return MediaGenerationOutcome(status="failed", error=retry_task.last_error)
        return MediaGenerationOutcome(status="retrying", error=str(error), retryable=True)

    # Expiry is checked before any provider request. An old poison task must
    # not keep consuming provider calls or modifying global credential state.
    expiry_reason = _media_task_expiry_reason(task)
    if expiry_reason:
        failed_task = await _finalize_failure(record_id, expiry_reason, task.last_response)
        await _write_task_metadata(
            failed_task,
            {
                "status": "Fail",
                "last_response": task.last_response,
                "error": expiry_reason,
                "reservation_status": "released",
            },
        )
        return MediaGenerationOutcome(status="failed", error=expiry_reason)

    try:
        # Runtime import avoids a module cycle while keeping one MiniMax protocol implementation.
        from app.services.agent_tools import (
            _load_minimax_tool_credential_by_id,
            _mark_minimax_tool_credential_failure,
            _minimax_download_file,
            _minimax_query_video_task,
            _minimax_retrieve_file_download_url,
            _minimax_video_file_id,
        )

        credential = await _load_minimax_tool_credential_by_id(task.credential_id)
        if status_data is None:
            status_data = await _minimax_query_video_task(
                credential.api_key,
                credential.base_url,
                task.provider_task_id,
            )
        provider_status = _provider_status(status_data)

        if provider_status == "Success":
            claim_status, claimed_task = await _claim_success_download(record_id)
            if claim_status == "succeeded":
                return MediaGenerationOutcome(status="succeeded", output_path=claimed_task.output_path)
            if claim_status == "failed":
                return MediaGenerationOutcome(status="failed", error=claimed_task.last_error)
            if claim_status != "claimed":
                return MediaGenerationOutcome(status="processing", retryable=True)
            task = claimed_task
            file_id = _minimax_video_file_id(status_data)
            if not file_id:
                raise ValueError("Completed MiniMax video response has no file_id")
            download_url = await _minimax_retrieve_file_download_url(
                credential.api_key,
                credential.base_url,
                file_id,
            )
            video_bytes = await _minimax_download_file(download_url)
            if not _valid_mp4(video_bytes):
                raise ValueError("MiniMax video download is not a valid MP4 payload")
            overlay_text = str((task.request_metadata or {}).get("overlay_text") or "").strip()
            if overlay_text:
                from app.services.media_assets import apply_video_text_overlay

                video_bytes = await apply_video_text_overlay(
                    video_bytes,
                    overlay_text,
                    position=str((task.request_metadata or {}).get("overlay_position") or "bottom"),
                )
                if not _valid_mp4(video_bytes):
                    raise ValueError("Video text overlay did not produce a valid MP4 payload")

            # Paid settlement is deliberately after durable storage. If storage
            # fails, Credits remain reserved and the daemon retries.
            await storage.write_bytes(output_key, video_bytes, content_type="video/mp4")
            completed_task = await _finalize_success(record_id, status_data, len(video_bytes))
            await _write_task_metadata(
                completed_task,
                {
                    "status": "Success",
                    "last_response": status_data,
                    "downloaded_path": completed_task.output_path,
                    "reservation_status": "finalized" if completed_task.reservation_id else "not_required",
                    "completed_at": completed_task.completed_at.isoformat() if completed_task.completed_at else _utcnow().isoformat(),
                },
            )
            return MediaGenerationOutcome(status="succeeded", output_path=completed_task.output_path)

        if provider_status == "Fail":
            fail_reason = (
                status_data.get("fail_reason")
                or (status_data.get("base_resp") or {}).get("status_msg")
                or "MiniMax video generation failed"
            )
            failed_task = await _finalize_failure(record_id, str(fail_reason), status_data)
            if failed_task.status == "succeeded":
                return MediaGenerationOutcome(status="succeeded", output_path=failed_task.output_path)
            await _write_task_metadata(
                failed_task,
                {
                    "status": "Fail",
                    "last_response": status_data,
                    "reservation_status": "released" if failed_task.reservation_id else "not_required",
                    "completed_at": failed_task.completed_at.isoformat() if failed_task.completed_at else _utcnow().isoformat(),
                    "error": str(fail_reason),
                },
            )
            return MediaGenerationOutcome(status="failed", error=str(fail_reason))

        expiry_reason = _media_task_expiry_reason(task)
        if expiry_reason:
            reason = expiry_reason
            failed_task = await _finalize_failure(record_id, reason, status_data)
            await _write_task_metadata(
                failed_task,
                {"status": "Fail", "last_response": status_data, "error": reason, "reservation_status": "released"},
            )
            return MediaGenerationOutcome(status="failed", error=reason)

        pending_task = await _record_provider_pending(record_id, provider_status, status_data)
        if pending_task.status == "succeeded":
            return MediaGenerationOutcome(status="succeeded", output_path=pending_task.output_path)
        if pending_task.status == "failed":
            return MediaGenerationOutcome(status="failed", error=pending_task.last_error)
        await _write_task_metadata(
            pending_task,
            {
                "status": provider_status,
                "last_response": status_data,
                "last_checked_at": pending_task.last_checked_at.isoformat() if pending_task.last_checked_at else _utcnow().isoformat(),
            },
        )
        return MediaGenerationOutcome(status="processing", retryable=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            if task.credential_id:
                await _mark_minimax_tool_credential_failure(
                    task.credential_id,
                    exc,
                    modality="video",
                )
        except Exception:
            logger.exception("[media] failed to update credential health task_id={}", record_id)
        expiry_reason = _media_task_expiry_reason(task)
        if expiry_reason or classify_error(exc) is FailoverErrorType.NON_RETRYABLE:
            reason = expiry_reason or _safe_error(exc)
            failed_task = await _finalize_failure(record_id, reason, None)
            try:
                await _write_task_metadata(
                    failed_task,
                    {"status": "Fail", "error": reason, "reservation_status": "released"},
                )
            except Exception:
                logger.exception("[media] failed to persist terminal metadata task_id={}", record_id)
            logger.warning(
                "[media] MiniMax video reconciliation failed task_id={} error_type={} error_code={}",
                record_id,
                type(exc).__name__,
                extract_minimax_code(str(exc)) or "unknown",
            )
            return MediaGenerationOutcome(status="failed", error=reason)
        try:
            retry_task = await record_media_generation_retry(record_id, exc)
        except Exception:
            logger.exception("[media] failed to record retry task_id={}", record_id)
            retry_task = None
        if retry_task and retry_task.status == "failed":
            return MediaGenerationOutcome(status="failed", error=retry_task.last_error)
        logger.warning(
            "[media] MiniMax video reconciliation retry task_id={} error_type={} error_code={}",
            record_id,
            type(exc).__name__,
            extract_minimax_code(str(exc)) or "unknown",
        )
        return MediaGenerationOutcome(status="retrying", error=_safe_error(exc), retryable=True)


async def _load_task(record_id: uuid.UUID) -> MediaGenerationTask | None:
    async with async_session() as db:
        return await db.get(MediaGenerationTask, record_id)


async def _record_provider_pending(
    record_id: uuid.UUID,
    provider_status: str,
    status_data: dict,
) -> MediaGenerationTask:
    settings = get_settings()
    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status in TERMINAL_MEDIA_STATUSES:
            return task
        task.status = "processing" if provider_status not in {"Unknown", "submitted"} else "submitted"
        task.last_response = status_data
        task.last_error = None
        task.consecutive_error_count = 0
        task.last_checked_at = now
        task.next_poll_at = now + timedelta(seconds=max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5))
        if task.reservation_id:
            reservation = await db.get(CreditReservation, task.reservation_id, with_for_update=True)
            if reservation and reservation.status == "reserved":
                reservation.expires_at = max(
                    reservation.expires_at or now,
                    now + timedelta(hours=24),
                )
        await db.commit()
        return task


async def _claim_success_download(
    record_id: uuid.UUID,
) -> tuple[str, MediaGenerationTask]:
    """Serialize asset download/settlement across inline and worker pollers."""
    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return "succeeded", task
        if task.status == "failed":
            return "failed", task
        if task.status == "downloading" and task.next_poll_at and task.next_poll_at > now:
            return "busy", task
        task.status = "downloading"
        task.last_checked_at = now
        task.next_poll_at = now + timedelta(minutes=5)
        await db.commit()
        return "claimed", task


async def _finalize_success(
    record_id: uuid.UUID,
    status_data: dict,
    output_size: int,
) -> MediaGenerationTask:
    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return task
        if task.status == "failed":
            raise ValueError("Failed media generation task cannot be finalized")

        if task.reservation_id:
            await finalize_reserved_credits_in_session(db, task.reservation_id)
        task.status = "succeeded"
        task.last_response = status_data
        task.last_error = None
        task.consecutive_error_count = 0
        task.last_checked_at = now
        task.completed_at = now
        task.next_poll_at = None

        db.add(AgentActivityLog(
            agent_id=task.agent_id,
            action_type="file_written",
            summary=f"Video ready: {task.output_path.rsplit('/', 1)[-1]}",
            detail_json={
                "path": task.output_path,
                "provider": task.provider,
                "provider_task_id": task.provider_task_id,
                "size": output_size,
                "media_generation_task_id": str(task.id),
            },
            related_id=task.id,
        ))
        if task.user_id:
            db.add(Notification(
                user_id=task.user_id,
                agent_id=task.agent_id,
                type="system",
                title="视频生成完成",
                body=f"视频已保存到 {task.output_path}",
                link=f"/agents/{task.agent_id}",
                ref_id=task.id,
                sender_name="Astra",
            ))
        await db.commit()
        return task


async def _finalize_failure(
    record_id: uuid.UUID,
    reason: str,
    status_data: dict | None,
) -> MediaGenerationTask:
    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return task
        if task.status == "failed":
            return task
        await _finalize_failure_in_session(db, task, reason, status_data, now=now)
        await db.commit()
    await _record_media_failure_issue(task, reason)
    return task


async def _finalize_failure_in_session(
    db,
    task: MediaGenerationTask,
    reason: str,
    status_data: dict | None,
    *,
    now: datetime | None = None,
) -> None:
    """Terminalize one task and release its reservation in one transaction."""

    now = now or _utcnow()
    if task.reservation_id:
        await release_reserved_credits_in_session(db, task.reservation_id)
    task.status = "failed"
    task.last_response = status_data
    task.last_error = reason[:1000]
    task.last_checked_at = now
    task.completed_at = task.completed_at or now
    task.next_poll_at = None
    if task.user_id:
        db.add(Notification(
            user_id=task.user_id,
            agent_id=task.agent_id,
            type="system",
            title="视频生成失败",
            body=reason[:500],
            link=f"/agents/{task.agent_id}",
            ref_id=task.id,
            sender_name="Astra",
        ))


async def _write_task_metadata(task: MediaGenerationTask, updates: dict) -> None:
    storage = get_storage_backend()
    key = agent_storage_key(task.agent_id, task.metadata_path)
    payload: dict = {}
    try:
        if await storage.exists(key):
            existing = json.loads(await storage.read_text(key, encoding="utf-8", errors="replace"))
            if isinstance(existing, dict):
                payload.update(existing)
    except Exception:
        logger.warning("[media] invalid existing metadata ignored key={}", key)
    payload.update(task.request_metadata or {})
    payload.update({
        "provider": task.provider,
        "task_record_id": str(task.id),
        "task_id": task.provider_task_id or payload.get("task_id") or "",
        "credential_id": str(task.credential_id) if task.credential_id else "",
        "reservation_id": str(task.reservation_id) if task.reservation_id else "",
        "save_path": task.output_path,
    })
    payload.update(updates)
    await storage.write_text(key, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _legacy_reserved_video_reservations_query():
    """Claim legacy reserved/finalized tasks once across worker replicas."""
    return (
        select(CreditReservation)
        .where(
            CreditReservation.status.in_(("reserved", "finalized")),
            CreditReservation.provider == "minimax",
            CreditReservation.action == "video",
            CreditReservation.agent_id.is_not(None),
        )
        .with_for_update(skip_locked=True)
    )


async def backfill_legacy_minimax_video_tasks() -> int:
    """Import pre-migration tasks, including already-paid successful assets."""
    storage = get_storage_backend()
    created = 0
    async with async_session() as db:
        result = await db.execute(_legacy_reserved_video_reservations_query())
        reservations = list(result.scalars().all())
        for reservation in reservations:
            existing_result = await db.execute(
                select(MediaGenerationTask).where(MediaGenerationTask.reservation_id == reservation.id)
            )
            existing_task = existing_result.scalar_one_or_none()
            if existing_task and existing_task.provider_task_id:
                continue
            agent_id = reservation.agent_id
            if not agent_id:
                continue
            video_dir_key = agent_storage_key(agent_id, "workspace/videos")
            for entry in await storage.list_dir(video_dir_key):
                if entry.is_dir or not entry.name.lower().endswith(".json"):
                    continue
                try:
                    metadata = json.loads(await storage.read_text(entry.key, encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                if str(metadata.get("reservation_id") or "") != str(reservation.id):
                    continue
                provider_task_id = str(metadata.get("task_id") or "").strip()
                credential_raw = str(metadata.get("credential_id") or "").strip()
                if not provider_task_id or not credential_raw:
                    continue
                provider_existing_result = await db.execute(
                    select(MediaGenerationTask).where(
                        MediaGenerationTask.provider == "minimax",
                        MediaGenerationTask.provider_task_id == provider_task_id,
                    )
                )
                provider_existing = provider_existing_result.scalar_one_or_none()
                if provider_existing and provider_existing.id != getattr(existing_task, "id", None):
                    continue
                try:
                    credential_id = uuid.UUID(credential_raw)
                except ValueError:
                    continue
                prefix = f"{agent_id}/"
                metadata_path = entry.key[len(prefix):] if entry.key.startswith(prefix) else f"workspace/videos/{entry.name}"
                output_path = str(metadata.get("downloaded_path") or metadata.get("save_path") or "").strip()
                if not output_path:
                    safe_task_id = "".join(ch for ch in provider_task_id if ch.isalnum() or ch in "_-")[:80] or uuid.uuid4().hex
                    output_path = f"workspace/videos/minimax_video_{safe_task_id}.mp4"
                output_key = agent_storage_key(agent_id, output_path)
                output_exists = await storage.exists(output_key) and await storage.is_file(output_key)
                recovered_success = reservation.status == "finalized" and output_exists
                request_metadata = {
                    key: metadata[key]
                    for key in ("credit_cost", "model", "prompt", "duration", "resolution", "created_at")
                    if key in metadata
                }
                if existing_task:
                    existing_task.credential_id = credential_id
                    existing_task.provider_task_id = provider_task_id
                    existing_task.status = "succeeded" if recovered_success else "submitted"
                    existing_task.metadata_path = metadata_path
                    existing_task.output_path = output_path
                    existing_task.request_metadata = request_metadata
                    existing_task.last_response = metadata.get("last_response")
                    existing_task.last_error = None
                    existing_task.completed_at = _utcnow() if recovered_success else None
                    existing_task.next_poll_at = None if recovered_success else _utcnow()
                else:
                    db.add(MediaGenerationTask(
                        tenant_id=reservation.tenant_id,
                        agent_id=agent_id,
                        user_id=reservation.user_id,
                        credential_id=credential_id,
                        reservation_id=reservation.id,
                        provider="minimax",
                        modality="video",
                        model=reservation.model,
                        provider_task_id=provider_task_id,
                        status="succeeded" if recovered_success else "submitted",
                        metadata_path=metadata_path,
                        output_path=output_path,
                        request_metadata=request_metadata,
                        last_response=metadata.get("last_response"),
                        completed_at=_utcnow() if recovered_success else None,
                        next_poll_at=None if recovered_success else _utcnow(),
                    ))
                await db.flush()
                created += 1
                logger.info(
                    "[media] backfilled legacy MiniMax video task provider_task_id={} reservation_id={}",
                    provider_task_id,
                    reservation.id,
                )
                break
        if created:
            await db.commit()
    return created


async def _claim_due_task_ids() -> list[uuid.UUID]:
    settings = get_settings()
    now = _utcnow()
    batch_size = max(int(settings.MEDIA_GENERATION_BATCH_SIZE), 1)
    lease_seconds = max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS) * 3, 30)
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask)
            .where(
                MediaGenerationTask.status.in_(ACTIVE_MEDIA_STATUSES),
                or_(MediaGenerationTask.next_poll_at.is_(None), MediaGenerationTask.next_poll_at <= now),
            )
            .order_by(MediaGenerationTask.next_poll_at.asc().nullsfirst(), MediaGenerationTask.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            task.attempt_count = (task.attempt_count or 0) + 1
            task.next_poll_at = now + timedelta(seconds=lease_seconds)
        await db.commit()
        return [task.id for task in tasks]


async def reconcile_pending_media_generation_tasks() -> int:
    task_ids = await _claim_due_task_ids()
    for task_id in task_ids:
        await reconcile_minimax_video_task(task_id)
    return len(task_ids)


async def start_media_generation_daemon() -> None:
    """Continuously recover provider tasks independently of Agent sessions."""
    settings = get_settings()
    interval = max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5)
    logger.info("[media] generation daemon started interval={}s", interval)
    while True:
        try:
            backfilled = await backfill_legacy_minimax_video_tasks()
            reconciled = await reconcile_pending_media_generation_tasks()
            if backfilled or reconciled:
                logger.info("[media] reconciliation complete backfilled={} reconciled={}", backfilled, reconciled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[media] generation daemon iteration failed")
        await asyncio.sleep(interval)
