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
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.logging_config import get_trace_id
from app.core.security import decrypt_data, encrypt_data
from app.database import async_session
from app.models.activity_log import AgentActivityLog
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditReservation
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    grant_credits_in_session,
    mark_credit_reservation_settlement_ready_in_session,
    release_reserved_credits_in_session,
    reserve_credits_in_session,
)
from app.services.llm.failover import (
    MINIMAX_QUOTA_CODES,
    extract_minimax_code,
)
from app.services.media_assets import (
    MediaContractError,
    apply_image_brand_overlays,
    apply_video_brand_overlays,
    image_asset_from_bytes,
    overlay_blocks_sha256,
    trim_generated_audio,
    valid_mp4,
    validate_generated_audio,
    validate_generated_image,
    validate_image_delivery_contract,
    validate_generated_video,
    validate_video_delivery_contract,
)
from app.services.storage import agent_storage_key, get_storage_backend, normalize_storage_key


ACTIVE_MEDIA_STATUSES = (
    "submitting",
    "submitted",
    "provider_accepted",
    "raw_ready",
    "processing",
    "retrying",
    "downloading",
    "sync_processing",
    "asset_repairing",
    "settlement_ready",
)
TERMINAL_MEDIA_STATUSES = (
    "succeeded",
    "failed",
    "compensated",
    "closed_nonrefundable",
)
UNRESOLVED_MEDIA_STATUSES = tuple(
    dict.fromkeys(
        ACTIVE_MEDIA_STATUSES
        + (
            "submission_ambiguous",
            "asset_delivery_failed",
            "backfill_scanning",
            "backfill_attention",
        )
    )
)


class ProviderTaskIdentityCollision(RuntimeError):
    """A provider task identity was already owned by another security scope."""


class InvalidSyncRecoveryIdentity(ValueError):
    """A legacy synchronous task has unusable private recovery metadata."""


_MEDIA_RECOVERY_PROVIDERS = frozenset({"minimax", "volcengine_agent_plan"})
_SYNC_MEDIA_MODALITIES = {"image", "audio", "music"}
_SYNC_MEDIA_EXTENSIONS = {
    "image": {"bin"},
    "audio": {"mp3", "wav", "flac", "pcm"},
    "music": {"mp3", "wav"},
}
_MAX_SYNC_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_SYNC_AUDIO_BYTES = 16 * 1024 * 1024
_NOTIFICATION_LINK_MAX_LENGTH = 500
_PUBLIC_MEDIA_METADATA_KEYS = frozenset(
    {
        "provider",
        "task_record_id",
        "task_id",
        "save_path",
        "downloaded_path",
        "status",
        "model",
        "tier",
        "prompt",
        "duration",
        "resolution",
        "created_at",
        "completed_at",
        "generation_mode",
        "has_first_frame",
        "has_last_frame",
        "prompt_optimizer",
        "overlay_text",
        "overlay_position",
        "brand_position",
        "brand_scale",
        "output_extension",
        "sample_rate",
        "error",
        "_astra_media_contract",
        "_astra_output_sha256",
    }
)
_PUBLIC_MEDIA_CONTRACT_KEYS = frozenset(
    {
        "rendered_text_sha256",
        "brand_asset_sha256",
        "font_sha256",
        "font_family",
        "font_face_index",
        "font_roles",
        "line_count",
        "background_sanitized",
        "layout_version",
        "block_count",
        "overlay_blocks_sha256",
        "deliverable_request_id",
        "expected_overlay_blocks_sha256",
        "execution_strategy",
        "allow_degraded_fallback",
        "layout_bounds_verified",
        "content_left",
        "content_top",
        "content_right",
        "content_bottom",
        "safe_margin_x",
        "safe_margin_y",
        "source_width",
        "source_height",
        "output_width",
        "output_height",
        "output_bytes",
        "size_adjusted",
        "duration_seconds",
        "requested_duration_seconds",
        "codec_name",
        "sample_rate",
        "channels",
        "container_format",
    }
)


def _public_media_metadata(values: dict) -> dict:
    public = {
        field: value
        for field, value in values.items()
        if field in _PUBLIC_MEDIA_METADATA_KEYS and field != "_astra_media_contract"
    }
    contract = values.get("_astra_media_contract")
    if isinstance(contract, dict):
        public["_astra_media_contract"] = {
            field: value
            for field, value in contract.items()
            if field in _PUBLIC_MEDIA_CONTRACT_KEYS
        }
    return public


def _media_provider_slug(provider: str | None) -> str:
    normalized = str(provider or "minimax").strip().lower()
    if normalized not in _MEDIA_RECOVERY_PROVIDERS:
        raise ValueError("Unsupported media recovery provider")
    return normalized


def _private_video_prefix(provider: str | None) -> str:
    return f"_internal/provider_recovery/{_media_provider_slug(provider)}/video"


def _private_sync_prefix(provider: str | None) -> str:
    return f"_internal/provider_recovery/{_media_provider_slug(provider)}/sync"


def _media_completion_notification_link(
    *,
    agent_id: uuid.UUID,
    output_path: str,
    session_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
) -> str:
    """Build a useful notification link within the database column limit.

    Unicode workspace filenames expand substantially when URL encoded.  The
    completion message already contains the durable artifact link, so a long
    workspace path may be omitted while retaining the exact session/message
    target instead of aborting settlement after the provider succeeded.
    """

    base = f"/agents/{agent_id}/chat"
    query = {"workspace_path": output_path}
    if session_id:
        query["session_id"] = str(session_id)
    if message_id:
        query["message_id"] = str(message_id)
    candidate = f"{base}?{urlencode(query)}"
    if len(candidate) <= _NOTIFICATION_LINK_MAX_LENGTH:
        return candidate

    stable_query = {}
    if session_id:
        stable_query["session_id"] = str(session_id)
    if message_id:
        stable_query["message_id"] = str(message_id)
    if not stable_query:
        return base
    fallback = f"{base}?{urlencode(stable_query)}"
    return fallback if len(fallback) <= _NOTIFICATION_LINK_MAX_LENGTH else base


def minimax_video_brand_asset_key(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    extension: str,
    *,
    provider: str = "minimax",
) -> str:
    """Return an Agent-inaccessible, task-bound brand-asset key."""

    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    if normalized_extension not in {"jpg", "png", "webp"}:
        raise ValueError("Unsupported private video brand asset extension")
    return normalize_storage_key(
        f"{_private_video_prefix(provider)}/{agent_id}/{record_id}/brand.{normalized_extension}"
    )


def minimax_video_provider_identity_evidence_key(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    *,
    provider: str = "minimax",
) -> str:
    return normalize_storage_key(
        f"{_private_video_prefix(provider)}/{agent_id}/{record_id}/provider_identity.json"
    )


def minimax_sync_recovery_asset_key(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    modality: str,
    extension: str,
    *,
    provider: str = "minimax",
) -> str:
    """Return a deterministic private key for a synchronous provider result."""

    normalized_modality = str(modality or "").strip().lower()
    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    if normalized_modality not in _SYNC_MEDIA_MODALITIES:
        raise ValueError("Unsupported synchronous MiniMax modality")
    if normalized_extension not in _SYNC_MEDIA_EXTENSIONS[normalized_modality]:
        raise ValueError("Unsupported synchronous MiniMax recovery extension")
    return normalize_storage_key(
        f"{_private_sync_prefix(provider)}/{normalized_modality}/{agent_id}/{record_id}"
        f"/provider.{normalized_extension}"
    )


def minimax_sync_brand_asset_key(
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    extension: str,
    *,
    provider: str = "minimax",
) -> str:
    """Return a task-bound private key for image post-processing inputs."""

    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    if normalized_extension not in {"jpg", "png", "webp"}:
        raise ValueError("Unsupported private image brand asset extension")
    return normalize_storage_key(
        f"{_private_sync_prefix(provider)}/image/{agent_id}/{record_id}"
        f"/brand.{normalized_extension}"
    )


def _validated_sync_recovery_asset_key(task: MediaGenerationTask) -> str | None:
    metadata = getattr(task, "request_metadata", None) or {}
    raw_key = str(metadata.get("recovery_asset_storage_key") or "")
    if not raw_key:
        return None
    extension = str(metadata.get("recovery_extension") or "")
    try:
        expected = minimax_sync_recovery_asset_key(
            task.agent_id,
            task.id,
            task.modality,
            extension,
            provider=getattr(task, "provider", "minimax"),
        )
    except ValueError as exc:
        raise InvalidSyncRecoveryIdentity(
            "Synchronous media recovery metadata is invalid"
        ) from exc
    normalized_key = normalize_storage_key(raw_key)
    if normalized_key != expected:
        raise MediaContractError(
            "Synchronous media recovery key is outside the task-private namespace"
        )
    return normalized_key


def _validated_sync_brand_asset_key(task: MediaGenerationTask) -> str | None:
    metadata = getattr(task, "request_metadata", None) or {}
    raw_key = str(metadata.get("brand_asset_storage_key") or "")
    if not raw_key:
        return None
    extension = str(metadata.get("brand_asset_extension") or "")
    expected = minimax_sync_brand_asset_key(
        task.agent_id,
        task.id,
        extension,
        provider=getattr(task, "provider", "minimax"),
    )
    normalized_key = normalize_storage_key(raw_key)
    if normalized_key != expected:
        raise MediaContractError(
            "Synchronous image brand asset key is outside the task-private namespace"
        )
    return normalized_key


def _validated_video_brand_asset_key(task: MediaGenerationTask) -> str | None:
    raw_key = str((getattr(task, "request_metadata", None) or {}).get("brand_asset_storage_key") or "")
    if not raw_key:
        return None
    normalized_key = normalize_storage_key(raw_key)
    expected_prefix = normalize_storage_key(
        f"{_private_video_prefix(getattr(task, 'provider', 'minimax'))}/{task.agent_id}/{task.id}/"
    )
    if not normalized_key.startswith(expected_prefix):
        raise MediaContractError("Frozen video brand asset key is outside the task-private namespace")
    return normalized_key


def _validated_video_provider_identity_evidence_key(
    task: MediaGenerationTask,
) -> str | None:
    raw_key = str(
        (getattr(task, "request_metadata", None) or {}).get(
            "provider_identity_evidence_storage_key"
        )
        or ""
    )
    if not raw_key:
        return None
    normalized_key = normalize_storage_key(raw_key)
    expected_key = minimax_video_provider_identity_evidence_key(
        task.agent_id,
        task.id,
        provider=getattr(task, "provider", "minimax"),
    )
    if normalized_key != expected_key:
        raise MediaContractError(
            "MiniMax video provider identity evidence is outside the task-private namespace"
        )
    return normalized_key


