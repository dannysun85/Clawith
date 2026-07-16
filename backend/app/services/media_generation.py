"""Durable recovery for asynchronous media-generation provider tasks.

Provider jobs routinely outlive an Agent tool-call timeout.  This service makes
their lifecycle independent from the originating LLM turn, stores completed
assets before settling Credits, and keeps settlement idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.logging_config import get_trace_id
from app.database import async_session
from app.models.activity_log import AgentActivityLog
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditReservation
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    mark_credit_reservation_settlement_ready_in_session,
    release_reserved_credits_in_session,
)
from app.services.llm.failover import (
    MINIMAX_QUOTA_CODES,
    extract_minimax_code,
)
from app.services.media_assets import (
    MediaContractError,
    OverlayReceipt,
    apply_video_brand_overlays,
    image_asset_from_bytes,
    valid_mp4,
    validate_generated_video,
)
from app.services.storage import agent_storage_key, get_storage_backend, normalize_storage_key


ACTIVE_MEDIA_STATUSES = (
    "submitting",
    "submitted",
    "processing",
    "retrying",
    "downloading",
    "asset_repairing",
    "settlement_ready",
)
TERMINAL_MEDIA_STATUSES = ("succeeded", "failed", "closed_nonrefundable")


class ProviderTaskIdentityCollision(RuntimeError):
    """A provider task identity was already owned by another security scope."""


_PRIVATE_VIDEO_BRAND_PREFIX = "_internal/provider_recovery/minimax/video"


def minimax_video_brand_asset_key(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    extension: str,
) -> str:
    """Return an Agent-inaccessible, task-bound brand-asset key."""

    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    if normalized_extension not in {"jpg", "png", "webp"}:
        raise ValueError("Unsupported private video brand asset extension")
    return normalize_storage_key(
        f"{_PRIVATE_VIDEO_BRAND_PREFIX}/{agent_id}/{record_id}/brand.{normalized_extension}"
    )


def _validated_video_brand_asset_key(task: MediaGenerationTask) -> str | None:
    raw_key = str((getattr(task, "request_metadata", None) or {}).get("brand_asset_storage_key") or "")
    if not raw_key:
        return None
    normalized_key = normalize_storage_key(raw_key)
    expected_prefix = normalize_storage_key(
        f"{_PRIVATE_VIDEO_BRAND_PREFIX}/{task.agent_id}/{task.id}/"
    )
    if not normalized_key.startswith(expected_prefix):
        raise MediaContractError("Frozen video brand asset key is outside the task-private namespace")
    return normalized_key


async def _delete_private_video_brand_asset(task: MediaGenerationTask) -> None:
    """Best-effort cleanup after irrecoverable failure or explicit operator closure."""

    try:
        key = _validated_video_brand_asset_key(task)
        if key:
            await get_storage_backend().delete(key)
    except Exception:
        logger.exception("[media] private video brand asset cleanup failed task_id={}", task.id)


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
    return valid_mp4(data)


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:1000]


def _media_download_url(agent_id: uuid.UUID, output_path: str) -> str:
    query = urlencode({"path": output_path, "inline": "1"})
    return f"/api/agents/{agent_id}/files/download?{query}"


def _media_completion_content(task: MediaGenerationTask) -> str:
    filename = Path(task.output_path).name or "video.mp4"
    return (
        f"✅ 视频生成完成：{filename}\n"
        f"保存位置：{task.output_path}\n\n"
        f"▶️ 播放视频：\n![]({_media_download_url(task.agent_id, task.output_path)})"
    )


async def _validated_origin_session_id(
    db,
    *,
    origin_session_id: str | uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Bind delivery only to the exact authenticated first-party chat session."""
    raw_session_id = str(origin_session_id or "").strip()
    if not raw_session_id:
        return None
    if user_id is None:
        raise ValueError("Media origin session requires an authenticated user")
    try:
        session_uuid = uuid.UUID(raw_session_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Media origin session is invalid") from exc

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.source_channel == "web",
            ChatSession.is_group.is_(False),
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Media origin session is not authorized for this task")
    return session_uuid


async def validate_media_origin_session(
    *,
    origin_session_id: str | uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Validate the delivery target before reserving Credits or calling a provider."""
    async with async_session() as db:
        return await _validated_origin_session_id(
            db,
            origin_session_id=origin_session_id,
            agent_id=agent_id,
            user_id=user_id,
        )


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
    origin_session_id: str | uuid.UUID | None,
    model: str,
    metadata_path: str,
    output_path: str,
    request_metadata: dict,
) -> MediaGenerationTask:
    """Create the durable row before asking the paid provider to start work."""
    async with async_session() as db:
        validated_session_id = await _validated_origin_session_id(
            db,
            origin_session_id=origin_session_id,
            agent_id=agent_id,
            user_id=user_id,
        )
        task = MediaGenerationTask(
            id=record_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            credential_id=credential_id,
            reservation_id=reservation_id,
            origin_session_id=validated_session_id,
            provider="minimax",
            modality="video",
            model=model,
            status="submitting",
            metadata_path=metadata_path,
            output_path=output_path,
            request_metadata=request_metadata,
            completion_delivery_status="pending",
            next_poll_at=_utcnow(),
        )
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
                    task.provider_task_id = None
                    task.status = "submission_ambiguous"
                    task.completed_at = _utcnow()
                    task.next_poll_at = None
                    task.last_error = (
                        f"Provider returned an identity already owned by media task {existing.id}"
                        if same_scope
                        else "Provider task identity collision across security scopes"
                    )
                    await db.commit()
                    attached_task = task
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
    if attached_task.status == "submission_ambiguous":
        await _record_media_failure_issue(
            attached_task,
            attached_task.last_error or "Provider task identity collision",
        )
        raise ProviderTaskIdentityCollision(
            attached_task.last_error or "Provider task identity collision"
        )
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


async def mark_media_generation_submission_failed(
    record_id: uuid.UUID,
    error: BaseException,
) -> bool:
    """Close a task only when no provider request was started.

    Return whether a durable task row was actually closed so the caller can
    release a just-created reservation if task-row creation itself failed.
    """
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status in TERMINAL_MEDIA_STATUSES:
            return False
        if task.reservation_id:
            await release_reserved_credits_in_session(
                db,
                task.reservation_id,
                release_provider_inflight=True,
            )
        task.status = "failed"
        task.last_error = _safe_error(error)
        task.completed_at = _utcnow()
        task.next_poll_at = None
        await db.commit()
    await _delete_private_video_brand_asset(task)
    await _record_media_failure_issue(task, task.last_error or "submission_failed")
    return True


async def mark_media_generation_submission_ambiguous(
    record_id: uuid.UUID,
    error: BaseException,
) -> None:
    """Retain the hold when a provider request may have been accepted."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status in TERMINAL_MEDIA_STATUSES:
            return
        task.status = "submission_ambiguous"
        task.last_error = _safe_error(error)
        task.completed_at = _utcnow()
        task.next_poll_at = None
        if task.user_id:
            db.add(Notification(
                user_id=task.user_id,
                agent_id=task.agent_id,
                type="system",
                title="视频任务提交结果待核对",
                body="供应商请求结果不确定，系统已保留 Credits，避免重复生成或错误退款。",
                link=f"/agents/{task.agent_id}/chat",
                ref_id=task.id,
                sender_name="Astra",
            ))
        await db.commit()
    await _record_media_failure_issue(
        task,
        f"Provider submission outcome is ambiguous: {task.last_error or 'unknown'}",
    )


async def record_media_generation_retry(record_id: uuid.UUID, error: BaseException) -> MediaGenerationTask | None:
    """Keep an accepted provider task recoverable without guessing a refund."""
    settings = get_settings()
    should_record_issue = False
    issue_reason = ""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status in TERMINAL_MEDIA_STATUSES:
            return task
        task.consecutive_error_count = (getattr(task, "consecutive_error_count", 0) or 0) + 1
        max_errors = max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1)
        provider_accepted = bool(getattr(task, "provider_task_id", None))
        reservation = None
        if task.reservation_id:
            reservation = await db.get(
                CreditReservation,
                task.reservation_id,
                with_for_update=True,
            )
        provider_outcome_uncertain = bool(
            reservation
            and reservation.status in {"provider_inflight", "settlement_ready", "finalized"}
        )
        if (
            task.consecutive_error_count >= max_errors
            and not provider_accepted
            and not provider_outcome_uncertain
        ):
            issue_reason = (
                f"Media generation stopped after {task.consecutive_error_count} "
                "consecutive recovery errors"
            )
            await _finalize_failure_in_session(db, task, issue_reason, None)
            await db.commit()
            await _record_media_failure_issue(task, issue_reason)
            return task
        if (
            task.consecutive_error_count >= max_errors
            and not provider_accepted
            and provider_outcome_uncertain
        ):
            should_record_issue = True
            issue_reason = (
                "Provider submission identity is unavailable while the Credits hold "
                "still represents possible provider debt"
            )
            task.status = "submission_ambiguous"
            task.last_error = _safe_error(error)
            task.last_checked_at = _utcnow()
            task.completed_at = task.completed_at or _utcnow()
            task.next_poll_at = None
            await db.commit()
            await _record_media_failure_issue(task, issue_reason)
            return task
        if provider_accepted and task.consecutive_error_count == max_errors:
            should_record_issue = True
            issue_reason = (
                f"Accepted provider task still needs recovery after "
                f"{task.consecutive_error_count} consecutive errors"
            )
        task.status = "retrying"
        task.last_error = _safe_error(error)
        task.last_checked_at = _utcnow()
        backoff = min(
            max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5) * (2 ** min(task.attempt_count, 5)),
            300,
        )
        task.next_poll_at = _utcnow() + timedelta(seconds=backoff)
        await db.commit()
    if should_record_issue:
        await _record_media_failure_issue(task, issue_reason)
    return task


