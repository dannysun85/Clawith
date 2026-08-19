"""Structured creative briefs for v2 deliverable workflows (FR-I1).

The compiler in this module is a pure function: it never calls a Provider,
never reserves Credits, and never invents missing brief elements.  An
incomplete brief is reported through the clarification seam instead of being
silently padded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import DeliverableCreativeBrief, DeliverableRequest
from app.services.poster_contract import poster_exact_copy_blocks


CREATIVE_BRIEF_SCHEMA_VERSION = "creative-brief-v1"
VIDEO_BRIEF_SCHEMA_VERSION = "video-brief-v1"
PRESENTATION_BRIEF_SCHEMA_VERSION = "presentation-brief-v1"
POSTER_V2_WORKFLOW_ID = "builtin.poster.v2"
VIDEO_V2_WORKFLOW_ID = "builtin.video.v2"
PRESENTATION_V2_WORKFLOW_ID = "builtin.presentation.v2"

# FR-V1: the audio mode is a mandatory up-front customer choice.  In-scene
# dialogue requires a provider-native audio track and is route-filtered at
# preflight; the brief never promises dialogue the route cannot honor.
AUDIO_MODES = ("in_scene_dialogue", "voiceover", "silent")

TIER_CANDIDATE_DEFAULTS = {"lite": 1, "pro": 2, "ultra": 3}
MAX_CANDIDATE_COUNT = 4
REDRAW_SCOPES = ("background_only", "style_adaptation", "full_creative")
# FR-P1: the customer picks the editability contract up front; the v2 runtime
# maps it to an explicit render mode (rollout §8.1) instead of silently
# rasterizing editable content.
EDITABILITY_CONTRACTS = ("editable", "hybrid", "visual_fidelity")
PRESENTATION_PAGE_COUNT_RANGE = (5, 15)

_IMAGE_INPUT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class ExactCopyBlock(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid", frozen=True)


class BrandAssetRef(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    position: str | None = Field(default=None, max_length=32)

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReferenceAssetRef(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    kind: Literal["exact_asset", "creative_reference"] = "creative_reference"

    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidatePolicy(BaseModel):
    """Tier-bound candidate count; a request may only tune the count down."""

    tier: str = Field(min_length=1, max_length=20)
    tier_default: int = Field(ge=1, le=MAX_CANDIDATE_COUNT)
    requested: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_COUNT)
    effective: int = Field(ge=1, le=MAX_CANDIDATE_COUNT)

    model_config = ConfigDict(extra="forbid", frozen=True)


class CreativeBrief(BaseModel):
    """Provider-neutral customer brief; provider/model fields are forbidden."""

    purpose: str = Field(min_length=1, max_length=2000)
    channel: str = Field(min_length=1, max_length=64)
    audience: str = Field(min_length=1, max_length=500)
    aspect_ratio: str = Field(min_length=1, max_length=16)
    style: str = Field(min_length=1, max_length=200)
    exact_copy_blocks: tuple[ExactCopyBlock, ...] = ()
    brand_assets: tuple[BrandAssetRef, ...] = ()
    reference_assets: tuple[ReferenceAssetRef, ...] = ()
    redraw_scope: Literal["background_only", "style_adaptation", "full_creative"] = "full_creative"
    prohibitions: tuple[str, ...] = ()
    candidate_policy: CandidatePolicy
    delivery_formats: tuple[str, ...] = ("png",)

    model_config = ConfigDict(extra="forbid", frozen=True)


def candidate_count_for_policy(tier: str, spec: Mapping[str, Any] | None) -> int:
    """Authoritative candidate count: the tier default may only be tuned down."""

    tier_default = TIER_CANDIDATE_DEFAULTS.get(str(tier or "").strip().lower(), 1)
    configured = (spec or {}).get("candidate_count")
    if isinstance(configured, bool) or not isinstance(configured, int):
        return tier_default
    return max(1, min(configured, tier_default, MAX_CANDIDATE_COUNT))


def brief_sha256(brief: CreativeBrief | VideoBrief | PresentationBrief) -> str:
    canonical = json.dumps(
        brief.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    return str(value or "").strip()


def _workspace_asset_path(value: object) -> str:
    path = _text(value).replace("\\", "/").lstrip("/")
    if not path or ".." in path.split("/"):
        return ""
    if path.startswith("uploads/"):
        path = f"workspace/{path}"
    if not path.startswith("workspace/"):
        return ""
    return path


def _input_reference_assets(
    inputs: Sequence[Mapping[str, Any] | object] | None,
) -> tuple[ReferenceAssetRef, ...]:
    assets: list[ReferenceAssetRef] = []
    seen: set[str] = set()
    for item in inputs or ():
        value = item.get("path") if isinstance(item, Mapping) else getattr(item, "path", None)
        path = _workspace_asset_path(value)
        normalized = path.casefold()
        if not path or not normalized.endswith(_IMAGE_INPUT_SUFFIXES) or normalized in seen:
            continue
        seen.add(normalized)
        assets.append(ReferenceAssetRef(path=path, kind="creative_reference"))
    return tuple(assets)


def _brand_assets(value: object, missing: list[str]) -> tuple[BrandAssetRef, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        missing.append("brand_assets")
        return ()
    assets: list[BrandAssetRef] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            missing.append(f"brand_assets[{index}]")
            continue
        path = _workspace_asset_path(entry.get("path"))
        if not path:
            missing.append(f"brand_assets[{index}].path")
            continue
        position = _text(entry.get("position")) or None
        assets.append(BrandAssetRef(path=path, position=position))
    return tuple(assets)


def _reference_assets(value: object, missing: list[str]) -> tuple[ReferenceAssetRef, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        missing.append("reference_assets")
        return ()
    assets: list[ReferenceAssetRef] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            missing.append(f"reference_assets[{index}]")
            continue
        path = _workspace_asset_path(entry.get("path"))
        if not path:
            missing.append(f"reference_assets[{index}].path")
            continue
        kind = _text(entry.get("kind")) or "creative_reference"
        if kind not in {"exact_asset", "creative_reference"}:
            missing.append(f"reference_assets[{index}].kind")
            continue
        assets.append(ReferenceAssetRef(path=path, kind=kind))
    return tuple(assets)


def _prohibitions(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        lines: Sequence[object] = value.splitlines()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        lines = value
    else:
        return ()
    return tuple(dict.fromkeys(item for item in (_text(line) for line in lines) if item))


def compile_creative_brief(
    goal: str,
    spec: Mapping[str, Any] | None,
    inputs: Sequence[Mapping[str, Any] | object] | None,
    *,
    tier: str | None = None,
    delivery_formats: Sequence[str] = ("png",),
) -> tuple[CreativeBrief | None, tuple[str, ...]]:
    """Compile a structured brief; missing elements are reported, never invented.

    Returns ``(None, missing_fields)`` when a required element is absent so the
    caller can park the request in the clarification state before any prompt is
    compiled or any Credits are reserved.
    """

    normalized_spec = dict(spec or {})
    missing: list[str] = []

    purpose = _text(normalized_spec.get("purpose")) or _text(goal)
    if not purpose:
        missing.append("purpose")
    channel = _text(normalized_spec.get("channel"))
    if not channel:
        missing.append("channel")
    audience = _text(normalized_spec.get("audience"))
    if not audience:
        missing.append("audience")
    aspect_ratio = _text(normalized_spec.get("aspect_ratio"))
    if not aspect_ratio:
        missing.append("aspect_ratio")
    style = _text(normalized_spec.get("style")) or "commercial"
    redraw_scope = _text(normalized_spec.get("redraw_scope")) or "full_creative"
    if redraw_scope not in REDRAW_SCOPES:
        missing.append("redraw_scope")

    try:
        copy_blocks = tuple(
            ExactCopyBlock(role=block["role"], text=block["text"])
            for block in poster_exact_copy_blocks(normalized_spec)
        )
    except Exception:
        copy_blocks = ()
        missing.append("exact_copy_blocks")

    brand_assets = _brand_assets(normalized_spec.get("brand_assets"), missing)
    reference_assets = _reference_assets(normalized_spec.get("reference_assets"), missing)
    input_assets = _input_reference_assets(inputs)
    known_paths = {asset.path.casefold() for asset in reference_assets}
    reference_assets += tuple(
        asset for asset in input_assets if asset.path.casefold() not in known_paths
    )

    if missing:
        return None, tuple(dict.fromkeys(missing))

    normalized_tier = str(tier or "").strip().lower() or "pro"
    tier_default = TIER_CANDIDATE_DEFAULTS.get(normalized_tier, 1)
    configured = normalized_spec.get("candidate_count")
    requested = (
        configured
        if isinstance(configured, int) and not isinstance(configured, bool)
        else None
    )
    brief = CreativeBrief(
        purpose=purpose[:2000],
        channel=channel,
        audience=audience,
        aspect_ratio=aspect_ratio,
        style=style,
        exact_copy_blocks=copy_blocks,
        brand_assets=brand_assets,
        reference_assets=reference_assets,
        redraw_scope=redraw_scope,
        prohibitions=_prohibitions(normalized_spec.get("prohibitions")),
        candidate_policy=CandidatePolicy(
            tier=normalized_tier,
            tier_default=tier_default,
            requested=requested,
            effective=candidate_count_for_policy(normalized_tier, normalized_spec),
        ),
        delivery_formats=tuple(
            str(item).strip().lower() for item in delivery_formats if str(item).strip()
        )
        or ("png",),
    )
    return brief, ()


def brief_projection(
    brief: CreativeBrief | None,
    missing_fields: Sequence[str],
) -> dict[str, Any]:
    """Secret-free brief projection for preflight snapshots and API reads."""

    projection: dict[str, Any] = {
        "schema_version": CREATIVE_BRIEF_SCHEMA_VERSION,
        "status": "confirmed" if brief is not None else "clarifying",
        "missing_fields": list(missing_fields),
    }
    if brief is not None:
        projection["brief_sha256"] = brief_sha256(brief)
        projection["candidate_count"] = brief.candidate_policy.effective
    return projection


def poster_v2_rollout_allowed(
    *,
    tenant_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None,
    enabled: bool,
    tenant_ids: str,
    agent_ids: str,
) -> bool:
    """v2 poster orchestration is allowlist-gated like the quality gate flags."""

    if not enabled:
        return False

    def parse(raw: str) -> set[str]:
        return {item.strip() for item in str(raw or "").split(",") if item.strip()}

    return str(tenant_id) in parse(tenant_ids) or str(agent_id) in parse(agent_ids)


async def current_request_brief(
    db: AsyncSession,
    request: DeliverableRequest,
) -> DeliverableCreativeBrief | None:
    result = await db.execute(
        select(DeliverableCreativeBrief)
        .where(
            DeliverableCreativeBrief.tenant_id == request.tenant_id,
            DeliverableCreativeBrief.request_id == request.id,
        )
        .order_by(DeliverableCreativeBrief.created_at.desc(), DeliverableCreativeBrief.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_request_creative_brief(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution_id: uuid.UUID | None = None,
    created_by_run_id: uuid.UUID | None = None,
) -> DeliverableCreativeBrief | None:
    """Compile and persist the v2 poster brief; no-op for non-v2 requests.

    An incomplete brief keeps the request launchable-as-draft only: the request
    stays ``ready`` but its stage becomes ``brief_clarifying`` and the current
    execution is parked as ``blocked`` with a ``brief_missing:<field>`` reason,
    so no prompt is compiled and no Provider task or reservation can be created.
    """

    if request.workflow_id != POSTER_V2_WORKFLOW_ID:
        return None
    brief, missing = compile_creative_brief(
        request.goal,
        request.spec,
        request.inputs,
        tier=request.tier,
        delivery_formats=request.output_contract or ("png",),
    )
    resolved_execution_id = execution_id or request.current_execution_id
    existing = None
    if resolved_execution_id is not None:
        result = await db.execute(
            select(DeliverableCreativeBrief).where(
                DeliverableCreativeBrief.tenant_id == request.tenant_id,
                DeliverableCreativeBrief.request_id == request.id,
                DeliverableCreativeBrief.execution_id == resolved_execution_id,
                DeliverableCreativeBrief.schema_version == CREATIVE_BRIEF_SCHEMA_VERSION,
            )
        )
        existing = result.scalar_one_or_none()
    status = "confirmed" if brief is not None else "clarifying"
    payload = brief.model_dump(mode="json") if brief is not None else {}
    digest = brief_sha256(brief) if brief is not None else hashlib.sha256(b"{}").hexdigest()
    if existing is None:
        existing = DeliverableCreativeBrief(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=resolved_execution_id,
            modality="image",
            schema_version=CREATIVE_BRIEF_SCHEMA_VERSION,
            status=status,
            brief=payload,
            source_inventory=[],
            missing_fields=list(missing),
            brief_sha256=digest,
            created_by_run_id=created_by_run_id,
        )
        db.add(existing)
    else:
        existing.status = status
        existing.brief = payload
        existing.missing_fields = list(missing)
        existing.brief_sha256 = digest
        if created_by_run_id is not None:
            existing.created_by_run_id = created_by_run_id
    request.current_stage = "brief_confirmed" if brief is not None else "brief_clarifying"
    return existing


class VideoBrief(BaseModel):
    """Provider-neutral video brief; provider/model fields are forbidden."""

    purpose: str = Field(min_length=1, max_length=2000)
    channel: str = Field(min_length=1, max_length=64)
    audience: str = Field(min_length=1, max_length=500)
    aspect_ratio: str = Field(min_length=1, max_length=16)
    language: str = Field(min_length=1, max_length=16)
    style: str = Field(min_length=1, max_length=200)
    duration_seconds: int = Field(ge=1, le=60)
    story: str = Field(min_length=1, max_length=4000)
    audio_mode: Literal["in_scene_dialogue", "voiceover", "silent"]
    caption_spec: str = Field(default="", max_length=2000)
    cta: str = Field(default="", max_length=500)
    dialogue_script: str = Field(default="", max_length=4000)
    reference_assets: tuple[ReferenceAssetRef, ...] = ()
    prohibitions: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


def compile_video_brief(
    goal: str,
    spec: Mapping[str, Any] | None,
    inputs: Sequence[Mapping[str, Any] | object] | None,
    *,
    tier: str | None = None,
) -> tuple[VideoBrief | None, tuple[str, ...]]:
    """Compile the structured video brief; missing elements are never invented.

    Returns ``(None, missing_fields)`` so the request can park in the
    clarification state before any storyboard, prompt, Provider task, or
    Credits reservation exists.
    """

    del tier  # The tier never changes which brief elements are required.
    normalized_spec = dict(spec or {})
    missing: list[str] = []

    purpose = _text(normalized_spec.get("purpose")) or _text(goal)
    if not purpose:
        missing.append("purpose")
    channel = _text(normalized_spec.get("channel"))
    if not channel:
        missing.append("channel")
    audience = _text(normalized_spec.get("audience"))
    if not audience:
        missing.append("audience")
    aspect_ratio = _text(normalized_spec.get("aspect_ratio"))
    if not aspect_ratio:
        missing.append("aspect_ratio")
    language = _text(normalized_spec.get("language"))
    if not language:
        missing.append("language")
    story = _text(normalized_spec.get("story"))
    if not story:
        missing.append("story")
    style = _text(normalized_spec.get("style")) or "commercial"

    raw_duration = normalized_spec.get("duration")
    try:
        duration_seconds = int(raw_duration)
    except (TypeError, ValueError):
        duration_seconds = 0
    if not 1 <= duration_seconds <= 60:
        missing.append("duration")

    audio_mode = _text(normalized_spec.get("audio_mode")).lower()
    if audio_mode not in AUDIO_MODES:
        missing.append("audio_mode")
    dialogue_script = _text(normalized_spec.get("dialogue_script"))
    if audio_mode == "in_scene_dialogue" and not dialogue_script:
        # Never promise in-scene synchronized dialogue without a script.
        missing.append("dialogue_script")

    if missing:
        return None, tuple(dict.fromkeys(missing))

    reference_assets = _reference_assets(normalized_spec.get("reference_assets"), missing)
    if missing:
        return None, tuple(dict.fromkeys(missing))
    input_assets = _input_reference_assets(inputs)
    known_paths = {asset.path.casefold() for asset in reference_assets}
    reference_assets += tuple(
        asset for asset in input_assets if asset.path.casefold() not in known_paths
    )

    brief = VideoBrief(
        purpose=purpose[:2000],
        channel=channel,
        audience=audience,
        aspect_ratio=aspect_ratio,
        language=language,
        style=style,
        duration_seconds=duration_seconds,
        story=story,
        audio_mode=audio_mode,  # type: ignore[arg-type]
        caption_spec=_text(normalized_spec.get("caption_spec")),
        cta=_text(normalized_spec.get("cta")),
        dialogue_script=dialogue_script,
        reference_assets=reference_assets,
        prohibitions=_prohibitions(normalized_spec.get("prohibitions")),
    )
    return brief, ()


def video_brief_projection(
    brief: VideoBrief | None,
    missing_fields: Sequence[str],
) -> dict[str, Any]:
    """Secret-free video brief projection for preflight snapshots/API reads."""

    projection: dict[str, Any] = {
        "schema_version": VIDEO_BRIEF_SCHEMA_VERSION,
        "status": "confirmed" if brief is not None else "clarifying",
        "missing_fields": list(missing_fields),
    }
    if brief is not None:
        projection["brief_sha256"] = brief_sha256(brief)
        projection["duration_seconds"] = brief.duration_seconds
        projection["audio_mode"] = brief.audio_mode
    return projection


async def upsert_request_video_brief(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution_id: uuid.UUID | None = None,
    created_by_run_id: uuid.UUID | None = None,
) -> DeliverableCreativeBrief | None:
    """Compile and persist the v2 video brief; no-op for non-v2 requests."""

    if request.workflow_id != VIDEO_V2_WORKFLOW_ID:
        return None
    brief, missing = compile_video_brief(
        request.goal,
        request.spec,
        request.inputs,
        tier=request.tier,
    )
    resolved_execution_id = execution_id or request.current_execution_id
    existing = None
    if resolved_execution_id is not None:
        result = await db.execute(
            select(DeliverableCreativeBrief).where(
                DeliverableCreativeBrief.tenant_id == request.tenant_id,
                DeliverableCreativeBrief.request_id == request.id,
                DeliverableCreativeBrief.execution_id == resolved_execution_id,
                DeliverableCreativeBrief.schema_version == VIDEO_BRIEF_SCHEMA_VERSION,
            )
        )
        existing = result.scalar_one_or_none()
    status = "confirmed" if brief is not None else "clarifying"
    payload = brief.model_dump(mode="json") if brief is not None else {}
    digest = brief_sha256(brief) if brief is not None else hashlib.sha256(b"{}").hexdigest()
    if existing is None:
        existing = DeliverableCreativeBrief(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=resolved_execution_id,
            modality="video",
            schema_version=VIDEO_BRIEF_SCHEMA_VERSION,
            status=status,
            brief=payload,
            source_inventory=[],
            missing_fields=list(missing),
            brief_sha256=digest,
            created_by_run_id=created_by_run_id,
        )
        db.add(existing)
    else:
        existing.status = status
        existing.brief = payload
        existing.missing_fields = list(missing)
        existing.brief_sha256 = digest
        if created_by_run_id is not None:
            existing.created_by_run_id = created_by_run_id
    request.current_stage = "brief_confirmed" if brief is not None else "brief_clarifying"
    return existing


class PresentationBrief(BaseModel):
    """Provider-neutral presentation brief; provider/model fields are forbidden."""

    purpose: str = Field(min_length=1, max_length=2000)
    audience: str = Field(min_length=1, max_length=500)
    scenario: str = Field(min_length=1, max_length=200)
    page_count: int = Field(
        ge=PRESENTATION_PAGE_COUNT_RANGE[0],
        le=PRESENTATION_PAGE_COUNT_RANGE[1],
    )
    language: str = Field(min_length=1, max_length=16)
    style: str = Field(min_length=1, max_length=200)
    required_points: tuple[str, ...] = Field(min_length=1)
    brand_theme: str = Field(default="", max_length=500)
    editability_contract: Literal["editable", "hybrid", "visual_fidelity"] = "editable"
    output_contract: tuple[str, ...] = ("pptx",)

    model_config = ConfigDict(extra="forbid", frozen=True)


def _text_lines(value: object) -> tuple[str, ...]:
    """Normalize a textarea string or list into distinct non-empty lines."""

    if value in (None, ""):
        return ()
    if isinstance(value, str):
        lines: Sequence[object] = value.splitlines()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        lines = value
    else:
        return ()
    return tuple(dict.fromkeys(item for item in (_text(line) for line in lines) if item))


def compile_presentation_brief(
    goal: str,
    spec: Mapping[str, Any] | None,
    inputs: Sequence[Mapping[str, Any] | object] | None,
    *,
    output_contract: Sequence[str] = ("pptx",),
) -> tuple[PresentationBrief | None, tuple[str, ...]]:
    """Compile the structured presentation brief; nothing is ever invented.

    Returns ``(None, missing_fields)`` so the request can park in the
    clarification state before any outline, render, Provider task, or Credits
    reservation exists (FR-P1).  ``inputs`` only feeds the source inventory;
    the brief itself never depends on file contents.
    """

    del inputs  # Source registration lives in source_inventory, not the brief.
    normalized_spec = dict(spec or {})
    missing: list[str] = []

    purpose = _text(normalized_spec.get("purpose")) or _text(goal)
    if not purpose:
        missing.append("purpose")
    audience = _text(normalized_spec.get("audience"))
    if not audience:
        missing.append("audience")
    scenario = _text(normalized_spec.get("scenario"))
    if not scenario:
        missing.append("scenario")
    language = _text(normalized_spec.get("language"))
    if not language:
        missing.append("language")
    style = _text(normalized_spec.get("style")) or "professional"

    raw_page_count = normalized_spec.get("page_count")
    try:
        page_count = int(raw_page_count)
    except (TypeError, ValueError):
        page_count = 0
    if not PRESENTATION_PAGE_COUNT_RANGE[0] <= page_count <= PRESENTATION_PAGE_COUNT_RANGE[1]:
        missing.append("page_count")

    required_points = _text_lines(normalized_spec.get("key_points"))
    if not required_points:
        missing.append("key_points")

    editability_contract = (
        _text(normalized_spec.get("editability_contract")).lower() or "editable"
    )
    if editability_contract not in EDITABILITY_CONTRACTS:
        missing.append("editability_contract")

    if missing:
        return None, tuple(dict.fromkeys(missing))

    outputs = tuple(
        dict.fromkeys(
            item
            for item in (str(value).strip().lower() for value in output_contract)
            if item
        )
    ) or ("pptx",)
    brief = PresentationBrief(
        purpose=purpose[:2000],
        audience=audience,
        scenario=scenario,
        page_count=page_count,
        language=language,
        style=style,
        required_points=required_points,
        brand_theme=_text(normalized_spec.get("brand_theme")),
        editability_contract=editability_contract,  # type: ignore[arg-type]
        output_contract=outputs,
    )
    return brief, ()


def presentation_brief_projection(
    brief: PresentationBrief | None,
    missing_fields: Sequence[str],
) -> dict[str, Any]:
    """Secret-free presentation brief projection for preflight snapshots."""

    projection: dict[str, Any] = {
        "schema_version": PRESENTATION_BRIEF_SCHEMA_VERSION,
        "status": "confirmed" if brief is not None else "clarifying",
        "missing_fields": list(missing_fields),
    }
    if brief is not None:
        projection["brief_sha256"] = brief_sha256(brief)
        projection["page_count"] = brief.page_count
        projection["editability_contract"] = brief.editability_contract
    return projection


async def upsert_request_presentation_brief(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution_id: uuid.UUID | None = None,
    created_by_run_id: uuid.UUID | None = None,
    storage=None,
) -> DeliverableCreativeBrief | None:
    """Compile and persist the v2 presentation brief plus its source inventory.

    The inventory (FR-P2) is registered at brief-compile time so every upload
    is hash-bound before any outline or render work exists.  An incomplete
    brief parks the request in ``brief_clarifying`` with zero paid work.
    """

    if request.workflow_id != PRESENTATION_V2_WORKFLOW_ID:
        return None
    from app.services.source_inventory import compile_source_inventory

    brief, missing = compile_presentation_brief(
        request.goal,
        request.spec,
        request.inputs,
        output_contract=request.output_contract or ("pptx",),
    )
    inventory = await compile_source_inventory(request, storage=storage)
    resolved_execution_id = execution_id or request.current_execution_id
    existing = None
    if resolved_execution_id is not None:
        result = await db.execute(
            select(DeliverableCreativeBrief).where(
                DeliverableCreativeBrief.tenant_id == request.tenant_id,
                DeliverableCreativeBrief.request_id == request.id,
                DeliverableCreativeBrief.execution_id == resolved_execution_id,
                DeliverableCreativeBrief.schema_version == PRESENTATION_BRIEF_SCHEMA_VERSION,
            )
        )
        existing = result.scalar_one_or_none()
    status = "confirmed" if brief is not None else "clarifying"
    payload = brief.model_dump(mode="json") if brief is not None else {}
    digest = brief_sha256(brief) if brief is not None else hashlib.sha256(b"{}").hexdigest()
    inventory_payload = [entry.model_dump(mode="json") for entry in inventory]
    if existing is None:
        existing = DeliverableCreativeBrief(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=resolved_execution_id,
            modality="presentation",
            schema_version=PRESENTATION_BRIEF_SCHEMA_VERSION,
            status=status,
            brief=payload,
            source_inventory=inventory_payload,
            missing_fields=list(missing),
            brief_sha256=digest,
            created_by_run_id=created_by_run_id,
        )
        db.add(existing)
    else:
        existing.status = status
        existing.brief = payload
        existing.source_inventory = inventory_payload
        existing.missing_fields = list(missing)
        existing.brief_sha256 = digest
        if created_by_run_id is not None:
            existing.created_by_run_id = created_by_run_id
    request.current_stage = "brief_confirmed" if brief is not None else "brief_clarifying"
    return existing


async def upsert_request_structured_brief(
    db: AsyncSession,
    request: DeliverableRequest,
    *,
    execution_id: uuid.UUID | None = None,
    created_by_run_id: uuid.UUID | None = None,
) -> DeliverableCreativeBrief | None:
    """Dispatch to the modality brief compiler for v2 deliverable requests."""

    if request.workflow_id == VIDEO_V2_WORKFLOW_ID:
        return await upsert_request_video_brief(
            db,
            request,
            execution_id=execution_id,
            created_by_run_id=created_by_run_id,
        )
    if request.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        return await upsert_request_presentation_brief(
            db,
            request,
            execution_id=execution_id,
            created_by_run_id=created_by_run_id,
        )
    return await upsert_request_creative_brief(
        db,
        request,
        execution_id=execution_id,
        created_by_run_id=created_by_run_id,
    )


__all__ = [
    "AUDIO_MODES",
    "CREATIVE_BRIEF_SCHEMA_VERSION",
    "EDITABILITY_CONTRACTS",
    "MAX_CANDIDATE_COUNT",
    "POSTER_V2_WORKFLOW_ID",
    "PRESENTATION_BRIEF_SCHEMA_VERSION",
    "PRESENTATION_PAGE_COUNT_RANGE",
    "PRESENTATION_V2_WORKFLOW_ID",
    "REDRAW_SCOPES",
    "TIER_CANDIDATE_DEFAULTS",
    "VIDEO_BRIEF_SCHEMA_VERSION",
    "VIDEO_V2_WORKFLOW_ID",
    "BrandAssetRef",
    "CandidatePolicy",
    "CreativeBrief",
    "ExactCopyBlock",
    "PresentationBrief",
    "ReferenceAssetRef",
    "VideoBrief",
    "brief_projection",
    "brief_sha256",
    "candidate_count_for_policy",
    "compile_creative_brief",
    "compile_presentation_brief",
    "compile_video_brief",
    "current_request_brief",
    "poster_v2_rollout_allowed",
    "presentation_brief_projection",
    "upsert_request_creative_brief",
    "upsert_request_presentation_brief",
    "upsert_request_structured_brief",
    "upsert_request_video_brief",
    "video_brief_projection",
]
