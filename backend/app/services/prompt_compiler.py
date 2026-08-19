"""Versioned, reproducible image prompt compiler for v2 poster deliverables.

The compiler is a pure function of the structured brief: the same brief and
the same ``COMPILER_VERSION`` always produce the same compiled output.  The
raw ``request.goal`` text is never passed through; only normalized brief
elements reach the provider-facing prompt.  Full prompt text lives in the
agent workspace; the database keeps only hashes and the workspace path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.services.creative_briefs import (
    CandidatePolicy,
    CreativeBrief,
    VideoBrief,
    brief_sha256,
)
from app.services.media_assets import MediaContractError
from app.services.storage import agent_storage_key, get_storage_backend
from app.services.volcengine_agent_plan import (
    image_request_payload,
    image_size_for_aspect_ratio,
)

if TYPE_CHECKING:
    from app.services.storyboard import ShotSpec


COMPILER_VERSION = "image-v1"
VIDEO_SHOT_COMPILER_VERSION = "video-shot-v1"
VIDEO_KEYFRAME_COMPILER_VERSION = "video-keyframe-v1"

PROVIDER_TARGETS = ("volcengine_agent_plan", "minimax")

TIER_QUALITY_SIZE = {"lite": "2K", "pro": "3K", "ultra": "4K"}

_CANDIDATE_COMPOSITION_CUES = (
    "hero subject centered with generous clean negative space reserved for overlay copy",
    "the subject in a coherent real usage context scene with the reserved copy area kept clean",
    "a minimal premium layout with strong light contrast and one clear focal hierarchy",
    "a close-up detail-led composition with a bold silhouette against a clean background",
)

_NO_GENERATED_TEXT_POLICY = (
    "Do not render any words, letters, digits, captions, logos, watermarks, signatures, "
    "UI chrome, or placeholder text anywhere in the image."
)

_CANDIDATE_PATH_RE = re.compile(
    r"^workspace/deliverables/(?P<request_id>[0-9a-fA-F-]{36})/"
    r"candidates/(?P<unit_key>candidate-\d{2})(?:_[0-9a-f]{12})?\.(?:png|jpg|jpeg)$"
)


class CompiledImagePrompt(BaseModel):
    """Two-layer compilation receipt: neutral brief plan + provider payload."""

    compiler_version: str = COMPILER_VERSION
    brief_sha256: str = Field(min_length=64, max_length=64)
    candidate_index: int = Field(ge=1)
    neutral: dict[str, Any]
    neutral_prompt: str = Field(min_length=1)
    provider_target: str
    provider_payload: dict[str, Any]
    prompt_sha256: str = Field(min_length=64, max_length=64)

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


def compile_image_prompt(
    brief: CreativeBrief,
    *,
    provider_target: str,
    candidate_index: int = 1,
    quality_size: str = "2K",
    compiler_version: str = COMPILER_VERSION,
) -> CompiledImagePrompt:
    """Compile one candidate prompt; pure and reproducible for a given version."""

    normalized_target = str(provider_target or "").strip().lower()
    if normalized_target not in PROVIDER_TARGETS:
        raise ValueError(f"Unsupported image provider target: {provider_target}")
    if candidate_index < 1:
        raise ValueError("candidate_index must be positive")

    composition_cue = _CANDIDATE_COMPOSITION_CUES[
        (candidate_index - 1) % len(_CANDIDATE_COMPOSITION_CUES)
    ]
    negative_constraints = [_NO_GENERATED_TEXT_POLICY, *brief.prohibitions]
    exact_asset_paths = [
        asset.path for asset in brief.reference_assets if asset.kind == "exact_asset"
    ]
    if exact_asset_paths:
        negative_constraints.append(
            "Keep the supplied exact assets visually identical; never redraw, restyle, "
            "or replace them."
        )
    neutral = {
        "subject": brief.purpose,
        "channel": brief.channel,
        "audience": brief.audience,
        "scene": (
            f"A {brief.style} commercial visual for {brief.channel} aimed at {brief.audience}, "
            f"framed for a {brief.aspect_ratio} canvas"
        ),
        "composition": composition_cue,
        "redraw_scope": brief.redraw_scope,
        "negative_constraints": list(negative_constraints),
        "text_policy": "no_generated_text",
    }
    neutral_prompt = (
        f"{brief.purpose}\n"
        f"Scene: {neutral['scene']}.\n"
        f"Composition: {composition_cue}.\n"
        f"Style: {brief.style}.\n"
        + "\n".join(f"Constraint: {item}" for item in negative_constraints)
    )

    if normalized_target == "volcengine_agent_plan":
        provider_payload = image_request_payload(
            prompt=neutral_prompt,
            size=image_size_for_aspect_ratio(quality_size, brief.aspect_ratio),
        )
    else:
        provider_payload = {
            "model": "image-01",
            "prompt": neutral_prompt,
            "aspect_ratio": brief.aspect_ratio,
            "response_format": "url",
        }

    prompt_sha256 = _canonical_sha256(
        {
            "compiler_version": compiler_version,
            "brief_sha256": brief_sha256(brief),
            "candidate_index": candidate_index,
            "neutral_prompt": neutral_prompt,
            "provider_target": normalized_target,
            "provider_payload": provider_payload,
        }
    )
    return CompiledImagePrompt(
        compiler_version=compiler_version,
        brief_sha256=brief_sha256(brief),
        candidate_index=candidate_index,
        neutral=neutral,
        neutral_prompt=neutral_prompt,
        provider_target=normalized_target,
        provider_payload=provider_payload,
        prompt_sha256=prompt_sha256,
    )


def compiled_prompt_workspace_path(request_id: uuid.UUID | str, unit_key: str) -> str:
    return f"workspace/deliverables/{request_id}/prompts/{unit_key}.txt"


def poster_v2_candidate_unit_key(value: object) -> str | None:
    """Parse the server-owned candidate unit key out of a workspace path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    match = _CANDIDATE_PATH_RE.match(normalized)
    return match.group("unit_key") if match else None


