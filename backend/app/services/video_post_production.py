"""FR-V5: server-owned deterministic post-production for v2 video.

Concatenation, captions/CTA, voiceover mixing, loudness, and the cover frame
are one deterministic stage driven only by durable facts: the versioned clips
of succeeded ``shot_generate`` units, the approved storyboard, and the already
synthesized voiceover asset.  This module never calls a Provider and never
regenerates a shot; a missing input fails the stage instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
from typing import Any
from urllib.parse import unquote, urlsplit
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_tool_execution import AgentToolExecution
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services.creative_briefs import VIDEO_V2_WORKFLOW_ID, VideoBrief
from app.services.deliverable_artifacts import (
    DeliverableArtifactError,
    _ensure_immutable_snapshot,
)
from app.services.media_assets import (
    MediaContractError,
    compose_video_audio_tracks,
    concat_shots,
    apply_video_text_overlay,
    extract_video_cover_frame,
    validate_generated_video,
    validate_video_delivery_contract,
)
from app.services.storage import agent_storage_key, get_storage_backend


POST_PRODUCTION_SCHEMA_VERSION = "video-post-v1"

_ACTIVE_UNIT_STATUSES = frozenset({"pending", "running", "blocked", "reconciling"})
_LEGACY_COMPOSE_TOOLS = ("compose_video_audio",)
_TTS_TOOLS = ("generate_speech_minimax",)


def final_video_workspace_path(request_id: uuid.UUID | str) -> str:
    return f"workspace/deliverables/{request_id}/final.mp4"


def cover_workspace_path(request_id: uuid.UUID | str) -> str:
    return f"workspace/deliverables/{request_id}/cover.jpg"


async def _run_tool_artifact_refs(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    tool_names: tuple[str, ...],
) -> tuple[tuple[AgentToolExecution, tuple[str, ...]], ...]:
    """Succeeded tool executions of the current run with their artifact refs."""

    if request.agent_run_id is None:
        return ()
    result = await db.execute(
        select(AgentToolExecution).where(
            AgentToolExecution.tenant_id == request.tenant_id,
            AgentToolExecution.run_id == request.agent_run_id,
            AgentToolExecution.tool_name.in_(tool_names),
            AgentToolExecution.status == "succeeded",
        )
    )
    found: list[tuple[AgentToolExecution, tuple[str, ...]]] = []
    for execution in result.scalars().all():
        metadata = (
            execution.result_metadata
            if isinstance(execution.result_metadata, Mapping)
            else {}
        )
        refs = metadata.get("artifact_refs")
        found.append(
            (
                execution,
                tuple(ref for ref in (refs or ()) if isinstance(ref, str) and ref),
            )
        )
    return tuple(found)


def _voiceover_workspace_path(
    refs: tuple[tuple[AgentToolExecution, tuple[str, ...]], ...],
    request: DeliverableRequest,
) -> str | None:
    prefix = f"workspace/deliverables/{request.id}/"
    for _execution, references in refs:
        for reference in references:
            try:
                parsed = urlsplit(reference)
                normalized = unquote(parsed.path).replace("\\", "/").lstrip("/")
            except (TypeError, ValueError):
                continue
            if parsed.scheme != "workspace" or parsed.netloc != str(request.agent_id):
                continue
            if not normalized.startswith(prefix):
                continue
            filename = normalized.rsplit("/", 1)[-1]
            if filename.startswith("voiceover") and filename.endswith(".mp3"):
                return normalized
    return None


def _unit_map(
    units: list[DeliverableExecutionUnit] | tuple[DeliverableExecutionUnit, ...],
) -> dict[tuple[str, str], DeliverableExecutionUnit]:
    return {(unit.stage_key, unit.unit_key): unit for unit in units}


async def run_video_v2_post_production(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    brief: VideoBrief,
    storyboard: Any | None,
    now: datetime | None = None,
    storage=None,
) -> bool:
    """Compose the final package from succeeded shot clips without a Provider.

    Returns ``True`` when the compose stage was fully handled here (success or
    an honest failure projection).  Returns ``False`` when the run composed the
    package through the legacy managed Tool, in which case the caller keeps the
    standard artifact reconciliation path.
    """

    if request.workflow_id != VIDEO_V2_WORKFLOW_ID or request.current_execution_id is None:
        return False
    legacy_compose = await _run_tool_artifact_refs(
        db,
        request=request,
        tool_names=_LEGACY_COMPOSE_TOOLS,
    )
    if legacy_compose:
        return False

    execution_result = await db.execute(
        select(DeliverableExecution).where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.id == request.current_execution_id,
        )
    )
    execution = execution_result.scalar_one_or_none()
    if execution is None:
        return False
    unit_result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
        )
        .with_for_update()
    )
    units = tuple(unit_result.scalars().all())
    by_stage_key = _unit_map(units)
    edit_unit = by_stage_key.get(("edit_compose", "final"))
    caption_unit = by_stage_key.get(("caption_voice_music", "final"))
    if edit_unit is not None and edit_unit.status == "succeeded" and (
        (edit_unit.result_snapshot or {}).get("artifact_revision_id")
    ):
        # Idempotent replay: the package was already composed and registered.
        return True
    timestamp = now or datetime.now(UTC)

    def fail(code: str) -> None:
        request.status = "failed"
        request.current_stage = "compose_failed"
        request.last_error_code = code[:100]
        request.completed_at = timestamp
        request.version += 1
        if edit_unit is not None and edit_unit.status in _ACTIVE_UNIT_STATUSES:
            edit_unit.status = "failed"
            edit_unit.last_error_code = code[:100]
            edit_unit.completed_at = timestamp

    shot_units = sorted(
        (unit for unit in units if unit.stage_key == "shot_generate"),
        key=lambda unit: unit.unit_key,
    )
    clip_paths = [
        str((unit.result_snapshot or {}).get("clip_path") or "")
        for unit in shot_units
        if unit.status == "succeeded"
    ]
    if not shot_units or len(clip_paths) != len(shot_units) or not all(clip_paths):
        fail("deliverable_shot_clips_missing")
        return True

    storage_backend = storage or get_storage_backend()
    clips: list[bytes] = []
    try:
        for clip_path in clip_paths:
            clips.append(
                await storage_backend.read_bytes(
                    agent_storage_key(request.agent_id, clip_path)
                )
            )
    except Exception:
        fail("deliverable_shot_clips_missing")
        return True

    # Fail fast on a missing voiceover asset before spending any local encode
    # work; the stage never regenerates or re-synthesizes its inputs.
    voiceover_path: str | None = None
    voiceover_raw: bytes | None = None
    if brief.audio_mode == "voiceover":
        voiceover_refs = await _run_tool_artifact_refs(
            db,
            request=request,
            tool_names=_TTS_TOOLS,
        )
        voiceover_path = _voiceover_workspace_path(voiceover_refs, request)
        if voiceover_path is not None:
            try:
                voiceover_raw = await storage_backend.read_bytes(
                    agent_storage_key(request.agent_id, voiceover_path)
                )
            except Exception:
                voiceover_raw = None
        if voiceover_raw is None:
            fail("deliverable_voiceover_missing")
            return True

    receipt: dict[str, Any] = {
        "schema_version": POST_PRODUCTION_SCHEMA_VERSION,
        "shot_count": len(clips),
        "audio_mode": brief.audio_mode,
        "composed_at": timestamp.isoformat(),
    }
    try:
        if len(clips) == 1:
            composed = clips[0]
            concat_receipt: dict[str, Any] = {
                "mode": "single_shot_passthrough",
                "shot_sha256": [hashlib.sha256(clips[0]).hexdigest()],
            }
        else:
            composed, concat_receipt = await concat_shots(clips, label="Shot clip")
        receipt["concat"] = concat_receipt
        # Transitions are deterministic hard cuts at the concat seam; the
        # approved storyboard's transition notes are recorded, not re-imagined.
        receipt["transitions"] = [
            str(getattr(shot, "transition", "") or "cut")
            for shot in (getattr(storyboard, "shots", ()) or ())
        ]

        overlay_parts = [
            part
            for part in (brief.caption_spec.strip(), brief.cta.strip())
            if part
        ]
        if overlay_parts:
            overlay_text = " · ".join(dict.fromkeys(overlay_parts))
            composed = await apply_video_text_overlay(composed, overlay_text)
            receipt["captions"] = {
                "mode": "deterministic_text_overlay",
                "text_sha256": hashlib.sha256(overlay_text.encode("utf-8")).hexdigest(),
            }
        else:
            receipt["captions"] = {"mode": "none"}

        if brief.audio_mode == "voiceover":
            # Presence was verified before any encode work; the mix applies
            # the loudness guard (alimiter=0.95) and records track hashes.
            assert voiceover_raw is not None and voiceover_path is not None
            composed, mix_receipt = await compose_video_audio_tracks(
                composed,
                voiceover_raw=voiceover_raw,
                voiceover_format="mp3",
            )
            receipt["audio_mix"] = {
                **mix_receipt.as_dict(),
                "voiceover_path": voiceover_path,
                "loudness": "alimiter-0.95",
            }
        elif brief.audio_mode == "in_scene_dialogue":
            receipt["audio_mix"] = {
                "mode": "source_audio_retained",
                "loudness": "source_passthrough",
            }
        else:
            receipt["audio_mix"] = {"mode": "silent", "loudness": "none"}

        final_info = await validate_generated_video(composed, label="v2 final package")
        validate_video_delivery_contract(
            final_info,
            expected_duration_seconds=brief.duration_seconds,
            expected_aspect_ratio=brief.aspect_ratio,
            require_audio=brief.audio_mode != "silent",
        )
        cover_bytes, cover_receipt = await extract_video_cover_frame(composed)
        receipt["cover"] = cover_receipt
    except MediaContractError:
        fail("deliverable_compose_contract_failed")
        return True

    final_path = final_video_workspace_path(request.id)
    cover_path = cover_workspace_path(request.id)
    await storage_backend.write_bytes(
        agent_storage_key(request.agent_id, final_path),
        composed,
        content_type="video/mp4",
    )
    await storage_backend.write_bytes(
        agent_storage_key(request.agent_id, cover_path),
        cover_bytes,
        content_type="image/jpeg",
    )
    receipt["final_path"] = final_path
    receipt["cover_path"] = cover_path

    artifact = DeliverableArtifactRevision(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        unit_id=edit_unit.id if edit_unit is not None else None,
        artifact_key="mp4",
        artifact_type="mp4",
        stage_key="edit_compose",
        unit_key="final",
        workspace_path=final_path,
        mime_type="video/mp4",
        content_hash=hashlib.sha256(composed).hexdigest(),
        size_bytes=len(composed),
        revision_number=1,
        status="candidate",
        evaluation={
            "version": 1,
            "verified": True,
            "verification_level": "contract",
            "source": "server_deterministic_post_production",
            "checks": [
                "tenant_scope",
                "shot_clips_complete",
                "duration",
                "aspect_ratio",
                "browser_codec",
                "audio_contract",
                "immutable_snapshot",
            ],
            "facts": {
                "duration_seconds": final_info.duration_seconds,
                "width": final_info.width,
                "height": final_info.height,
                "codec_name": final_info.codec_name,
                "pixel_format": final_info.pixel_format,
                "audio_codec_name": final_info.audio_codec_name,
                "fast_start": final_info.fast_start,
            },
            "post_production": receipt,
        },
    )
    try:
        await _ensure_immutable_snapshot(storage_backend, artifact=artifact, data=composed)
    except DeliverableArtifactError:
        fail("deliverable_artifact_snapshot_unavailable")
        return True
    # Keep the immutable lineage: any earlier candidate mp4 is superseded.
    existing_result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
            DeliverableArtifactRevision.request_id == request.id,
            DeliverableArtifactRevision.artifact_key == "mp4",
            DeliverableArtifactRevision.status == "candidate",
        )
        .with_for_update()
    )
    for prior in existing_result.scalars().all():
        prior.status = "superseded"
        artifact.revision_number = max(artifact.revision_number, prior.revision_number + 1)
        if artifact.parent_revision_id is None:
            artifact.parent_revision_id = prior.id
    db.add(artifact)

    if edit_unit is not None:
        edit_unit.status = "succeeded"
        edit_unit.completed_at = timestamp
        edit_unit.last_error_code = None
        edit_unit.result_snapshot = {
            **dict(edit_unit.result_snapshot or {}),
            "artifact_revision_id": str(artifact.id),
            "final_path": final_path,
            "content_hash": artifact.content_hash,
            "post_production": receipt,
        }
    if caption_unit is not None and caption_unit.status in _ACTIVE_UNIT_STATUSES:
        caption_unit.status = "succeeded"
        caption_unit.completed_at = timestamp
        caption_unit.last_error_code = None
        caption_unit.result_snapshot = {
            **dict(caption_unit.result_snapshot or {}),
            "captions": receipt.get("captions"),
            "audio_mix": receipt.get("audio_mix"),
        }
    request.status = "waiting_approval"
    request.current_stage = "output_review"
    request.completed_at = None
    request.last_error_code = None
    request.version += 1
    await db.flush()
    return True


__all__ = [
    "POST_PRODUCTION_SCHEMA_VERSION",
    "cover_workspace_path",
    "final_video_workspace_path",
    "run_video_v2_post_production",
]
