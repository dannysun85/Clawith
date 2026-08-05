"""Versioned product workflow manifests and safe deliverable launch helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import posixpath
from datetime import UTC, datetime
from typing import Any, Literal, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.deliverable import DeliverableExecution, DeliverableRequest
from app.models.tool import AgentTool, Tool
from app.services.deliverable_artifacts import reconcile_runtime_deliverable_artifacts
from app.services.deliverable_executions import (
    bind_artifacts_to_current_execution,
    current_execution,
    project_execution_lifecycle,
    record_execution_preflight,
)
from app.services.entitlements import get_tenant_entitlements
from app.services.media_capabilities import get_agent_media_capabilities
from app.services.media_provider_routing import media_provider_order_for_image_strategy
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.model_router import resolve_route
from app.services.provider_pricing import minimax_image_credits, minimax_video_credits
from app.services.quota_guard import QuotaExceeded
from app.services.presentation_visual_policy import (
    MINIMUM_PICTURE_COVERAGE_RATIO,
    presentation_brief_is_image_led,
)
from app.services.tool_visibility import tool_enabled_for_agent


SAAS_TIERS = ("lite", "pro", "ultra")
DELIVERABLE_STATUSES = (
    "draft",
    "ready",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
)


class WorkflowField(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label_zh: str
    label_en: str
    kind: Literal["text", "textarea", "number", "select"]
    required: bool = False
    default: str | int | None = None
    minimum: int | None = None
    maximum: int | None = None
    options: list[str] = Field(default_factory=list)
    placeholder_zh: str = ""
    placeholder_en: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowManifest(BaseModel):
    workflow_id: str
    workflow_version: str
    work_type: Literal["presentation", "poster", "video", "report", "spreadsheet"]
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    fields: list[WorkflowField]
    approval_policy: list[str]
    output_contract: list[str]
    required_capability: Literal["presentation", "image", "video", "document"]
    launch_policy: Literal["agent_runtime", "dry_run"] = "dry_run"

    model_config = ConfigDict(extra="forbid", frozen=True)


def _fallback_policy_field() -> WorkflowField:
    return WorkflowField(
        key="fallback_policy",
        label_zh="质量降级策略",
        label_en="Quality fallback policy",
        kind="select",
        default="primary_only",
        options=["primary_only", "allow_degraded"],
        placeholder_zh="正式质量优先，或明确允许应急质量",
        placeholder_en="Prefer formal quality or explicitly allow emergency quality",
    )


_WORKFLOWS = (
    WorkflowManifest(
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
        work_type="presentation",
        label_zh="PPT 演示文稿",
        label_en="Presentation",
        description_zh="先确认目标、受众和结构，再交给数字员工生成可预览的文件。",
        description_en="Confirm the goal, audience, and structure before generating a previewable file.",
        fields=[
            WorkflowField(
                key="audience", label_zh="目标受众", label_en="Audience", kind="text",
                placeholder_zh="例如：潜在投资人", placeholder_en="e.g. prospective investors",
            ),
            WorkflowField(
                key="page_count", label_zh="页数", label_en="Slides", kind="number", required=True,
                default=8, minimum=5, maximum=15,
            ),
            WorkflowField(
                key="language", label_zh="语言", label_en="Language", kind="select", required=True,
                default="zh-CN", options=["zh-CN", "en-US"],
            ),
            WorkflowField(
                key="style", label_zh="视觉风格", label_en="Visual style", kind="text", required=True,
                default="professional", placeholder_zh="例如：专业、简洁、科技感", placeholder_en="e.g. professional and concise",
            ),
            WorkflowField(
                key="key_points", label_zh="必须覆盖", label_en="Required points", kind="textarea",
                placeholder_zh="逐条写出必须出现的数据、观点或文案", placeholder_en="List required data, claims, or copy",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["outline", "final"],
        output_contract=["pptx", "pdf"],
        required_capability="presentation",
        launch_policy="agent_runtime",
    ),
    WorkflowManifest(
        workflow_id="builtin.poster.v1",
        workflow_version="1.0.0",
        work_type="poster",
        label_zh="海报 / 图片",
        label_en="Poster / Image",
        description_zh="确认商品、品牌和精确文案要求，再生成可直接预览的商业图片。",
        description_en="Confirm product, brand, and exact-copy requirements before generating a previewable commercial image.",
        fields=[
            WorkflowField(
                key="channel", label_zh="使用渠道", label_en="Channel", kind="select", required=True,
                default="social", options=["social", "ecommerce", "print"],
            ),
            WorkflowField(
                key="aspect_ratio", label_zh="画面比例", label_en="Aspect ratio", kind="select", required=True,
                default="3:4", options=["1:1", "3:4", "9:16", "16:9"],
            ),
            WorkflowField(
                key="exact_copy", label_zh="精确文案", label_en="Exact copy", kind="textarea",
                placeholder_zh="需要逐字保留的中英文文案", placeholder_en="Copy that must be preserved exactly",
            ),
            WorkflowField(
                key="style", label_zh="视觉风格", label_en="Visual style", kind="text", required=True,
                default="commercial", placeholder_zh="例如：高端商业广告", placeholder_en="e.g. premium commercial campaign",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["composition", "final"],
        output_contract=["png"],
        required_capability="image",
        launch_policy="agent_runtime",
    ),
    WorkflowManifest(
        workflow_id="builtin.video.v1",
        workflow_version="1.0.0",
        work_type="video",
        label_zh="短视频",
        label_en="Short video",
        description_zh="制作带人物、分镜和旁白的可审核广告短视频。",
        description_en="Create a reviewable short ad with people, storyboard, and voiceover.",
        fields=[
            WorkflowField(
                key="channel", label_zh="发布渠道", label_en="Channel", kind="select", required=True,
                default="social", options=["social", "ecommerce", "presentation"],
            ),
            WorkflowField(
                key="aspect_ratio", label_zh="画面比例", label_en="Aspect ratio", kind="select", required=True,
                default="9:16", options=["9:16", "16:9", "1:1"],
            ),
            WorkflowField(
                key="duration", label_zh="时长（秒）", label_en="Duration (seconds)", kind="select", required=True,
                default="6", options=["6", "10"],
            ),
            WorkflowField(
                key="audience", label_zh="目标受众", label_en="Audience", kind="text", required=True,
                default="潜在消费者", placeholder_zh="例如：25–35 岁都市白领",
                placeholder_en="e.g. urban professionals aged 25–35",
            ),
            WorkflowField(
                key="language", label_zh="语言", label_en="Language", kind="select", required=True,
                default="zh-CN", options=["zh-CN", "en-US"],
            ),
            WorkflowField(
                key="audio_mode", label_zh="声音模式", label_en="Audio mode", kind="select", required=True,
                default="voiceover", options=["voiceover", "silent"],
            ),
            WorkflowField(
                key="story", label_zh="故事与镜头要求", label_en="Story and shots", kind="textarea", required=True,
                placeholder_zh="产品、场景、镜头运动、字幕和声音要求", placeholder_en="Product, scene, camera, captions, and audio",
            ),
            WorkflowField(
                key="cta", label_zh="行动号召", label_en="Call to action", kind="text",
                placeholder_zh="例如：立即了解更多", placeholder_en="e.g. Learn more today",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
        required_capability="video",
        launch_policy="agent_runtime",
    ),
)

WORKFLOW_BY_TYPE = {workflow.work_type: workflow for workflow in _WORKFLOWS}
WORKFLOW_BY_ID = {workflow.workflow_id: workflow for workflow in _WORKFLOWS}


class DeliverableWorkflowError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def list_workflow_manifests() -> list[WorkflowManifest]:
    return list(_WORKFLOWS)


async def list_agent_launchable_workflows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tier: str,
) -> list[WorkflowManifest]:
    """Return only real executable workflows for the exact Agent context."""
    normalized_tier = str(tier or "").strip().lower()
    if normalized_tier not in SAAS_TIERS:
        return []
    try:
        await resolve_route(tenant_id, normalized_tier, "text")
    except QuotaExceeded:
        return []

    available: list[WorkflowManifest] = []
    for workflow in _WORKFLOWS:
        if workflow.launch_policy != "agent_runtime":
            continue
        if (
            workflow.required_capability == "presentation"
            and not await _presentation_tool_available(db, agent_id)
        ):
            continue
        if (
            workflow.required_capability == "image"
            and not await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_image_minimax",
            )
        ):
            continue
        if (
            workflow.required_capability == "video"
            and not await _video_post_production_tools_available(db, agent_id)
        ):
            continue
        available.append(workflow)
    return available


def require_workflow(
    work_type: str,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
) -> WorkflowManifest:
    workflow = WORKFLOW_BY_TYPE.get(str(work_type or "").strip().lower())
    if workflow is None:
        raise DeliverableWorkflowError("unsupported_work_type", "Unsupported deliverable type")
    if workflow_id is not None and workflow_id != workflow.workflow_id:
        raise DeliverableWorkflowError("workflow_mismatch", "Workflow does not match the work type")
    if workflow_version is not None and workflow_version != workflow.workflow_version:
        raise DeliverableWorkflowError("workflow_version_mismatch", "Workflow version is not supported")
    return workflow


def validate_workflow_spec(workflow: WorkflowManifest, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise DeliverableWorkflowError("invalid_spec", "Deliverable spec must be an object")
    fields = {field.key: field for field in workflow.fields}
    unknown = sorted(set(spec).difference(fields))
    if unknown:
        raise DeliverableWorkflowError(
            "unknown_spec_field",
            f"Unsupported spec fields: {', '.join(unknown)}",
        )
    normalized: dict[str, Any] = {}
    for key, field in fields.items():
        value = spec.get(key, field.default)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            if field.required:
                raise DeliverableWorkflowError("missing_spec_field", f"Missing required field: {key}")
            continue
        if field.kind == "number":
            if isinstance(value, bool):
                raise DeliverableWorkflowError("invalid_spec_field", f"{key} must be a number")
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise DeliverableWorkflowError("invalid_spec_field", f"{key} must be a number") from exc
            if field.minimum is not None and value < field.minimum:
                raise DeliverableWorkflowError("invalid_spec_field", f"{key} is below the minimum")
            if field.maximum is not None and value > field.maximum:
                raise DeliverableWorkflowError("invalid_spec_field", f"{key} exceeds the maximum")
        elif not isinstance(value, str):
            raise DeliverableWorkflowError("invalid_spec_field", f"{key} must be text")
        if field.options and str(value) not in field.options:
            raise DeliverableWorkflowError("invalid_spec_field", f"{key} has an unsupported value")
        normalized[key] = value
    return normalized


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def presentation_media_roles_for_brief(
    goal: str,
    spec: Mapping[str, Any],
    *,
    tier: str | None = None,
) -> tuple[str, ...]:
    """Compile the image contract from the same brief used by the prompt.

    Keeping this pure and request-independent lets the API preflight inspect
    composite PPT dependencies before a Deliverable is launched.  The prompt
    builder and the launch preflight must agree on whether a deck is
    image-led; otherwise a missing image route is only discovered halfway
    through execution.
    """
    brief = " ".join(
        (
            str(goal or ""),
            json.dumps(dict(spec or {}), ensure_ascii=False, sort_keys=True),
        )
    ).casefold()
    if not presentation_brief_is_image_led(goal, spec):
        return ()

    roles: list[str] = []
    if any(
        keyword in brief
        for keyword in (
            "产品",
            "商品",
            "新品",
            "包装",
            "保温杯",
            "product",
            "packaging",
            "launch",
        )
    ):
        roles.append("product_hero")
    if any(
        keyword in brief
        for keyword in (
            "人物",
            "真人",
            "人像",
            "模特",
            "用户场景",
            "person",
            "people",
            "actor",
            "portrait",
            "lifestyle",
        )
    ):
        roles.append("people_lifestyle")
    if any(keyword in brief for keyword in ("故事板", "分镜", "storyboard", "shot plan")) or (
        "镜头" in brief and "脚本" in brief
    ):
        roles.append("people_storyboard")

    page_count = int((spec or {}).get("page_count") or 8)
    tier_bonus = 1 if str(tier or "").lower() == "ultra" else 0
    desired_assets = min(5, max(2, math.ceil(page_count / 3) + tier_bonus))
    for fallback_role in (
        "commercial_hero",
        "context_scene",
        "detail_texture",
        "audience_moment",
        "closing_hero",
    ):
        if len(roles) >= desired_assets:
            break
        if fallback_role not in roles:
            roles.append(fallback_role)
    return tuple(roles)


def _presentation_media_roles(request: DeliverableRequest) -> tuple[str, ...]:
    """Compile an explicit media contract for image-led commercial decks."""

    return presentation_media_roles_for_brief(
        request.goal,
        request.spec or {},
        tier=request.tier,
    )


_PRESENTATION_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def presentation_supplied_image_paths(
    inputs: Sequence[Mapping[str, Any] | object] | None,
) -> tuple[str, ...]:
    """Return distinct, safe image attachments in the user's declared order."""

    paths: list[str] = []
    seen: set[str] = set()
    for item in inputs or ():
        value = item.get("path") if isinstance(item, Mapping) else getattr(item, "path", None)
        path = str(value or "").strip().replace("\\", "/")
        normalized = path.casefold()
        if (
            not path.startswith("workspace/")
            or ".." in path.split("/")
            or not normalized.endswith(_PRESENTATION_IMAGE_SUFFIXES)
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        paths.append(path)
    return tuple(paths)


def _presentation_media_plan(
    request: DeliverableRequest,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...], tuple[str, ...]]:
    """Bind supplied imagery first and generate only genuinely missing roles."""

    roles = _presentation_media_roles(request)
    supplied_paths = presentation_supplied_image_paths(request.inputs)
    bindings = tuple(
        {
            "role": role,
            "workspace_path": path,
            "asset_ref": posixpath.relpath(
                path,
                start=f"workspace/deliverables/{request.id}",
            ),
        }
        for role, path in zip(roles, supplied_paths, strict=False)
    )
    generated_roles = roles[len(bindings) :]
    return roles, bindings, generated_roles


