"""Storyboard compilation and per-shot orchestration for v2 video (FR-V1~V4).

The storyboard compiler is a pure function: it normalizes the Runtime-drafted
storyboard into validated per-shot specs and never calls a Provider or reserves
Credits.  Paid work stays behind the storyboard approval receipt — the shot and
keyframe Tool bindings in this module fail closed before any Provider
submission when the storyboard was never approved, and the deliverable-side
reconciler advances units only from durable media-task facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditReservation, CreditTransaction
from app.services.candidate_qa import (
    CandidateQaCheck,
    CandidateQaReport,
    candidate_qa_enforcement_for_request,
    evaluate_video_shot,
    evaluate_video_v2_package,
)
from app.services.creative_briefs import (
    VIDEO_V2_WORKFLOW_ID,
    VideoBrief,
    compile_video_brief,
)
from app.services.media_assets import MediaContractError
from app.services.video_post_production import run_video_v2_post_production
from app.services.prompt_compiler import (
    VIDEO_KEYFRAME_COMPILER_VERSION,
    VIDEO_SHOT_COMPILER_VERSION,
)
from app.services.storage import agent_storage_key, get_storage_backend


STORYBOARD_SCHEMA_VERSION = "storyboard-v1"
DEFAULT_MAX_SHOT_DURATION_SECONDS = 15

_ACTIVE_UNIT_STATUSES = frozenset({"pending", "running", "blocked", "reconciling"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

_SHOT_PATH_RE = re.compile(
    r"^workspace/deliverables/(?P<request_id>[0-9a-fA-F-]{36})/"
    r"shots/(?P<unit_key>shot-\d{2})(?:_[0-9a-f]{12})?\.mp4$"
)
_KEYFRAME_PATH_RE = re.compile(
    r"^workspace/deliverables/(?P<request_id>[0-9a-fA-F-]{36})/"
    r"keyframes/(?P<unit_key>shot-\d{2})(?:_[0-9a-f]{12})?\.(?:png|jpg|jpeg)$"
)


class StoryboardError(RuntimeError):
    """A drafted storyboard violates the paid-work storyboard contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ShotSpec(BaseModel):
    """One provider-neutral shot; the customer contract never names a model."""

    shot_id: str = Field(pattern=r"^shot-\d{2}$")
    duration_seconds: int = Field(ge=1, le=60)
    visual: str = Field(min_length=1, max_length=2000)
    camera: str = Field(default="", max_length=500)
    subject_refs: tuple[str, ...] = ()
    first_frame_ref: str | None = Field(default=None, max_length=1000)
    last_frame_ref: str | None = Field(default=None, max_length=1000)
    dialogue: str = Field(default="", max_length=2000)
    caption: str = Field(default="", max_length=1000)
    transition: str = Field(default="", max_length=64)

    model_config = ConfigDict(extra="forbid", frozen=True)