async def store_minimax_video_provider_identity_evidence(
    *,
    record_id: uuid.UUID,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    credential_id: uuid.UUID,
    model: str,
    provider_task_id: str,
    provider: str = "minimax",
) -> str:
    """Freeze an accepted video provider identity before its SQL attachment.

    This write intentionally needs no database read: it remains possible while
    the SQL attachment path is unavailable after the paid provider side effect.
    """

    normalized_provider_task_id = str(provider_task_id or "").strip()
    if not normalized_provider_task_id:
        raise ValueError("Provider task identity is empty")
    normalized_provider = _media_provider_slug(provider)
    key = minimax_video_provider_identity_evidence_key(
        agent_id,
        record_id,
        provider=normalized_provider,
    )
    plaintext = json.dumps(
        {
            "provider": normalized_provider,
            "record_id": str(record_id),
            "agent_id": str(agent_id),
            "tenant_id": str(tenant_id) if tenant_id else "",
            "credential_id": str(credential_id),
            "model": str(model or ""),
            "provider_task_id": normalized_provider_task_id,
            "stored_at": _utcnow().isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    ciphertext = encrypt_data(plaintext, get_settings().SECRET_KEY)
    await get_storage_backend().write_bytes(
        key,
        json.dumps(
            {"version": 1, "ciphertext": ciphertext},
            separators=(",", ":"),
        ).encode("utf-8"),
        content_type="application/json",
    )
    return key


async def _load_minimax_video_provider_identity_evidence(
    key: str,
    *,
    provider: str = "minimax",
) -> dict[str, object]:
    normalized_key = normalize_storage_key(key)
    normalized_provider = _media_provider_slug(provider)
    if not normalized_key.startswith(f"{_private_video_prefix(normalized_provider)}/"):
        raise MediaContractError(
            "MiniMax video provider identity evidence is outside the private namespace"
        )
    envelope = json.loads(
        (await get_storage_backend().read_bytes(normalized_key)).decode("utf-8")
    )
    if envelope.get("version") != 1 or not isinstance(
        envelope.get("ciphertext"),
        str,
    ):
        raise MediaContractError(
            "MiniMax video provider identity evidence envelope is invalid"
        )
    plaintext = decrypt_data(
        envelope["ciphertext"],
        get_settings().SECRET_KEY,
    )
    payload = json.loads(plaintext)
    if not isinstance(payload, dict) or payload.get("provider") != normalized_provider:
        raise MediaContractError(
            "MiniMax video provider identity evidence payload is invalid"
        )
    return payload


async def _delete_minimax_video_provider_identity_evidence(
    task: MediaGenerationTask,
    *,
    strict: bool = False,
) -> bool:
    try:
        key = _validated_video_provider_identity_evidence_key(task)
        if not key:
            return False
        await get_storage_backend().delete(key)
        async with async_session() as db:
            current = await db.get(MediaGenerationTask, task.id, with_for_update=True)
            if not current:
                return True
            metadata = dict(current.request_metadata or {})
            if metadata.get("provider_identity_evidence_storage_key") == key:
                metadata.pop("provider_identity_evidence_storage_key", None)
                metadata["provider_identity_evidence_deleted_at"] = (
                    _utcnow().isoformat()
                )
                current.request_metadata = metadata
                await db.commit()
        return True
    except Exception:
        logger.exception(
            "[media] private video provider identity evidence cleanup failed task_id={}",
            task.id,
        )
        if strict:
            raise
    return False


async def _cleanup_private_video_recovery_assets(
    task: MediaGenerationTask,
    *,
    strict: bool = False,
    retention_expired_at: datetime | None = None,
) -> int:
    """Delete task-owned private video recovery objects and acknowledge each."""

    try:
        entries = _private_media_recovery_entries(task)
    except Exception:
        logger.exception(
            "[media] invalid private video recovery metadata task_id={}",
            task.id,
        )
        if strict:
            raise
        return 0

    deleted = 0
    storage = get_storage_backend()
    for metadata_key, key in entries:
        try:
            await storage.delete(key)
            async with async_session() as db:
                current = await db.get(
                    MediaGenerationTask,
                    task.id,
                    with_for_update=True,
                )
                if not current:
                    deleted += 1
                    continue
                metadata = dict(current.request_metadata or {})
                if metadata.get(metadata_key) != key:
                    continue
                metadata.pop(metadata_key, None)
                if metadata_key == "brand_asset_storage_key":
                    metadata["brand_asset_deleted_at"] = _utcnow().isoformat()
                    if retention_expired_at:
                        metadata["brand_asset_retention_expired_at"] = (
                            retention_expired_at.isoformat()
                        )
                else:
                    metadata["provider_identity_evidence_deleted_at"] = (
                        _utcnow().isoformat()
                    )
                current.request_metadata = metadata
                await db.commit()
                deleted += 1
        except Exception:
            logger.exception(
                "[media] private video recovery cleanup failed task_id={} key_type={}",
                task.id,
                metadata_key,
            )
            if strict:
                raise
    return deleted


def _private_media_recovery_entries(
    task: MediaGenerationTask,
) -> list[tuple[str, str]]:
    metadata = task.request_metadata or {}
    entries: list[tuple[str, str]] = []
    modality = str(getattr(task, "modality", "video") or "video")
    if modality == "video":
        key = _validated_video_brand_asset_key(task)
        if key:
            entries.append(("brand_asset_storage_key", key))
        evidence_key = _validated_video_provider_identity_evidence_key(task)
        if evidence_key:
            entries.append(
                ("provider_identity_evidence_storage_key", evidence_key)
            )
        return entries
    if modality not in _SYNC_MEDIA_MODALITIES:
        return entries
    raw_key = _validated_sync_recovery_asset_key(task)
    if raw_key:
        entries.append(("recovery_asset_storage_key", raw_key))
    brand_key = _validated_sync_brand_asset_key(task)
    if brand_key:
        entries.append(("brand_asset_storage_key", brand_key))
    evidence_key = str(metadata.get("acceptance_evidence_storage_key") or "")
    if evidence_key:
        normalized_evidence_key = normalize_storage_key(evidence_key)
        expected_evidence_key = normalize_storage_key(
            f"_internal/provider_recovery/{_media_provider_slug(getattr(task, 'provider', 'minimax'))}"
            f"/image/{task.agent_id}/{task.id}.json"
        )
        if modality != "image" or normalized_evidence_key != expected_evidence_key:
            raise MediaContractError(
                "MiniMax image evidence is outside the task-private namespace"
            )
        entries.append(("acceptance_evidence_storage_key", normalized_evidence_key))
    return entries


async def delete_private_media_recovery_assets_for_agent(
    agent_id: uuid.UUID,
) -> int:
    """Durably request, then strictly delete every private Agent media asset.

    Object storage cannot participate in the Agent-deletion SQL transaction.
    Persisting the intent first makes a storage success non-rollbackable by
    design and leaves a retryable tombstone if the process or database fails
    between object deletion and final metadata acknowledgement.
    """

    deletion_request_id = str(uuid.uuid4())
    requested_at = _utcnow().isoformat()
    pending: list[tuple[uuid.UUID, str, str]] = []
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask)
            .where(MediaGenerationTask.agent_id == agent_id)
            .order_by(MediaGenerationTask.id)
            .with_for_update()
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            entries = _private_media_recovery_entries(task)
            if not entries:
                continue
            metadata = dict(task.request_metadata or {})
            metadata.update(
                {
                    "media_recovery_delete_requested_at": requested_at,
                    "media_recovery_delete_request_id": deletion_request_id,
                    "media_recovery_delete_reason": "agent_deletion",
                }
            )
            if any(metadata_key == "brand_asset_storage_key" for metadata_key, _key in entries):
                # Preserve the existing video cleanup audit keys for operators
                # and backwards-compatible incident tooling.
                metadata.update(
                    {
                        "brand_asset_delete_requested_at": requested_at,
                        "brand_asset_delete_request_id": deletion_request_id,
                        "brand_asset_delete_reason": "agent_deletion",
                    }
                )
            task.request_metadata = metadata
            pending.extend(
                (task.id, metadata_key, key)
                for metadata_key, key in entries
            )
        if pending:
            await db.commit()

    storage = get_storage_backend()
    deleted = 0
    for task_id, metadata_key, key in pending:
        await storage.delete(key)
        async with async_session() as db:
            task = await db.get(MediaGenerationTask, task_id, with_for_update=True)
            if task is None or task.agent_id != agent_id:
                raise MediaContractError(
                    "Private brand asset owner changed during Agent deletion"
                )
            metadata = dict(task.request_metadata or {})
            if (
                metadata.get(metadata_key) != key
                or metadata.get("media_recovery_delete_request_id")
                != deletion_request_id
            ):
                raise MediaContractError(
                    "Private media asset deletion intent changed"
                )
            metadata.pop(metadata_key, None)
            deleted_audit_key = {
                "brand_asset_storage_key": "brand_asset_deleted_with_agent_at",
                "recovery_asset_storage_key": "recovery_asset_deleted_with_agent_at",
                "acceptance_evidence_storage_key": "acceptance_evidence_deleted_with_agent_at",
                "provider_identity_evidence_storage_key": (
                    "provider_identity_evidence_deleted_with_agent_at"
                ),
            }.get(metadata_key, f"{metadata_key}_deleted_with_agent_at")
            metadata[deleted_audit_key] = _utcnow().isoformat()
            task.request_metadata = metadata
            await db.commit()
            deleted += 1
    return deleted


async def delete_private_video_brand_assets_for_agent(
    agent_id: uuid.UUID,
) -> int:
    """Backward-compatible alias for the unified Agent deletion fence."""

    return await delete_private_media_recovery_assets_for_agent(agent_id)


async def cleanup_expired_video_brand_assets(
    *,
    now: datetime | None = None,
) -> int:
    """Expire frozen success-recovery assets after the configured privacy TTL."""
    settings = get_settings()
    retention_days = max(int(settings.MEDIA_GENERATION_BRAND_RECOVERY_RETENTION_DAYS), 1)
    current_time = now or _utcnow()
    cutoff = current_time - timedelta(days=retention_days)
    limit = max(min(int(settings.MEDIA_GENERATION_BATCH_SIZE), 100), 1)
    cleaned = 0
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask.id)
            .where(
                MediaGenerationTask.modality == "video",
                MediaGenerationTask.completed_at.is_not(None),
                or_(
                    and_(
                        MediaGenerationTask.status == "succeeded",
                        MediaGenerationTask.completed_at <= cutoff,
                    ),
                    and_(
                        MediaGenerationTask.status.in_(
                            ("failed", "compensated", "closed_nonrefundable")
                        ),
                        MediaGenerationTask.completed_at <= current_time,
                    ),
                ),
                or_(
                    and_(
                        MediaGenerationTask.request_metadata[
                            "brand_asset_storage_key"
                        ].as_string().is_not(None),
                        MediaGenerationTask.request_metadata[
                            "brand_asset_storage_key"
                        ].as_string()
                        != "",
                    ),
                    and_(
                        MediaGenerationTask.request_metadata[
                            "provider_identity_evidence_storage_key"
                        ].as_string().is_not(None),
                        MediaGenerationTask.request_metadata[
                            "provider_identity_evidence_storage_key"
                        ].as_string()
                        != "",
                    ),
                ),
            )
            .order_by(MediaGenerationTask.completed_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        task_ids = [row[0] for row in result.all()]

    for task_id in task_ids:
        task = await _load_task(task_id)
        if not task:
            continue
        if task.status == "succeeded" and not await _stored_media_output_is_usable(task):
            await _begin_missing_asset_repair(task.id)
            continue
        if task.status not in {
            "succeeded",
            "failed",
            "compensated",
            "closed_nonrefundable",
        }:
            continue
        try:
            deleted = await _cleanup_private_video_recovery_assets(
                task,
                strict=True,
                retention_expired_at=current_time,
            )
        except Exception:
            continue
        if deleted:
            cleaned += 1
    return cleaned


async def cleanup_expired_sync_recovery_assets(
    *,
    now: datetime | None = None,
) -> int:
    """Delete private sync raw/brand assets after the recovery retention TTL."""

    current_time = now or _utcnow()
    retention_days = max(
        int(get_settings().MEDIA_GENERATION_BRAND_RECOVERY_RETENTION_DAYS),
        1,
    )
    cutoff = current_time - timedelta(days=retention_days)
    limit = max(min(int(get_settings().MEDIA_GENERATION_BATCH_SIZE), 100), 1)
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask.id)
            .where(
                MediaGenerationTask.modality.in_(tuple(_SYNC_MEDIA_MODALITIES)),
                MediaGenerationTask.completed_at.is_not(None),
                or_(
                    and_(
                        MediaGenerationTask.status == "succeeded",
                        MediaGenerationTask.completed_at <= cutoff,
                    ),
                    and_(
                        MediaGenerationTask.status.in_(
                            ("failed", "compensated", "closed_nonrefundable")
                        ),
                        MediaGenerationTask.completed_at <= current_time,
                    ),
                ),
                or_(
                    MediaGenerationTask.request_metadata[
                        "recovery_asset_storage_key"
                    ].as_string().is_not(None),
                    MediaGenerationTask.request_metadata[
                        "brand_asset_storage_key"
                    ].as_string().is_not(None),
                    MediaGenerationTask.request_metadata[
                        "acceptance_evidence_storage_key"
                    ].as_string().is_not(None),
                ),
            )
            .order_by(MediaGenerationTask.completed_at.asc())
            .limit(limit)
        )
        task_ids = [row[0] for row in result.all()]

    cleaned = 0
    for task_id in task_ids:
        task = await _load_task(task_id)
        if not task:
            continue
        if task.status == "succeeded" and not await _stored_media_output_is_usable(task):
            await _begin_missing_asset_repair(task.id)
            continue
        if task.status not in {
            "succeeded",
            "failed",
            "compensated",
            "closed_nonrefundable",
        }:
            continue
        before = task.request_metadata or {}
        had_private_assets = bool(
            before.get("recovery_asset_storage_key")
            or before.get("brand_asset_storage_key")
            or before.get("acceptance_evidence_storage_key")
        )
        if not had_private_assets:
            continue
        await _cleanup_sync_private_assets(task, delete_recovery_assets=True)
        refreshed = await _load_task(task_id)
        remaining = (refreshed.request_metadata if refreshed else {}) or {}
        if (
            not remaining.get("recovery_asset_storage_key")
            and not remaining.get("brand_asset_storage_key")
            and not remaining.get("acceptance_evidence_storage_key")
        ):
            cleaned += 1
    return cleaned