def _presentation_visual_policy(
    request: DeliverableRequest,
    media_roles: tuple[str, ...],
) -> dict[str, float | int | str]:
    """Build a server-owned, page-count-aware visual variety contract."""

    page_count = int((request.spec or {}).get("page_count") or 8)
    minimum_distinct_images = len(media_roles)
    return {
        "version": "adaptive-v1",
        "minimum_distinct_layouts": min(page_count, max(3, math.ceil(page_count / 2))),
        "minimum_distinct_images": minimum_distinct_images,
        # Image-led decks must distribute imagery across the narrative rather
        # than hiding every generated asset on one page.  This is a page-count
        # policy, not a fixed template; text/data-led decks keep this at zero.
        "minimum_image_slides": (
            min(page_count, max(1, math.ceil(page_count / 2)))
            if minimum_distinct_images
            else 0
        ),
        # This is enforced again against the final PPTX geometry.  Declaring
        # it in the prompt/source contract makes the quality expectation
        # visible to the Agent instead of silently applying a post-hoc rule.
        "minimum_picture_coverage_ratio": (
            MINIMUM_PICTURE_COVERAGE_RATIO if minimum_distinct_images else 0.0
        ),
        "maximum_uses_per_image": (
            max(2, math.ceil(page_count / minimum_distinct_images))
            if minimum_distinct_images
            else 0
        ),
        "minimum_editable_compositions": max(1, page_count // 4),
    }


def build_deliverable_prompt(request: DeliverableRequest) -> str:
    """Build server-owned execution context; provider/model choice is intentionally absent."""

    from app.services.poster_contract import (
        poster_exact_copy_contract,
        poster_execution_policy,
    )

    poster_copy_blocks, poster_copy_sha256 = (
        poster_exact_copy_contract(request.spec)
        if request.work_type == "poster"
        else ((), None)
    )

    contract = {
        "request_id": str(request.id),
        "work_type": request.work_type,
        "workflow_id": request.workflow_id,
        "workflow_version": request.workflow_version,
        "goal": request.goal,
        "inputs": request.inputs,
        "spec": request.spec,
        "tier": request.tier,
        "approval_policy": request.approval_policy,
        "output_contract": request.output_contract,
        "exact_copy_blocks": list(poster_copy_blocks),
        "exact_copy_blocks_sha256": poster_copy_sha256,
    }
    poster_policy = (
        poster_execution_policy(request.spec)
        if request.work_type == "poster"
        else None
    )
    if poster_policy is not None:
        contract["execution_policy"] = {
            "execution_strategy": poster_policy.execution_strategy,
            "allow_degraded_fallback": poster_policy.allow_degraded_fallback,
        }
    allow_degraded_fallback = (
        poster_policy.allow_degraded_fallback
        if poster_policy is not None
        else str((request.spec or {}).get("fallback_policy") or "primary_only")
        == "allow_degraded"
    )
    allow_degraded_literal = str(allow_degraded_fallback).lower()
    if request.work_type == "poster":
        return (
            "You are executing a persisted Astra Deliverable Request. Treat the following JSON as "
            "the authoritative product brief. Do not choose or reveal a provider/model. Use only enabled "
            "tools and keep every artifact under workspace/deliverables/"
            f"{request.id}/. Create one polished commercial image with generate_image_minimax exactly once, "
            "using save_path='workspace/deliverables/"
            f"{request.id}/final.png', aspect_ratio=spec.aspect_ratio, and "
            "execution_strategy='commercial_quality', and "
            f"allow_degraded_fallback={allow_degraded_literal}. The composition must match the "
            "requested channel and style. Preserve every requested art-direction element, palette, lighting "
            "cue, spatial layer, and financial/technology motif in the provider prompt; do not simplify a "
            "detailed brief into a flat gradient or empty template. Use a clear visual hierarchy, reserve "
            "clean negative space for the "
            "requested copy, and contain no generated words, "
            "captions, logos, watermarks, signatures, UI chrome, or placeholder text. If exact_copy is "
            "non-empty, pass DELIVERABLE_REQUEST.exact_copy_blocks unchanged as overlay_blocks in that same call; "
            "the server will reject any mismatch before a paid provider request. The returned "
            "PNG uses deterministic text composition, so never ask the image model to spell exact copy. The "
            "returned "
            "PNG is already the final deterministic composition, so never overlay the same copy again in HTML, "
            "PDF, PPTX, or another image. Create only formats explicitly listed in output_contract. Call the generation "
            "Tool exactly once because it owns provider fallback and durable recovery. If no provider accepts "
            "the request, stop without retrying or claiming delivery. Do not read the binary image. The final "
            "response must report the exact versioned PNG workspace path returned by the Tool, and never claim "
            "success until the registered artifact contract confirms it.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )
    if request.work_type == "video":
        first_frame_path = f"workspace/deliverables/{request.id}/first_frame.png"
        return (
            "You are executing a persisted Astra Deliverable Request. Treat the following JSON as "
            "the authoritative product brief. Do not choose or reveal a provider/model. Use only enabled "
            "tools, keep every artifact under workspace/deliverables/"
            f"{request.id}/, and never claim success until every output_contract file exists and validates. "
            "This is a real people-led advertising video, not a slideshow, product spin, storyboard-only "
            "deliverable, or empty MP4. First write workspace/deliverables/"
            f"{request.id}/storyboard.json with a concise hook, three visual beats, adult actor direction, "
            "product interaction, CTA, narration, and shot timing. Then generate one coherent continuous "
            "clip with an adult on-camera actor visibly using or presenting the product. Before submitting "
            "video, call generate_image_minimax exactly once to create a clean commercial first frame with "
            f"save_path='{first_frame_path}' and aspect_ratio=spec.aspect_ratio. The first frame must match "
            "the storyboard opening shot, show the same adult actor and product interaction required by the "
            "brief, and contain no generated words, captions, logos, watermarks, or CTA. Use the exact "
            "versioned workspace output_path returned by that successful image Tool call as first_frame_image "
            "for the video Tool; never assume the unversioned requested file name and never submit video if "
            "the first-frame Tool did not succeed. Preserve the requested aspect ratio and duration and use "
            "generate_video_minimax with save_path='workspace/deliverables/"
            f"{request.id}/visual.mp4', aspect_ratio=spec.aspect_ratio, duration=spec.duration, "
            "first_frame_image=<exact returned first-frame output_path>, require_audio=false, "
            f"wait_for_completion=true, poll_timeout_seconds=300, and allow_degraded_fallback={allow_degraded_literal}. "
            "The call must omit the resolution argument because the server locks the current configured formal-tier quality profile. "
            "The first-frame generate_image_minimax call must use the same allow_degraded_fallback value. "
            "Call each generation Tool exactly once "
            "because each Tool owns provider fallback and durable recovery. If no provider accepts the "
            "request, stop without "
            "retrying, scheduling a trigger, or claiming partial delivery. Do not attempt to read binary "
            "video files. "
            "When the visual task completes, if spec.audio_mode is 'voiceover', call generate_speech_minimax "
            "with the approved concise narration and save_path='workspace/deliverables/"
            f"{request.id}/voiceover.mp3', then call compose_video_audio with the completed visual path, "
            "that voiceover path, and save_path='workspace/deliverables/"
            f"{request.id}/final.mp4'. If spec.audio_mode is 'silent', generate directly to "
            f"'workspace/deliverables/{request.id}/final.mp4' and do not claim audio exists. "
            "The final response must report the exact final.mp4 workspace path. A provider task id, "
            "storyboard, prompt, visual-only intermediate, or voiceover-only file is not the final deliverable.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )
    (
        presentation_media_roles,
        presentation_supplied_assets,
        presentation_generation_roles,
    ) = _presentation_media_plan(request)
    presentation_visual_policy = _presentation_visual_policy(
        request,
        presentation_media_roles,
    )
    presentation_media_contract = {
        "required": bool(presentation_media_roles),
        "asset_roles": list(presentation_media_roles),
        "minimum_distinct_images": len(presentation_media_roles),
        "supplied_assets": list(presentation_supplied_assets),
        "generation_required_roles": list(presentation_generation_roles),
    }
    media_instructions = (
        "PRESENTATION_MEDIA_CONTRACT="
        f"{json.dumps(presentation_media_contract, ensure_ascii=False, sort_keys=True)} "
    )
    if presentation_media_roles:
        role_instructions: list[str] = []
        for role in presentation_generation_roles:
            if role == "product_hero":
                role_instructions.append(
                    "For product_hero, call generate_image_minimax exactly once with a polished "
                    "16:9 product advertising prompt, no generated words/logos/watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/product_hero.png', and "
                    f"allow_degraded_fallback={allow_degraded_literal}."
                )
            elif role == "people_storyboard":
                role_instructions.append(
                    "For people_storyboard, call generate_image_minimax exactly once with a "
                    "single coherent three-panel people-led storyboard image, no generated "
                    "words/logos/watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/people_storyboard.png'. "
                    f"Use allow_degraded_fallback={allow_degraded_literal}. "
                    "If product_hero was generated, pass its exact versioned output_path as "
                    "reference_image so the product remains recognizable."
                )
            elif role == "people_lifestyle":
                role_instructions.append(
                    "For people_lifestyle, call generate_image_minimax exactly once with a "
                    "single coherent people-led commercial lifestyle scene, no generated "
                    "words/logos/watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/people_lifestyle.png'. "
                    f"Use allow_degraded_fallback={allow_degraded_literal}. "
                    "If product_hero was generated, pass its exact versioned output_path as "
                    "reference_image so the product remains recognizable."
                )
            elif role == "context_scene":
                role_instructions.append(
                    "For context_scene, call generate_image_minimax exactly once with a "
                    "distinct environmental scene that supports the deck story without "
                    "generated words/logos/watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/context_scene.png'. "
                    f"Use allow_degraded_fallback={allow_degraded_literal}. "
                    "Use product_hero as reference_image when product identity matters."
                )
            elif role == "detail_texture":
                role_instructions.append(
                    "For detail_texture, call generate_image_minimax exactly once with a "
                    "macro detail or material-led composition, no generated words/logos/"
                    "watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/detail_texture.png'. "
                    f"Use allow_degraded_fallback={allow_degraded_literal}. "
                    "Use product_hero as reference_image when product identity matters."
                )
            else:
                role_instructions.append(
                    f"For {role}, call generate_image_minimax exactly once with a polished 16:9 "
                    "commercial hero prompt, no generated words/logos/watermarks, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/{role}.png', and "
                    f"allow_degraded_fallback={allow_degraded_literal}."
                )
        supplied_instructions = ""
        if presentation_supplied_assets:
            supplied_instructions = (
                "PRESENTATION_MEDIA_CONTRACT.supplied_assets is authoritative. Use every mapped "
                "workspace image without regenerating, redrawing, moving, or replacing it. For each "
                "mapped role, use its exact asset_ref in both presentation.html and "
                "slide_spec.asset_ref, and include its workspace_path in source_refs. The relative "
                "asset_ref may contain '..'; the converter resolves and materializes it safely. "
            )
        generation_instructions = " ".join(role_instructions)
        if not presentation_generation_roles:
            generation_instructions = (
                "All required media roles are satisfied by supplied assets. Do not call "
                "generate_image_minimax for this presentation."
            )
        media_instructions = (
            "The server-owned PRESENTATION_MEDIA_CONTRACT below requires real imagery. "
            "Complete every required asset role before writing presentation.html. "
            + supplied_instructions
            + generation_instructions
            + " For generated roles only, use the exact versioned workspace output_path returned "
            "by each successful Tool call to derive slide_spec.asset_ref and reference it from "
            "presentation.html with the same path relative to presentation.html (for example "
            "assets/<versioned-file>.png). "
            "Keep slide_spec.visual_asset as a human-readable description. Embed every "
            "required image in at least one visible <img> or CSS background-image region and crop "
            "it intentionally; decorative CSS silhouettes, gradients, icons, and emoji do not "
            "satisfy this media contract. Do not use emoji as a substitute for photography or "
            "illustration. Each required generation Tool call owns provider fallback, so call it only once "
            "per role and never manually retry. If any required image Tool call fails or is "
            "unavailable, stop without converting or claiming a commercial-quality deck; report "
            "the missing asset role and request supplied imagery instead. "
            f"PRESENTATION_MEDIA_CONTRACT={json.dumps(presentation_media_contract, ensure_ascii=False, sort_keys=True)} "
        )
    return (
        "You are executing a persisted Astra Deliverable Request. Treat the following JSON as "
        "the authoritative product brief. Do not choose or reveal a provider/model. Use only enabled "
        "tools, keep every artifact under workspace/deliverables/"
        f"{request.id}/, and never claim success until every output_contract file exists and validates. "
        "For presentation requests, first write workspace/deliverables/"
        f"{request.id}/outline.json with deck_title, audience, core_message, and exactly "
        "spec.page_count ordered slides. Every outline slide must contain slide_id, purpose, headline, "
        "evidence, and visual_intent; evidence may be one non-empty sentence or an array of evidence "
        "sentences. Then write workspace/deliverables/"
        f"{request.id}/slide_spec.json with the same ordered slide_ids and a headline, layout, body_points, "
        "visual_asset, and source_refs for every slide. The top-level slide_spec must include "
        "visual_plan_version='adaptive-v1' and visual_policy exactly matching PRESENTATION_VISUAL_POLICY. "
        "Every slide must also declare slide_type and visual_kind. visual_kind must be one of "
        "generated_image, supplied_image, editable_chart, editable_diagram, editable_table, or "
        "editable_typography. Image slides must declare asset_ref using the exact relative path rendered "
        "by that slide; editable compositions must use an empty asset_ref. source_refs must be an array whose entries are "
        "either non-empty reference strings or objects with a non-empty ref field. The outline, "
        "slide_spec, and final visible slide "
        "titles must agree exactly; never fabricate evidence or a source reference. source_refs must point "
        "to the user brief, a supplied workspace file, or a real URL and must never cite an internal "
        "workflow id. "
        f"{media_instructions}"
        "PRESENTATION_VISUAL_POLICY="
        f"{json.dumps(presentation_visual_policy, ensure_ascii=False, sort_keys=True)}. "
        "Treat it as a minimum quality contract, not a list of templates: choose layouts from each "
        "slide's purpose and information shape, do not repeat the same layout on consecutive slides, "
        "respect the image reuse cap, and reserve the required number of slides for editable charts, "
        "diagrams, tables, or typography compositions. For image-led decks, "
        "the final PPTX must meet minimum_picture_coverage_ratio across the "
        "whole deck; do not satisfy the image contract with tiny thumbnails "
        "or decorative image chips. "
        "Avoid one oversized write_file call: the first presentation.html write_file call MUST stay "
        "under 3500 characters and contain only shared CSS plus one unique placeholder section "
        "for every requested slide. Do not put complete slide bodies in that first call. Then replace "
        "exactly one placeholder per edit_file call so no tool call contains the whole deck. Read the "
        "finished source back before conversion and "
        "confirm it contains exactly spec.page_count HTML sections matching `.slide[data-slide]`, "
        "with exactly one visible `[data-slide-title]` in every section matching slide_spec.headline, "
        "a `data-layout` value matching slide_spec.layout, and at least one `[data-visual]` region that "
        "actually implements the declared visual_asset. A title plus bullet list, an empty frame, a "
        "descriptive placeholder, or unused visual_intent is not an implemented visual. Use CSS/HTML "
        "diagrams, timelines, comparison matrices, process lanes, metric compositions, or real supplied/"
        "generated images as appropriate, while keeping all important text editable. "
        "Treat visual fit as a hard contract: every visible text node must remain fully inside its "
        "1280x720 slide with at least 24px bottom safety. Never rely on overflow:hidden to hide dense "
        "content. Use at least 16px computed font size for every title, body, table, caption, footnote, "
        "and decision label. Only short folios or eyebrow labels placed at the top/bottom slide edge may "
        "use 10-15px, and those elements must declare data-clawith-text-role='metadata'; never use that "
        "role for body copy, tables, evidence, or footnotes. Split a slide or reduce copy density instead "
        "of shrinking readable content. Reduce card padding, gaps, image height, or copy density before "
        "conversion when a slide approaches the canvas edge. "
        "Do not invent star ratings, scores, percentages, ROI claims, price support, market rankings, "
        "performance thresholds, or other quantified judgments unless they are present in the brief or "
        "a supplied source. When data is absent, label the statement as a hypothesis to validate and use "
        "qualitative decision criteria instead of fabricated numbers. Never emit unresolved placeholder "
        "copy such as [Your Brand Logo], TODO, TBD, 待替换, 待补充, 请填写, or 占位符; the deterministic "
        "presentation contract rejects placeholders and rating glyphs even if the rest of the deck renders. "
        "each fixed at 1280x720 pixels with overflow hidden and a print page break after every slide "
        "except the last. Do not invent a brand, organization, team, author, "
        "or internal-use label that is absent from the brief. When the brief calls for commercial "
        "product or people imagery, use supplied or generated image assets rather than empty frames "
        "or diagram placeholders. Treat a supplied product_hero as the canonical identity reference; "
        "never generate a replacement for an already mapped supplied role. When no canonical "
        "product_hero is supplied, generate one canonical hero asset first, before other missing "
        "roles, and pass its exact workspace path as "
        "reference_image for later generated scenes. If the available generator cannot preserve the "
        "product identity, reuse and crop the canonical asset instead of independently inventing a "
        "different product. Convert that single HTML "
        "source to PPTX using convert_html_to_pptx "
        "with design_width=1280, design_height=720, render_mode='hybrid_editable', render_scale=2, "
        "expected_page_count=spec.page_count, outline_path='workspace/deliverables/"
        f"{request.id}/outline.json', and slide_spec_path='workspace/deliverables/"
        f"{request.id}/slide_spec.json'. "
        "Convert the same source to PDF using convert_html_to_pdf with design_width=1280, "
        "design_height=720, pdf_mode='pages', scale=1, paper_width=13.333333, paper_height=7.5, "
        "expected_page_count=spec.page_count, outline_path='workspace/deliverables/"
        f"{request.id}/outline.json', and slide_spec_path='workspace/deliverables/"
        f"{request.id}/slide_spec.json'. "
        "The PPTX and PDF must each contain exactly spec.page_count 16:9 pages. Report both exact "
        "workspace paths and do not claim visual consistency, no-overflow, or page-count success "
        "unless the registered artifact contract confirms it.\n"
        f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
    )


async def _agent_tool_available(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_name: str,
) -> bool:
    result = await db.execute(
        select(Tool).where(
            Tool.name == tool_name,
            Tool.enabled == True,  # noqa: E712
        )
    )
    tool = result.scalar_one_or_none()
    if tool is None:
        return False
    assignment_result = await db.execute(
        select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id == tool.id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    return tool_enabled_for_agent(tool, assignment)


async def _presentation_tool_available(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    return all(
        [
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="convert_html_to_pptx",
            ),
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="convert_html_to_pdf",
            ),
        ]
    )


async def _video_post_production_tools_available(
    db: AsyncSession,
    agent_id: uuid.UUID,
) -> bool:
    return all(
        [
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_image_minimax",
            ),
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_video_minimax",
            ),
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_speech_minimax",
            ),
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="compose_video_audio",
            ),
        ]
    )