class Storyboard(BaseModel):
    """Validated, hash-bound storyboard receipt payload."""

    schema_version: str = STORYBOARD_SCHEMA_VERSION
    shots: tuple[ShotSpec, ...]
    voiceover_script: str = Field(default="", max_length=4000)
    storyboard_sha256: str = Field(min_length=64, max_length=64)

    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_storyboard(
    brief: VideoBrief,
    raw_storyboard: Mapping[str, Any] | None,
    *,
    expected_shot_count: int,
    max_shot_duration_seconds: int = DEFAULT_MAX_SHOT_DURATION_SECONDS,
) -> Storyboard:
    """Normalize the drafted storyboard into validated per-shot specs.

    Fail closed on any contract violation: a storyboard that does not match the
    approved brief must never reach a paid shot submission.
    """

    if not isinstance(raw_storyboard, Mapping):
        raise StoryboardError(
            "storyboard_not_object",
            "storyboard.json must contain a JSON object",
        )
    raw_shots = raw_storyboard.get("shots")
    if not isinstance(raw_shots, Sequence) or isinstance(raw_shots, (str, bytes)):
        raise StoryboardError(
            "storyboard_shots_missing",
            "storyboard.json must contain a shots array",
        )
    expected = max(int(expected_shot_count), 1)
    if len(raw_shots) != expected:
        raise StoryboardError(
            "storyboard_shot_count_mismatch",
            f"storyboard must contain exactly {expected} shots, found {len(raw_shots)}",
        )
    shots: list[ShotSpec] = []
    for index, raw_shot in enumerate(raw_shots, start=1):
        expected_id = f"shot-{index:02d}"
        if not isinstance(raw_shot, Mapping):
            raise StoryboardError(
                "storyboard_shot_invalid",
                f"{expected_id} must be a JSON object",
            )
        candidate = {**dict(raw_shot), "shot_id": str(raw_shot.get("shot_id") or expected_id)}
        try:
            shot = ShotSpec.model_validate(candidate)
        except ValidationError as exc:
            raise StoryboardError(
                "storyboard_shot_invalid",
                f"{expected_id} is invalid: {exc.errors()[0].get('msg')}",
            ) from exc
        if shot.shot_id != expected_id:
            raise StoryboardError(
                "storyboard_shot_id_sequence",
                f"shots must be ordered shot-01..shot-{expected:02d}; found {shot.shot_id}",
            )
        if shot.duration_seconds > max_shot_duration_seconds:
            raise StoryboardError(
                "storyboard_shot_too_long",
                f"{expected_id} lasts {shot.duration_seconds}s; the provider limit is "
                f"{max_shot_duration_seconds}s",
            )
        shots.append(shot)

    total_duration = sum(shot.duration_seconds for shot in shots)
    if total_duration != brief.duration_seconds:
        raise StoryboardError(
            "storyboard_duration_mismatch",
            f"shot durations sum to {total_duration}s; the brief requires exactly "
            f"{brief.duration_seconds}s",
        )

    voiceover_script = str(raw_storyboard.get("voiceover_script") or "").strip()
    has_dialogue = any(shot.dialogue.strip() for shot in shots)
    if brief.audio_mode == "silent":
        if has_dialogue or voiceover_script:
            raise StoryboardError(
                "storyboard_audio_mode_mismatch",
                "a silent brief must not contain dialogue or a voiceover script",
            )
    elif brief.audio_mode == "voiceover":
        if has_dialogue:
            raise StoryboardError(
                "storyboard_audio_mode_mismatch",
                "a voiceover brief must not contain in-scene dialogue",
            )
        if not voiceover_script:
            raise StoryboardError(
                "storyboard_voiceover_missing",
                "a voiceover brief requires a voiceover_script",
            )
    else:  # in_scene_dialogue
        if not has_dialogue:
            raise StoryboardError(
                "storyboard_dialogue_missing",
                "an in-scene-dialogue brief requires dialogue on at least one shot",
            )
        if voiceover_script:
            raise StoryboardError(
                "storyboard_audio_mode_mismatch",
                "an in-scene-dialogue brief must not add a voiceover script",
            )

    digest = _canonical_sha256(
        {
            "schema_version": STORYBOARD_SCHEMA_VERSION,
            "brief_sha256": _canonical_sha256(brief.model_dump(mode="json")),
            "shots": [shot.model_dump(mode="json") for shot in shots],
            "voiceover_script": voiceover_script,
        }
    )
    return Storyboard(
        shots=tuple(shots),
        voiceover_script=voiceover_script,
        storyboard_sha256=digest,
    )


def storyboard_workspace_path(request_id: uuid.UUID | str) -> str:
    return f"workspace/deliverables/{request_id}/storyboard.json"


def shot_clip_workspace_path(request_id: uuid.UUID | str, unit_key: str) -> str:
    return f"workspace/deliverables/{request_id}/shots/{unit_key}.mp4"


def keyframe_workspace_path(request_id: uuid.UUID | str, unit_key: str) -> str:
    return f"workspace/deliverables/{request_id}/keyframes/{unit_key}.png"


def video_v2_shot_unit_key(value: object) -> str | None:
    """Parse the server-owned shot unit key out of a workspace path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    match = _SHOT_PATH_RE.match(normalized)
    return match.group("unit_key") if match else None


def video_v2_keyframe_unit_key(value: object) -> str | None:
    """Parse the server-owned keyframe unit key out of a workspace path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    match = _KEYFRAME_PATH_RE.match(normalized)
    return match.group("unit_key") if match else None