async def store_compiled_prompt(
    *,
    agent_id: uuid.UUID,
    request_id: uuid.UUID,
    unit_key: str,
    content: str,
) -> str:
    """Persist the compiled prompt text in the agent workspace; DB keeps hashes."""

    path = compiled_prompt_workspace_path(request_id, unit_key)
    storage = get_storage_backend()
    await storage.write_bytes(
        agent_storage_key(agent_id, path),
        content.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )
    return path


async def record_prompt_compilation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    execution_id: uuid.UUID | None,
    unit_id: uuid.UUID | None,
    compiled: CompiledImagePrompt,
    compiled_prompt_path: str,
) -> DeliverablePromptCompilation:
    receipt = DeliverablePromptCompilation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        request_id=request_id,
        execution_id=execution_id,
        unit_id=unit_id,
        compiler_version=compiled.compiler_version,
        brief_sha256=compiled.brief_sha256,
        compiled_prompt_sha256=compiled.prompt_sha256,
        compiled_prompt_path=compiled_prompt_path,
        provider_target=compiled.provider_target,
    )
    db.add(receipt)
    return receipt


@dataclass(frozen=True, slots=True)
class PosterV2CandidateBinding:
    execution_id: uuid.UUID
    unit_id: uuid.UUID
    unit_key: str


