"""Versioned product workflow manifests and safe deliverable launch helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import posixpath
from datetime import UTC, datetime
from typing import Any, Literal, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent_run import AgentRun
from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.models.tool import AgentTool, Tool
from app.services.creative_briefs import (
    POSTER_V2_WORKFLOW_ID,
    PRESENTATION_V2_WORKFLOW_ID,
    VIDEO_V2_WORKFLOW_ID,
    brief_projection,
    candidate_count_for_policy,
    compile_creative_brief,
    compile_presentation_brief,
    compile_video_brief,
    poster_v2_rollout_allowed,
    presentation_brief_projection,
    upsert_request_creative_brief,
    video_brief_projection,
)
from app.services.deliverable_artifacts import reconcile_runtime_deliverable_artifacts
from app.services.deliverable_executions import (
    bind_artifacts_to_current_execution,
    current_execution,
    execution_units,
    project_execution_lifecycle,
    record_execution_preflight,
)
from app.services.entitlements import get_tenant_entitlements
from app.services.media_capabilities import (
    get_agent_media_capabilities,
    video_providers_with_native_audio,
)
from app.services.media_provider_routing import (
    media_provider_order_for_image_strategy,
    media_provider_order_for_modality,
)
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.model_router import resolve_route
from app.services.provider_pricing import minimax_image_credits, minimax_video_credits
from app.services.quota_guard import QuotaExceeded
from app.services.presentation_visual_policy import (
    MINIMUM_PICTURE_COVERAGE_RATIO,
    deck_quality_policy,
    presentation_brief_is_image_led,
)
from app.services.presentation_pipeline import (
    advance_presentation_v2_after_run,
    load_latest_outline,
    load_presentation_v2_inventory_projection,
    outline_approved,
)
from app.services.storyboard import (
    advance_video_v2_after_run,
    load_latest_storyboard,
    storyboard_approved,
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
    kind: Literal["text", "textarea", "number", "select", "json"]
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
        output_contract=["pptx"],
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
    WorkflowManifest(
        workflow_id="builtin.poster.v2",
        workflow_version="2.0.0",
        work_type="poster",
        label_zh="海报 / 图片（多候选）",
        label_en="Poster / Image (multi-candidate)",
        description_zh="结构化工作说明驱动，按档位生成多个候选并通过自动 QA。",
        description_en="Structured-brief driven, tier-bound candidates with automated QA.",
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
                key="audience", label_zh="目标受众", label_en="Audience", kind="text",
                placeholder_zh="例如：25–35 岁都市白领", placeholder_en="e.g. urban professionals aged 25–35",
            ),
            WorkflowField(
                key="style", label_zh="视觉风格", label_en="Visual style", kind="text", required=True,
                default="commercial", placeholder_zh="例如：高端商业广告", placeholder_en="e.g. premium commercial campaign",
            ),
            WorkflowField(
                key="exact_copy", label_zh="精确文案", label_en="Exact copy", kind="textarea",
                placeholder_zh="需要逐字保留的中英文文案", placeholder_en="Copy that must be preserved exactly",
            ),
            WorkflowField(
                key="exact_copy_blocks", label_zh="结构化精确文案", label_en="Structured exact copy", kind="json",
                placeholder_zh='[{"role":"title","text":"…"}]', placeholder_en='[{"role":"title","text":"…"}]',
            ),
            WorkflowField(
                key="brand_assets", label_zh="品牌资产", label_en="Brand assets", kind="json",
                placeholder_zh='[{"path":"workspace/…/logo.png","position":"top"}]',
                placeholder_en='[{"path":"workspace/…/logo.png","position":"top"}]',
            ),
            WorkflowField(
                key="reference_assets", label_zh="参考素材", label_en="Reference assets", kind="json",
                placeholder_zh='[{"path":"workspace/…/product.png","kind":"exact_asset"}]',
                placeholder_en='[{"path":"workspace/…/product.png","kind":"exact_asset"}]',
            ),
            WorkflowField(
                key="prohibitions", label_zh="禁止项", label_en="Prohibitions", kind="textarea",
                placeholder_zh="每行一条禁止出现的元素", placeholder_en="One prohibited element per line",
            ),
            WorkflowField(
                key="redraw_scope", label_zh="允许重绘范围", label_en="Redraw scope", kind="select",
                default="full_creative", options=["background_only", "style_adaptation", "full_creative"],
            ),
            WorkflowField(
                key="candidate_count", label_zh="候选数量", label_en="Candidates", kind="number",
                minimum=1, maximum=4,
                placeholder_zh="只能向下调整档位默认候选数", placeholder_en="May only tune the tier default down",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["composition", "final"],
        output_contract=["png"],
        required_capability="image",
        launch_policy="agent_runtime",
    ),
    WorkflowManifest(
        workflow_id="builtin.video.v2",
        workflow_version="2.0.0",
        work_type="video",
        label_zh="短视频（分镜审批）",
        label_en="Short video (storyboard-gated)",
        description_zh="结构化工作说明驱动，分镜批准前零付费，逐镜头独立生成与 QA。",
        description_en="Structured-brief driven, storyboard approval before any paid work, per-shot generation and QA.",
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
                key="duration", label_zh="总时长（秒）", label_en="Total duration (seconds)", kind="select", required=True,
                default="10", options=["6", "10"],
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
                key="style", label_zh="视觉风格", label_en="Visual style", kind="text",
                default="commercial", placeholder_zh="例如：高端商业广告",
                placeholder_en="e.g. premium commercial campaign",
            ),
            WorkflowField(
                key="audio_mode", label_zh="声音模式", label_en="Audio mode", kind="select", required=True,
                default="voiceover", options=["in_scene_dialogue", "voiceover", "silent"],
            ),
            WorkflowField(
                key="story", label_zh="故事与镜头要求", label_en="Story and shots", kind="textarea", required=True,
                placeholder_zh="产品、场景、镜头运动、字幕和声音要求", placeholder_en="Product, scene, camera, captions, and audio",
            ),
            WorkflowField(
                key="shot_count", label_zh="镜头数量", label_en="Shots", kind="number",
                minimum=1, maximum=4,
                placeholder_zh="逐镜头独立生成，费用按镜头分解", placeholder_en="Per-shot generation with per-shot pricing",
            ),
            WorkflowField(
                key="cta", label_zh="行动号召", label_en="Call to action", kind="text",
                placeholder_zh="例如：立即了解更多", placeholder_en="e.g. Learn more today",
            ),
            WorkflowField(
                key="caption_spec", label_zh="字幕要求", label_en="Caption spec", kind="textarea",
                placeholder_zh="逐镜头字幕或整体字幕规范", placeholder_en="Per-shot captions or an overall caption spec",
            ),
            WorkflowField(
                key="dialogue_script", label_zh="对白脚本", label_en="Dialogue script", kind="textarea",
                placeholder_zh="镜头内同步对白模式必填", placeholder_en="Required for in-scene dialogue",
            ),
            WorkflowField(
                key="prohibitions", label_zh="禁止项", label_en="Prohibitions", kind="textarea",
                placeholder_zh="每行一条禁止出现的元素", placeholder_en="One prohibited element per line",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
        required_capability="video",
        launch_policy="agent_runtime",
    ),
    WorkflowManifest(
        workflow_id="builtin.presentation.v2",
        workflow_version="2.0.0",
        work_type="presentation",
        label_zh="PPT 演示文稿（大纲审批）",
        label_en="Presentation (outline-gated)",
        description_zh="结构化工作说明 + 来源清单驱动，大纲批准前零付费，事实断言必须可溯源。",
        description_en="Structured brief and source inventory driven, zero spend before outline approval, traceable fact assertions.",
        fields=[
            WorkflowField(
                key="audience", label_zh="目标受众", label_en="Audience", kind="text", required=True,
                placeholder_zh="例如：潜在投资人", placeholder_en="e.g. prospective investors",
            ),
            WorkflowField(
                key="scenario", label_zh="演示场景", label_en="Scenario", kind="select", required=True,
                options=["client_proposal", "investor_pitch", "internal_review", "training", "launch_review"],
                placeholder_zh="选择演示的实际使用场景", placeholder_en="Choose the real presentation scenario",
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
                key="key_points", label_zh="必须覆盖", label_en="Required points", kind="textarea", required=True,
                placeholder_zh="逐条写出必须出现的数据、观点或文案；这些会登记为可引用来源",
                placeholder_en="List required data, claims, or copy; these register as citable sources",
            ),
            WorkflowField(
                key="brand_theme", label_zh="品牌主题", label_en="Brand theme", kind="text",
                placeholder_zh="品牌色/字体/语气要求", placeholder_en="Brand colors, fonts, tone",
            ),
            WorkflowField(
                key="source_urls", label_zh="来源链接", label_en="Source URLs", kind="json",
                placeholder_zh='[{"url":"https://…","facts":["可引用事实"]}]',
                placeholder_en='[{"url":"https://…","facts":["citable fact"]}]',
            ),
            WorkflowField(
                key="editability_contract", label_zh="可编辑性合同", label_en="Editability contract", kind="select",
                default="editable", options=["editable", "hybrid", "visual_fidelity"],
                placeholder_zh="默认全文字/图表可编辑", placeholder_en="Fully editable text/charts by default",
            ),
            _fallback_policy_field(),
        ],
        approval_policy=["outline", "final"],
        output_contract=["pptx"],
        required_capability="presentation",
        launch_policy="agent_runtime",
    ),
)

# The first manifest registered for a work type stays the default for that
# type; later (v2+) manifests are only reachable by explicit workflow_id.
WORKFLOW_BY_TYPE: dict[str, WorkflowManifest] = {}
for _workflow in _WORKFLOWS:
    WORKFLOW_BY_TYPE.setdefault(_workflow.work_type, _workflow)
del _workflow
WORKFLOW_BY_ID = {workflow.workflow_id: workflow for workflow in _WORKFLOWS}


class DeliverableWorkflowError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def list_workflow_manifests() -> list[WorkflowManifest]:
    return list(_WORKFLOWS)


def poster_v2_workflow_allowed(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    settings = get_settings()
    return poster_v2_rollout_allowed(
        tenant_id=tenant_id,
        agent_id=agent_id,
        enabled=settings.DELIVERABLE_POSTER_V2_ENABLED,
        tenant_ids=settings.DELIVERABLE_POSTER_V2_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_POSTER_V2_AGENT_IDS,
    )


def video_v2_workflow_allowed(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    settings = get_settings()
    return poster_v2_rollout_allowed(
        tenant_id=tenant_id,
        agent_id=agent_id,
        enabled=settings.DELIVERABLE_VIDEO_V2_ENABLED,
        tenant_ids=settings.DELIVERABLE_VIDEO_V2_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_VIDEO_V2_AGENT_IDS,
    )


def presentation_v2_workflow_allowed(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    settings = get_settings()
    return poster_v2_rollout_allowed(
        tenant_id=tenant_id,
        agent_id=agent_id,
        enabled=settings.DELIVERABLE_PRESENTATION_V2_ENABLED,
        tenant_ids=settings.DELIVERABLE_PRESENTATION_V2_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_PRESENTATION_V2_AGENT_IDS,
    )


def deliverable_stage_approvals_enabled() -> bool:
    """Return the server-owned gate required by staged video/deck workflows."""

    return bool(get_settings().DELIVERABLE_STAGE_APPROVALS_ENABLED)


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
    poster_v2_allowed = poster_v2_workflow_allowed(tenant_id, agent_id)
    stage_approvals_enabled = deliverable_stage_approvals_enabled()
    # Video/PPT v2 are only usable when both the canary allowlist and their
    # approval state machine are enabled.  Keeping v1 visible under a partial
    # rollout prevents users from entering a workflow that cannot pass its
    # mandatory storyboard/outline gate.
    video_v2_allowed = (
        video_v2_workflow_allowed(tenant_id, agent_id)
        and stage_approvals_enabled
    )
    presentation_v2_allowed = (
        presentation_v2_workflow_allowed(tenant_id, agent_id)
        and stage_approvals_enabled
    )
    for workflow in _WORKFLOWS:
        # The v2 poster pipeline replaces the v1 manifest for allowlisted
        # tenants/Agents only; by default the v1 contract is the only poster
        # workflow ever listed.
        if workflow.workflow_id == POSTER_V2_WORKFLOW_ID and not poster_v2_allowed:
            continue
        if workflow.workflow_id == "builtin.poster.v1" and poster_v2_allowed:
            continue
        # Same canary discipline for the v2 storyboard-gated video pipeline.
        if workflow.workflow_id == VIDEO_V2_WORKFLOW_ID and not video_v2_allowed:
            continue
        if workflow.workflow_id == "builtin.video.v1" and video_v2_allowed:
            continue
        # Same canary discipline for the v2 outline-gated presentation pipeline.
        if workflow.workflow_id == PRESENTATION_V2_WORKFLOW_ID and not presentation_v2_allowed:
            continue
        if workflow.workflow_id == "builtin.presentation.v1" and presentation_v2_allowed:
            continue
        if workflow.launch_policy != "agent_runtime":
            continue
        if (
            workflow.required_capability == "presentation"
            and not await _presentation_tool_available(
                db,
                agent_id,
                workflow.output_contract,
            )
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
    normalized_type = str(work_type or "").strip().lower()
    workflow = WORKFLOW_BY_TYPE.get(normalized_type)
    if workflow is None:
        raise DeliverableWorkflowError("unsupported_work_type", "Unsupported deliverable type")
    if workflow_id is not None:
        workflow = WORKFLOW_BY_ID.get(workflow_id)
        if workflow is None or workflow.work_type != normalized_type:
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
        elif field.kind == "json":
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise DeliverableWorkflowError(
                        "invalid_spec_field", f"{key} must be valid JSON"
                    ) from exc
            if isinstance(value, str) or not isinstance(value, (list, dict)):
                raise DeliverableWorkflowError(
                    "invalid_spec_field", f"{key} must be a JSON array or object"
                )
            try:
                json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise DeliverableWorkflowError(
                    "invalid_spec_field", f"{key} must be JSON serializable"
                ) from exc
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
    policy: dict[str, float | int | str] = {
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
    if request.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        # FR-P4: v2 extends adaptive-v1 with structural font/density/contrast
        # parameters.  Legacy slide_specs without visual_plan_version keep the
        # v1 validation path; only v2 decks carry and honor these keys.
        policy.update(deck_quality_policy())
    return policy


def build_deliverable_prompt(
    request: DeliverableRequest,
    *,
    video_v2_stage: str | None = None,
    video_v2_storyboard: Any | None = None,
    video_v2_shot_clips: Sequence[Mapping[str, Any]] = (),
    presentation_v2_stage: str | None = None,
    presentation_v2_source_inventory: Sequence[Mapping[str, Any]] = (),
    presentation_v2_target_units: Sequence[str] = (),
    presentation_v2_revision_instruction: str | None = None,
) -> str:
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
    if request.work_type == "poster" and request.workflow_id == POSTER_V2_WORKFLOW_ID:
        candidate_count = candidate_count_for_policy(request.tier, request.spec)
        candidate_instructions: list[str] = []
        for index in range(1, candidate_count + 1):
            unit_key = f"candidate-{index:02d}"
            prompt_path = f"workspace/deliverables/{request.id}/prompts/{unit_key}.txt"
            candidate_instructions.append(
                f"For {unit_key}: read the server-compiled provider prompt from '{prompt_path}' with "
                "read_file, then call generate_image_minimax exactly once with that file's content passed "
                "verbatim as the prompt argument (never write, rewrite, extend, translate, or summarize it), "
                f"save_path='workspace/deliverables/{request.id}/candidates/{unit_key}.png', "
                "aspect_ratio=spec.aspect_ratio, execution_strategy='commercial_quality', "
                f"allow_degraded_fallback={allow_degraded_literal}, and overlay_blocks set to "
                "DELIVERABLE_REQUEST.exact_copy_blocks unchanged. The server rejects a rewritten prompt or a "
                "different save_path before any paid provider request."
            )
        return (
            "You are executing a persisted Astra Deliverable Request under the v2 multi-candidate poster "
            "pipeline. Treat the following JSON as the authoritative product brief. Do not choose or reveal "
            "a provider/model. Use only enabled tools and keep every artifact under workspace/deliverables/"
            f"{request.id}/. The server has compiled one provider prompt per candidate; your job is managed "
            "orchestration only, never prompt authorship. Execute these candidate steps in order, one managed "
            "Tool call each: "
            + " ".join(candidate_instructions)
            + " Every candidate call is independently durable and owns its own provider fallback and "
            "recovery, so call the Tool exactly once per candidate and never retry manually. The managed "
            "provider creates only the text-free visual background; Astra's server composes the exact copy "
            "blocks with installed real fonts on every candidate. Never overlay the same copy again yourself. "
            "Create only formats explicitly listed in output_contract. If no provider accepts a candidate, "
            "record the failure for that candidate and continue with the remaining candidates; if every "
            "candidate fails, stop without claiming delivery. Do not read the binary images. The final "
            "response must report every candidate's exact versioned PNG workspace path, and never claim "
            "success until the registered artifact contract confirms it.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )
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
            "the server will reject any mismatch before a paid provider request. The managed provider creates "
            "only the text-free visual background; after it returns, Astra's server composes those exact blocks "
            "with installed real fonts and freezes the final dimensions from the active tier plus aspect_ratio. "
            "This server post-processing remains part of the same Tool call and does not submit a second provider "
            "generation. Do not look for a delivery_size argument and never refuse the request because that field "
            "is absent. The returned PNG has a poster-v3 receipt with deterministic copy, bounds, and contrast "
            "checks, so never ask the image model to spell exact copy. The "
            "returned "
            "PNG is already the final deterministic composition, so never overlay the same copy again in HTML, "
            "PDF, PPTX, or another image. Create only formats explicitly listed in output_contract. Call the generation "
            "Tool exactly once because it owns provider fallback and durable recovery. If no provider accepts "
            "the request, stop without retrying or claiming delivery. Do not read the binary image. The final "
            "response must report the exact versioned PNG workspace path returned by the Tool, and never claim "
            "success until the registered artifact contract confirms it.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )
    if request.work_type == "video" and request.workflow_id == VIDEO_V2_WORKFLOW_ID:
        return _video_v2_prompt(
            request,
            contract=contract,
            stage=video_v2_stage or "storyboard_draft",
            storyboard=video_v2_storyboard,
            shot_clips=video_v2_shot_clips,
            allow_degraded_literal=allow_degraded_literal,
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
    if request.work_type == "presentation" and request.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        return _presentation_v2_prompt(
            request,
            contract=contract,
            stage=presentation_v2_stage or "outline_draft",
            source_inventory=presentation_v2_source_inventory,
            allow_degraded_literal=allow_degraded_literal,
            target_units=presentation_v2_target_units,
            revision_instruction=presentation_v2_revision_instruction,
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
    presentation_outputs = {
        str(value or "").strip().lower() for value in request.output_contract
    }
    if "pdf" in presentation_outputs:
        presentation_delivery_instructions = (
            "Convert the same source to PDF using convert_html_to_pdf with design_width=1280, "
            "design_height=720, pdf_mode='pages', scale=1, paper_width=13.333333, paper_height=7.5, "
            "expected_page_count=spec.page_count, outline_path='workspace/deliverables/"
            f"{request.id}/outline.json', and slide_spec_path='workspace/deliverables/"
            f"{request.id}/slide_spec.json'. "
            "The PPTX and PDF must each contain exactly spec.page_count 16:9 pages. Report both exact "
            "workspace paths and do not claim visual consistency, no-overflow, or page-count success "
            "unless the registered artifact contract confirms it."
        )
    else:
        presentation_delivery_instructions = (
            "The customer output contract requires PPTX only. Do not call convert_html_to_pdf, do not "
            "register or report a customer-facing PDF, and report only the exact PPTX workspace path. "
            "A PDF render may be created separately by an internal QA workflow, but it is not a customer "
            "deliverable unless output_contract explicitly lists pdf. The PPTX must contain exactly "
            "spec.page_count 16:9 slides; do not claim no-overflow, page-count success, or editability "
            "unless the registered artifact contract confirms it."
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
        f"{presentation_delivery_instructions}\n"
        f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
    )


def _presentation_v2_prompt(
    request: DeliverableRequest,
    *,
    contract: Mapping[str, Any],
    stage: str,
    source_inventory: Sequence[Mapping[str, Any]],
    allow_degraded_literal: str,
    target_units: Sequence[str] = (),
    revision_instruction: str | None = None,
) -> str:
    """Server-owned v2 presentation stage prompts; the Agent never invents facts."""

    from app.services.presentation_pipeline import EDITABILITY_RENDER_MODES

    spec = request.spec if isinstance(request.spec, Mapping) else {}
    page_count = int(spec.get("page_count") or 8)
    editability = str(spec.get("editability_contract") or "editable").strip().lower()
    render_mode = EDITABILITY_RENDER_MODES.get(editability, "hybrid_editable")
    outline_path = f"workspace/deliverables/{request.id}/outline.json"
    slide_spec_path = f"workspace/deliverables/{request.id}/slide_spec.json"
    inventory_payload = {
        "schema_version": "source-inventory-v1",
        "entries": list(source_inventory),
    }

    if stage == "outline_draft":
        return (
            "You are executing a persisted Astra Deliverable Request under the v2 outline-gated "
            "presentation pipeline. Treat the following JSON as the authoritative product brief. Do not "
            "choose or reveal a provider/model. In this run you only plan the deck: write exactly two "
            "files and call no generation or conversion Tool — no image, PPTX, or PDF calls. Paid work "
            "starts only after the customer approves the outline. "
            f"First write '{outline_path}' with deck_title, audience, core_message, one_sentence_claim, "
            "a storyline array of ordered narrative beats, and exactly "
            f"{page_count} slides. Every outline slide must contain slide_id (slide-01..slide-"
            f"{page_count:02d} in order), purpose, headline, evidence, and visual_intent; evidence may be "
            "one non-empty sentence or an array of evidence sentences. "
            f"Then write '{slide_spec_path}' with the same ordered slide_ids and a headline, layout, "
            "body_points, visual_asset, source_refs, slide_type, visual_kind, data_slide, and asset_ref "
            "for every slide. The top-level slide_spec must include visual_plan_version='adaptive-v1' and "
            "visual_policy exactly matching PRESENTATION_VISUAL_POLICY. visual_kind must be one of "
            "generated_image, supplied_image, editable_chart, editable_diagram, editable_table, or "
            "editable_typography. Image slides declare asset_ref and must leave data_slide=false; "
            "editable compositions use an empty asset_ref. Any slide carrying charts, tables, process "
            "flows, or key numbers must set data_slide=true and use an editable_* visual_kind. "
            "SOURCE_INVENTORY below is the authoritative evidence registry. Every source_refs entry must "
            "cite a registered source_id, workspace path, URL, or SHA-256 from SOURCE_INVENTORY; never "
            "invent a reference and never cite an internal workflow id. Every quantified or ranking claim "
            "(numbers, percentages, durations, rankings, superlatives) in headline or body_points must be "
            "traceable to facts registered in the cited source; if a claim has no registered evidence, "
            "rewrite it as an explicitly labelled assumption (prefix it with 假设 or 'assumption:') or "
            "remove it. Fabricating a fact is a hard delivery failure. "
            "Do not invent a brand, organization, team, author, or internal-use label that is absent from "
            "the brief. After writing both files, end your turn and report the two paths.\n"
            f"SOURCE_INVENTORY={json.dumps(inventory_payload, ensure_ascii=False, sort_keys=True)} "
            f"PRESENTATION_VISUAL_POLICY="
            f"{json.dumps(_presentation_visual_policy(request, _presentation_media_roles(request)), ensure_ascii=False, sort_keys=True)} "
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    if stage == "slide_revision":
        # FR-P6: a page-targeted revision re-renders only the named slides and
        # reassembles the deck; every other page carries forward unchanged.
        targets = [item for item in target_units if re.fullmatch(r"slide-\d{2}", item)]
        if not targets or not str(revision_instruction or "").strip():
            raise DeliverableWorkflowError(
                "deliverable_revision_target_invalid",
                "A page-targeted presentation revision requires slide targets and an instruction",
            )
        targets_listing = ", ".join(targets)
        presentation_outputs = {
            str(value or "").strip().lower() for value in request.output_contract
        }
        revision_pdf_instructions = ""
        if "pdf" in presentation_outputs:
            revision_pdf_instructions = (
                " Then convert the same source to PDF using convert_html_to_pdf with "
                "design_width=1280, design_height=720, pdf_mode='pages', scale=1, "
                "paper_width=13.333333, paper_height=7.5, "
                f"expected_page_count={page_count}, outline_path='{outline_path}', and "
                f"slide_spec_path='{slide_spec_path}'."
            )
        return (
            "You are executing a page-targeted revision of an approved deck for a persisted Astra "
            "Deliverable Request under the v2 outline-gated presentation pipeline. Do not choose or "
            "reveal a provider/model. The customer approved the outline and reviewed the produced "
            "deck; only these slides are in scope for this revision: "
            f"{targets_listing}. The customer's revision instruction is: "
            f"{str(revision_instruction or '').strip()}. "
            f"Read the approved '{outline_path}', '{slide_spec_path}', and the current "
            f"workspace/deliverables/{request.id}/presentation.html back first. Apply the "
            "instruction only to the in-scope slides; every other slide's text, data, layout, and "
            "assets must carry forward unchanged — never reflow, restyle, or rewrite an out-of-scope "
            "slide, and never add a new unsourced number or fact anywhere. Reuse every existing deck "
            "asset as-is; call generate_image_minimax only when the instruction explicitly changes an "
            "in-scope image slide's visual, at most once per such role with "
            f"save_path='workspace/deliverables/{request.id}/assets/<role>.png' and "
            f"allow_degraded_fallback={allow_degraded_literal}. Keep the font, density, contrast, and "
            "editability contracts exactly as in the approved deck: data slides stay editable "
            "shapes/charts/tables. Write the updated presentation.html back, then convert with "
            f"convert_html_to_pptx using design_width=1280, design_height=720, "
            f"render_mode='{render_mode}', render_scale=2, expected_page_count={page_count}, "
            f"outline_path='{outline_path}', and slide_spec_path='{slide_spec_path}'. The render_mode "
            f"'{render_mode}' comes from the approved editability contract; never substitute another "
            f"mode.{revision_pdf_instructions} Create only formats explicitly listed in "
            "output_contract. The final response must report the exact versioned workspace path of "
            "every regenerated contract file.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    if stage == "slide_render":
        (
            presentation_media_roles,
            presentation_supplied_assets,
            presentation_generation_roles,
        ) = _presentation_media_plan(request)
        presentation_visual_policy = _presentation_visual_policy(
            request,
            presentation_media_roles,
        )
        media_instructions = ""
        if presentation_media_roles:
            role_instructions: list[str] = []
            for role in presentation_generation_roles:
                role_instructions.append(
                    f"For {role}, call generate_image_minimax exactly once with a polished 16:9 "
                    "commercial prompt, no generated words/logos/watermarks/numbers, and "
                    f"save_path='workspace/deliverables/{request.id}/assets/{role}.png', and "
                    f"allow_degraded_fallback={allow_degraded_literal}."
                )
            supplied_instructions = ""
            if presentation_supplied_assets:
                supplied_instructions = (
                    "PRESENTATION_MEDIA_CONTRACT.supplied_assets is authoritative. Use every mapped "
                    "workspace image without regenerating, redrawing, moving, or replacing it. "
                )
            generation_instructions = (
                " ".join(role_instructions)
                if presentation_generation_roles
                else (
                    "All required media roles are satisfied by supplied assets. Do not call "
                    "generate_image_minimax for this presentation."
                )
            )
            media_instructions = (
                "The approved outline requires real imagery. Complete every required asset role before "
                "writing presentation.html. "
                + supplied_instructions
                + generation_instructions
                + " Generated images are decorative only: they must never carry facts, numbers, or "
                "claims, and image slides must not state fact assertions in their text. "
                "Call each generation Tool at most once per role and never manually retry. If any "
                "required image Tool call fails or is unavailable, stop without converting; report the "
                "missing asset role. "
            )
        presentation_outputs = {
            str(value or "").strip().lower() for value in request.output_contract
        }
        pdf_instructions = ""
        if "pdf" in presentation_outputs:
            pdf_instructions = (
                " Then convert the same source to PDF using convert_html_to_pdf with "
                "design_width=1280, design_height=720, pdf_mode='pages', scale=1, "
                "paper_width=13.333333, paper_height=7.5, "
                f"expected_page_count={page_count}, outline_path='{outline_path}', and "
                f"slide_spec_path='{slide_spec_path}'."
            )
        return (
            "You are producing an approved deck for a persisted Astra Deliverable Request under the v2 "
            "outline-gated presentation pipeline. The customer has approved the outline; do not rewrite "
            "it. Do not choose or reveal a provider/model. "
            + media_instructions
            + f"Read the approved '{outline_path}' and '{slide_spec_path}' back, then write "
            f"workspace/deliverables/{request.id}/presentation.html implementing exactly the approved "
            f"{page_count} slides at 1280x720 each, keeping every slide title, data_slide flag, and "
            "source-backed claim consistent with the approved slide_spec. Keep every quantified or "
            "ranking statement exactly as sourced in the approved slide_spec; never add a new unsourced "
            "number. Use CSS/HTML charts, tables, timelines, comparison matrices, and process lanes for "
            "every data slide so all data stays editable; only decorative pages may be image-led. "
            "Every visible text node must stay inside its slide with at least 24px bottom safety, use at "
            "least 16px computed font size for body/title/table/caption text, keep strong text/background "
            "contrast, and respect the density band in PRESENTATION_VISUAL_POLICY — neither sparse "
            "near-empty content slides nor overstuffed ones. "
            f"Then convert with convert_html_to_pptx using design_width=1280, design_height=720, "
            f"render_mode='{render_mode}', render_scale=2, expected_page_count={page_count}, "
            f"outline_path='{outline_path}', and slide_spec_path='{slide_spec_path}'. "
            f"The render_mode '{render_mode}' comes from the approved editability contract "
            f"('{editability}'); never substitute another mode."
            + pdf_instructions
            + " Create only formats explicitly listed in output_contract. The final response must report "
            "the exact versioned workspace path of every contract file, and never claim success until "
            "the registered artifact contract confirms it.\n"
            f"PRESENTATION_VISUAL_POLICY="
            f"{json.dumps(presentation_visual_policy, ensure_ascii=False, sort_keys=True)} "
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    raise DeliverableWorkflowError(
        "deliverable_continuation_not_ready",
        f"Unknown v2 presentation stage: {stage}",
    )


def _video_v2_prompt(
    request: DeliverableRequest,
    *,
    contract: Mapping[str, Any],
    stage: str,
    storyboard: Any | None,
    shot_clips: Sequence[Mapping[str, Any]],
    allow_degraded_literal: str,
) -> str:
    """Server-owned v2 video stage prompts; the Agent never authors prompts."""

    spec = request.spec if isinstance(request.spec, Mapping) else {}
    duration = int(spec.get("duration") or 10)
    shot_count = spec.get("shot_count")
    if not isinstance(shot_count, int) or isinstance(shot_count, bool):
        shot_count = max(1, min(math.ceil(duration / 4), 12))
    shot_count = max(1, min(shot_count, 12))
    audio_mode = str(spec.get("audio_mode") or "voiceover").strip()
    storyboard_path = f"workspace/deliverables/{request.id}/storyboard.json"

    if stage == "storyboard_draft":
        audio_rules = {
            "voiceover": (
                "Every shot's dialogue field must be empty, and the top-level "
                "voiceover_script must contain the full narration to synthesize later."
            ),
            "silent": (
                "Every shot's dialogue field must be empty and voiceover_script must be empty."
            ),
            "in_scene_dialogue": (
                "Speaking shots must carry their exact spoken lines in the dialogue field, and "
                "voiceover_script must be empty. The dialogue must come from the approved "
                "dialogue_script in the brief."
            ),
        }[audio_mode if audio_mode in {"voiceover", "silent", "in_scene_dialogue"} else "voiceover"]
        return (
            "You are executing a persisted Astra Deliverable Request under the v2 storyboard-gated "
            "video pipeline. Treat the following JSON as the authoritative product brief. Do not "
            "choose or reveal a provider/model. In this run you only draft the storyboard: write "
            f"exactly one file '{storyboard_path}' containing a JSON object with a 'shots' array "
            f"of exactly {shot_count} entries and a top-level 'voiceover_script' string. Each shot "
            "is an object with keys shot_id, duration_seconds, visual, camera, subject_refs, "
            "first_frame_ref, last_frame_ref, dialogue, caption, transition. Hard contract: shot_id "
            f"values must be exactly shot-01..shot-{shot_count:02d} in order; duration_seconds are "
            f"positive integers summing to exactly {duration}; no shot may exceed 15 seconds; every "
            "shot must describe real on-camera action consistent with the brief story, audience, and "
            f"channel. Audio mode is '{audio_mode}': {audio_rules} Do not call any generation Tool "
            "in this run — no image, video, speech, music, or compose calls. Paid generation starts "
            "only after the customer approves this storyboard. After writing the file, end your turn "
            "and report the storyboard path.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    if stage == "shot_generation":
        shots = tuple(storyboard.shots) if storyboard is not None else ()
        require_audio = "true" if audio_mode == "in_scene_dialogue" else "false"
        per_shot_instructions: list[str] = []
        for shot in shots:
            unit_key = shot.shot_id
            per_shot_instructions.append(
                f"For {unit_key} ({shot.duration_seconds}s): first call generate_image_minimax "
                "exactly once with the prompt read verbatim from "
                f"'workspace/deliverables/{request.id}/prompts/keyframe-{unit_key}.txt' via read_file "
                f"and save_path='workspace/deliverables/{request.id}/keyframes/{unit_key}.png', "
                f"aspect_ratio=spec.aspect_ratio, execution_strategy='commercial_quality', "
                f"allow_degraded_fallback={allow_degraded_literal}. Then call generate_video_minimax "
                "exactly once with the prompt read verbatim from "
                f"'workspace/deliverables/{request.id}/prompts/{unit_key}.txt', "
                f"save_path='workspace/deliverables/{request.id}/shots/{unit_key}.mp4', "
                f"aspect_ratio=spec.aspect_ratio, duration={shot.duration_seconds}, "
                f"first_frame_image=<the exact versioned workspace path returned by that shot's "
                f"keyframe call>, require_audio={require_audio}, wait_for_completion=false, and "
                f"allow_degraded_fallback={allow_degraded_literal}."
            )
        return (
            "You are executing the approved storyboard of a persisted Astra Deliverable Request "
            "under the v2 storyboard-gated video pipeline. The customer has approved the storyboard; "
            "the server has compiled one provider prompt per shot and per keyframe. Your job is "
            "managed submission only, never prompt authorship: read each compiled prompt file with "
            "read_file and pass its content verbatim (never write, rewrite, extend, translate, or "
            "summarize it). Execute these shot steps in order: "
            + " ".join(per_shot_instructions)
            + " wait_for_completion=false is mandatory: the media daemon drives provider polling and "
            "Credits settlement, so never poll a submitted shot and never call a generation Tool "
            "twice for the same shot. If one shot's submission is rejected, record it and continue "
            "with the remaining shots. After submitting every shot, end your turn without composing "
            "anything and without claiming delivery.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    if stage == "compose":
        clip_paths = [str(clip.get("clip_path") or "") for clip in shot_clips]
        clip_listing = ", ".join(
            f"'{path}'" for path in clip_paths if path
        )
        if audio_mode == "voiceover":
            narration = str(getattr(storyboard, "voiceover_script", "") or "").strip()
            compose_instruction = (
                "Your only paid step is the voiceover: call generate_speech_minimax exactly once "
                f"with the approved narration and save_path='workspace/deliverables/{request.id}/voiceover.mp3'. "
                f"The approved narration is: {narration}. "
            )
        else:
            compose_instruction = ""
        return (
            "You are finishing a persisted Astra Deliverable Request under the v2 storyboard-gated "
            "video pipeline. Every approved shot clip is complete: "
            f"{clip_listing}. "
            + compose_instruction
            + "Assembly is server-owned and deterministic: Astra concatenates the versioned shot "
            "clips, applies the approved captions and CTA, mixes the voiceover with loudness "
            "control, and extracts the cover frame without any further generation. Never call "
            "compose_video_audio, never regenerate or resubmit a shot, and never edit the clips "
            "yourself. After the voiceover step (or immediately, when the audio mode needs no "
            "voiceover), end your turn; the server assembles and quality-checks the final package. "
            "Never claim delivery yourself.\n"
            f"DELIVERABLE_REQUEST={json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        )

    raise DeliverableWorkflowError(
        "deliverable_continuation_not_ready",
        f"Unknown v2 video continuation stage: {stage}",
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


async def _presentation_tool_available(
    db: AsyncSession,
    agent_id: uuid.UUID,
    output_contract: Sequence[str] | None = None,
) -> bool:
    required_tools = ["convert_html_to_pptx"]
    normalized_outputs = {
        str(value or "").strip().lower() for value in (output_contract or ())
    }
    if "pdf" in normalized_outputs:
        required_tools.append("convert_html_to_pdf")
    return all(
        [
            await _agent_tool_available(
                db,
                agent_id=agent_id,
                tool_name=tool_name,
            )
            for tool_name in required_tools
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
        if workflow.workflow_id == POSTER_V2_WORKFLOW_ID:
            # FR-I3: the candidate count is bound to the tier; deterministic
            # compose adds no Provider cost, so minimum == maximum.
            candidates = candidate_count_for_policy(tier, spec)
            return {
                "mode": "estimate",
                "minimum": credits * candidates,
                "maximum": credits * candidates,
                "billing_unit": f"{candidates}_candidate_images",
                "candidates": candidates,
                "per_candidate_credits": credits,
            }
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
        if workflow.workflow_id == VIDEO_V2_WORKFLOW_ID:
            # FR-C2: per-shot estimate with the managed keyframe chain.  The
            # storyboard is approved before any spend, so minimum == maximum.
            shot_count = spec.get("shot_count")
            if not isinstance(shot_count, int) or isinstance(shot_count, bool):
                shot_count = max(1, min(math.ceil(duration / 4), 12))
            shot_count = max(1, min(shot_count, 12))
            per_shot_duration = max(1, math.ceil(duration / shot_count))
            # MiniMax bills clips in 6s/10s buckets; the estimate uses the
            # billed bucket so preflight never understates per-shot cost.
            billed_shot_duration = 6 if per_shot_duration <= 6 else 10
            per_shot_video = minimax_video_credits(
                profile.model,
                duration=billed_shot_duration,
                resolution=resolution,
            )
            per_shot_keyframe = minimax_image_credits("image-01", images=1)
            per_shot = per_shot_video + per_shot_keyframe
            total = per_shot * shot_count
            return {
                "mode": "estimate",
                "minimum": total,
                "maximum": total,
                "billing_unit": f"{shot_count}_keyframed_shots",
                "shots": shot_count,
                "per_shot_credits": per_shot,
                "per_shot_video_credits": per_shot_video,
                "per_shot_keyframe_credits": per_shot_keyframe,
            }
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

    creative_brief_projection: dict[str, Any] | None = None
    if workflow.workflow_id == POSTER_V2_WORKFLOW_ID:
        # FR-I1: an incomplete brief parks the request in the clarification
        # state; no prompt is compiled and no Credits are reserved.
        if not poster_v2_workflow_allowed(tenant_id, agent_id):
            reasons.append("deliverable_poster_v2_not_allowlisted")
        brief, missing_fields = compile_creative_brief(
            goal,
            normalized_spec,
            inputs,
            tier=normalized_tier,
            delivery_formats=workflow.output_contract,
        )
        creative_brief_projection = brief_projection(brief, missing_fields)
        reasons.extend(f"brief_missing:{field}" for field in missing_fields)
    if workflow.workflow_id == VIDEO_V2_WORKFLOW_ID:
        # FR-V1: same clarification seam for the structured video brief.
        if not video_v2_workflow_allowed(tenant_id, agent_id):
            reasons.append("deliverable_video_v2_not_allowlisted")
        if not deliverable_stage_approvals_enabled():
            reasons.append("deliverable_stage_approvals_disabled")
        video_brief, video_missing = compile_video_brief(
            goal,
            normalized_spec,
            inputs,
            tier=normalized_tier,
        )
        creative_brief_projection = video_brief_projection(video_brief, video_missing)
        reasons.extend(f"brief_missing:{field}" for field in video_missing)
    if workflow.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        # FR-P1: same clarification seam for the structured presentation brief.
        if not presentation_v2_workflow_allowed(tenant_id, agent_id):
            reasons.append("deliverable_presentation_v2_not_allowlisted")
        if not deliverable_stage_approvals_enabled():
            reasons.append("deliverable_stage_approvals_disabled")
        presentation_brief, presentation_missing = compile_presentation_brief(
            goal,
            normalized_spec,
            inputs,
            output_contract=workflow.output_contract,
        )
        creative_brief_projection = presentation_brief_projection(
            presentation_brief,
            presentation_missing,
        )
        reasons.extend(f"brief_missing:{field}" for field in presentation_missing)

    try:
        await resolve_route(tenant_id, normalized_tier, "text")
    except QuotaExceeded as exc:
        reasons.append(str(getattr(exc, "quota_type", None) or "text_route_unavailable"))

    presentation_image_required = False
    if workflow.required_capability == "presentation":
        if not await _presentation_tool_available(
            db,
            agent_id,
            workflow.output_contract,
        ):
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
            if (
                workflow.workflow_id == VIDEO_V2_WORKFLOW_ID
                and str(normalized_spec.get("audio_mode") or "").strip()
                == "in_scene_dialogue"
            ):
                # FR-V1: in-scene synchronized dialogue needs a provider-native
                # audio track.  Without one the route can only offer
                # voiceover/silent, so the request must never launch promising
                # dialogue it cannot honor.
                video_providers = (
                    row.get("available_providers", ())
                    if row is not None and row.get("available")
                    else ()
                )
                if not video_providers_with_native_audio(video_providers):
                    reasons.append("audio_mode_route_mismatch")
                    capability_status = "unavailable"
                    next_action = (
                        "镜头内同步对白需要具备原生音轨能力的线路；当前没有可用线路。"
                        "请改用旁白或静音模式后重新确认，或保留工作说明等待线路恢复。"
                    )

    if workflow.launch_policy == "dry_run":
        reasons.append("workflow_execution_not_enabled")
    non_capability_reasons = {
        "workflow_execution_not_enabled",
        "degraded_route_requires_confirmation",
        "poster_exact_copy_contract_invalid",
        "poster_layout_unfit",
    }
    reasons = list(dict.fromkeys(reasons))
    capability_reasons = [
        reason
        for reason in reasons
        if reason not in non_capability_reasons
        and not reason.startswith("brief_missing:")
    ]
    if "deliverable_stage_approvals_disabled" in reasons:
        next_action = (
            "阶段审批总闸尚未开启；请继续使用 V1，或由管理员同时开启阶段审批与该账号的 V2 灰度后再试。"
        )
    elif any(reason.endswith("_not_allowlisted") for reason in reasons):
        next_action = (
            "该账号不在此 V2 工作流灰度范围；请继续使用 V1，或由管理员将当前公司或数字员工加入灰度白名单。"
        )
    else:
        missing_fields = [
            reason.split(":", 1)[1]
            for reason in reasons
            if reason.startswith("brief_missing:")
        ]
        if missing_fields:
            next_action = (
                "请补充工作说明中的必要字段："
                + "、".join(missing_fields)
                + "。补充完成前不会调用生成服务或扣除 Credits。"
            )
        elif "poster_exact_copy_contract_invalid" in reasons:
            next_action = "请按精确文案字段重新整理海报文字；修正前不会提交图片生成。"
        elif "poster_layout_unfit" in reasons:
            next_action = "当前文案无法安全排入所选画幅；请减少文案或调整画面比例后重新检查。"
    result: dict[str, Any] = {
        "available": not capability_reasons,
        "launchable": not reasons and workflow.launch_policy == "agent_runtime",
        "reasons": reasons,
        "capability_status": capability_status,
        "next_action": next_action,
        "tier": normalized_tier,
        "normalized_spec": normalized_spec,
        "credit_estimate": _credit_estimate(workflow, normalized_tier, normalized_spec),
        "creates_reservation": False,
    }
    if creative_brief_projection is not None:
        result["creative_brief"] = creative_brief_projection
    return result


async def _prepare_poster_v2_compilations(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    execution: DeliverableExecution | None,
) -> None:
    """Compile and persist every candidate prompt before a v2 poster launch."""

    from app.services.prompt_compiler import (
        TIER_QUALITY_SIZE,
        compile_image_prompt,
        record_prompt_compilation,
        store_compiled_prompt,
    )

    brief, missing_fields = compile_creative_brief(
        request.goal,
        request.spec,
        request.inputs,
        tier=request.tier,
        delivery_formats=request.output_contract or ("png",),
    )
    if brief is None:
        raise DeliverableWorkflowError(
            "deliverable_brief_incomplete",
            "Creative brief is incomplete: " + ", ".join(missing_fields),
        )
    if execution is None:
        raise DeliverableWorkflowError(
            "deliverable_execution_missing",
            "v2 poster launch requires an active execution",
        )
    unit_result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
            DeliverableExecutionUnit.stage_key == "candidate_generate",
        )
        .order_by(DeliverableExecutionUnit.unit_key)
    )
    units = tuple(unit_result.scalars().all())
    if not units:
        raise DeliverableWorkflowError(
            "deliverable_execution_missing",
            "v2 poster execution has no candidate units",
        )
    strategy_order = media_provider_order_for_image_strategy("commercial_quality")
    provider_target = str(strategy_order[0]) if strategy_order else "minimax"
    quality_size = TIER_QUALITY_SIZE.get(request.tier, "2K")
    for unit in units:
        candidate_index = int(str(unit.unit_key).rsplit("-", 1)[-1])
        compiled = compile_image_prompt(
            brief,
            provider_target=provider_target,
            candidate_index=candidate_index,
            quality_size=quality_size,
        )
        prompt_path = await store_compiled_prompt(
            agent_id=request.agent_id,
            request_id=request.id,
            unit_key=unit.unit_key,
            content=compiled.neutral_prompt,
        )
        await record_prompt_compilation(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=execution.id,
            unit_id=unit.id,
            compiled=compiled,
            compiled_prompt_path=prompt_path,
        )
    await upsert_request_creative_brief(db, request, execution_id=execution.id)
    await db.flush()


async def _prepare_video_v2_shot_compilations(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    execution: DeliverableExecution,
    storyboard,
) -> None:
    """Compile every approved shot/keyframe prompt before a v2 shot run.

    Idempotent: an existing receipt for the same unit and compiler version is
    kept, so a resumed continuation never double-writes compilation facts.
    """

    from app.services.prompt_compiler import (
        TIER_QUALITY_SIZE,
        VIDEO_KEYFRAME_COMPILER_VERSION,
        VIDEO_SHOT_COMPILER_VERSION,
        compile_video_keyframe_prompt,
        compile_video_shot_prompt,
        record_prompt_compilation,
        store_compiled_prompt,
    )

    brief, missing_fields = compile_video_brief(
        request.goal,
        request.spec,
        request.inputs,
        tier=request.tier,
    )
    if brief is None:
        raise DeliverableWorkflowError(
            "deliverable_brief_incomplete",
            "Video brief is incomplete: " + ", ".join(missing_fields),
        )
    unit_result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
            DeliverableExecutionUnit.stage_key.in_(("shot_generate", "keyframe_pack")),
        )
        .order_by(DeliverableExecutionUnit.unit_key)
    )
    units = tuple(unit_result.scalars().all())
    shots_by_id = {shot.shot_id: shot for shot in storyboard.shots}
    video_target = next(iter(media_provider_order_for_modality("video")), "minimax")
    image_target = next(
        iter(media_provider_order_for_image_strategy("commercial_quality")),
        "volcengine_agent_plan",
    )
    quality_size = TIER_QUALITY_SIZE.get(request.tier, "2K")
    for unit in units:
        shot = shots_by_id.get(unit.unit_key)
        if shot is None:
            raise DeliverableWorkflowError(
                "deliverable_storyboard_missing",
                f"The approved storyboard has no spec for unit {unit.unit_key}",
            )
        if unit.stage_key == "shot_generate":
            compiler_version = VIDEO_SHOT_COMPILER_VERSION
            prompt_unit_key = unit.unit_key
            compiled = compile_video_shot_prompt(
                brief,
                shot,
                provider_target=video_target,
            )
        else:
            compiler_version = VIDEO_KEYFRAME_COMPILER_VERSION
            prompt_unit_key = f"keyframe-{unit.unit_key}"
            compiled = compile_video_keyframe_prompt(
                brief,
                shot,
                provider_target=image_target,
                quality_size=quality_size,
            )
        existing_result = await db.execute(
            select(DeliverablePromptCompilation).where(
                DeliverablePromptCompilation.tenant_id == request.tenant_id,
                DeliverablePromptCompilation.execution_id == execution.id,
                DeliverablePromptCompilation.unit_id == unit.id,
                DeliverablePromptCompilation.compiler_version == compiler_version,
            )
        )
        if existing_result.scalar_one_or_none() is not None:
            continue
        prompt_path = await store_compiled_prompt(
            agent_id=request.agent_id,
            request_id=request.id,
            unit_key=prompt_unit_key,
            content=compiled.neutral_prompt,
        )
        await record_prompt_compilation(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=execution.id,
            unit_id=unit.id,
            compiled=compiled,
            compiled_prompt_path=prompt_path,
        )
    await db.flush()


def _video_v2_run_is_active(run: AgentRun | None) -> bool:
    return run is not None and str(run.status or "") not in {
        "completed",
        "failed",
        "cancelled",
    }


async def _prepare_video_v2_continuation(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    message_id: uuid.UUID,
) -> PreparedDeliverableLaunch:
    """FR-V2/V3: short stage runs after storyboard approval or shot completion.

    A continuation only ever submits shots or assembles the final package and
    then returns; provider polling stays with the media daemon, so the direct
    lane is never held by a long video wait.
    """

    if request.status not in {"ready", "running"}:
        raise DeliverableWorkflowError(
            "deliverable_invalid_status",
            f"Deliverable request cannot continue from status {request.status}",
        )
    stage = str(request.current_stage or "")
    execution = (
        await current_execution(db, request, lock=True)
        if request.current_execution_id is not None
        else None
    )
    if execution is None:
        raise DeliverableWorkflowError(
            "deliverable_execution_missing",
            "v2 video continuation requires an active execution",
        )
    if stage in {"storyboard_draft", "shot_generation", "compose"}:
        # Resume is only safe once the previous short run is terminal.
        last_run_id = execution.coordinator_run_id or execution.intake_run_id
        if last_run_id is not None:
            last_run = await db.get(AgentRun, last_run_id)
            if _video_v2_run_is_active(last_run):
                raise DeliverableWorkflowError(
                    "deliverable_continuation_not_ready",
                    "The previous stage run is still active",
                )
    if stage == "storyboard_draft":
        # The intake run ended without a compiled storyboard (crash or sync
        # gap): re-run the same draft stage; no paid work exists at this stage.
        prompt = build_deliverable_prompt(request, video_v2_stage="storyboard_draft")
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "storyboard_draft"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    if stage in {"storyboard_approved", "shot_generation"}:
        # Hard gate: no paid shot without an approved storyboard receipt.
        if not await storyboard_approved(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        ):
            raise DeliverableWorkflowError(
                "deliverable_storyboard_approval_required",
                "The storyboard must be approved before any paid shot is submitted",
            )
        storyboard = await load_latest_storyboard(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        if storyboard is None:
            raise DeliverableWorkflowError(
                "deliverable_storyboard_missing",
                "No approved storyboard is recorded for this request",
            )
        await _prepare_video_v2_shot_compilations(
            db,
            request=request,
            execution=execution,
            storyboard=storyboard,
        )
        prompt = build_deliverable_prompt(
            request,
            video_v2_stage="shot_generation",
            video_v2_storyboard=storyboard,
        )
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "shot_generation"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    if stage in {"compose_ready", "compose"}:
        unit_result = await db.execute(
            select(DeliverableExecutionUnit)
            .where(
                DeliverableExecutionUnit.tenant_id == request.tenant_id,
                DeliverableExecutionUnit.execution_id == execution.id,
                DeliverableExecutionUnit.stage_key == "shot_generate",
                DeliverableExecutionUnit.status == "succeeded",
            )
            .order_by(DeliverableExecutionUnit.unit_key)
        )
        clips = [
            {
                "unit_key": unit.unit_key,
                "clip_path": str((unit.result_snapshot or {}).get("clip_path") or ""),
            }
            for unit in unit_result.scalars().all()
            if str((unit.result_snapshot or {}).get("clip_path") or "")
        ]
        expected_shots = sum(
            1
            for unit in (await execution_units(db, execution.id))
            if unit.stage_key == "shot_generate"
        )
        if not clips or len(clips) != expected_shots:
            raise DeliverableWorkflowError(
                "deliverable_shot_clips_missing",
                "Every approved shot clip must be complete before assembly",
            )
        storyboard = await load_latest_storyboard(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        prompt = build_deliverable_prompt(
            request,
            video_v2_stage="compose",
            video_v2_storyboard=storyboard,
            video_v2_shot_clips=clips,
        )
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "compose"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    raise DeliverableWorkflowError(
        "deliverable_continuation_not_ready",
        f"The v2 video pipeline cannot continue from stage {stage or 'unknown'}",
    )


async def _prepare_presentation_v2_continuation(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    message_id: uuid.UUID,
) -> PreparedDeliverableLaunch:
    """FR-P3: short stage runs after outline approval or a crashed draft.

    The outline draft run produces only planning files; the production run
    renders locally and converts with the approved editability render mode.
    No Provider polling exists in this pipeline, so runs stay short and the
    direct lane is never held.
    """

    if request.status not in {"ready", "running"}:
        raise DeliverableWorkflowError(
            "deliverable_invalid_status",
            f"Deliverable request cannot continue from status {request.status}",
        )
    stage = str(request.current_stage or "")
    execution = (
        await current_execution(db, request, lock=True)
        if request.current_execution_id is not None
        else None
    )
    if execution is None:
        raise DeliverableWorkflowError(
            "deliverable_execution_missing",
            "v2 presentation continuation requires an active execution",
        )
    if stage in {"outline_draft", "slide_render", "slide_revision"}:
        # Resume is only safe once the previous short run is terminal.
        last_run_id = execution.coordinator_run_id or execution.intake_run_id
        if last_run_id is not None:
            last_run = await db.get(AgentRun, last_run_id)
            if _video_v2_run_is_active(last_run):
                raise DeliverableWorkflowError(
                    "deliverable_continuation_not_ready",
                    "The previous stage run is still active",
                )
    if stage == "slide_revision":
        # FR-P6 resume: the revision run crashed before converting; rebuild the
        # same page-targeted prompt from the frozen revision contract.
        snapshot = (
            execution.contract_snapshot
            if isinstance(execution.contract_snapshot, Mapping)
            else {}
        )
        raw_targets = snapshot.get("target_units")
        revision_targets = tuple(
            str(item) for item in raw_targets if str(item).strip()
        ) if isinstance(raw_targets, (list, tuple)) else ()
        if not revision_targets:
            raise DeliverableWorkflowError(
                "deliverable_revision_target_invalid",
                "The revision execution has no page targets",
            )
        inventory = await load_presentation_v2_inventory_projection(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        prompt = build_deliverable_prompt(
            request,
            presentation_v2_stage="slide_revision",
            presentation_v2_source_inventory=inventory,
            presentation_v2_target_units=revision_targets,
            presentation_v2_revision_instruction=execution.revision_instruction,
        )
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "slide_revision"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    if stage == "outline_draft":
        # The intake run ended without a validated outline (crash or sync
        # gap): re-run the same draft stage; no paid work exists at this stage.
        inventory = await load_presentation_v2_inventory_projection(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        prompt = build_deliverable_prompt(
            request,
            presentation_v2_stage="outline_draft",
            presentation_v2_source_inventory=inventory,
        )
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "outline_draft"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    if stage in {"outline_approved", "slide_render"}:
        # Hard gate: no rendering or paid imagery without an approved outline.
        if not await outline_approved(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        ):
            raise DeliverableWorkflowError(
                "deliverable_outline_approval_required",
                "The outline must be approved before any rendering starts",
            )
        outline = await load_latest_outline(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        if outline is None:
            raise DeliverableWorkflowError(
                "deliverable_outline_missing",
                "No approved outline is recorded for this request",
            )
        inventory = await load_presentation_v2_inventory_projection(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        prompt = build_deliverable_prompt(
            request,
            presentation_v2_stage="slide_render",
            presentation_v2_source_inventory=inventory,
        )
        request.launch_message_id = message_id
        request.agent_run_id = None
        request.status = "running"
        request.current_stage = "slide_render"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    raise DeliverableWorkflowError(
        "deliverable_continuation_not_ready",
        f"The v2 presentation pipeline cannot continue from stage {stage or 'unknown'}",
    )


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
        if workflow.workflow_id == VIDEO_V2_WORKFLOW_ID:
            # FR-V2/V3: storyboard approval and shot completion drive short
            # continuation runs instead of one long foreground wait.
            return await _prepare_video_v2_continuation(
                db,
                request=request,
                message_id=message_id,
            )
        if workflow.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
            # FR-P3: outline approval drives a short production continuation;
            # rendering is local so no run ever parks on a Provider.
            return await _prepare_presentation_v2_continuation(
                db,
                request=request,
                message_id=message_id,
            )
        raise DeliverableWorkflowError(
            "deliverable_already_launched",
            "Deliverable request has already been launched",
        )
    if request.status not in {"ready", "running"}:
        raise DeliverableWorkflowError(
            "deliverable_invalid_status",
            f"Deliverable request cannot be launched from status {request.status}",
        )
    if workflow.workflow_id == VIDEO_V2_WORKFLOW_ID:
        # v2 video first launch drafts the storyboard; a shot-targeted revision
        # launch re-enters the approved shot stage directly.
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
        shot_units = (
            [
                unit
                for unit in await execution_units(db, execution.id)
                if unit.stage_key == "shot_generate"
            ]
            if execution is not None
            else []
        )
        revision_snapshot = (
            execution.contract_snapshot
            if execution is not None
            and isinstance(execution.contract_snapshot, Mapping)
            else {}
        )
        revision_stage = str(revision_snapshot.get("revision_stage") or "")
        if (
            execution is not None
            and execution.kind == "revision"
            and revision_stage != "storyboard"
            and shot_units
        ):
            if not await storyboard_approved(
                db,
                tenant_id=request.tenant_id,
                request_id=request.id,
            ):
                raise DeliverableWorkflowError(
                    "deliverable_storyboard_approval_required",
                    "The storyboard must be approved before any paid shot is submitted",
                )
            storyboard = await load_latest_storyboard(
                db,
                tenant_id=request.tenant_id,
                request_id=request.id,
            )
            if storyboard is None:
                raise DeliverableWorkflowError(
                    "deliverable_storyboard_missing",
                    "No approved storyboard is recorded for this request",
                )
            await _prepare_video_v2_shot_compilations(
                db,
                request=request,
                execution=execution,
                storyboard=storyboard,
            )
            prompt = build_deliverable_prompt(
                request,
                video_v2_stage="shot_generation",
                video_v2_storyboard=storyboard,
            )
            next_stage = "shot_generation"
        else:
            prompt = build_deliverable_prompt(
                request,
                video_v2_stage="storyboard_draft",
            )
            next_stage = "storyboard_draft"
        request.launch_message_id = message_id
        request.status = "running"
        request.current_stage = next_stage
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
        )
    if workflow.workflow_id == PRESENTATION_V2_WORKFLOW_ID:
        # FR-P1/P3: the v2 first launch only drafts the outline and slide_spec;
        # the customer approves the outline before any rendering or imagery.
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
        inventory = await load_presentation_v2_inventory_projection(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        # FR-P6: a page-targeted revision execution re-renders only its target
        # slides; the outline stays approved and every other page carries
        # forward.  A full-deck revision (no targets) re-drafts the outline.
        revision_targets: tuple[str, ...] = ()
        revision_instruction: str | None = None
        if execution is not None and execution.kind == "revision":
            snapshot = (
                execution.contract_snapshot
                if isinstance(execution.contract_snapshot, Mapping)
                else {}
            )
            raw_targets = snapshot.get("target_units")
            revision_targets = tuple(
                str(item) for item in raw_targets if str(item).strip()
            ) if isinstance(raw_targets, (list, tuple)) else ()
            revision_instruction = execution.revision_instruction
            revision_stage = str(snapshot.get("revision_stage") or "")
        else:
            revision_stage = ""
        if revision_targets and revision_stage != "outline":
            prompt = build_deliverable_prompt(
                request,
                presentation_v2_stage="slide_revision",
                presentation_v2_source_inventory=inventory,
                presentation_v2_target_units=revision_targets,
                presentation_v2_revision_instruction=revision_instruction,
            )
            request.launch_message_id = message_id
            request.status = "running"
            request.current_stage = "slide_revision"
            request.version += 1
            return PreparedDeliverableLaunch(
                request=request,
                prompt=prompt,
                execution=execution,
            )
        prompt = build_deliverable_prompt(
            request,
            presentation_v2_stage="outline_draft",
            presentation_v2_source_inventory=inventory,
        )
        request.launch_message_id = message_id
        request.status = "running"
        request.current_stage = "outline_draft"
        request.version += 1
        return PreparedDeliverableLaunch(
            request=request,
            prompt=prompt,
            execution=execution,
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
        if workflow.workflow_id == POSTER_V2_WORKFLOW_ID:
            # FR-I2: compile every candidate prompt before the run starts so
            # the Runtime only ever injects server-owned prompt file paths.
            await _prepare_poster_v2_compilations(
                db,
                request=request,
                execution=execution,
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
    is_video_v2 = request.workflow_id == VIDEO_V2_WORKFLOW_ID
    # v2 stage pipelines (video storyboard, presentation outline) park at
    # explicit stages between short runs; the run attach must preserve the
    # stage instead of stomping it with a generic "running".
    is_stage_v2 = is_video_v2 or request.workflow_id == PRESENTATION_V2_WORKFLOW_ID
    request.agent_run_id = run_id
    request.launched_at = launched_at
    if not is_stage_v2:
        request.current_stage = "running"
    execution = getattr(prepared, "execution", None)
    if isinstance(execution, DeliverableExecution):
        if execution.intake_run_id is not None and execution.intake_run_id != run_id:
            if not is_stage_v2:
                raise DeliverableWorkflowError(
                    "deliverable_execution_run_mismatch",
                    "Deliverable execution is already linked to another run",
                )
            # v2 continuation runs are short stage runs; the latest one is the
            # coordinator, and earlier runs stay traceable through the units.
            execution.coordinator_run_id = run_id
        else:
            execution.intake_run_id = run_id
        execution.launch_message_id = request.launch_message_id
        execution.status = "running"
        execution.current_stage = request.current_stage if is_stage_v2 else "running"
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
            if candidate.workflow_id == POSTER_V2_WORKFLOW_ID:
                from app.services.candidate_qa import evaluate_poster_v2_candidates

                await evaluate_poster_v2_candidates(db, request=candidate, run_id=run_id)
            if candidate.workflow_id == VIDEO_V2_WORKFLOW_ID and await advance_video_v2_after_run(
                db,
                request=candidate,
                run_id=run_id,
                lifecycle_status=normalized_lifecycle_status,
                now=now,
            ):
                # Storyboard drafting and shot submission stages never reach
                # artifact reconciliation; their terminal projection is owned
                # by the v2 stage machine above.
                return await finish(candidate)
            if candidate.workflow_id == PRESENTATION_V2_WORKFLOW_ID and await advance_presentation_v2_after_run(
                db,
                request=candidate,
                run_id=run_id,
                lifecycle_status=normalized_lifecycle_status,
                now=now,
            ):
                # The outline draft stage never reaches artifact
                # reconciliation; its terminal projection is owned above.
                return await finish(candidate)
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
        if request.workflow_id == POSTER_V2_WORKFLOW_ID:
            from app.services.candidate_qa import evaluate_poster_v2_candidates

            await evaluate_poster_v2_candidates(db, request=request, run_id=run_id)
        if request.workflow_id == VIDEO_V2_WORKFLOW_ID and await advance_video_v2_after_run(
            db,
            request=request,
            run_id=run_id,
            lifecycle_status=normalized_lifecycle_status,
            now=now,
        ):
            return await finish(request)
        if request.workflow_id == PRESENTATION_V2_WORKFLOW_ID and await advance_presentation_v2_after_run(
            db,
            request=request,
            run_id=run_id,
            lifecycle_status=normalized_lifecycle_status,
            now=now,
        ):
            return await finish(request)
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