async def storyboard_approved(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> bool:
    """A storyboard approval receipt on any execution of the request counts."""

    result = await db.execute(
        select(DeliverableApprovalReceipt)
        .where(
            DeliverableApprovalReceipt.tenant_id == tenant_id,
            DeliverableApprovalReceipt.request_id == request_id,
            DeliverableApprovalReceipt.stage == "storyboard",
            DeliverableApprovalReceipt.action == "approve",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def load_latest_storyboard(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> Storyboard | None:
    """Load the newest successfully compiled storyboard across executions."""

    result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == tenant_id,
            DeliverableExecutionUnit.request_id == request_id,
            DeliverableExecutionUnit.stage_key == "storyboard",
            DeliverableExecutionUnit.status == "succeeded",
        )
        .order_by(
            DeliverableExecutionUnit.created_at.desc(),
            DeliverableExecutionUnit.id.desc(),
        )
        .limit(1)
    )
    unit = result.scalar_one_or_none()
    payload = (unit.result_snapshot or {}).get("storyboard") if unit else None
    if not isinstance(payload, Mapping):
        return None
    try:
        return Storyboard.model_validate(payload)
    except ValidationError:
        return None


@dataclass(frozen=True, slots=True)
class VideoV2UnitBinding:
    execution_id: uuid.UUID
    unit_id: uuid.UUID
    unit_key: str


async def _resolve_video_v2_unit(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    request_id: uuid.UUID,
    save_path: str,
    prompt: str,
    stage_key: str,
    compiler_version: str,
    unit_key_fn,
    duration_seconds: int | None = None,
    has_first_frame: bool = False,
) -> VideoV2UnitBinding | None:
    """Bind one managed media Tool call to its shot/keyframe unit, fail closed.

    Returns ``None`` for non-v2 requests so the v1 path is untouched.  For a v2
    video request the server-owned save_path naming, the storyboard approval
    receipt, and the verbatim compiled prompt are hard requirements checked
    before any Provider spend or Credits reservation.
    """

    async with async_session() as db:
        result = await db.execute(
            select(DeliverableRequest).where(
                DeliverableRequest.id == request_id,
                DeliverableRequest.tenant_id == tenant_id,
                DeliverableRequest.agent_id == agent_id,
            )
        )
        request = result.scalar_one_or_none()
        if request is None or request.workflow_id != VIDEO_V2_WORKFLOW_ID:
            return None

        unit_key = unit_key_fn(save_path)
        if unit_key is None:
            raise MediaContractError(
                f"v2 video {stage_key} outputs must save to "
                f"workspace/deliverables/<request_id>/"
                f"{'shots' if stage_key == 'shot_generate' else 'keyframes'}/shot-NN."
                f"{'mp4' if stage_key == 'shot_generate' else 'png'}"
            )
        execution = None
        if request.current_execution_id is not None:
            execution_result = await db.execute(
                select(DeliverableExecution).where(
                    DeliverableExecution.id == request.current_execution_id,
                    DeliverableExecution.tenant_id == tenant_id,
                )
            )
            execution = execution_result.scalar_one_or_none()
        if execution is None:
            raise MediaContractError("v2 video request has no active execution")
        unit_result = await db.execute(
            select(DeliverableExecutionUnit).where(
                DeliverableExecutionUnit.tenant_id == tenant_id,
                DeliverableExecutionUnit.execution_id == execution.id,
                DeliverableExecutionUnit.stage_key == stage_key,
                DeliverableExecutionUnit.unit_key == unit_key,
            )
        )
        unit = unit_result.scalar_one_or_none()
        if unit is None:
            raise MediaContractError(f"Unknown v2 video {stage_key} unit: {unit_key}")
        if unit.media_generation_task_id is not None:
            # One durable media task per unit, ever.  A repeated Tool call for
            # an already-submitted shot would double-charge; the daemon drives
            # the existing task instead.
            raise MediaContractError(
                f"v2 video unit {unit_key} already has a durable media task; "
                "resubmission is rejected to protect Credits"
            )
        if not await storyboard_approved(
            db,
            tenant_id=tenant_id,
            request_id=request.id,
        ):
            raise MediaContractError(
                "deliverable_storyboard_approval_required: the storyboard must be "
                "approved before any paid shot or keyframe is submitted"
            )

        spec = request.spec if isinstance(request.spec, Mapping) else {}
        aspect_ratio = str(spec.get("aspect_ratio") or "").strip()
        if (
            stage_key == "shot_generate"
            and aspect_ratio
            and aspect_ratio != "16:9"
            and not has_first_frame
        ):
            raise MediaContractError(
                "media_video_requires_first_frame_for_aspect_ratio: a non-16:9 "
                "shot must submit with its approved same-aspect keyframe as "
                "first_frame_image; text-to-video fallback is rejected before "
                "any Provider submission"
            )

        if stage_key == "shot_generate" and duration_seconds is not None:
            storyboard = await load_latest_storyboard(
                db,
                tenant_id=tenant_id,
                request_id=request.id,
            )
            shot_spec = next(
                (shot for shot in (storyboard.shots if storyboard else ()) if shot.shot_id == unit_key),
                None,
            )
            if shot_spec is not None and int(duration_seconds) != shot_spec.duration_seconds:
                raise MediaContractError(
                    f"v2 video shot {unit_key} must keep the approved duration "
                    f"{shot_spec.duration_seconds}s, not {duration_seconds}s"
                )

        compilation_result = await db.execute(
            select(DeliverablePromptCompilation).where(
                DeliverablePromptCompilation.tenant_id == tenant_id,
                DeliverablePromptCompilation.execution_id == execution.id,
                DeliverablePromptCompilation.unit_id == unit.id,
                DeliverablePromptCompilation.compiler_version == compiler_version,
            )
        )
        compilation = compilation_result.scalar_one_or_none()
        if compilation is None:
            raise MediaContractError(
                f"v2 video unit {unit_key} has no compiled prompt receipt"
            )
        storage = get_storage_backend()
        try:
            compiled_bytes = await storage.read_bytes(
                agent_storage_key(agent_id, compilation.compiled_prompt_path)
            )
        except FileNotFoundError as exc:
            raise MediaContractError(
                f"v2 video unit {unit_key} compiled prompt is missing"
            ) from exc
        expected_prompt = compiled_bytes.decode("utf-8").strip()
        if str(prompt or "").strip() != expected_prompt:
            raise MediaContractError(
                "v2 video shot prompts must be passed verbatim from the "
                "server-compiled prompt file; rewritten prompts are rejected"
            )
        return VideoV2UnitBinding(
            execution_id=execution.id,
            unit_id=unit.id,
            unit_key=unit_key,
        )


async def resolve_video_v2_shot_unit(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    request_id: uuid.UUID | str | None,
    save_path: str,
    prompt: str,
    duration_seconds: int | None = None,
    has_first_frame: bool = False,
) -> VideoV2UnitBinding | None:
    if request_id in (None, ""):
        return None
    return await _resolve_video_v2_unit(
        tenant_id=tenant_id,
        agent_id=agent_id,
        request_id=uuid.UUID(str(request_id)),
        save_path=save_path,
        prompt=prompt,
        stage_key="shot_generate",
        compiler_version=VIDEO_SHOT_COMPILER_VERSION,
        unit_key_fn=video_v2_shot_unit_key,
        duration_seconds=duration_seconds,
        has_first_frame=has_first_frame,
    )


async def resolve_video_v2_keyframe_unit(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    request_id: uuid.UUID | str | None,
    save_path: str,
    prompt: str,
) -> VideoV2UnitBinding | None:
    if request_id in (None, ""):
        return None
    return await _resolve_video_v2_unit(
        tenant_id=tenant_id,
        agent_id=agent_id,
        request_id=uuid.UUID(str(request_id)),
        save_path=save_path,
        prompt=prompt,
        stage_key="keyframe_pack",
        compiler_version=VIDEO_KEYFRAME_COMPILER_VERSION,
        unit_key_fn=video_v2_keyframe_unit_key,
    )


async def mark_video_v2_unit_submitted(
    binding: VideoV2UnitBinding,
    *,
    tenant_id: uuid.UUID,
    media_task_id: uuid.UUID,
) -> None:
    """Attach the durable media task to its shot/keyframe unit exactly once."""

    async with async_session() as db:
        result = await db.execute(
            select(DeliverableExecutionUnit)
            .where(
                DeliverableExecutionUnit.id == binding.unit_id,
                DeliverableExecutionUnit.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        unit = result.scalar_one_or_none()
        if unit is None or unit.media_generation_task_id not in (None, media_task_id):
            return
        unit.media_generation_task_id = media_task_id
        if unit.status == "pending":
            unit.status = "running"
            unit.started_at = datetime.now(UTC)
        unit.attempt_count = int(unit.attempt_count or 0) + 1
        await db.commit()


async def _run_shot_qa(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    execution: DeliverableExecution,
    shot_unit: DeliverableExecutionUnit,
    qa_unit: DeliverableExecutionUnit | None,
    clip_path: str,
    now: datetime,
    storage=None,
) -> None:
    """FR-V7: evaluate one succeeded shot and bind the report to its hash."""

    brief, _missing = compile_video_brief(request.goal, request.spec, request.inputs)
    settings = get_settings()
    enforcement = candidate_qa_enforcement_for_request(
        request,
        mode=settings.DELIVERABLE_CREATIVE_QA_ENFORCEMENT,
        tenant_ids=settings.DELIVERABLE_CREATIVE_QA_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_CREATIVE_QA_AGENT_IDS,
    )
    storyboard = await load_latest_storyboard(
        db,
        tenant_id=request.tenant_id,
        request_id=request.id,
    )
    shot_spec = next(
        (
            shot
            for shot in (storyboard.shots if storyboard else ())
            if shot.shot_id == shot_unit.unit_key
        ),
        None,
    )
    keyframe_unit_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
            DeliverableExecutionUnit.stage_key == "keyframe_pack",
            DeliverableExecutionUnit.unit_key == shot_unit.unit_key,
        )
    )
    keyframe_unit = keyframe_unit_result.scalar_one_or_none()
    storage_backend = storage or get_storage_backend()
    first_frame_bytes: bytes | None = None
    keyframe_path = str(
        (keyframe_unit.result_snapshot or {}).get("keyframe_path") or ""
    ) if keyframe_unit is not None else ""
    if keyframe_path:
        try:
            first_frame_bytes = await storage_backend.read_bytes(
                agent_storage_key(request.agent_id, keyframe_path)
            )
        except Exception:
            first_frame_bytes = None
    expected_copy: list[str] = []
    if shot_spec is not None and shot_spec.caption.strip():
        expected_copy.append(shot_spec.caption.strip())
    if brief is not None and brief.cta.strip():
        expected_copy.append(brief.cta.strip())
    try:
        data = await storage_backend.read_bytes(
            agent_storage_key(request.agent_id, clip_path)
        )
    except Exception:
        report = CandidateQaReport(
            schema_version="shot-qa-v1",
            unit_key=shot_unit.unit_key,
            artifact_path=clip_path,
            artifact_sha256="0" * 64,
            status="failed",
            score=0,
            checks=(
                CandidateQaCheck(
                    name="artifact_decodable",
                    status="failed",
                    evidence=("shot bytes unavailable in storage",),
                ),
            ),
        )
    else:
        report = await evaluate_video_shot(
            data=data,
            unit_key=shot_unit.unit_key,
            artifact_path=clip_path,
            expected_aspect_ratio=(brief.aspect_ratio if brief else None),
            expected_duration_seconds=(
                shot_spec.duration_seconds if shot_spec is not None else None
            ),
            require_audio=bool(brief and brief.audio_mode == "in_scene_dialogue"),
            expected_copy_texts=expected_copy,
            prohibited_terms=(brief.prohibitions if brief else ()),
            first_frame_bytes=first_frame_bytes,
            expected_languages=((brief.language,) if brief else ("zh-CN", "en-US")),
        )
    if qa_unit is not None:
        qa_unit.quality_evaluation = {
            "shot_qa": report.model_dump(mode="json"),
            "enforcement": enforcement,
            "evaluated_at": now.isoformat(),
        }
        if enforcement == "enforcing":
            if report.status == "failed" and qa_unit.status != "failed":
                qa_unit.status = "failed"
                qa_unit.last_error_code = "shot_qa_failed"
                qa_unit.completed_at = now
            elif report.status == "passed" and qa_unit.status in _ACTIVE_UNIT_STATUSES:
                qa_unit.status = "succeeded"
                qa_unit.completed_at = now


async def reconcile_video_v2_units(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    now: datetime | None = None,
    storage=None,
) -> int:
    """Advance shot/keyframe units from durable media-task facts (FR-V3).

    A failed shot never drags the whole request down: sibling units keep their
    own state, and the request parks in ``shot_review`` for a targeted revision
    instead of failing wholesale.
    """

    if request.workflow_id != VIDEO_V2_WORKFLOW_ID or request.current_execution_id is None:
        return 0
    execution_result = await db.execute(
        select(DeliverableExecution)
        .where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.id == request.current_execution_id,
        )
        .with_for_update()
    )
    execution = execution_result.scalar_one_or_none()
    if execution is None:
        return 0
    unit_result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
        )
        .with_for_update()
    )
    units = tuple(unit_result.scalars().all())
    media_units = {
        unit.id: unit
        for unit in units
        if unit.stage_key in {"shot_generate", "keyframe_pack"}
    }
    qa_units = {
        unit.unit_key: unit for unit in units if unit.stage_key == "shot_qa"
    }
    timestamp = now or datetime.now(UTC)
    advanced = 0
    if media_units:
        task_result = await db.execute(
            select(MediaGenerationTask).where(
                MediaGenerationTask.deliverable_unit_id.in_(tuple(media_units))
            )
        )
        for task in task_result.scalars().all():
            unit = media_units.get(task.deliverable_unit_id)
            if (
                unit is None
                or unit.media_generation_task_id != task.id
                or unit.status not in _ACTIVE_UNIT_STATUSES
            ):
                continue
            if task.status == "succeeded":
                unit.status = "succeeded"
                unit.completed_at = timestamp
                unit.last_error_code = None
                path_key = (
                    "clip_path" if unit.stage_key == "shot_generate" else "keyframe_path"
                )
                unit.result_snapshot = {
                    **dict(unit.result_snapshot or {}),
                    path_key: task.output_path,
                    "media_generation_task_id": str(task.id),
                    "reconciled_via": "media_daemon",
                }
                advanced += 1
                if unit.stage_key == "shot_generate":
                    await _run_shot_qa(
                        db,
                        request=request,
                        execution=execution,
                        shot_unit=unit,
                        qa_unit=qa_units.get(unit.unit_key),
                        clip_path=str(task.output_path or ""),
                        now=timestamp,
                        storage=storage,
                    )
            elif task.status in {"failed", "cancelled"}:
                unit.status = "failed"
                unit.completed_at = timestamp
                unit.last_error_code = "media_task_failed"
                unit.result_snapshot = {
                    **dict(unit.result_snapshot or {}),
                    "media_generation_task_id": str(task.id),
                    "reconciled_via": "media_daemon",
                }
                advanced += 1

    shot_units = [unit for unit in units if unit.stage_key == "shot_generate"]
    if (
        shot_units
        and request.current_stage == "shot_generation"
        and all(unit.status not in _ACTIVE_UNIT_STATUSES for unit in shot_units)
    ):
        qa_failed = any(
            unit.status == "failed" for unit in qa_units.values()
        )
        shots_ok = all(unit.status == "succeeded" for unit in shot_units)
        # Single-shot failure never fails the whole request: the request parks
        # in a reviewable state so only failed shots are redone (FR-V3/V4).
        request.status = "ready"
        request.current_stage = (
            "compose_ready" if shots_ok and not qa_failed else "shot_review"
        )
        request.agent_run_id = None
        request.version += 1
    return advanced