@dataclass(slots=True, frozen=True)
class MediaGenerationOutcome:
    status: str
    output_path: str | None = None
    error: str | None = None
    retryable: bool = False


@dataclass(slots=True, frozen=True)
class _MediaCompensationAttempt:
    outcome: str
    task: MediaGenerationTask

    @property
    def compensated(self) -> bool:
        return self.outcome == "compensated"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider_status(data: dict) -> str:
    return str(data.get("status") or (data.get("data") or {}).get("status") or "Unknown")


def _valid_mp4(data: bytes) -> bool:
    return valid_mp4(data)


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:1000]


async def _lock_owned_media_reservation(
    db,
    task: MediaGenerationTask,
    *,
    with_for_update: bool = True,
) -> CreditReservation:
    """Lock and verify the exact Credits hold owned by a media task."""

    if not task.reservation_id:
        raise MediaContractError("Media task has no Credits reservation")
    reservation = await db.get(
        CreditReservation,
        task.reservation_id,
        with_for_update=with_for_update,
    )
    if not reservation:
        raise MediaContractError("Media Credits reservation is unavailable")
    if (
        reservation.tenant_id != task.tenant_id
        or reservation.agent_id != task.agent_id
        or reservation.user_id != task.user_id
        or reservation.ref_type != "media_task"
        or reservation.ref_id != task.id
    ):
        raise MediaContractError("Media Credits reservation ownership is invalid")
    return reservation


def _media_download_url(agent_id: uuid.UUID, output_path: str) -> str:
    query = urlencode({"path": output_path, "inline": "1"})
    return f"/api/agents/{agent_id}/files/download?{query}"


def _media_labels(task: MediaGenerationTask) -> tuple[str, str, str]:
    labels = {
        "image": ("图片", "Image", "🖼️ 查看图片"),
        "audio": ("语音", "Audio", "🔊 播放音频"),
        "music": ("音乐", "Music", "🎵 播放音乐"),
        "video": ("视频", "Video", "▶️ 播放视频"),
    }
    return labels.get(
        str(getattr(task, "modality", "video") or "video").lower(),
        ("媒体", "Media", "查看媒体"),
    )


def _media_completion_content(task: MediaGenerationTask) -> str:
    chinese_label, _english_label, action_label = _media_labels(task)
    filename = Path(task.output_path).name or "media"
    return (
        f"✅ {chinese_label}生成完成：{filename}\n"
        f"保存位置：{task.output_path}\n\n"
        f"{action_label}：\n![]({_media_download_url(task.agent_id, task.output_path)})"
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


async def create_minimax_sync_media_task_record(
    *,
    record_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    credential_id: uuid.UUID,
    origin_session_id: str | uuid.UUID | None,
    modality: str,
    tier: str,
    model: str,
    credit_cost: int,
    output_path: str,
    request_metadata: dict,
    provider: str = "minimax",
) -> MediaGenerationTask:
    """Atomically persist a sync media task and its pre-provider Credits hold."""

    normalized_modality = str(modality or "").strip().lower()
    if normalized_modality not in _SYNC_MEDIA_MODALITIES:
        raise ValueError("Unsupported synchronous MiniMax modality")
    normalized_provider = _media_provider_slug(provider)
    metadata = dict(request_metadata or {})
    recovery_extension = str(metadata.get("recovery_extension") or "").lower()
    # Validate the deterministic recovery namespace before reserving Credits.
    recovery_key = minimax_sync_recovery_asset_key(
        agent_id,
        record_id,
        normalized_modality,
        recovery_extension,
        provider=normalized_provider,
    )
    metadata["recovery_asset_storage_key"] = recovery_key
    if normalized_modality == "image":
        metadata["acceptance_evidence_storage_key"] = normalize_storage_key(
            f"_internal/provider_recovery/{normalized_provider}/image/{agent_id}/{record_id}.json"
        )
    metadata["credit_cost"] = max(int(credit_cost or 0), 0)
    metadata["tier"] = str(tier or "").strip().lower()

    async with async_session() as db:
        validated_session_id = await _validated_origin_session_id(
            db,
            origin_session_id=origin_session_id,
            agent_id=agent_id,
            user_id=user_id,
        )
        reservation = None
        if metadata["credit_cost"] > 0:
            reservation = await reserve_credits_in_session(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                action=normalized_modality,
                modality=normalized_modality,
                saas_tier=metadata["tier"],
                provider=normalized_provider,
                model=model,
                amount=metadata["credit_cost"],
                ref_type="media_task",
                ref_id=record_id,
                initial_status="provider_inflight",
            )
            await db.flush()
        task = MediaGenerationTask(
            id=record_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            credential_id=credential_id,
            reservation_id=reservation.id if reservation else None,
            origin_session_id=validated_session_id,
            provider=normalized_provider,
            modality=normalized_modality,
            model=model,
            status="submitting",
            metadata_path=(
                f"workspace/media_tasks/{normalized_provider}_{normalized_modality}_{record_id}.json"
            ),
            output_path=output_path,
            request_metadata=metadata,
            completion_delivery_status="pending",
            next_poll_at=_utcnow()
            + timedelta(
                seconds=max(
                    int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
                    60,
                )
            ),
        )
        db.add(task)
        await db.commit()
        return task


async def store_minimax_sync_brand_asset(
    record_id: uuid.UUID,
    raw: bytes,
    *,
    extension: str,
) -> str:
    """Freeze an immutable image overlay before starting paid provider work."""

    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id)
        if not task or task.modality != "image" or not task.agent_id:
            raise ValueError("Synchronous image task not found")
        key = _validated_sync_brand_asset_key(task)
        if not key:
            raise MediaContractError("Synchronous image brand asset intent is missing")
        expected_key = minimax_sync_brand_asset_key(
            task.agent_id,
            task.id,
            extension,
            provider=getattr(task, "provider", "minimax"),
        )
        if key != expected_key:
            raise MediaContractError("Synchronous image brand asset identity changed")
    asset = image_asset_from_bytes(raw, label="Frozen image brand asset")
    await get_storage_backend().write_bytes(key, raw, content_type=asset.mime_type)
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task or task.modality != "image":
            raise ValueError("Synchronous image task not found")
        metadata = dict(task.request_metadata or {})
        if _validated_sync_brand_asset_key(task) != key:
            raise MediaContractError("Synchronous image brand asset identity changed")
        expected_sha256 = str(metadata.get("brand_asset_sha256") or "")
        if not expected_sha256 or not hmac.compare_digest(expected_sha256, asset.sha256):
            raise MediaContractError("Synchronous image brand asset hash changed")
        metadata["brand_asset_stored_at"] = _utcnow().isoformat()
        task.request_metadata = metadata
        await db.commit()
    return key


async def mark_minimax_sync_provider_accepted(
    record_id: uuid.UUID,
    *,
    evidence_key: str | None = None,
    accepted_metadata: dict | None = None,
) -> MediaGenerationTask:
    """Persist provider acceptance and the exact settlement debt in one DB txn."""

    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status in TERMINAL_MEDIA_STATUSES:
            return task
        if task.modality not in _SYNC_MEDIA_MODALITIES:
            raise ValueError("Media generation task is not synchronous")
        metadata = dict(task.request_metadata or {})
        if evidence_key:
            normalized_evidence_key = normalize_storage_key(evidence_key)
            expected_evidence_key = normalize_storage_key(
                f"_internal/provider_recovery/{_media_provider_slug(getattr(task, 'provider', 'minimax'))}"
                f"/image/{task.agent_id}/{task.id}.json"
            )
            if task.modality != "image" or normalized_evidence_key != expected_evidence_key:
                raise MediaContractError(
                    "MiniMax image evidence is outside the task-private namespace"
                )
            metadata["acceptance_evidence_storage_key"] = normalized_evidence_key
        accepted_at = _utcnow()
        capture_grace_seconds = max(
            int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
            60,
        )
        capture_deadline = accepted_at + timedelta(seconds=capture_grace_seconds)
        metadata["provider_accepted_at"] = metadata.get(
            "provider_accepted_at"
        ) or accepted_at.isoformat()
        metadata["raw_capture_deadline_at"] = metadata.get(
            "raw_capture_deadline_at"
        ) or capture_deadline.isoformat()
        task.request_metadata = metadata
        if task.reservation_id:
            reservation = await _lock_owned_media_reservation(db, task)
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                reservation.id,
                amount=int(reservation.amount),
            )
        task.status = "provider_accepted"
        task.completed_at = None
        task.last_response = dict(accepted_metadata or {"status": "Accepted"})
        task.last_error = None
        task.consecutive_error_count = 0
        task.last_checked_at = accepted_at
        # The provider callback runs before large audio payloads are decoded
        # and before any modality writes its raw recovery object.  Do not let a
        # daemon compensate the paid request inside that capture window.
        task.next_poll_at = capture_deadline
        await db.commit()
        return task


async def record_minimax_sync_provider_response_retry(
    record_id: uuid.UUID,
    error: BaseException,
) -> MediaGenerationTask:
    """Keep a witnessed successful sync response on the automatic recovery path.

    This transition is intentionally separate from ``submission_ambiguous``:
    the caller must have observed a successful provider response, while the
    durable acceptance/raw metadata transaction did not complete.  Predeclared
    private object keys then let the daemon repair an object-first/DB-second
    crash without guessing that every ambiguous submission was accepted.
    """

    now = _utcnow()
    capture_grace_seconds = max(
        int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
        60,
    )
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.modality not in _SYNC_MEDIA_MODALITIES:
            raise ValueError("Media generation task is not synchronous")
        if task.status in TERMINAL_MEDIA_STATUSES or task.status in {
            "raw_ready",
            "settlement_ready",
        }:
            return task

        metadata = dict(task.request_metadata or {})
        metadata["provider_accepted_at"] = metadata.get(
            "provider_accepted_at"
        ) or now.isoformat()
        metadata["raw_capture_deadline_at"] = metadata.get(
            "raw_capture_deadline_at"
        ) or (now + timedelta(seconds=capture_grace_seconds)).isoformat()
        task.request_metadata = metadata

        if task.reservation_id:
            reservation = await _lock_owned_media_reservation(db, task)
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                reservation.id,
                amount=int(reservation.amount or 0),
            )

        task.status = "asset_repairing"
        task.completed_at = None
        task.last_response = {
            "status": "Accepted",
            "acceptance_record_recovery": True,
        }
        task.last_error = _safe_error(error)
        task.last_checked_at = now
        task.next_poll_at = now
        await db.commit()
        return task