async def _record_provider_success_asset_failure(
    record_id: uuid.UUID,
    error: BaseException,
    status_data: dict | None,
) -> MediaGenerationTask:
    """Hold provider debt while bounded local asset repair is attempted.

    MiniMax has already completed successfully at this boundary.  A font,
    overlay, validation, storage, or delivery error is therefore never grounds
    for a Credits release and must never cause another generation submission.
    """
    settings = get_settings()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return task

        if task.reservation_id:
            reservation = await db.get(
                CreditReservation,
                task.reservation_id,
                with_for_update=True,
            )
            exact_amount = int(reservation.amount) if reservation else 0
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                task.reservation_id,
                amount=exact_amount,
            )

        task.consecutive_error_count = (task.consecutive_error_count or 0) + 1
        max_errors = max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1)
        task.last_response = status_data
        task.last_error = (
            f"Provider succeeded; local asset delivery failed: {_safe_error(error)}"
        )[:1000]
        task.last_checked_at = _utcnow()
        if task.consecutive_error_count >= max_errors:
            # Durable manual-reconciliation state. It is intentionally neither
            # terminal nor daemon-due: Credits remain held and Agent deletion
            # is fenced until an operator repairs or settles the artifact.
            task.status = "asset_delivery_failed"
            task.next_poll_at = None
        else:
            task.status = "asset_repairing"
            backoff = min(
                max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5)
                * (2 ** min(task.consecutive_error_count, 6)),
                600,
            )
            task.next_poll_at = _utcnow() + timedelta(seconds=backoff)
        await db.commit()

    await _record_media_failure_issue(task, task.last_error or "Asset delivery failed")
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