def _credit_estimate(workflow: WorkflowManifest, tier: str, spec: dict[str, Any]) -> dict[str, Any]:
    if workflow.work_type == "presentation":
        return {
            "mode": "usage_based",
            "minimum": None,
            "maximum": None,
            "billing_unit": "actual_usage",
        }
    if workflow.work_type == "poster":
        credits = minimax_image_credits("image-01", images=1)
        return {
            "mode": "estimate",
            "minimum": credits,
            "maximum": credits,
            "billing_unit": "one_final_image",
        }
    if workflow.work_type == "video":
        profile = resolve_minimax_media_profile("video", tier)
        requested_duration = int(spec.get("duration") or profile.duration or 6)
        duration = requested_duration
        resolution = str(profile.resolution or "768P")
        if tier == "lite":
            duration, resolution = 6, "768P"
        elif tier == "pro" and duration not in {6, 10}:
            duration = 6
        elif tier == "ultra" and duration == 10:
            resolution = "768P"
        first_frame_credits = minimax_image_credits("image-01", images=1)
        video_credits = minimax_video_credits(profile.model, duration=duration, resolution=resolution)
        credits = first_frame_credits + video_credits
        return {
            "mode": "estimate",
            "minimum": credits,
            "maximum": credits,
            "billing_unit": f"one_first_frame_plus_{duration}s_{resolution.lower()}_clip",
        }
    return {"mode": "usage_based", "minimum": None, "maximum": None, "billing_unit": "actual_usage"}