async def store_minimax_sync_recovery_asset(
    record_id: uuid.UUID,
    raw: bytes,
    *,
    content_type: str,
    expected_key: str | None = None,
) -> str:
    """Durably store paid provider bytes before validation or final delivery."""

    if not raw:
        raise MediaContractError("MiniMax recovery asset is empty")
    if expected_key:
        key = normalize_storage_key(expected_key)
    else:
        async with async_session() as db:
            task = await db.get(MediaGenerationTask, record_id)
            if not task or task.modality not in _SYNC_MEDIA_MODALITIES or not task.agent_id:
                raise ValueError("Synchronous media task not found")
            metadata = dict(task.request_metadata or {})
            key = minimax_sync_recovery_asset_key(
                task.agent_id,
                task.id,
                task.modality,
                str(metadata.get("recovery_extension") or ""),
                provider=getattr(task, "provider", "minimax"),
            )
    await get_storage_backend().write_bytes(key, raw, content_type=content_type)
    digest = hashlib.sha256(raw).hexdigest()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Synchronous media task not found")
        canonical_key = _validated_sync_recovery_asset_key(task)
        if not canonical_key or canonical_key != key:
            raise MediaContractError("Synchronous media recovery identity changed")
        if task.status in TERMINAL_MEDIA_STATUSES:
            if task.status != "succeeded":
                # A terminal transition may have won the race after the object
                # write.  Remove the unowned raw object instead of leaking it.
                try:
                    await get_storage_backend().delete(key)
                except Exception:
                    logger.exception(
                        "[media] terminal sync raw cleanup failed task_id={}",
                        record_id,
                    )
                raise MediaContractError(
                    "Synchronous media task closed before raw capture completed"
                )
            return key
        metadata = dict(task.request_metadata or {})
        accepted_at = _utcnow()
        capture_grace_seconds = max(
            int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
            60,
        )
        metadata.update(
            {
                "recovery_asset_sha256": digest,
                "recovery_asset_size": len(raw),
                "recovery_content_type": str(content_type or ""),
                "recovery_stored_at": accepted_at.isoformat(),
                "provider_accepted_at": metadata.get("provider_accepted_at")
                or accepted_at.isoformat(),
                "raw_capture_deadline_at": metadata.get("raw_capture_deadline_at")
                or (accepted_at + timedelta(seconds=capture_grace_seconds)).isoformat(),
            }
        )
        if task.reservation_id:
            reservation = await _lock_owned_media_reservation(db, task)
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                reservation.id,
                amount=int(reservation.amount or 0),
            )
        task.request_metadata = metadata
        task.status = "raw_ready"
        task.output_size = len(raw)
        task.last_error = None
        task.last_checked_at = accepted_at
        task.next_poll_at = accepted_at
        await db.commit()
    return key


def _media_task_age(task: MediaGenerationTask) -> timedelta:
    created_at = task.created_at or _utcnow()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return _utcnow() - created_at


def _media_task_expiry_reason(task: MediaGenerationTask) -> str | None:
    max_age = max(int(get_settings().MEDIA_GENERATION_MAX_AGE_SECONDS), 3600)
    if _media_task_age(task) > timedelta(seconds=max_age):
        modality = str(getattr(task, "modality", "video") or "video")
        return f"MiniMax {modality} task exceeded the {max_age}-second recovery window"
    return None


def _sync_raw_capture_deadline(task: MediaGenerationTask) -> datetime:
    """Return the paid-response grace deadline, including legacy-row fallback."""

    metadata = task.request_metadata or {}
    raw_deadline = str(metadata.get("raw_capture_deadline_at") or "").strip()
    if raw_deadline:
        try:
            deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return deadline
        except ValueError:
            logger.warning(
                "[media] invalid raw capture deadline task_id={}",
                task.id,
            )
    accepted_at = str(metadata.get("provider_accepted_at") or "").strip()
    if accepted_at:
        try:
            base = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        except ValueError:
            base = task.created_at or _utcnow()
    else:
        base = task.created_at or _utcnow()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    grace_seconds = max(
        int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
        60,
    )
    return base + timedelta(seconds=grace_seconds)


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
    origin_session_id: str | uuid.UUID | None,
    model: str,
    tier: str,
    credit_cost: int,
    metadata_path: str,
    output_path: str,
    request_metadata: dict,
    provider: str = "minimax",
) -> MediaGenerationTask:
    """Create the durable row before asking the paid provider to start work."""
    normalized_provider = _media_provider_slug(provider)
    metadata = dict(request_metadata or {})
    metadata["provider_identity_evidence_storage_key"] = (
        minimax_video_provider_identity_evidence_key(
            agent_id,
            record_id,
            provider=normalized_provider,
        )
    )
    async with async_session() as db:
        validated_session_id = await _validated_origin_session_id(
            db,
            origin_session_id=origin_session_id,
            agent_id=agent_id,
            user_id=user_id,
        )
        reservation = None
        if int(credit_cost or 0) > 0:
            reservation = await reserve_credits_in_session(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                action="video",
                modality="video",
                saas_tier=str(tier or "").strip().lower(),
                provider=normalized_provider,
                model=model,
                amount=int(credit_cost),
                ref_type="media_task",
                ref_id=record_id,
                initial_status="provider_inflight",
            )
            await db.flush()
        task = MediaGenerationTask(
            id=record_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            credential_id=credential_id,
            reservation_id=reservation.id if reservation else None,
            origin_session_id=validated_session_id,
            provider=normalized_provider,
            modality="video",
            model=model,
            status="submitting",
            metadata_path=metadata_path,
            output_path=output_path,
            request_metadata=metadata,
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
                        MediaGenerationTask.provider
                        == getattr(task, "provider", "minimax"),
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
                    task.completed_at = None
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
    # SQL now owns the provider identity. Evidence cleanup is deliberately
    # best-effort and cannot roll back that durable attachment.
    await _delete_minimax_video_provider_identity_evidence(attached_task)
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


async def _recover_minimax_video_provider_identity(
    task: MediaGenerationTask,
) -> MediaGenerationTask:
    """Attach a paid provider task from task-bound encrypted evidence."""

    if task.provider_task_id:
        return task
    evidence_key = _validated_video_provider_identity_evidence_key(task)
    if not evidence_key:
        return task
    storage = get_storage_backend()
    if not await storage.exists(evidence_key):
        return task
    evidence = await _load_minimax_video_provider_identity_evidence(
        evidence_key,
        provider=getattr(task, "provider", "minimax"),
    )
    expected = {
        "provider": getattr(task, "provider", "minimax"),
        "record_id": str(task.id),
        "agent_id": str(task.agent_id),
        "tenant_id": str(task.tenant_id) if task.tenant_id else "",
        "credential_id": str(task.credential_id) if task.credential_id else "",
        "model": str(task.model or ""),
    }
    for field, expected_value in expected.items():
        actual_value = str(evidence.get(field) or "")
        if not hmac.compare_digest(actual_value, expected_value):
            raise MediaContractError(
                f"MiniMax video provider identity evidence changed field {field}"
            )
    provider_task_id = str(evidence.get("provider_task_id") or "").strip()
    if not provider_task_id:
        raise MediaContractError(
            "MiniMax video provider identity evidence has no provider task id"
        )
    metadata = {
        **dict(task.request_metadata or {}),
        "provider": getattr(task, "provider", "minimax"),
        "task_record_id": str(task.id),
        "task_id": provider_task_id,
        "status": "submitted",
        "model": task.model,
        "save_path": task.output_path,
    }
    canonical_record_id = await mark_minimax_video_task_submitted(
        task.id,
        provider_task_id=provider_task_id,
        metadata=metadata,
    )
    recovered = await _load_task(canonical_record_id)
    if not recovered:
        raise ValueError("Recovered MiniMax video task is unavailable")
    return recovered


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
            reservation = await _lock_owned_media_reservation(db, task)
            await release_reserved_credits_in_session(
                db,
                reservation.id,
                release_provider_inflight=True,
            )
        task.status = "failed"
        task.last_error = _safe_error(error)
        task.completed_at = _utcnow()
        task.next_poll_at = None
        await db.commit()
    await _delete_private_media_recovery_assets(task)
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
            chinese_label, _english_label, _action_label = _media_labels(task)
            db.add(Notification(
                user_id=task.user_id,
                agent_id=task.agent_id,
                type="system",
                title=f"{chinese_label}任务提交结果待核对",
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
            reservation = await _lock_owned_media_reservation(db, task)
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
    *,
    expected_working_status: str,
    processing_lease_token: str,
    retryable: bool = True,
) -> tuple[bool, MediaGenerationTask]:
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
            return False, task
        metadata = dict(task.request_metadata or {})
        if (
            task.status != expected_working_status
            or not processing_lease_token
            or metadata.get("processing_lease_token") != processing_lease_token
        ):
            # A newer worker owns the task. The stale worker must not change
            # task state, Credits, retry counters, or public metadata.
            return False, task

        if task.reservation_id:
            reservation = await _lock_owned_media_reservation(db, task)
            exact_amount = int(reservation.amount)
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                reservation.id,
                amount=exact_amount,
            )

        task.consecutive_error_count = (task.consecutive_error_count or 0) + 1
        max_errors = max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1)
        task.last_response = status_data
        task.last_error = (
            f"Provider succeeded; local asset delivery failed: {_safe_error(error)}"
        )[:1000]
        task.last_checked_at = _utcnow()
        if not retryable or task.consecutive_error_count >= max_errors:
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

    # ``asset_repairing`` is a bounded internal recovery state, not one new
    # production incident per poll.  Persist a production event only when the
    # task actually reaches the manual-reconciliation state.  This keeps the
    # incident count aligned with affected tasks instead of retry attempts.
    if task.status == "asset_delivery_failed":
        await _record_media_failure_issue(task, task.last_error or "Asset delivery failed")
    return True, task


async def find_media_generation_task(
    *,
    agent_id: uuid.UUID,
    provider_task_id: str,
    provider: str | None = None,
) -> MediaGenerationTask | None:
    async with async_session() as db:
        conditions = [
            MediaGenerationTask.agent_id == agent_id,
            MediaGenerationTask.provider_task_id == provider_task_id,
        ]
        if provider:
            conditions.append(
                MediaGenerationTask.provider == _media_provider_slug(provider)
            )
        result = await db.execute(
            select(MediaGenerationTask).where(*conditions)
        )
        return result.scalars().first()