async def reconcile_pending_video_v2_deliverables(limit: int = 20) -> int:
    """Daemon entry point: advance every v2 video request awaiting shot facts."""

    async with async_session() as db:
        result = await db.execute(
            select(DeliverableRequest)
            .where(
                DeliverableRequest.workflow_id == VIDEO_V2_WORKFLOW_ID,
                DeliverableRequest.status == "running",
                DeliverableRequest.current_stage == "shot_generation",
            )
            .order_by(DeliverableRequest.updated_at)
            .limit(max(int(limit), 1))
        )
        requests = tuple(result.scalars().all())
        advanced = 0
        for request in requests:
            advanced += await reconcile_video_v2_units(db, request=request)
        await db.commit()
        return advanced


async def advance_video_v2_after_run(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    run_id: uuid.UUID,
    lifecycle_status: str = "completed",
    now: datetime | None = None,
    storage=None,
) -> bool:
    """Project a terminated v2 video run onto the storyboard/shot stages.

    Returns ``True`` when the stage was fully handled here (storyboard draft
    and shot submission); the compose stage returns ``False`` so the caller can
    run the standard artifact reconciliation for the final MP4.
    """

    if request.workflow_id != VIDEO_V2_WORKFLOW_ID:
        return False
    del run_id
    timestamp = now or datetime.now(UTC)
    stage = str(request.current_stage or "")
    cancelled = str(lifecycle_status or "").strip().lower() == "cancelled"

    if stage == "shot_generation":
        if cancelled:
            request.status = "cancelled"
            request.current_stage = "cancelled"
            request.completed_at = timestamp
            request.version += 1
            return True
        await reconcile_video_v2_units(db, request=request, now=timestamp, storage=storage)
        return True

    if stage == "compose":
        if cancelled:
            return False
        # FR-V5: deterministic post-production is a server-owned stage fed by
        # the versioned shot clips; the legacy agent-composed package (a
        # succeeded compose_video_audio call in this run) keeps the standard
        # artifact reconciliation path.
        brief, _missing = compile_video_brief(request.goal, request.spec, request.inputs)
        if brief is not None:
            storyboard = await load_latest_storyboard(
                db,
                tenant_id=request.tenant_id,
                request_id=request.id,
            )
            if await run_video_v2_post_production(
                db,
                request=request,
                brief=brief,
                storyboard=storyboard,
                now=timestamp,
                storage=storage,
            ):
                if request.status == "waiting_approval":
                    await evaluate_video_v2_package(db, request=request, storage=storage)
                return True
        await evaluate_video_v2_package(db, request=request, storage=storage)
        return False

    if stage != "storyboard_draft":
        return False

    units_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == request.current_execution_id,
        )
    )
    units = tuple(units_result.scalars().all())
    storyboard_unit = next(
        (unit for unit in units if unit.stage_key == "storyboard"), None
    )
    script_unit = next((unit for unit in units if unit.stage_key == "script"), None)
    shot_spec_units = {
        unit.unit_key: unit for unit in units if unit.stage_key == "shot_spec_compile"
    }
    expected_shots = sum(1 for unit in units if unit.stage_key == "shot_generate")

    def fail(code: str) -> None:
        request.status = "failed"
        request.current_stage = "storyboard_invalid"
        request.last_error_code = code[:100]
        request.completed_at = timestamp
        request.version += 1
        if storyboard_unit is not None:
            storyboard_unit.status = "failed"
            storyboard_unit.last_error_code = code[:100]
            storyboard_unit.completed_at = timestamp

    if cancelled:
        request.status = "cancelled"
        request.current_stage = "cancelled"
        request.completed_at = timestamp
        request.version += 1
        return True

    storage_backend = storage or get_storage_backend()
    brief, missing = compile_video_brief(request.goal, request.spec, request.inputs)
    if brief is None:
        fail(f"brief_missing:{next(iter(missing), 'unknown')}")
        return True
    try:
        raw_text = await storage_backend.read_text(
            agent_storage_key(request.agent_id, storyboard_workspace_path(request.id)),
            encoding="utf-8",
        )
    except Exception:
        fail("deliverable_storyboard_missing")
        return True
    try:
        raw_payload = json.loads(raw_text)
    except ValueError:
        fail("deliverable_storyboard_invalid")
        return True
    try:
        storyboard = compile_storyboard(
            brief,
            raw_payload if isinstance(raw_payload, Mapping) else None,
            expected_shot_count=max(expected_shots, 1),
        )
    except StoryboardError as exc:
        fail(exc.code)
        return True

    if script_unit is not None and script_unit.status in _ACTIVE_UNIT_STATUSES:
        script_unit.status = "succeeded"
        script_unit.completed_at = timestamp
    if storyboard_unit is not None:
        storyboard_unit.status = "succeeded"
        storyboard_unit.completed_at = timestamp
        storyboard_unit.last_error_code = None
        storyboard_unit.result_snapshot = {
            **dict(storyboard_unit.result_snapshot or {}),
            "storyboard": storyboard.model_dump(mode="json"),
            "storyboard_sha256": storyboard.storyboard_sha256,
        }
    for shot in storyboard.shots:
        spec_unit = shot_spec_units.get(shot.shot_id)
        if spec_unit is None:
            continue
        spec_unit.input_snapshot = {
            **dict(spec_unit.input_snapshot or {}),
            "shot_spec": shot.model_dump(mode="json"),
        }
        if spec_unit.status in _ACTIVE_UNIT_STATUSES:
            spec_unit.status = "succeeded"
            spec_unit.completed_at = timestamp
    request.status = "waiting_approval"
    request.current_stage = "storyboard_review"
    request.last_error_code = None
    request.version += 1
    return True