async def _begin_missing_asset_repair(record_id: uuid.UUID) -> MediaGenerationTask:
    """Move a completed row back into download recovery when its object vanished."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            task.status = "asset_repairing"
            task.last_error = "Completed media object is missing; repair scheduled"
            task.next_poll_at = _utcnow()
            await db.commit()
        return task


async def _record_unrepairable_asset(
    record_id: uuid.UUID,
    reason: str,
    status_data: dict | None,
) -> None:
    """Preserve settlement history while surfacing a terminal asset-loss incident."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            return
        # The original provider cost and completion history remain settled,
        # but the customer-visible object no longer exists and the provider
        # has proven it cannot be downloaded again.  Never report success for
        # a missing artifact; retain the non-refundable debt state for manual
        # remediation and Agent-deletion fencing.
        task.status = "asset_delivery_failed"
        task.last_response = status_data
        task.last_error = f"Asset repair failed: {reason}"[:1000]
        task.next_poll_at = None
        await db.commit()
    await _record_media_failure_issue(task, task.last_error)
    await _delete_private_video_brand_asset(task)


async def reconcile_minimax_video_task(
    record_id: uuid.UUID,
    *,
    status_data: dict | None = None,
    deliver_completion: bool = True,
) -> MediaGenerationOutcome:
    """Poll and settle one task. Safe to run repeatedly and concurrently."""
    task = await _load_task(record_id)
    if not task:
        return MediaGenerationOutcome(status="failed", error="Media generation task not found")

    storage = get_storage_backend()
    output_key = agent_storage_key(task.agent_id, task.output_path)
    if task.status == "succeeded" and await storage.exists(output_key):
        # Retain the frozen brand asset for the missing-object recovery path.
        # A later retention job or explicit operator closure owns deletion.
        return MediaGenerationOutcome(status="succeeded", output_path=task.output_path)
    if task.status == "succeeded":
        task = await _begin_missing_asset_repair(record_id)
    if task.status == "failed":
        return MediaGenerationOutcome(status="failed", error=task.last_error)
    if task.status == "submission_ambiguous":
        return MediaGenerationOutcome(
            status="failed",
            error=task.last_error or "Provider submission outcome requires operator reconciliation",
        )
    if task.status == "settlement_ready":
        try:
            completed_task = await _finalize_success(
                record_id,
                task.last_response or status_data or {"status": "Success"},
                int(getattr(task, "output_size", 0) or 0),
                deliver_completion=deliver_completion,
            )
        except Exception as exc:
            await _record_settlement_retry(record_id, exc)
            logger.warning(
                "[media] settlement retry task_id={} error_type={}",
                record_id,
                type(exc).__name__,
            )
            return MediaGenerationOutcome(
                status="retrying",
                error=_safe_error(exc),
                retryable=True,
            )
        try:
            await _write_task_metadata(
                completed_task,
                {
                    "status": "Success",
                    "last_response": completed_task.last_response,
                    "downloaded_path": completed_task.output_path,
                    "reservation_status": (
                        "finalized" if completed_task.reservation_id else "not_required"
                    ),
                    "completed_at": (
                        completed_task.completed_at.isoformat()
                        if completed_task.completed_at
                        else _utcnow().isoformat()
                    ),
                },
            )
        except Exception:
            logger.exception(
                "[media] finalized task metadata repair failed task_id={}",
                record_id,
            )
        return MediaGenerationOutcome(
            status="succeeded",
            output_path=completed_task.output_path,
        )
    if not task.provider_task_id or not task.credential_id:
        created_at = task.created_at or _utcnow()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        submission_timeout = max(int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS), 60)
        if _utcnow() - created_at > timedelta(seconds=submission_timeout):
            reason = "Provider submission did not produce a recoverable task identity"
            reservation = None
            if task.reservation_id:
                async with async_session() as db:
                    reservation = await db.get(CreditReservation, task.reservation_id)
            if reservation and reservation.status in {
                "provider_inflight",
                "settlement_ready",
                "finalized",
            }:
                await mark_media_generation_submission_ambiguous(
                    record_id,
                    RuntimeError(reason),
                )
                return MediaGenerationOutcome(
                    status="failed",
                    error=(
                        "Provider submission outcome is ambiguous; Credits remain held "
                        "for operator reconciliation"
                    ),
                )
            await _finalize_failure(record_id, reason, None)
            return MediaGenerationOutcome(status="failed", error=reason)
        error = ValueError("Provider task identity is not available yet")
        retry_task = await record_media_generation_retry(record_id, error)
        if retry_task and retry_task.status == "failed":
            return MediaGenerationOutcome(status="failed", error=retry_task.last_error)
        return MediaGenerationOutcome(status="retrying", error=str(error), retryable=True)

    # Provider acceptance is an external side effect. Age alone cannot prove
    # failure or authorize a refund; keep polling and raise a durable issue.
    expiry_reason = _media_task_expiry_reason(task)
    if expiry_reason:
        logger.error("[media] overdue accepted provider task task_id={}", record_id)
        await _record_media_failure_issue(task, expiry_reason)

    provider_succeeded = False
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
            provider_succeeded = True
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
            await validate_generated_video(
                video_bytes,
                label="MiniMax video download",
                require_browser_safe=False,
            )
            request_metadata = task.request_metadata or {}
            overlay_text = str(request_metadata.get("overlay_text") or "")
            expected_text_sha256 = str(request_metadata.get("overlay_text_sha256") or "")
            if expected_text_sha256 and not hmac.compare_digest(
                expected_text_sha256,
                hashlib.sha256(overlay_text.encode("utf-8")).hexdigest(),
            ):
                raise MediaContractError("Frozen video copy hash does not match the submitted text")

            brand_asset = None
            frozen_brand_key = str(request_metadata.get("brand_asset_storage_key") or "")
            expected_brand_sha256 = str(request_metadata.get("brand_asset_sha256") or "")
            if frozen_brand_key:
                brand_key = _validated_video_brand_asset_key(task)
                if not brand_key:
                    raise MediaContractError("Frozen video brand asset key is missing")
                try:
                    brand_bytes = await storage.read_bytes(brand_key)
                except Exception as exc:
                    raise MediaContractError("Frozen video brand asset is unavailable") from exc
                brand_asset = image_asset_from_bytes(
                    brand_bytes,
                    label="Frozen video brand asset",
                    source_path=brand_key,
                )
                if not expected_brand_sha256 or not hmac.compare_digest(
                    expected_brand_sha256,
                    brand_asset.sha256,
                ):
                    raise MediaContractError("Frozen video brand asset hash does not match the submitted asset")

            if overlay_text.strip() or brand_asset:
                video_bytes, overlay_receipt = await apply_video_brand_overlays(
                    video_bytes,
                    overlay_text,
                    text_position=str(request_metadata.get("overlay_position") or "bottom"),
                    brand_asset=brand_asset,
                    brand_position=str(request_metadata.get("brand_position") or "center"),
                    brand_scale=float(request_metadata.get("brand_scale") or 0.42),
                )
                status_data = {
                    **(status_data or {}),
                    "_astra_media_contract": overlay_receipt.as_dict(),
                }
            else:
                overlay_receipt = OverlayReceipt()
            await validate_generated_video(video_bytes, label="Final brand-safe video")

            # Paid settlement is deliberately after durable storage. If storage
            # fails, Credits remain reserved and the daemon retries.
            await storage.write_bytes(output_key, video_bytes, content_type="video/mp4")
            await _mark_settlement_ready(record_id, status_data, len(video_bytes))
            try:
                completed_task = await _finalize_success(
                    record_id,
                    status_data,
                    len(video_bytes),
                    deliver_completion=deliver_completion,
                )
            except Exception as exc:
                await _record_settlement_retry(record_id, exc)
                return MediaGenerationOutcome(
                    status="retrying",
                    error=_safe_error(exc),
                    retryable=True,
                )
            try:
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
            except Exception:
                logger.exception(
                    "[media] successful task metadata write failed task_id={}",
                    record_id,
                )
            return MediaGenerationOutcome(status="succeeded", output_path=completed_task.output_path)

        if provider_status == "Fail":
            fail_reason = (
                status_data.get("fail_reason")
                or (status_data.get("base_resp") or {}).get("status_msg")
                or "MiniMax video generation failed"
            )
            if task.completion_message_id or task.completed_at:
                await _record_unrepairable_asset(
                    record_id,
                    str(fail_reason),
                    status_data,
                )
                return MediaGenerationOutcome(
                    status="failed",
                    error=f"Completed video asset could not be restored: {fail_reason}",
                )
            failed_task = await _finalize_failure(
                record_id,
                str(fail_reason),
                status_data,
                provider_confirmed_failure=True,
            )
            await _delete_private_video_brand_asset(failed_task)
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
    except MediaContractError as exc:
        reason = f"Brand-safe video contract failed: {_safe_error(exc)}"
        repair_task = await _record_provider_success_asset_failure(
            record_id,
            exc,
            status_data,
        )
        try:
            await _write_task_metadata(
                repair_task,
                {
                    "status": "AssetDeliveryFailed",
                    "provider_status": "Success",
                    "last_response": status_data,
                    "reservation_status": (
                        "settlement_ready"
                        if repair_task.reservation_id
                        else "not_required"
                    ),
                    "error": reason,
                },
            )
        except Exception:
            logger.exception("[media] contract failure metadata write failed task_id={}", record_id)
        return MediaGenerationOutcome(
            status=(
                "retrying"
                if repair_task.status == "asset_repairing"
                else "failed"
            ),
            error=reason,
            retryable=repair_task.status == "asset_repairing",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if provider_succeeded:
            reason = f"Provider succeeded; local video delivery failed: {_safe_error(exc)}"
            repair_task = await _record_provider_success_asset_failure(
                record_id,
                exc,
                status_data,
            )
            try:
                await _write_task_metadata(
                    repair_task,
                    {
                        "status": "AssetDeliveryFailed",
                        "provider_status": "Success",
                        "last_response": status_data,
                        "reservation_status": (
                            "settlement_ready"
                            if repair_task.reservation_id
                            else "not_required"
                        ),
                        "error": reason,
                    },
                )
            except Exception:
                logger.exception(
                    "[media] asset delivery failure metadata write failed task_id={}",
                    record_id,
                )
            return MediaGenerationOutcome(
                status=(
                    "retrying"
                    if repair_task.status == "asset_repairing"
                    else "failed"
                ),
                error=reason,
                retryable=repair_task.status == "asset_repairing",
            )
        try:
            if task.credential_id:
                await _mark_minimax_tool_credential_failure(
                    task.credential_id,
                    exc,
                    modality="video",
                    model=(
                        str(
                            task.model
                            or (task.request_metadata or {}).get("model")
                            or ""
                        ).strip()
                        or None
                    ),
                )
        except Exception:
            logger.exception("[media] failed to update credential health task_id={}", record_id)
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
            if reservation and reservation.status in {"reserved", "provider_inflight"}:
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


async def _mark_settlement_ready(
    record_id: uuid.UUID,
    status_data: dict,
    output_size: int,
) -> MediaGenerationTask:
    """Record the irreversible provider-success boundary after durable storage."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "failed":
            raise ValueError("Failed media generation task cannot be settled")
        if task.status != "succeeded":
            if task.reservation_id:
                reservation = await db.get(
                    CreditReservation,
                    task.reservation_id,
                    with_for_update=True,
                )
                exact_amount = int(reservation.amount) if reservation else 0
                await mark_credit_reservation_settlement_ready_in_session(
                    db,
                    task.reservation_id,
                    amount=exact_amount,
                )
            task.status = "settlement_ready"
            task.last_response = status_data
            task.output_size = output_size
            task.last_error = None
            task.last_checked_at = _utcnow()
            task.next_poll_at = _utcnow()
            await db.commit()
        return task


async def _record_settlement_retry(
    record_id: uuid.UUID,
    error: BaseException,
) -> MediaGenerationTask | None:
    """Retry local settlement without refunding a provider-successful task."""
    settings = get_settings()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status == "succeeded":
            return task
        if task.status != "settlement_ready":
            raise ValueError("Media task is not ready for settlement")
        task.consecutive_error_count = (task.consecutive_error_count or 0) + 1
        task.last_error = _safe_error(error)
        task.last_checked_at = _utcnow()
        backoff = min(
            max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5)
            * (2 ** min(task.consecutive_error_count, 6)),
            600,
        )
        task.next_poll_at = _utcnow() + timedelta(seconds=backoff)
        await db.commit()
        return task


async def _finalize_success(
    record_id: uuid.UUID,
    status_data: dict,
    output_size: int,
    *,
    deliver_completion: bool = True,
) -> MediaGenerationTask:
    now = _utcnow()
    should_publish = False
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            should_publish = bool(
                task.completion_message_id and not task.realtime_published_at
            )
        elif task.status == "failed":
            raise ValueError("Failed media generation task cannot be finalized")
        else:
            was_already_completed = bool(
                task.completed_at
                or task.completion_message_id
                or task.completion_delivery_status in {"inline", "persisted"}
            )
            if task.reservation_id:
                await finalize_reserved_credits_in_session(db, task.reservation_id)
            task.status = "succeeded"
            task.last_response = status_data
            task.last_error = None
            task.output_size = output_size
            task.consecutive_error_count = 0
            task.last_checked_at = now
            task.completed_at = task.completed_at or now
            task.next_poll_at = None

            valid_session: ChatSession | None = None
            if task.origin_session_id and task.user_id:
                session_result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == task.origin_session_id,
                        ChatSession.agent_id == task.agent_id,
                        ChatSession.user_id == task.user_id,
                        ChatSession.source_channel == "web",
                        ChatSession.is_group.is_(False),
                    )
                )
                valid_session = session_result.scalar_one_or_none()

            if task.completion_message_id:
                task.completion_delivery_status = "persisted"
                should_publish = not bool(task.realtime_published_at)
            elif was_already_completed:
                # Asset repair must not create a second assistant result for a
                # task that was already delivered inline or through history.
                pass
            elif deliver_completion and valid_session:
                completion_message = ChatMessage(
                    agent_id=task.agent_id,
                    user_id=task.user_id,
                    role="assistant",
                    content=_media_completion_content(task),
                    conversation_id=str(valid_session.id),
                )
                db.add(completion_message)
                await db.flush()
                task.completion_message_id = completion_message.id
                task.completion_delivery_status = "persisted"
                task.realtime_next_attempt_at = now
                valid_session.last_message_at = now
                should_publish = True
                try:
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer

                    await maybe_mark_session_read_for_active_viewer(
                        db,
                        agent_id=task.agent_id,
                        session_id=str(valid_session.id),
                        user_id=task.user_id,
                    )
                except Exception:
                    logger.debug(
                        "[media] active-view read marker unavailable task_id={}",
                        task.id,
                    )
            elif deliver_completion:
                task.completion_delivery_status = "not_applicable"
            else:
                task.completion_delivery_status = "inline"

            if not was_already_completed:
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
                        "completion_message_id": (
                            str(task.completion_message_id)
                            if task.completion_message_id
                            else None
                        ),
                    },
                    related_id=task.id,
                ))
            if task.user_id and not was_already_completed:
                query = {
                    "workspace_path": task.output_path,
                }
                if valid_session:
                    query["session_id"] = str(valid_session.id)
                if task.completion_message_id:
                    query["message_id"] = str(task.completion_message_id)
                db.add(Notification(
                    user_id=task.user_id,
                    agent_id=task.agent_id,
                    type="system",
                    title="视频生成完成",
                    body=f"视频已保存到 {task.output_path}",
                    link=f"/agents/{task.agent_id}/chat?{urlencode(query)}",
                    ref_id=task.id,
                    sender_name="Astra",
                ))
            await db.commit()

    if should_publish:
        try:
            await publish_media_completion_event(record_id)
        except Exception:
            # The database message is authoritative; the outbox daemon will
            # retry a failed realtime publish without touching Credits.
            logger.exception(
                "[media] completion realtime publish failed task_id={}",
                record_id,
            )
    return task


async def publish_media_completion_event(record_id: uuid.UUID) -> bool:
    """Publish one durable completion message after its transaction commits."""
    now = _utcnow()
    payload: dict | None = None
    agent_id = ""
    session_id = ""
    user_id = ""

    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if (
            not task
            or task.status != "succeeded"
            or not task.completion_message_id
            or task.realtime_published_at
        ):
            return False
        if task.realtime_next_attempt_at and task.realtime_next_attempt_at > now:
            return False

        message = await db.get(ChatMessage, task.completion_message_id)
        session = (
            await db.get(ChatSession, task.origin_session_id)
            if task.origin_session_id
            else None
        )
        if (
            not message
            or not session
            or not task.user_id
            or session.agent_id != task.agent_id
            or session.user_id != task.user_id
            or session.source_channel != "web"
            or bool(session.is_group)
            or message.agent_id != task.agent_id
            or message.conversation_id != str(session.id)
        ):
            task.realtime_published_at = now
            task.realtime_last_error = "completion realtime target is no longer valid"
            task.realtime_next_attempt_at = None
            await db.commit()
            return False

        task.realtime_attempt_count = (task.realtime_attempt_count or 0) + 1
        task.realtime_next_attempt_at = now + timedelta(seconds=60)
        task.realtime_last_error = None
        agent_id = str(task.agent_id)
        session_id = str(session.id)
        user_id = str(task.user_id)
        payload = {
            "type": "media_generation_result",
            "event_id": str(message.id),
            "session_id": session_id,
            "workspace_path": task.output_path,
            "media_generation_task_id": str(task.id),
            "message": {
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "created_at": (
                    message.created_at.isoformat()
                    if message.created_at
                    else now.isoformat()
                ),
            },
        }
        await db.commit()

    assert payload is not None
    try:
        from app.api.websocket import manager as ws_manager

        await ws_manager.send_to_session_user(
            agent_id,
            session_id,
            user_id,
            payload,
        )
    except Exception as exc:
        async with async_session() as db:
            task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
            if task and not task.realtime_published_at:
                task.realtime_last_error = _safe_error(exc)
                task.realtime_next_attempt_at = _utcnow() + timedelta(seconds=60)
                await db.commit()
        return False

    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if task and not task.realtime_published_at:
            task.realtime_published_at = _utcnow()
            task.realtime_next_attempt_at = None
            task.realtime_last_error = None
            await db.commit()
    return True


async def publish_pending_media_completion_events(limit: int = 50) -> int:
    """Retry committed media-completion events without replaying settlement."""
    now = _utcnow()
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask.id)
            .where(
                MediaGenerationTask.status == "succeeded",
                MediaGenerationTask.completion_message_id.is_not(None),
                MediaGenerationTask.realtime_published_at.is_(None),
                or_(
                    MediaGenerationTask.realtime_next_attempt_at.is_(None),
                    MediaGenerationTask.realtime_next_attempt_at <= now,
                ),
            )
            .order_by(MediaGenerationTask.completed_at.asc().nullsfirst())
            .limit(max(int(limit), 1))
        )
        task_ids = [row[0] for row in result.all()]

    published = 0
    for task_id in task_ids:
        if await publish_media_completion_event(task_id):
            published += 1
    return published


async def _finalize_failure(
    record_id: uuid.UUID,
    reason: str,
    status_data: dict | None,
    *,
    provider_confirmed_failure: bool = False,
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
        await _finalize_failure_in_session(
            db,
            task,
            reason,
            status_data,
            now=now,
            provider_confirmed_failure=provider_confirmed_failure,
        )
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
    provider_confirmed_failure: bool = False,
) -> None:
    """Terminalize one task and release its reservation in one transaction."""

    now = now or _utcnow()
    if task.reservation_id:
        await release_reserved_credits_in_session(
            db,
            task.reservation_id,
            release_provider_inflight=provider_confirmed_failure,
        )
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


def _legacy_reserved_video_reservations_query(limit: int = 20):
    """Select an unclaimed legacy batch without locking the whole ledger."""
    already_claimed = select(MediaGenerationTask.id).where(
        MediaGenerationTask.reservation_id == CreditReservation.id
    ).exists()
    return (
        select(CreditReservation)
        .where(
            CreditReservation.status.in_(("reserved", "finalized")),
            CreditReservation.provider == "minimax",
            CreditReservation.action == "video",
            CreditReservation.agent_id.is_not(None),
            ~already_claimed,
        )
        .order_by(CreditReservation.created_at.asc())
        .limit(max(int(limit), 1))
        .with_for_update(skip_locked=True)
    )


async def _claim_legacy_minimax_video_placeholders(limit: int) -> int:
    """Persist bounded claims before any object-storage I/O."""
    now = _utcnow()
    claimed = 0
    async with async_session() as db:
        result = await db.execute(_legacy_reserved_video_reservations_query(limit))
        reservations = list(result.scalars().all())
        for reservation in reservations:
            agent_id = reservation.agent_id
            if not agent_id:
                continue
            db.add(MediaGenerationTask(
                tenant_id=reservation.tenant_id,
                agent_id=agent_id,
                user_id=reservation.user_id,
                credential_id=None,
                reservation_id=reservation.id,
                provider="minimax",
                modality="video",
                model=reservation.model,
                status="backfill_scanning",
                metadata_path="",
                output_path="",
                request_metadata={"legacy_backfill_claimed_at": now.isoformat()},
                completion_delivery_status="not_applicable",
                next_poll_at=now,
            ))
            claimed += 1
        if claimed:
            await db.commit()
    return claimed


async def _claim_legacy_minimax_video_scan_ids(limit: int) -> list[uuid.UUID]:
    """Lease durable placeholders so crashed workers can resume safely."""
    now = _utcnow()
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask)
            .where(
                MediaGenerationTask.status == "backfill_scanning",
                or_(
                    MediaGenerationTask.next_poll_at.is_(None),
                    MediaGenerationTask.next_poll_at <= now,
                ),
            )
            .order_by(MediaGenerationTask.created_at.asc())
            .limit(max(int(limit), 1))
            .with_for_update(skip_locked=True)
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            task.attempt_count = (task.attempt_count or 0) + 1
            task.next_poll_at = now + timedelta(minutes=5)
        if tasks:
            await db.commit()
        return [task.id for task in tasks]


def _safe_legacy_workspace_video_path(raw_path: object, provider_task_id: str) -> str:
    """Accept only Agent-workspace MP4 paths from editable legacy metadata."""
    path = str(raw_path or "").strip().replace("\\", "/").lstrip("/")
    parts = path.split("/") if path else []
    if (
        path.startswith("workspace/")
        and path.lower().endswith(".mp4")
        and all(part not in {"", ".", ".."} for part in parts)
    ):
        return path
    safe_task_id = (
        "".join(ch for ch in provider_task_id if ch.isalnum() or ch in "_-")[:80]
        or uuid.uuid4().hex
    )
    return f"workspace/videos/minimax_video_{safe_task_id}.mp4"


async def _record_legacy_backfill_attention(
    record_id: uuid.UUID,
    reason: str,
) -> None:
    """Dead-letter an untrusted legacy record without releasing possible debt."""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status != "backfill_scanning":
            return
        reservation = (
            await db.get(CreditReservation, task.reservation_id, with_for_update=True)
            if task.reservation_id
            else None
        )
        if reservation and reservation.status == "reserved":
            reservation.status = "provider_inflight"
            reservation.expires_at = _utcnow() + timedelta(hours=24)
        task.status = "backfill_attention"
        task.last_error = reason[:1000]
        task.completed_at = _utcnow()
        task.next_poll_at = None
        await db.commit()
    await _record_media_failure_issue(task, reason)


async def _record_legacy_backfill_retry(
    record_id: uuid.UUID,
    error: BaseException,
) -> None:
    settings = get_settings()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.status != "backfill_scanning":
            return
        task.consecutive_error_count = (task.consecutive_error_count or 0) + 1
        task.last_error = _safe_error(error)
        task.next_poll_at = _utcnow() + timedelta(
            seconds=min(
                max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5)
                * (2 ** min(task.consecutive_error_count, 6)),
                600,
            )
        )
        should_alert = task.consecutive_error_count == max(
            int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS),
            1,
        )
        await db.commit()
    if should_alert:
        await _record_media_failure_issue(
            task,
            f"Legacy media backfill storage scan keeps failing: {task.last_error}",
        )


async def _backfill_one_legacy_minimax_video_task(record_id: uuid.UUID) -> bool:
    """Recover only already-paid local assets from untrusted legacy metadata."""
    attention_reason = ""
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id)
        if not task or task.status != "backfill_scanning" or not task.reservation_id:
            return False
        reservation = await db.get(CreditReservation, task.reservation_id)
        if not reservation or not task.agent_id:
            attention_reason = (
                "Legacy MiniMax task is missing its Credits reservation or Agent scope"
            )
        else:
            agent_id = task.agent_id
            reservation_id = reservation.id
            reservation_status = reservation.status

    if attention_reason:
        await _record_legacy_backfill_attention(record_id, attention_reason)
        return False

    storage = get_storage_backend()
    try:
        entries = await storage.list_dir(agent_storage_key(agent_id, "workspace/videos"))
        candidate: tuple[dict, object] | None = None
        for entry in entries:
            if entry.is_dir or not entry.name.lower().endswith(".json"):
                continue
            try:
                metadata = json.loads(
                    await storage.read_text(entry.key, encoding="utf-8", errors="replace")
                )
            except (json.JSONDecodeError, UnicodeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("reservation_id") or "") != str(reservation_id):
                continue
            candidate = (metadata, entry)
            break
    except Exception as exc:
        await _record_legacy_backfill_retry(record_id, exc)
        return False

    if candidate is None:
        await _record_legacy_backfill_attention(
            record_id,
            "Legacy MiniMax reservation has no matching durable task metadata",
        )
        return False

    metadata, entry = candidate
    if reservation_status != "finalized":
        # Legacy JSON is editable from the Agent workspace. It cannot authorize
        # provider access, credential selection, or Credits settlement. Keep
        # the hold for an operator instead of trusting task_id/credential_id.
        await _record_legacy_backfill_attention(
            record_id,
            "Unverified legacy MiniMax task requires operator reconciliation",
        )
        return False

    provider_task_id = str(metadata.get("task_id") or "").strip()
    prefix = f"{agent_id}/"
    metadata_path = (
        entry.key[len(prefix):]
        if entry.key.startswith(prefix)
        else f"workspace/videos/{entry.name}"
    )
    output_path = _safe_legacy_workspace_video_path(
        metadata.get("downloaded_path") or metadata.get("save_path"),
        provider_task_id,
    )
    output_key = agent_storage_key(agent_id, output_path)
    try:
        output_exists = await storage.exists(output_key) and await storage.is_file(output_key)
    except Exception as exc:
        await _record_legacy_backfill_retry(record_id, exc)
        return False
    if not output_exists:
        await _record_legacy_backfill_attention(
            record_id,
            "Finalized legacy MiniMax task has no usable workspace video asset",
        )
        return False
    request_metadata = {
        key: metadata[key]
        for key in ("credit_cost", "model", "duration", "resolution", "created_at")
        if key in metadata
    }

    try:
        async with async_session() as db:
            task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
            if not task or task.status != "backfill_scanning":
                return False
            task.credential_id = None
            task.provider_task_id = None
            task.status = "succeeded"
            task.metadata_path = metadata_path
            task.output_path = output_path
            task.request_metadata = request_metadata
            task.last_response = None
            task.last_error = None
            task.consecutive_error_count = 0
            task.completed_at = _utcnow()
            task.next_poll_at = None
            await db.commit()
    except IntegrityError:
        attention_reason = "Legacy MiniMax recovery raced another durable owner"

    if attention_reason:
        await _record_legacy_backfill_attention(record_id, attention_reason)
        return False
    logger.info(
        "[media] backfilled finalized legacy MiniMax asset reservation_id={}",
        reservation_id,
    )
    return True


async def backfill_legacy_minimax_video_tasks() -> int:
    """Import a bounded legacy batch without holding DB locks during storage I/O."""
    settings = get_settings()
    limit = max(min(int(settings.MEDIA_GENERATION_BATCH_SIZE), 50), 1)
    await _claim_legacy_minimax_video_placeholders(limit)
    record_ids = await _claim_legacy_minimax_video_scan_ids(limit)
    imported = 0
    for record_id in record_ids:
        if await _backfill_one_legacy_minimax_video_task(record_id):
            imported += 1
    return imported


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
            published = await publish_pending_media_completion_events()
            if backfilled or reconciled or published:
                logger.info(
                    "[media] reconciliation complete backfilled={} reconciled={} published={}",
                    backfilled,
                    reconciled,
                    published,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[media] generation daemon iteration failed")
        await asyncio.sleep(interval)