async def preflight_workflow(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    workflow: WorkflowManifest,
    tier: str,
    spec: dict[str, Any],
    goal: str = "",
    inputs: Sequence[Mapping[str, Any] | object] | None = None,
) -> dict[str, Any]:
    normalized_tier = str(tier or "lite").strip().lower()
    if normalized_tier not in SAAS_TIERS:
        raise DeliverableWorkflowError("invalid_tier", "Tier must be lite, pro, or ultra")
    normalized_spec = validate_workflow_spec(workflow, spec)
    reasons: list[str] = []
    capability_status = "available"
    next_action = "确认工作说明后，由平台按正式质量合同选择执行线路。"

    if workflow.work_type == "poster":
        from app.services.media_assets import MediaContractError, preflight_poster_layout
        from app.services.poster_contract import poster_exact_copy_contract

        try:
            poster_blocks, _poster_digest = poster_exact_copy_contract(normalized_spec)
        except MediaContractError:
            poster_blocks = ()
            reasons.append("poster_exact_copy_contract_invalid")
        if poster_blocks:
            try:
                preflight_poster_layout(
                    poster_blocks,
                    aspect_ratio=str(normalized_spec.get("aspect_ratio") or ""),
                )
            except MediaContractError:
                reasons.append("poster_layout_unfit")

    try:
        await resolve_route(tenant_id, normalized_tier, "text")
    except QuotaExceeded as exc:
        reasons.append(str(getattr(exc, "quota_type", None) or "text_route_unavailable"))

    presentation_image_required = False
    if workflow.required_capability == "presentation":
        if not await _presentation_tool_available(db, agent_id):
            reasons.append("presentation_tool_unavailable")
        presentation_roles = presentation_media_roles_for_brief(
            goal,
            normalized_spec,
            tier=normalized_tier,
        )
        supplied_image_count = min(
            len(presentation_roles),
            len(presentation_supplied_image_paths(inputs)),
        )
        presentation_image_required = bool(presentation_roles[supplied_image_count:])

    media_modality = (
        "image"
        if presentation_image_required
        else workflow.required_capability
        if workflow.required_capability in {"image", "video"}
        else None
    )
    media: list[dict[str, Any]] = []
    if media_modality is not None:
        entitlements = await get_tenant_entitlements(tenant_id)
        media = await get_agent_media_capabilities(
            db,
            agent_id=agent_id,
            entitlements=entitlements,
            tier=normalized_tier,
        )
        row = next((item for item in media if item["modality"] == media_modality), None)
        if row is None or not row["available"]:
            reasons.append(str((row or {}).get("reason") or "media_capability_unavailable"))
            capability_status = "unavailable"
            next_action = str(
                (row or {}).get("next_action")
                or "保留工作说明并修复套餐、工具或账号池配置后重试。"
            )
        else:
            capability_status = str(row.get("capability_status") or "available")
            next_action = str(row.get("next_action") or next_action)
            if media_modality == "image" and "available_providers" in row:
                strategy_order = media_provider_order_for_image_strategy(
                    "commercial_quality"
                )
                available_providers = {
                    str(provider or "").strip().lower()
                    for provider in row.get("available_providers", [])
                    if str(provider or "").strip()
                }
                preferred_ready = bool(
                    strategy_order
                    and strategy_order[0] in available_providers
                )
                strategy_ready = any(
                    provider in available_providers
                    for provider in strategy_order
                )
                if strategy_ready and not preferred_ready:
                    capability_status = "degraded"
                    next_action = (
                        "正式工作流固定使用商用品质策略；当前只有备选图片线路，"
                        "保持正式质量优先可等待首选线路，或明确允许备选线路。"
                    )
            if (
                capability_status == "degraded"
                and normalized_spec.get("fallback_policy") != "allow_degraded"
            ):
                reasons.append("degraded_route_requires_confirmation")
                next_action = (
                    "当前只有应急质量线路。保持正式质量优先可保存工作说明并等待；"
                    "如接受质量差异，请在高级设置中明确允许应急质量。"
                )
        if (
            media_modality == "image"
            and not await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_image_minimax",
            )
        ):
            reasons.append("image_generation_tool_unavailable")
        if (
            media_modality == "video"
            and normalized_spec.get("audio_mode") == "voiceover"
            and not await _video_post_production_tools_available(db, agent_id)
        ):
            reasons.append("video_post_production_tool_unavailable")

        if workflow.work_type == "video":
            first_frame_row = next(
                (item for item in media if item["modality"] == "image"),
                None,
            )
            if first_frame_row is None or not first_frame_row.get("available"):
                reasons.append("video_first_frame_image_capability_unavailable")
                capability_status = "unavailable"
            else:
                strategy_order = media_provider_order_for_image_strategy(
                    "commercial_quality"
                )
                available_providers = {
                    str(provider or "").strip().lower()
                    for provider in first_frame_row.get("available_providers", [])
                    if str(provider or "").strip()
                }
                if not any(
                    provider in available_providers for provider in strategy_order
                ):
                    reasons.append("video_first_frame_image_capability_unavailable")
                    capability_status = "unavailable"
                elif (
                    strategy_order[0] not in available_providers
                    and normalized_spec.get("fallback_policy") != "allow_degraded"
                ):
                    reasons.append("degraded_route_requires_confirmation")
                    if capability_status == "available":
                        capability_status = "degraded"
            if not await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name="generate_image_minimax",
            ):
                reasons.append("video_first_frame_image_tool_unavailable")

    if workflow.launch_policy == "dry_run":
        reasons.append("workflow_execution_not_enabled")
    soft_reasons = {
        "workflow_execution_not_enabled",
        "degraded_route_requires_confirmation",
    }
    reasons = list(dict.fromkeys(reasons))
    hard_reasons = [reason for reason in reasons if reason not in soft_reasons]
    return {
        "available": not hard_reasons,
        "launchable": not reasons and workflow.launch_policy == "agent_runtime",
        "reasons": reasons,
        "capability_status": capability_status,
        "next_action": next_action,
        "tier": normalized_tier,
        "normalized_spec": normalized_spec,
        "credit_estimate": _credit_estimate(workflow, normalized_tier, normalized_spec),
        "creates_reservation": False,
    }