async def resolve_poster_v2_candidate_unit(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    request_id: uuid.UUID | None,
    save_path: str,
    prompt: str,
) -> PosterV2CandidateBinding | None:
    """Bind one managed image Tool call to its candidate unit, fail closed.

    Returns ``None`` for non-v2 requests so the v1 path is untouched.  For a
    v2 poster request the candidate save_path naming and the verbatim compiled
    prompt are hard requirements checked before any Provider spend.
    """

    if request_id is None:
        return None
    async with async_session() as db:
        result = await db.execute(
            select(DeliverableRequest).where(
                DeliverableRequest.id == request_id,
                DeliverableRequest.tenant_id == tenant_id,
                DeliverableRequest.agent_id == agent_id,
            )
        )
        request = result.scalar_one_or_none()
        if request is None or request.workflow_id != "builtin.poster.v2":
            return None

        unit_key = poster_v2_candidate_unit_key(save_path)
        if unit_key is None:
            raise MediaContractError(
                "v2 poster candidates must save to "
                "workspace/deliverables/<request_id>/candidates/candidate-NN.png"
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
            raise MediaContractError("v2 poster request has no active execution")
        unit_result = await db.execute(
            select(DeliverableExecutionUnit).where(
                DeliverableExecutionUnit.tenant_id == tenant_id,
                DeliverableExecutionUnit.execution_id == execution.id,
                DeliverableExecutionUnit.stage_key == "candidate_generate",
                DeliverableExecutionUnit.unit_key == unit_key,
            )
        )
        unit = unit_result.scalar_one_or_none()
        if unit is None:
            raise MediaContractError(f"Unknown v2 poster candidate unit: {unit_key}")

        compilation_result = await db.execute(
            select(DeliverablePromptCompilation).where(
                DeliverablePromptCompilation.tenant_id == tenant_id,
                DeliverablePromptCompilation.execution_id == execution.id,
                DeliverablePromptCompilation.unit_id == unit.id,
                DeliverablePromptCompilation.compiler_version == COMPILER_VERSION,
            )
        )
        compilation = compilation_result.scalar_one_or_none()
        if compilation is None:
            raise MediaContractError(
                f"v2 poster candidate {unit_key} has no compiled prompt receipt"
            )
        storage = get_storage_backend()
        try:
            compiled_bytes = await storage.read_bytes(
                agent_storage_key(agent_id, compilation.compiled_prompt_path)
            )
        except FileNotFoundError as exc:
            raise MediaContractError(
                f"v2 poster candidate {unit_key} compiled prompt is missing"
            ) from exc
        expected_prompt = compiled_bytes.decode("utf-8").strip()
        if str(prompt or "").strip() != expected_prompt:
            raise MediaContractError(
                "v2 poster candidate prompts must be passed verbatim from the "
                "server-compiled prompt file; rewritten prompts are rejected"
            )
        return PosterV2CandidateBinding(
            execution_id=execution.id,
            unit_id=unit.id,
            unit_key=unit_key,
        )


async def mark_poster_v2_candidate_submitted(
    binding: PosterV2CandidateBinding,
    *,
    tenant_id: uuid.UUID,
    media_task_id: uuid.UUID,
) -> None:
    """Attach the durable media task to its candidate unit exactly once."""

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


class CompiledVideoShotPrompt(BaseModel):
    """Two-layer shot compilation receipt: neutral shot plan + payload."""

    compiler_version: str = VIDEO_SHOT_COMPILER_VERSION
    brief_sha256: str = Field(min_length=64, max_length=64)
    shot_id: str = Field(pattern=r"^shot-\d{2}$")
    neutral: dict[str, Any]
    neutral_prompt: str = Field(min_length=1)
    provider_target: str
    provider_payload: dict[str, Any]
    prompt_sha256: str = Field(min_length=64, max_length=64)

    model_config = ConfigDict(extra="forbid", frozen=True)


_NO_GENERATED_VIDEO_TEXT_POLICY = (
    "Do not render any burned-in words, letters, digits, captions, logos, "
    "watermarks, signatures, or UI chrome anywhere in the clip."
)


def compile_video_shot_prompt(
    brief: VideoBrief,
    shot: ShotSpec,
    *,
    provider_target: str,
) -> CompiledVideoShotPrompt:
    """Compile one shot prompt; pure and reproducible for a given version.

    The raw ``request.goal`` text is never passed through; only the approved
    storyboard shot and structured brief elements reach the provider-facing
    prompt.
    """

    normalized_target = str(provider_target or "").strip().lower()
    if normalized_target not in PROVIDER_TARGETS:
        raise ValueError(f"Unsupported video provider target: {provider_target}")

    negative_constraints = [_NO_GENERATED_VIDEO_TEXT_POLICY, *brief.prohibitions]
    if brief.audio_mode != "in_scene_dialogue":
        negative_constraints.append(
            "Do not generate any speech or dialogue audio track in the clip."
        )
    neutral: dict[str, Any] = {
        "shot_id": shot.shot_id,
        "visual": shot.visual,
        "camera": shot.camera,
        "transition": shot.transition,
        "audio_mode": brief.audio_mode,
        "duration_seconds": shot.duration_seconds,
        "aspect_ratio": brief.aspect_ratio,
        "negative_constraints": list(negative_constraints),
        "text_policy": "no_generated_text",
    }
    prompt_parts = [
        f"{shot.visual}",
        f"Camera: {shot.camera or 'stable commercial shot'}.",
        (
            f"Style: {brief.style} commercial video for {brief.channel} aimed at "
            f"{brief.audience}, framed for a {brief.aspect_ratio} canvas."
        ),
    ]
    if brief.audio_mode == "in_scene_dialogue" and shot.dialogue.strip():
        # Only a native-audio route ever sees this line; the preflight route
        # filter keeps the dialogue contract off audio-less providers.
        prompt_parts.append(
            "Synchronized in-scene dialogue spoken on camera: "
            f"{shot.dialogue.strip()}"
        )
    prompt_parts.extend(f"Constraint: {item}" for item in negative_constraints)
    neutral_prompt = "\n".join(prompt_parts)

    if normalized_target == "volcengine_agent_plan":
        provider_payload: dict[str, Any] = {
            "prompt": neutral_prompt,
            "ratio": brief.aspect_ratio,
            "duration_seconds": shot.duration_seconds,
            "generate_audio": brief.audio_mode == "in_scene_dialogue",
        }
    else:
        provider_payload = {
            "model": "MiniMax-Hailuo-02",
            "prompt": neutral_prompt,
            "duration": shot.duration_seconds,
            "first_frame_required": brief.aspect_ratio != "16:9",
        }

    prompt_sha256 = _canonical_sha256(
        {
            "compiler_version": VIDEO_SHOT_COMPILER_VERSION,
            "brief_sha256": brief_sha256(brief),
            "shot_id": shot.shot_id,
            "neutral_prompt": neutral_prompt,
            "provider_target": normalized_target,
            "provider_payload": provider_payload,
        }
    )
    return CompiledVideoShotPrompt(
        brief_sha256=brief_sha256(brief),
        shot_id=shot.shot_id,
        neutral=neutral,
        neutral_prompt=neutral_prompt,
        provider_target=normalized_target,
        provider_payload=provider_payload,
        prompt_sha256=prompt_sha256,
    )


def compile_video_keyframe_prompt(
    brief: VideoBrief,
    shot: ShotSpec,
    *,
    provider_target: str,
    quality_size: str = "2K",
) -> CompiledImagePrompt:
    """FR-V6: compile the same-aspect first frame through the M1 image pipeline."""

    surrogate = CreativeBrief(
        purpose=f"{brief.purpose}\nOpening frame: {shot.visual}"[:2000],
        channel=brief.channel,
        audience=brief.audience,
        aspect_ratio=brief.aspect_ratio,
        style=brief.style,
        reference_assets=brief.reference_assets,
        prohibitions=brief.prohibitions,
        candidate_policy=CandidatePolicy(
            tier="keyframe",
            tier_default=1,
            requested=None,
            effective=1,
        ),
        delivery_formats=("png",),
    )
    candidate_index = int(shot.shot_id.rsplit("-", 1)[-1])
    return compile_image_prompt(
        surrogate,
        provider_target=provider_target,
        candidate_index=candidate_index,
        quality_size=quality_size,
        compiler_version=VIDEO_KEYFRAME_COMPILER_VERSION,
    )


__all__ = [
    "COMPILER_VERSION",
    "PROVIDER_TARGETS",
    "TIER_QUALITY_SIZE",
    "VIDEO_KEYFRAME_COMPILER_VERSION",
    "VIDEO_SHOT_COMPILER_VERSION",
    "CompiledImagePrompt",
    "CompiledVideoShotPrompt",
    "PosterV2CandidateBinding",
    "compile_image_prompt",
    "compile_video_keyframe_prompt",
    "compile_video_shot_prompt",
    "compiled_prompt_workspace_path",
    "mark_poster_v2_candidate_submitted",
    "poster_v2_candidate_unit_key",
    "record_prompt_compilation",
    "resolve_poster_v2_candidate_unit",
    "store_compiled_prompt",
]