async def find_media_generation_task_by_id(
    *,
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    modality: str = "video",
) -> MediaGenerationTask | None:
    """Load one durable media task without trusting editable workspace metadata."""

    normalized_modality = str(modality or "").strip().lower()
    if normalized_modality not in {*_SYNC_MEDIA_MODALITIES, "video"}:
        return None
    async with async_session() as db:
        result = await db.execute(
            select(MediaGenerationTask).where(
                MediaGenerationTask.id == record_id,
                MediaGenerationTask.agent_id == agent_id,
                MediaGenerationTask.provider.in_(_MEDIA_RECOVERY_PROVIDERS),
                MediaGenerationTask.modality == normalized_modality,
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
    await _delete_private_media_recovery_assets(task)


async def _record_sync_recovery_retry(
    record_id: uuid.UUID,
    error: BaseException,
    *,
    expected_working_status: str | None = None,
    processing_lease_token: str | None = None,
) -> tuple[bool, MediaGenerationTask]:
    """Keep accepted synchronous provider work recoverable until its hard TTL."""

    settings = get_settings()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status in TERMINAL_MEDIA_STATUSES:
            return False, task
        if expected_working_status is not None:
            metadata = dict(task.request_metadata or {})
            if task.status != expected_working_status:
                # The lease was replaced while this worker was processing.
                # Do not let its late failure disturb the current owner.
                return False, task
            if expected_working_status == "sync_processing" and (
                not processing_lease_token
                or metadata.get("processing_lease_token")
                != processing_lease_token
            ):
                return False, task
        task.status = "asset_repairing"
        metadata = dict(task.request_metadata or {})
        metadata.pop("processing_lease_token", None)
        task.request_metadata = metadata
        task.consecutive_error_count = (task.consecutive_error_count or 0) + 1
        task.last_error = _safe_error(error)
        task.last_checked_at = _utcnow()
        backoff = min(
            max(int(settings.MEDIA_GENERATION_POLL_INTERVAL_SECONDS), 5)
            * (2 ** min(task.consecutive_error_count, 7)),
            1800,
        )
        task.next_poll_at = _utcnow() + timedelta(seconds=backoff)
        should_alert = task.consecutive_error_count in {
            max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1),
            max(int(settings.MEDIA_GENERATION_MAX_CONSECUTIVE_ERRORS), 1) * 2,
        }
        await db.commit()
    if should_alert:
        await _record_media_failure_issue(
            task,
            f"Accepted synchronous media task still needs recovery: {task.last_error}",
        )
    return True, task


async def _claim_sync_local_processing(
    record_id: uuid.UUID,
) -> tuple[str, MediaGenerationTask]:
    """Serialize synchronous artifact repair across inline and daemon workers."""

    now = _utcnow()
    lease_seconds = max(int(get_settings().MEDIA_GENERATION_TASK_LEASE_SECONDS), 60)
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return "succeeded", task
        if task.status in TERMINAL_MEDIA_STATUSES:
            return "failed", task
        if (
            task.status == "sync_processing"
            and task.next_poll_at
            and task.next_poll_at > now
        ):
            return "busy", task
        task.status = "sync_processing"
        task.last_checked_at = now
        task.next_poll_at = now + timedelta(seconds=lease_seconds)
        metadata = dict(task.request_metadata or {})
        metadata["processing_lease_token"] = str(uuid.uuid4())
        task.request_metadata = metadata
        await db.commit()
        return "claimed", task


async def _cleanup_sync_private_assets(
    task: MediaGenerationTask,
    *,
    delete_recovery_assets: bool = False,
    strict: bool = False,
) -> int:
    """Delete signed-URL evidence; keep raw recovery until TTL unless closing."""

    metadata = dict(task.request_metadata or {})
    keys: list[tuple[str, str]] = []
    try:
        if delete_recovery_assets:
            raw_key = _validated_sync_recovery_asset_key(task)
            if raw_key:
                keys.append(("recovery_asset_storage_key", raw_key))
            brand_key = _validated_sync_brand_asset_key(task)
            if brand_key:
                keys.append(("brand_asset_storage_key", brand_key))
        evidence_key = str(metadata.get("acceptance_evidence_storage_key") or "")
        if evidence_key:
            normalized_evidence_key = normalize_storage_key(evidence_key)
            expected_evidence_key = normalize_storage_key(
                f"_internal/provider_recovery/{_media_provider_slug(getattr(task, 'provider', 'minimax'))}"
                f"/image/{task.agent_id}/{task.id}.json"
            )
            if normalized_evidence_key != expected_evidence_key:
                raise MediaContractError(
                    "MiniMax image evidence is outside the task-private namespace"
                )
            keys.append(("acceptance_evidence_storage_key", normalized_evidence_key))
    except Exception:
        logger.exception("[media] invalid sync recovery metadata task_id={}", task.id)
        if strict:
            raise
        return 0

    storage = get_storage_backend()
    deleted = 0
    for metadata_key, storage_key in keys:
        try:
            await storage.delete(storage_key)
        except Exception:
            logger.warning(
                "[media] sync recovery cleanup deferred task_id={} key_type={}",
                task.id,
                metadata_key,
            )
            if strict:
                raise
            continue
        async with async_session() as db:
            current = await db.get(MediaGenerationTask, task.id, with_for_update=True)
            if not current:
                continue
            current_metadata = dict(current.request_metadata or {})
            if current_metadata.get(metadata_key) != storage_key:
                continue
            current_metadata.pop(metadata_key, None)
            if metadata_key == "recovery_asset_storage_key":
                current_metadata.pop("recovery_asset_sha256", None)
                current_metadata.pop("recovery_asset_size", None)
                current_metadata["recovery_asset_deleted_at"] = _utcnow().isoformat()
            elif metadata_key == "brand_asset_storage_key":
                current_metadata["brand_asset_deleted_at"] = _utcnow().isoformat()
            else:
                current_metadata["acceptance_evidence_deleted_at"] = _utcnow().isoformat()
            current.request_metadata = current_metadata
            await db.commit()
            deleted += 1
    return deleted


async def _delete_private_media_recovery_assets(
    task: MediaGenerationTask,
    *,
    strict: bool = False,
) -> int:
    """Delete all private recovery objects owned by one terminal media task."""

    modality = str(getattr(task, "modality", "video") or "video")
    if modality == "video":
        return await _cleanup_private_video_recovery_assets(task, strict=strict)
    if modality in _SYNC_MEDIA_MODALITIES:
        return await _cleanup_sync_private_assets(
            task,
            delete_recovery_assets=True,
            strict=strict,
        )
    return 0


async def _compensate_unrecoverable_sync_task(
    record_id: uuid.UUID,
    reason: str,
    *,
    expected_working_status: str | None = None,
    processing_lease_token: str | None = None,
) -> _MediaCompensationAttempt:
    """Settle provider debt and refund the customer exactly once in one txn."""

    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status in TERMINAL_MEDIA_STATUSES:
            return _MediaCompensationAttempt("terminal", task)
        if task.modality not in _SYNC_MEDIA_MODALITIES:
            raise ValueError("Media generation task is not synchronous")
        claimed_asset_failure = expected_working_status == "sync_processing"
        if expected_working_status is not None:
            metadata = dict(task.request_metadata or {})
            if (
                task.status != expected_working_status
                or not processing_lease_token
                or metadata.get("processing_lease_token")
                != processing_lease_token
            ):
                return _MediaCompensationAttempt("stale_lease", task)
        if _utcnow() < _sync_raw_capture_deadline(task):
            return _MediaCompensationAttempt("capture_window_open", task)

        if not claimed_asset_failure:
            if task.status not in {
                "provider_accepted",
                "asset_repairing",
                "retrying",
            }:
                return _MediaCompensationAttempt("ineligible_status", task)

            # Final object-first/DB-second check while the task row is locked.
            # A concurrent writer may have created the predeclared raw/evidence
            # object after this reconciler's earlier read but before this txn.
            storage = get_storage_backend()
            try:
                raw_key = _validated_sync_recovery_asset_key(task)
            except InvalidSyncRecoveryIdentity:
                # Legacy rows may contain an obsolete recovery key without a
                # supported extension. Once the raw-capture window has closed,
                # never read an untrusted key or retry compensation forever.
                logger.warning(
                    "[media] invalid sync recovery identity ignored during "
                    "compensation task_id={}",
                    task.id,
                )
                raw_key = None
            if raw_key and await storage.exists(raw_key):
                return _MediaCompensationAttempt("asset_appeared", task)
            if task.modality == "image":
                expected_evidence_key = normalize_storage_key(
                    f"_internal/provider_recovery/{_media_provider_slug(getattr(task, 'provider', 'minimax'))}"
                    f"/image/{task.agent_id}/{task.id}.json"
                )
                metadata = dict(task.request_metadata or {})
                configured_evidence_key = str(
                    metadata.get("acceptance_evidence_storage_key") or ""
                )
                if configured_evidence_key and (
                    normalize_storage_key(configured_evidence_key)
                    != expected_evidence_key
                ):
                    logger.warning(
                        "[media] invalid sync evidence identity ignored during "
                        "compensation task_id={}",
                        task.id,
                    )
                if await storage.exists(expected_evidence_key):
                    return _MediaCompensationAttempt("asset_appeared", task)
        refunded = 0
        invalid_reservation_error: str | None = None
        if task.reservation_id:
            try:
                reservation = await _lock_owned_media_reservation(db, task)
            except MediaContractError as exc:
                invalid_reservation_error = (
                    "Unrecoverable media task has invalid Credits reservation "
                    f"ownership: {_safe_error(exc)}"
                )[:1000]
            else:
                refunded = int(reservation.amount or 0)
                await mark_credit_reservation_settlement_ready_in_session(
                    db,
                    reservation.id,
                    amount=refunded,
                )
                if refunded > 0:
                    # Refund first inside this same transaction. A conservative
                    # provider-debt resize may exceed the current balance; the
                    # compensating grant guarantees the consume can then settle.
                    await grant_credits_in_session(
                        db,
                        tenant_id=reservation.tenant_id,
                        amount=refunded,
                        reason="refund",
                        granted_by=task.user_id,
                        ref_type="media_task",
                        ref_id=task.id,
                    )
                await finalize_reserved_credits_in_session(db, reservation.id)
        if invalid_reservation_error:
            # Never refund or finalize a Credits hold that is owned by a
            # different scope. Dead-letter the corrupt legacy task for
            # operator review instead of retrying it forever.
            task.status = "asset_delivery_failed"
            task.last_error = invalid_reservation_error
            task.last_checked_at = now
            task.completed_at = task.completed_at or now
            task.next_poll_at = None
            task.last_response = {
                "status": "AssetDeliveryFailed",
                "refunded_credits": 0,
            }
            task.completion_delivery_status = "not_applicable"
        else:
            task.status = "compensated"
            task.last_error = reason[:1000]
            task.last_checked_at = now
            task.completed_at = task.completed_at or now
            task.next_poll_at = None
            task.last_response = {
                "status": "Compensated",
                "refunded_credits": refunded,
            }
            task.completion_delivery_status = "not_applicable"
            if task.user_id:
                chinese_label, _english_label, _action_label = _media_labels(task)
                db.add(
                    Notification(
                        user_id=task.user_id,
                        agent_id=task.agent_id,
                        type="system",
                        title=f"{chinese_label}生成结果已退款",
                        body=(
                            f"供应商已受理，但{chinese_label}结果无法安全恢复。"
                            f"系统已退回 {refunded} Credits。"
                        ),
                        link=f"/agents/{task.agent_id}/chat",
                        ref_id=task.id,
                        sender_name="Astra",
                    )
                )
        await db.commit()
    if invalid_reservation_error:
        await _record_media_failure_issue(task, task.last_error)
        return _MediaCompensationAttempt("invalid_reservation_ownership", task)
    await _record_media_failure_issue(
        task,
        f"Provider result was unrecoverable; customer Credits compensated: {reason}",
    )
    await _cleanup_sync_private_assets(task, delete_recovery_assets=True)
    return _MediaCompensationAttempt("compensated", task)


async def _load_sync_recovery_bytes(
    task: MediaGenerationTask,
) -> bytes | None:
    key = _validated_sync_recovery_asset_key(task)
    if not key:
        return None
    storage = get_storage_backend()
    if not await storage.exists(key):
        return None
    raw = await storage.read_bytes(key)
    max_bytes = (
        _MAX_SYNC_IMAGE_BYTES if task.modality == "image" else _MAX_SYNC_AUDIO_BYTES
    )
    if not raw or len(raw) > max_bytes:
        raise MediaContractError("Synchronous media recovery asset is outside its size limit")
    metadata = task.request_metadata or {}
    expected_size = int(metadata.get("recovery_asset_size") or 0)
    expected_sha256 = str(metadata.get("recovery_asset_sha256") or "")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_size and expected_size != len(raw):
        raise MediaContractError("Synchronous media recovery asset size changed")
    if expected_sha256 and not hmac.compare_digest(expected_sha256, actual_sha256):
        raise MediaContractError("Synchronous media recovery asset hash changed")
    if not expected_size or not expected_sha256:
        await store_minimax_sync_recovery_asset(
            task.id,
            raw,
            content_type=str(metadata.get("recovery_content_type") or "application/octet-stream"),
        )
    return raw


async def _stored_media_output_is_usable(task: MediaGenerationTask) -> bool:
    """Verify the authoritative object before any Credits finalization."""

    try:
        storage = get_storage_backend()
        key = agent_storage_key(task.agent_id, task.output_path)
        if not await storage.exists(key) or not await storage.is_file(key):
            return False
        raw = await storage.read_bytes(key)
        metadata = task.request_metadata or {}
        await _validate_authoritative_media_bytes(
            task,
            raw,
            expected_size=int(getattr(task, "output_size", 0) or 0),
            expected_sha256=str(metadata.get("output_sha256") or ""),
        )
        return True
    except Exception:
        logger.exception("[media] stored output verification failed task_id={}", task.id)
        return False


async def _validate_authoritative_media_bytes(
    task: MediaGenerationTask,
    raw: bytes,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    if not raw:
        raise MediaContractError("Stored media output is empty")
    if expected_size and expected_size != len(raw):
        raise MediaContractError("Stored media output size changed")
    if expected_sha256 and not hmac.compare_digest(
        expected_sha256,
        hashlib.sha256(raw).hexdigest(),
    ):
        raise MediaContractError("Stored media output hash changed")
    metadata = task.request_metadata or {}
    modality = str(getattr(task, "modality", "") or "")
    if modality == "video":
        await validate_generated_video(raw, label="Stored MiniMax video")
    elif modality == "image":
        width, height = validate_generated_image(raw)
        validate_image_delivery_contract(
            width,
            height,
            expected_aspect_ratio=metadata.get("aspect_ratio"),
        )
    elif modality in {"audio", "music"}:
        await validate_generated_audio(
            raw,
            audio_format=str(metadata.get("output_extension") or "").lstrip("."),
            sample_rate=int(metadata.get("sample_rate") or 0) or None,
            label=f"Stored MiniMax {modality}",
        )
    else:
        raise MediaContractError("Stored media modality is unsupported")


async def _store_authoritative_media_output(
    record_id: uuid.UUID,
    raw: bytes,
    *,
    content_type: str,
    status_data: dict,
    expected_working_status: str,
    processing_lease_token: str,
) -> MediaGenerationTask:
    """Fence stale workers while writing and read-back validating final media."""

    expected_sha256 = str(status_data.get("_astra_output_sha256") or "")
    if not expected_sha256 or not hmac.compare_digest(
        expected_sha256,
        hashlib.sha256(raw).hexdigest(),
    ):
        raise MediaContractError("Authoritative media output hash is missing or invalid")
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        metadata = dict(task.request_metadata or {})
        if (
            task.status != expected_working_status
            or not processing_lease_token
            or metadata.get("processing_lease_token") != processing_lease_token
        ):
            raise MediaContractError("Media processing lease is stale")

        storage = get_storage_backend()
        output_key = agent_storage_key(task.agent_id, task.output_path)
        await storage.write_bytes(output_key, raw, content_type=content_type)
        if not await storage.exists(output_key) or not await storage.is_file(output_key):
            raise MediaContractError("Authoritative media object is not readable after write")
        stored = await storage.read_bytes(output_key)
        await _validate_authoritative_media_bytes(
            task,
            stored,
            expected_size=len(raw),
            expected_sha256=expected_sha256,
        )

        if task.reservation_id:
            reservation = await _lock_owned_media_reservation(db, task)
            exact_amount = int(reservation.amount)
            await mark_credit_reservation_settlement_ready_in_session(
                db,
                reservation.id,
                amount=exact_amount,
            )
        metadata["output_sha256"] = expected_sha256
        metadata.pop("processing_lease_token", None)
        task.request_metadata = metadata
        task.status = "settlement_ready"
        task.last_response = status_data
        task.output_size = len(raw)
        task.last_error = None
        task.last_checked_at = _utcnow()
        task.next_poll_at = _utcnow()
        await db.commit()
        return task


async def _return_settlement_task_to_asset_repair(
    record_id: uuid.UUID,
    reason: str,
) -> MediaGenerationTask:
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "settlement_ready":
            task.status = "asset_repairing"
            task.last_error = reason[:1000]
            task.next_poll_at = _utcnow()
            await db.commit()
        return task


async def _finalize_verified_success(
    record_id: uuid.UUID,
    status_data: dict,
    output_size: int,
    *,
    deliver_completion: bool,
) -> MediaGenerationTask | None:
    """Use one read-back verification gate for every successful settlement."""

    task = await _load_task(record_id)
    if not task:
        raise ValueError("Media generation task not found")
    if task.status == "succeeded":
        if await _stored_media_output_is_usable(task):
            return task
        await _begin_missing_asset_repair(record_id)
        return None
    if task.status != "settlement_ready":
        raise ValueError("Media generation task is not settlement-ready")
    if not await _stored_media_output_is_usable(task):
        await _return_settlement_task_to_asset_repair(
            record_id,
            "Authoritative media object failed read-back verification before settlement",
        )
        return None
    return await _finalize_success(
        record_id,
        status_data,
        output_size,
        deliver_completion=deliver_completion,
    )


async def _recover_sync_image_url(task: MediaGenerationTask) -> bytes | None:
    """Recover a provider's short-lived signed URL without exposing it in the DB."""

    storage = get_storage_backend()
    expected_evidence_key = normalize_storage_key(
        f"_internal/provider_recovery/{_media_provider_slug(getattr(task, 'provider', 'minimax'))}"
        f"/image/{task.agent_id}/{task.id}.json"
    )
    metadata = dict(task.request_metadata or {})
    evidence_key = str(
        metadata.get("acceptance_evidence_storage_key") or expected_evidence_key
    )
    if normalize_storage_key(evidence_key) != expected_evidence_key:
        raise MediaContractError("MiniMax image evidence identity changed")
    if not await storage.exists(expected_evidence_key):
        return None

    from app.services.agent_tools import (
        MAX_GENERATED_IMAGE_BYTES,
        _bounded_public_http_download,
        _load_minimax_image_acceptance_evidence,
    )

    evidence = await _load_minimax_image_acceptance_evidence(
        expected_evidence_key,
        provider=getattr(task, "provider", "minimax"),
    )
    if (
        str(evidence.get("save_path") or "") != task.output_path
        or str(evidence.get("model") or "") != str(task.model or "")
    ):
        raise MediaContractError("MiniMax image recovery evidence does not own this task")
    image_url = str(evidence.get("image_url") or "").strip()
    if not image_url:
        raise MediaContractError("MiniMax image recovery evidence has no signed URL")
    if task.status in {"submitting", "submission_ambiguous"}:
        task = await mark_minimax_sync_provider_accepted(
            task.id,
            evidence_key=expected_evidence_key,
            accepted_metadata={"status": "Accepted", "recovered_evidence": True},
        )
    raw = await _bounded_public_http_download(
        image_url,
        max_bytes=MAX_GENERATED_IMAGE_BYTES,
        label="MiniMax image recovery download",
        timeout=60,
    )
    await store_minimax_sync_recovery_asset(
        task.id,
        raw,
        content_type="application/octet-stream",
    )
    return raw


async def reconcile_minimax_sync_media_task(
    record_id: uuid.UUID,
    *,
    deliver_completion: bool = True,
) -> MediaGenerationOutcome:
    """Recover and settle one MiniMax image/audio/music task idempotently."""

    task = await _load_task(record_id)
    if not task:
        return MediaGenerationOutcome(status="failed", error="Media generation task not found")
    if task.modality not in _SYNC_MEDIA_MODALITIES:
        return MediaGenerationOutcome(status="failed", error="Media task modality is not synchronous")
    if task.status == "compensated":
        return MediaGenerationOutcome(status="compensated", error=task.last_error)
    if task.status == "asset_delivery_failed":
        return MediaGenerationOutcome(
            status="asset_delivery_failed",
            error=task.last_error,
            retryable=False,
        )
    if task.status in {"failed", "closed_nonrefundable"}:
        return MediaGenerationOutcome(status="failed", error=task.last_error)

    storage = get_storage_backend()
    if task.status == "succeeded" and await _stored_media_output_is_usable(task):
        await _cleanup_sync_private_assets(task)
        return MediaGenerationOutcome(status="succeeded", output_path=task.output_path)
    if task.status == "succeeded":
        task = await _begin_missing_asset_repair(record_id)

    if task.status == "settlement_ready":
        try:
            completed = await _finalize_verified_success(
                record_id,
                task.last_response or {"status": "Success"},
                int(task.output_size or 0),
                deliver_completion=deliver_completion,
            )
            if completed is None:
                return MediaGenerationOutcome(
                    status="retrying",
                    error="Stored media failed read-back verification",
                    retryable=True,
                )
        except Exception as exc:
            fresh = await _load_task(record_id)
            if fresh and fresh.status == "settlement_ready":
                await _record_settlement_retry(record_id, exc)
            return MediaGenerationOutcome(
                status="retrying", error=_safe_error(exc), retryable=True
            )
        await _cleanup_sync_private_assets(completed)
        return MediaGenerationOutcome(status="succeeded", output_path=completed.output_path)

    submission_timeout = max(
        int(get_settings().MEDIA_GENERATION_SUBMISSION_TIMEOUT_SECONDS),
        60,
    )
    processing_lease_token: str | None = None
    failure_expected_status = str(task.status or "")
    status_data: dict | None = None

    try:
        raw = await _load_sync_recovery_bytes(task)
        if raw is None and task.modality == "image":
            raw = await _recover_sync_image_url(task)
            task = await _load_task(record_id) or task
            failure_expected_status = str(task.status or "")
        if raw is None:
            if task.status == "submission_ambiguous":
                return MediaGenerationOutcome(
                    status="failed",
                    error=(
                        task.last_error
                        or "Provider submission outcome requires operator reconciliation"
                    ),
                )
            if task.status == "submitting":
                if _media_task_age(task) <= timedelta(seconds=submission_timeout):
                    return MediaGenerationOutcome(status="processing", retryable=True)
                await mark_media_generation_submission_ambiguous(
                    record_id,
                    RuntimeError("Provider submission outcome was not durably accepted"),
                )
                return MediaGenerationOutcome(
                    status="failed",
                    error="Provider submission outcome requires operator reconciliation",
                )
            if _utcnow() < _sync_raw_capture_deadline(task):
                return MediaGenerationOutcome(
                    status="processing",
                    error="Provider response is still inside the durable raw-capture window",
                    retryable=True,
                )
            compensation = await _compensate_unrecoverable_sync_task(
                record_id,
                "Accepted provider response bytes were not durably recoverable",
            )
            if not compensation.compensated:
                if compensation.task.status == "asset_delivery_failed":
                    return MediaGenerationOutcome(
                        status="asset_delivery_failed",
                        error=compensation.task.last_error,
                        retryable=False,
                    )
                if compensation.task.status == "succeeded":
                    return MediaGenerationOutcome(
                        status="succeeded",
                        output_path=compensation.task.output_path,
                    )
                return MediaGenerationOutcome(
                    status="processing",
                    error=(
                        "Media recovery compensation was deferred: "
                        f"{compensation.outcome}"
                    ),
                    retryable=True,
                )
            return MediaGenerationOutcome(
                status="compensated",
                error=compensation.task.last_error,
            )

        claim_status, claimed = await _claim_sync_local_processing(record_id)
        if claim_status == "succeeded":
            return MediaGenerationOutcome(status="succeeded", output_path=claimed.output_path)
        if claim_status == "failed":
            return MediaGenerationOutcome(status=claimed.status, error=claimed.last_error)
        if claim_status != "claimed":
            return MediaGenerationOutcome(status="processing", retryable=True)
        task = claimed
        failure_expected_status = "sync_processing"
        metadata = dict(task.request_metadata or {})
        processing_lease_token = str(metadata.get("processing_lease_token") or "")
        status_data = {"status": "Success", "recovered": True}
        content_type = str(metadata.get("output_content_type") or "application/octet-stream")
        output_bytes = raw
        if task.modality == "image":
            width, height = validate_generated_image(raw)
            validate_image_delivery_contract(
                width,
                height,
                expected_aspect_ratio=metadata.get("aspect_ratio"),
            )
            overlay_text = str(metadata.get("overlay_text") or "")
            expected_text_sha256 = str(metadata.get("overlay_text_sha256") or "")
            if expected_text_sha256 and not hmac.compare_digest(
                expected_text_sha256,
                hashlib.sha256(overlay_text.encode("utf-8")).hexdigest(),
            ):
                raise MediaContractError("Frozen image copy hash changed")
            overlay_blocks = metadata.get("overlay_blocks") or []
            expected_blocks_sha256 = str(metadata.get("overlay_blocks_sha256") or "")
            actual_blocks_sha256 = overlay_blocks_sha256(overlay_blocks)
            if overlay_blocks and not expected_blocks_sha256:
                raise MediaContractError("Frozen image overlay blocks hash is missing")
            if expected_blocks_sha256 and (
                not actual_blocks_sha256
                or not hmac.compare_digest(expected_blocks_sha256, actual_blocks_sha256)
            ):
                raise MediaContractError("Frozen image overlay blocks hash changed")
            brand_asset = None
            brand_key = _validated_sync_brand_asset_key(task)
            if brand_key:
                brand_raw = await storage.read_bytes(brand_key)
                brand_asset = image_asset_from_bytes(
                    brand_raw,
                    label="Frozen image brand asset",
                    source_path=brand_key,
                )
                expected_brand_sha256 = str(metadata.get("brand_asset_sha256") or "")
                if not expected_brand_sha256 or not hmac.compare_digest(
                    expected_brand_sha256,
                    brand_asset.sha256,
                ):
                    raise MediaContractError("Frozen image brand asset hash changed")
            output_bytes, receipt = apply_image_brand_overlays(
                raw,
                overlay_text,
                overlay_blocks=overlay_blocks,
                text_position=str(metadata.get("overlay_position") or "bottom"),
                brand_asset=brand_asset,
                brand_position=str(metadata.get("brand_position") or "center"),
                brand_scale=float(metadata.get("brand_scale") or 0.42),
                output_format=str(metadata.get("output_extension") or ".png"),
                output_dimensions=metadata.get("delivery_size"),
                sanitize_generated_background=bool(
                    metadata.get("sanitize_generated_background")
                ),
            )
            width, height = validate_generated_image(output_bytes)
            validate_image_delivery_contract(
                width,
                height,
                expected_aspect_ratio=metadata.get("aspect_ratio"),
            )
            deliverable_request_id = str(
                metadata.get("deliverable_request_id") or ""
            ).strip()
            expected_deliverable_digest = str(
                metadata.get("expected_overlay_blocks_sha256") or ""
            ).strip()
            if expected_deliverable_digest and (
                not receipt.overlay_blocks_sha256
                or not hmac.compare_digest(
                    expected_deliverable_digest,
                    receipt.overlay_blocks_sha256,
                )
            ):
                raise MediaContractError(
                    "Composed poster copy does not match the persisted deliverable contract"
                )
            receipt_contract = receipt.as_dict()
            if deliverable_request_id:
                receipt_contract["deliverable_request_id"] = deliverable_request_id
            if expected_deliverable_digest:
                receipt_contract["expected_overlay_blocks_sha256"] = (
                    expected_deliverable_digest
                )
            if deliverable_request_id:
                receipt_contract["execution_strategy"] = str(
                    metadata.get("execution_strategy") or ""
                )
                receipt_contract["allow_degraded_fallback"] = (
                    metadata.get("allow_degraded_fallback") is True
                )
            status_data["_astra_media_contract"] = receipt_contract
        else:
            audio_format = str(metadata.get("output_extension") or "").lstrip(".")
            audio_info = await validate_generated_audio(
                raw,
                audio_format=audio_format,
                sample_rate=int(metadata.get("sample_rate") or 0) or None,
                label=f"MiniMax {task.modality} recovery output",
            )
            requested_duration = metadata.get("duration")
            if task.modality == "music" and requested_duration is not None:
                output_bytes, audio_info = await trim_generated_audio(
                    raw,
                    audio_format=audio_format,
                    duration_seconds=float(requested_duration),
                    label="MiniMax music recovery output",
                )
            status_data["_astra_media_contract"] = {
                "duration_seconds": round(audio_info.duration_seconds, 3),
                "requested_duration_seconds": (
                    float(requested_duration)
                    if requested_duration is not None
                    else None
                ),
                "codec_name": audio_info.codec_name,
                "sample_rate": audio_info.sample_rate,
                "channels": audio_info.channels,
                "container_format": audio_info.container_format,
            }

        status_data["_astra_output_sha256"] = hashlib.sha256(output_bytes).hexdigest()
        await _store_authoritative_media_output(
            record_id,
            output_bytes,
            content_type=content_type,
            status_data=status_data,
            expected_working_status="sync_processing",
            processing_lease_token=processing_lease_token,
        )
        try:
            completed = await _finalize_verified_success(
                record_id,
                status_data,
                len(output_bytes),
                deliver_completion=deliver_completion,
            )
            if completed is None:
                return MediaGenerationOutcome(
                    status="retrying",
                    error="Stored media failed read-back verification",
                    retryable=True,
                )
        except Exception as exc:
            fresh = await _load_task(record_id)
            if fresh and fresh.status == "settlement_ready":
                await _record_settlement_retry(record_id, exc)
            return MediaGenerationOutcome(
                status="retrying", error=_safe_error(exc), retryable=True
            )
        try:
            await _write_task_metadata(
                completed,
                {
                    "status": "Success",
                    "downloaded_path": completed.output_path,
                    "reservation_status": (
                        "finalized" if completed.reservation_id else "not_required"
                    ),
                    "completed_at": (
                        completed.completed_at.isoformat()
                        if completed.completed_at
                        else _utcnow().isoformat()
                    ),
                },
            )
        except Exception:
            logger.exception("[media] sync task metadata write failed task_id={}", record_id)
        await _cleanup_sync_private_assets(completed)
        return MediaGenerationOutcome(status="succeeded", output_path=completed.output_path)
    except asyncio.CancelledError:
        raise
    except MediaContractError as exc:
        _recorded, failed_task = await _record_provider_success_asset_failure(
            record_id,
            exc,
            status_data,
            expected_working_status=failure_expected_status or "sync_processing",
            processing_lease_token=processing_lease_token,
            retryable=False,
        )
        return MediaGenerationOutcome(
            status=failed_task.status,
            error=failed_task.last_error or _safe_error(exc),
            retryable=False,
        )
    except Exception as exc:
        task = await _load_task(record_id) or task
        expiry_reason = _media_task_expiry_reason(task)
        if expiry_reason:
            compensation = await _compensate_unrecoverable_sync_task(
                record_id,
                f"{expiry_reason}: {_safe_error(exc)}",
                expected_working_status=(
                    "sync_processing" if processing_lease_token else None
                ),
                processing_lease_token=processing_lease_token,
            )
            if compensation.compensated:
                return MediaGenerationOutcome(
                    status="compensated", error=compensation.task.last_error
                )
            if compensation.task.status == "asset_delivery_failed":
                return MediaGenerationOutcome(
                    status="asset_delivery_failed",
                    error=compensation.task.last_error,
                    retryable=False,
                )
            return MediaGenerationOutcome(
                status="retrying",
                error=(
                    "Media recovery compensation was deferred: "
                    f"{compensation.outcome}"
                ),
                retryable=True,
            )
        await _record_sync_recovery_retry(
            record_id,
            exc,
            expected_working_status=failure_expected_status or None,
            processing_lease_token=processing_lease_token,
        )
        return MediaGenerationOutcome(
            status="retrying", error=_safe_error(exc), retryable=True
        )


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
    if task.status == "closed_nonrefundable":
        return MediaGenerationOutcome(
            status="failed",
            error=task.last_error or "Media generation task was closed by an operator",
        )
    if task.status == "failed":
        return MediaGenerationOutcome(status="failed", error=task.last_error)

    storage = get_storage_backend()
    if task.status == "succeeded" and await _stored_media_output_is_usable(task):
        # Retain the frozen brand asset for the missing-object recovery path.
        # A later retention job or explicit operator closure owns deletion.
        return MediaGenerationOutcome(status="succeeded", output_path=task.output_path)
    if task.status == "succeeded":
        task = await _begin_missing_asset_repair(record_id)
    request_metadata = getattr(task, "request_metadata", None) or {}
    if (
        task.status == "asset_repairing"
        and request_metadata.get("brand_asset_sha256")
        and request_metadata.get("brand_asset_retention_expired_at")
    ):
        reason = "Frozen brand asset recovery retention has expired"
        await _record_unrepairable_asset(record_id, reason, task.last_response)
        return MediaGenerationOutcome(status="failed", error=reason)
    if task.status == "settlement_ready":
        try:
            completed_task = await _finalize_verified_success(
                record_id,
                task.last_response or status_data or {"status": "Success"},
                int(getattr(task, "output_size", 0) or 0),
                deliver_completion=deliver_completion,
            )
            if completed_task is None:
                return MediaGenerationOutcome(
                    status="retrying",
                    error="Stored video failed read-back verification",
                    retryable=True,
                )
        except Exception as exc:
            fresh = await _load_task(record_id)
            if fresh and fresh.status == "settlement_ready":
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
    if not getattr(task, "provider_task_id", None):
        try:
            task = await _recover_minimax_video_provider_identity(task)
        except ProviderTaskIdentityCollision as exc:
            return MediaGenerationOutcome(status="failed", error=_safe_error(exc))
        except MediaContractError as exc:
            await mark_media_generation_submission_ambiguous(record_id, exc)
            return MediaGenerationOutcome(status="failed", error=_safe_error(exc))
        except Exception as exc:
            retry_task = await record_media_generation_retry(record_id, exc)
            return MediaGenerationOutcome(
                status=(
                    "failed"
                    if retry_task and retry_task.status == "failed"
                    else "retrying"
                ),
                error=_safe_error(exc),
                retryable=not retry_task or retry_task.status != "failed",
            )
    if task.status == "submission_ambiguous":
        return MediaGenerationOutcome(
            status="failed",
            error=(
                task.last_error
                or "Provider submission outcome requires operator reconciliation"
            ),
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
    processing_lease_token: str | None = None
    try:
        # Runtime imports avoid a module cycle while keeping provider protocol
        # adapters separate from the durable settlement state machine.
        from app.services.agent_tools import (
            _load_minimax_tool_credential_by_id,
            _mark_media_provider_credential_failure,
            _minimax_download_file,
            _minimax_query_video_task,
            _minimax_retrieve_file_download_url,
            _minimax_video_file_id,
        )
        provider = _media_provider_slug(task.provider)
        if provider == "volcengine_agent_plan":
            from app.models.llm import LLMCredential
            from app.services.llm.utils import get_credential_api_key
            from app.services.volcengine_agent_plan import (
                download_video,
                normalize_base_url,
                normalized_video_status,
                query_video_task,
                video_url_from_response,
            )

            async with async_session() as db:
                provider_credential = await db.get(LLMCredential, task.credential_id)
            if (
                not provider_credential
                or provider_credential.provider != provider
                or provider_credential.tenant_id is not None
                or not provider_credential.enabled
            ):
                raise ValueError("Media provider credential is unavailable for this task")
            provider_api_key = get_credential_api_key(provider_credential)
            if not provider_api_key:
                raise ValueError("Media provider credential is missing an API key")
            provider_base_url = normalize_base_url(provider_credential.base_url)
            if status_data is None:
                status_data = await query_video_task(
                    api_key=provider_api_key,
                    base_url=provider_base_url,
                    task_id=task.provider_task_id,
                )
            provider_status = normalized_video_status(status_data)
        else:
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
            request_metadata = task.request_metadata or {}
            processing_lease_token = str(
                request_metadata.get("processing_lease_token") or ""
            )
            if provider == "volcengine_agent_plan":
                download_url = video_url_from_response(status_data)
                if not download_url:
                    raise ValueError("Completed video response has no video URL")
                video_bytes = await download_video(download_url)
            else:
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
                label="Provider video download",
                require_browser_safe=False,
            )
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

            # Provider output is not guaranteed to be directly playable in a
            # browser.  The overlay helper is also the single compatibility
            # normalizer: when no copy or brand asset is requested it keeps an
            # already-safe MP4 unchanged, otherwise it transcodes to
            # H.264/yuv420p, optional AAC audio, and faststart.  Skipping the
            # helper for an unbranded video left successful provider tasks in
            # the durable ``downloading`` retry state.
            video_bytes, overlay_receipt = await apply_video_brand_overlays(
                video_bytes,
                overlay_text,
                text_position=str(request_metadata.get("overlay_position") or "bottom"),
                brand_asset=brand_asset,
                brand_position=str(request_metadata.get("brand_position") or "center"),
                brand_scale=float(request_metadata.get("brand_scale") or 0.42),
                sanitize_generated_background=bool(
                    request_metadata.get("sanitize_generated_background")
                ),
            )
            if overlay_text.strip() or brand_asset:
                status_data = {
                    **(status_data or {}),
                    "_astra_media_contract": overlay_receipt.as_dict(),
                }
            final_video_info = await validate_generated_video(
                video_bytes,
                label="Final brand-safe video",
            )
            validate_video_delivery_contract(
                final_video_info,
                expected_duration_seconds=request_metadata.get("duration"),
                expected_aspect_ratio=request_metadata.get("aspect_ratio"),
                require_audio=bool(request_metadata.get("require_audio")),
            )
            status_data = {
                **(status_data or {}),
                "_astra_output_sha256": hashlib.sha256(video_bytes).hexdigest(),
            }

            # Hold the task row lock across write/readback/settlement so an
            # expired worker cannot overwrite a newer worker's artifact.
            await _store_authoritative_media_output(
                record_id,
                video_bytes,
                content_type="video/mp4",
                status_data=status_data,
                expected_working_status="downloading",
                processing_lease_token=processing_lease_token,
            )
            try:
                completed_task = await _finalize_verified_success(
                    record_id,
                    status_data,
                    len(video_bytes),
                    deliver_completion=deliver_completion,
                )
                if completed_task is None:
                    return MediaGenerationOutcome(
                        status="retrying",
                        error="Stored video failed read-back verification",
                        retryable=True,
                    )
            except Exception as exc:
                fresh = await _load_task(record_id)
                if fresh and fresh.status == "settlement_ready":
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
            await _delete_private_media_recovery_assets(failed_task)
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
        transition_applied, repair_task = await _record_provider_success_asset_failure(
            record_id,
            exc,
            status_data,
            expected_working_status="downloading",
            processing_lease_token=processing_lease_token or "",
            retryable=False,
        )
        if repair_task.status == "succeeded":
            return MediaGenerationOutcome(
                status="succeeded",
                output_path=repair_task.output_path,
            )
        if not transition_applied:
            return MediaGenerationOutcome(
                status="processing",
                error="A newer media worker owns the processing lease",
                retryable=True,
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
            transition_applied, repair_task = await _record_provider_success_asset_failure(
                record_id,
                exc,
                status_data,
                expected_working_status="downloading",
                processing_lease_token=processing_lease_token or "",
            )
            if repair_task.status == "succeeded":
                return MediaGenerationOutcome(
                    status="succeeded",
                    output_path=repair_task.output_path,
                )
            if not transition_applied:
                return MediaGenerationOutcome(
                    status="processing",
                    error="A newer media worker owns the processing lease",
                    retryable=True,
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
                await _mark_media_provider_credential_failure(
                    task.credential_id,
                    exc,
                    provider=task.provider,
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
            reservation = await _lock_owned_media_reservation(db, task)
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
    settings = get_settings()
    now = _utcnow()
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, record_id, with_for_update=True)
        if not task:
            raise ValueError("Media generation task not found")
        if task.status == "succeeded":
            return "succeeded", task
        if task.status in TERMINAL_MEDIA_STATUSES:
            return "failed", task
        if task.status == "downloading" and task.next_poll_at and task.next_poll_at > now:
            return "busy", task
        task.status = "downloading"
        task.last_checked_at = now
        lease_seconds = max(int(settings.MEDIA_GENERATION_TASK_LEASE_SECONDS), 60)
        task.next_poll_at = now + timedelta(seconds=lease_seconds)
        metadata = dict(task.request_metadata or {})
        metadata["processing_lease_token"] = str(uuid.uuid4())
        task.request_metadata = metadata
        await db.commit()
        return "claimed", task


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
        elif task.status in TERMINAL_MEDIA_STATUSES:
            raise ValueError("Terminal media generation task cannot be finalized")
        elif task.status != "settlement_ready":
            raise ValueError("Media generation task is not settlement-ready")
        else:
            was_already_completed = bool(
                task.completed_at
                or task.completion_message_id
                or task.completion_delivery_status in {"inline", "persisted"}
            )
            if task.reservation_id:
                reservation = await _lock_owned_media_reservation(db, task)
                await finalize_reserved_credits_in_session(db, reservation.id)
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
                _chinese_label, english_label, _action_label = _media_labels(task)
                db.add(AgentActivityLog(
                    agent_id=task.agent_id,
                    action_type="file_written",
                    summary=(
                        f"{english_label} ready: "
                        f"{task.output_path.rsplit('/', 1)[-1]}"
                    ),
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
                chinese_label, _english_label, _action_label = _media_labels(task)
                db.add(Notification(
                    user_id=task.user_id,
                    agent_id=task.agent_id,
                    type="system",
                    title=f"{chinese_label}生成完成",
                    body=f"{chinese_label}已保存到 {task.output_path}",
                    link=_media_completion_notification_link(
                        agent_id=task.agent_id,
                        output_path=task.output_path,
                        session_id=valid_session.id if valid_session else None,
                        message_id=task.completion_message_id,
                    ),
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
        from app.services.media_tool_registry import minimax_completion_tool

        modality = str(getattr(task, "modality", "") or "").strip().lower()
        payload = {
            "type": "media_generation_result",
            "event_id": str(message.id),
            "session_id": session_id,
            "workspace_path": task.output_path,
            "media_generation_task_id": str(task.id),
            "modality": modality,
            "tool_name": minimax_completion_tool(modality),
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
        if task.status in TERMINAL_MEDIA_STATUSES:
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

    if task.status in TERMINAL_MEDIA_STATUSES:
        return
    now = now or _utcnow()
    if task.reservation_id:
        reservation = await _lock_owned_media_reservation(db, task)
        await release_reserved_credits_in_session(
            db,
            reservation.id,
            release_provider_inflight=provider_confirmed_failure,
        )
    task.status = "failed"
    task.last_response = status_data
    task.last_error = reason[:1000]
    task.last_checked_at = now
    task.completed_at = task.completed_at or now
    task.next_poll_at = None
    if task.user_id:
        chinese_label, _english_label, _action_label = _media_labels(task)
        db.add(Notification(
            user_id=task.user_id,
            agent_id=task.agent_id,
            type="system",
            title=f"{chinese_label}生成失败",
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
                payload.update(
                    _public_media_metadata(existing)
                )
    except Exception:
        logger.warning("[media] invalid existing metadata ignored key={}", key)
    payload.update(_public_media_metadata(task.request_metadata or {}))
    payload.update({
        # The concrete route is an operator concern stored in SQL. Workspace
        # compatibility metadata remains stable across transparent failover.
        "provider": "platform_media",
        "task_record_id": str(task.id),
        "task_id": task.provider_task_id or payload.get("task_id") or "",
        "save_path": task.output_path,
    })
    payload.update(_public_media_metadata(updates))
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
        reservation_owned = bool(
            reservation
            and reservation.tenant_id == task.tenant_id
            and reservation.agent_id == task.agent_id
            and reservation.user_id == task.user_id
        )
        if reservation_owned and reservation.status == "reserved":
            reservation.status = "provider_inflight"
            reservation.expires_at = _utcnow() + timedelta(hours=24)
        elif reservation and not reservation_owned:
            reason = "Legacy MiniMax reservation ownership is invalid"
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
        if (
            not reservation
            or not task.agent_id
            or reservation.tenant_id != task.tenant_id
            or reservation.agent_id != task.agent_id
            or reservation.user_id != task.user_id
        ):
            attention_reason = (
                "Legacy MiniMax task has no valid Credits reservation or Agent scope"
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
    try:
        # Legacy workspace metadata is editable and therefore cannot itself
        # prove that the referenced object is a deliverable.  Read and probe
        # the bytes before importing the record into the durable success
        # state.  Requiring the browser-safe contract prevents old H.265,
        # truncated, or otherwise non-playable files from being surfaced as
        # successful media tasks.
        raw = await storage.read_bytes(output_key)
        video_info = await validate_generated_video(
            raw,
            label="Legacy MiniMax video",
        )
        validate_video_delivery_contract(
            video_info,
            expected_duration_seconds=metadata.get("duration"),
            expected_aspect_ratio=metadata.get("aspect_ratio"),
            require_audio=bool(metadata.get("require_audio")),
        )
    except Exception as exc:
        await _record_legacy_backfill_attention(
            record_id,
            f"Legacy MiniMax workspace video failed media validation: {_safe_error(exc)}",
        )
        return False
    request_metadata = {
        key: metadata[key]
        for key in (
            "credit_cost",
            "model",
            "duration",
            "resolution",
            "aspect_ratio",
            "require_audio",
            "created_at",
        )
        if key in metadata
    }
    request_metadata["output_sha256"] = hashlib.sha256(raw).hexdigest()
    request_metadata["output_content_type"] = "video/mp4"

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
            task.output_size = len(raw)
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
    lease_seconds = max(int(settings.MEDIA_GENERATION_TASK_LEASE_SECONDS), 60)
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
            # A previous worker may have died after acquiring the download
            # transition.  Reset only an expired row selected by this locked
            # claim so the same owner can re-enter _claim_success_download;
            # otherwise renewing next_poll_at here would make it busy forever.
            if task.status in {"downloading", "sync_processing"}:
                task.status = "asset_repairing"
            task.next_poll_at = now + timedelta(seconds=lease_seconds)
        await db.commit()
        return [task.id for task in tasks]


async def reconcile_pending_media_generation_tasks() -> int:
    task_ids = await _claim_due_task_ids()
    settings = get_settings()
    concurrency = max(
        min(int(settings.MEDIA_GENERATION_RECONCILIATION_CONCURRENCY), 16),
        1,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def reconcile_one(task_id: uuid.UUID) -> None:
        async with semaphore:
            try:
                task = await _load_task(task_id)
                if not task:
                    return
                if task.modality == "video":
                    runtime_managed = bool(
                        (getattr(task, "request_metadata", None) or {}).get(
                            "runtime_managed_completion"
                        )
                    )
                    if runtime_managed:
                        await reconcile_minimax_video_task(
                            task_id,
                            deliver_completion=False,
                        )
                    else:
                        await reconcile_minimax_video_task(task_id)
                elif task.modality in _SYNC_MEDIA_MODALITIES:
                    runtime_managed = bool(
                        (getattr(task, "request_metadata", None) or {}).get(
                            "runtime_managed_completion"
                        )
                    )
                    if runtime_managed:
                        await reconcile_minimax_sync_media_task(
                            task_id,
                            deliver_completion=False,
                        )
                    else:
                        await reconcile_minimax_sync_media_task(task_id)
                else:
                    await _record_media_failure_issue(
                        task,
                        f"Unsupported media reconciliation modality: {task.modality}",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[media] task reconciliation crashed task_id={}",
                    task_id,
                )

    await asyncio.gather(*(reconcile_one(task_id) for task_id in task_ids))
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
            expired_brand_assets = await cleanup_expired_video_brand_assets()
            expired_sync_assets = await cleanup_expired_sync_recovery_assets()
            if (
                backfilled
                or reconciled
                or published
                or expired_brand_assets
                or expired_sync_assets
            ):
                logger.info(
                    "[media] reconciliation complete backfilled={} reconciled={} published={} "
                    "expired_brand_assets={} expired_sync_assets={}",
                    backfilled,
                    reconciled,
                    published,
                    expired_brand_assets,
                    expired_sync_assets,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[media] generation daemon iteration failed")
        await asyncio.sleep(interval)