@dataclass(slots=True)
class PreparedDeliverableLaunch:
    request: DeliverableRequest
    prompt: str
    execution: DeliverableExecution | None = None


async def prepare_deliverable_launch(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
) -> PreparedDeliverableLaunch:
    result = await db.execute(
        select(DeliverableRequest)
        .where(
            DeliverableRequest.id == request_id,
            DeliverableRequest.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise DeliverableWorkflowError("deliverable_not_found", "Deliverable request was not found")
    if (
        request.created_by_user_id != user_id
        or request.agent_id != agent_id
        or request.session_id != session_id
    ):
        raise DeliverableWorkflowError("deliverable_scope_mismatch", "Deliverable request is not available in this chat")
    workflow = require_workflow(request.work_type, request.workflow_id, request.workflow_version)
    if workflow.launch_policy != "agent_runtime":
        raise DeliverableWorkflowError(
            "deliverable_not_launchable",
            "This deliverable workflow is currently available for planning only",
        )
    if request.launch_message_id is not None and request.launch_message_id != message_id:
        raise DeliverableWorkflowError(
            "deliverable_already_launched",
            "Deliverable request has already been launched",
        )
    if request.status not in {"ready", "running"}:
        raise DeliverableWorkflowError(
            "deliverable_invalid_status",
            f"Deliverable request cannot be launched from status {request.status}",
        )
    # Prompt compilation performs exact-copy/font/layout validation. It must
    # complete before status or launch ownership is mutated so a malformed
    # persisted request cannot be left falsely marked as running.
    prompt = build_deliverable_prompt(request)
    if request.launch_message_id is None:
        preflight = await preflight_workflow(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            workflow=workflow,
            tier=request.tier,
            spec=request.spec,
            goal=request.goal,
            inputs=request.inputs,
        )
        execution = None
        if request.current_execution_id is not None:
            execution = await current_execution(db, request, lock=True)
            if execution is not None:
                record_execution_preflight(request, execution, preflight)
        if not preflight["launchable"]:
            reason = next(iter(preflight["reasons"]), "deliverable_capability_unavailable")
            raise DeliverableWorkflowError(
                "deliverable_preflight_failed",
                f"Deliverable capability check failed: {reason}",
            )
        request.launch_message_id = message_id
        request.status = "running"
        request.current_stage = "execution_queued"
        request.version += 1
    else:
        execution = (
            await current_execution(db, request, lock=True)
            if request.current_execution_id is not None
            else None
        )
    return PreparedDeliverableLaunch(
        request=request,
        prompt=prompt,
        execution=execution,
    )


def attach_deliverable_run(
    prepared: PreparedDeliverableLaunch,
    *,
    run_id: uuid.UUID,
    launched_at: datetime,
) -> None:
    request = prepared.request
    if request.agent_run_id is not None and request.agent_run_id != run_id:
        raise DeliverableWorkflowError(
            "deliverable_run_mismatch",
            "Deliverable request is already linked to another run",
        )
    request.agent_run_id = run_id
    request.launched_at = launched_at
    request.current_stage = "running"
    execution = getattr(prepared, "execution", None)
    if isinstance(execution, DeliverableExecution):
        if execution.intake_run_id is not None and execution.intake_run_id != run_id:
            raise DeliverableWorkflowError(
                "deliverable_execution_run_mismatch",
                "Deliverable execution is already linked to another run",
            )
        execution.intake_run_id = run_id
        execution.launch_message_id = request.launch_message_id
        execution.status = "running"
        execution.current_stage = "running"
        execution.launched_at = launched_at


async def sync_deliverable_lifecycle(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    lifecycle_status: str,
    lifecycle: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> DeliverableRequest | None:
    """Project one authoritative Runtime terminal state onto its deliverable."""

    result = await db.execute(
        select(DeliverableRequest)
        .where(
            DeliverableRequest.tenant_id == tenant_id,
            DeliverableRequest.agent_run_id == run_id,
        )
        .with_for_update()
    )
    request = result.scalar_one_or_none()
    normalized_lifecycle_status = str(lifecycle_status or "").strip().lower()

    async def finish(
        target: DeliverableRequest,
        reconciliation: Any | None = None,
    ) -> DeliverableRequest:
        if target.current_execution_id is None:
            return target
        artifacts = tuple(getattr(reconciliation, "artifacts", ()) or ())
        if artifacts:
            await bind_artifacts_to_current_execution(
                db,
                target,
                artifacts,
                now=now,
            )
        await project_execution_lifecycle(db, target, now=now)
        return target

    if not isinstance(request, DeliverableRequest):
        if normalized_lifecycle_status not in {"completed", "cancelled"}:
            return None
        run_result = await db.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.id == run_id,
            )
        )
        run = run_result.scalar_one_or_none()
        if (
            not isinstance(run, AgentRun)
            or run.session_id is None
            or run.agent_id is None
        ):
            return None
        candidates_result = await db.execute(
            select(DeliverableRequest)
            .where(
                DeliverableRequest.tenant_id == tenant_id,
                DeliverableRequest.session_id == run.session_id,
                DeliverableRequest.agent_id == run.agent_id,
                DeliverableRequest.work_type.in_(
                    ("poster", "presentation", "video"),
                ),
                DeliverableRequest.status.in_(
                    ("running", "waiting_approval", "failed", "cancelled"),
                ),
                DeliverableRequest.current_stage != "changes_requested",
            )
            .order_by(
                DeliverableRequest.created_at.desc(),
                DeliverableRequest.id.desc(),
            )
            .with_for_update()
        )
        for candidate in candidates_result.scalars().all():
            reconciliation = await reconcile_runtime_deliverable_artifacts(
                db,
                request=candidate,
                run_id=run_id,
            )
            attempted_types = tuple(
                getattr(reconciliation, "attempted_types", ()) or (),
            )
            if not attempted_types:
                continue
            if reconciliation.complete:
                created_types = tuple(
                    getattr(reconciliation, "created_types", ()) or (),
                )
                changed = bool(created_types) or (
                    candidate.status != "waiting_approval"
                    or candidate.current_stage != "output_review"
                    or candidate.last_error_code is not None
                    or candidate.completed_at is not None
                )
                if not changed:
                    return await finish(candidate, reconciliation)
                candidate.status = "waiting_approval"
                candidate.current_stage = "output_review"
                candidate.completed_at = None
                candidate.last_error_code = None
                candidate.version += 1
                return await finish(candidate, reconciliation)
            if normalized_lifecycle_status == "cancelled":
                continue
            failure_codes = tuple(
                getattr(reconciliation, "failure_codes", ()) or (),
            )
            if failure_codes:
                next_error_code = str(failure_codes[0][1])[:100]
            elif reconciliation.unavailable_types:
                next_error_code = "deliverable_artifact_verification_unavailable"
            elif reconciliation.invalid_types:
                next_error_code = "deliverable_artifact_invalid"
            else:
                next_error_code = "deliverable_artifact_missing"
            changed = (
                candidate.status != "failed"
                or candidate.current_stage != "artifact_verification_failed"
                or candidate.last_error_code != next_error_code
            )
            if not changed:
                return await finish(candidate, reconciliation)
            candidate.status = "failed"
            candidate.current_stage = "artifact_verification_failed"
            candidate.completed_at = now or datetime.now(UTC)
            candidate.last_error_code = next_error_code
            candidate.version += 1
            return await finish(candidate, reconciliation)
        return None
    # A provider write may settle after the user has cancelled a stalled model
    # turn. Re-run artifact reconciliation for cancelled creative requests so
    # a valid, paid output is not hidden from review.
    if request.status == "succeeded":
        return await finish(request)
    if request.status == "failed" and request.current_stage == "changes_requested":
        return await finish(request)
    if normalized_lifecycle_status in {"completed", "cancelled"} and request.work_type in {
        "poster",
        "presentation",
        "video",
    }:
        reconciliation = await reconcile_runtime_deliverable_artifacts(
            db,
            request=request,
            run_id=run_id,
        )
        if reconciliation.complete:
            next_status, next_stage, next_error_code = (
                "waiting_approval",
                "output_review",
                None,
            )
        elif normalized_lifecycle_status == "cancelled":
            next_status = next_stage = next_error_code = None
        elif getattr(reconciliation, "failure_codes", ()):
            next_status, next_stage, next_error_code = (
                "failed",
                "artifact_verification_failed",
                str(reconciliation.failure_codes[0][1])[:100],
            )
        elif reconciliation.unavailable_types:
            next_status, next_stage, next_error_code = (
                "failed",
                "artifact_verification_failed",
                "deliverable_artifact_verification_unavailable",
            )
        elif reconciliation.invalid_types:
            next_status, next_stage, next_error_code = (
                "failed",
                "artifact_verification_failed",
                "deliverable_artifact_invalid",
            )
        else:
            next_status, next_stage, next_error_code = (
                "failed",
                "artifact_verification_failed",
                "deliverable_artifact_missing",
            )
        if next_status is not None and next_stage is not None:
            if (
                request.status == next_status
                and request.current_stage == next_stage
                and request.last_error_code == next_error_code
            ):
                return await finish(request, reconciliation)
            request.status = next_status
            request.current_stage = next_stage
            request.completed_at = now or datetime.now(UTC) if next_status == "failed" else None
            request.last_error_code = next_error_code
            request.version += 1
            return await finish(request, reconciliation)
    terminal_mapping = {
        # A completed Runtime only proves that the agent stopped normally. The
        # deliverable still needs an artifact revision and evaluator evidence.
        "completed": ("waiting_approval", "output_review"),
        "failed": ("failed", "failed"),
        "cancelled": ("cancelled", "cancelled"),
    }
    transition = terminal_mapping.get(normalized_lifecycle_status)
    if transition is None:
        return await finish(request)
    next_status, next_stage = transition
    if next_status == "failed":
        error = lifecycle.get("error") if isinstance(lifecycle, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        next_error_code = str(code)[:100] if code else "runtime_failed"
    else:
        next_error_code = None
    if (
        request.status == next_status
        and request.current_stage == next_stage
        and request.last_error_code == next_error_code
    ):
        return await finish(request)
    request.status = next_status
    request.current_stage = next_stage
    request.completed_at = (
        now or datetime.now(UTC)
        if next_status in {"failed", "cancelled"}
        else None
    )
    request.last_error_code = next_error_code
    request.version += 1
    return await finish(request)