async def video_v2_credit_reconciliation(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
) -> dict[str, Any]:
    """FR-V4: ledger facts proving only redone shots are charged twice.

    The report is provider-free: it reads the durable reservation/transaction
    rows and surfaces duplicates or orphan consumes so tests and audits can
    assert exactly-once settlement per succeeded shot.
    """

    unit_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.request_id == request.id,
            DeliverableExecutionUnit.stage_key == "shot_generate",
        )
    )
    units = tuple(unit_result.scalars().all())
    execution_result = await db.execute(
        select(DeliverableExecution).where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.request_id == request.id,
        )
    )
    executions = {
        execution.id: execution for execution in execution_result.scalars().all()
    }
    unit_ids = tuple(unit.id for unit in units)
    task_result = await db.execute(
        select(MediaGenerationTask).where(
            MediaGenerationTask.tenant_id == request.tenant_id,
            MediaGenerationTask.deliverable_unit_id.in_(unit_ids or (uuid.uuid4(),)),
        )
    )
    tasks = tuple(task_result.scalars().all())
    tasks_by_unit: dict[uuid.UUID, MediaGenerationTask] = {}
    for task in tasks:
        if task.deliverable_unit_id is not None:
            tasks_by_unit[task.deliverable_unit_id] = task
    reservation_ids = tuple(
        dict.fromkeys(
            task.reservation_id for task in tasks if task.reservation_id is not None
        )
    )
    reservation_result = await db.execute(
        select(CreditReservation).where(
            CreditReservation.id.in_(reservation_ids or (uuid.uuid4(),))
        )
    )
    reservations = {row.id: row for row in reservation_result.scalars().all()}
    tx_result = await db.execute(
        select(CreditTransaction).where(
            CreditTransaction.tenant_id == request.tenant_id,
            CreditTransaction.reason == "consume",
            CreditTransaction.ref_type == "reservation",
            CreditTransaction.ref_id.in_(reservation_ids or (uuid.uuid4(),)),
        )
    )
    transactions = tuple(tx_result.scalars().all())
    consume_count: dict[uuid.UUID, int] = {}
    for tx in transactions:
        if tx.ref_id is None:
            continue
        consume_count[tx.ref_id] = consume_count.get(tx.ref_id, 0) + 1

    shots: list[dict[str, Any]] = []
    consumed_total = 0
    redo_consumed = 0
    for unit in sorted(units, key=lambda item: (item.created_at, item.unit_key)):
        task = tasks_by_unit.get(unit.id)
        reservation = (
            reservations.get(task.reservation_id)
            if task is not None and task.reservation_id is not None
            else None
        )
        execution = executions.get(unit.execution_id)
        consume_txs = consume_count.get(reservation.id, 0) if reservation else 0
        consumed = bool(reservation is not None and reservation.status == "finalized")
        amount = int(reservation.amount or 0) if reservation is not None else 0
        if consumed:
            consumed_total += amount
            if execution is not None and execution.kind == "revision":
                redo_consumed += amount
        shots.append(
            {
                "unit_key": unit.unit_key,
                "execution_id": str(unit.execution_id),
                "execution_number": (
                    execution.execution_number if execution is not None else None
                ),
                "execution_kind": execution.kind if execution is not None else None,
                "unit_status": unit.status,
                "media_generation_task_id": str(task.id) if task is not None else None,
                "reservation_id": (
                    str(reservation.id) if reservation is not None else None
                ),
                "reservation_status": (
                    reservation.status if reservation is not None else None
                ),
                "reservation_amount": amount,
                "consumed": consumed,
                "consume_tx_count": consume_txs,
            }
        )
    duplicate_consumes = sorted(
        str(ref_id) for ref_id, count in consume_count.items() if count > 1
    )
    return {
        "request_id": str(request.id),
        "shots": shots,
        "consume_ref_ids": sorted(str(ref_id) for ref_id in consume_count),
        "duplicate_consume_ref_ids": duplicate_consumes,
        "consumed_credits_total": consumed_total,
        "redo_consumed_credits": redo_consumed,
        "redo_share": (
            round(redo_consumed / consumed_total, 4) if consumed_total else 0.0
        ),
    }


__all__ = [
    "DEFAULT_MAX_SHOT_DURATION_SECONDS",
    "STORYBOARD_SCHEMA_VERSION",
    "ShotSpec",
    "Storyboard",
    "StoryboardError",
    "VideoV2UnitBinding",
    "advance_video_v2_after_run",
    "compile_storyboard",
    "keyframe_workspace_path",
    "load_latest_storyboard",
    "mark_video_v2_unit_submitted",
    "reconcile_pending_video_v2_deliverables",
    "reconcile_video_v2_units",
    "resolve_video_v2_keyframe_unit",
    "resolve_video_v2_shot_unit",
    "shot_clip_workspace_path",
    "storyboard_approved",
    "storyboard_workspace_path",
    "video_v2_credit_reconciliation",
    "video_v2_keyframe_unit_key",
    "video_v2_shot_unit_key",
]
