"""Versioned product workflow manifests and safe deliverable launch helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, Mapping
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import DeliverableRequest
from app.models.tool import AgentTool, Tool
from app.services.deliverable_artifacts import reconcile_runtime_deliverable_artifacts
from app.services.entitlements import get_tenant_entitlements
from app.services.media_capabilities import get_agent_media_capabilities
from app.services.minimax_media_profiles import resolve_minimax_media_profile
from app.services.model_router import resolve_route
from app.services.provider_pricing import minimax_image_credits, minimax_video_credits
from app.services.quota_guard import QuotaExceeded


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
                key="audience", label_zh="目标受众", label_en="Audience", kind="text", required=True,
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
        description_zh="保存商品、品牌和精确文案要求；本阶段只做能力与费用预检。",
        description_en="Capture product, brand, and exact-copy requirements; this phase performs preflight only.",
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
        ],
        approval_policy=["composition", "final"],
        output_contract=["png"],
        required_capability="image",
        launch_policy="dry_run",
    ),
    WorkflowManifest(
        workflow_id="builtin.video.v1",
        workflow_version="1.0.0",
        work_type="video",
        label_zh="短视频",
        label_en="Short video",
        description_zh="保存分镜、比例和时长要求；本阶段只做能力与费用预检。",
        description_en="Capture storyboard, ratio, and duration requirements; this phase performs preflight only.",
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
                key="story", label_zh="故事与镜头要求", label_en="Story and shots", kind="textarea", required=True,
                placeholder_zh="产品、场景、镜头运动、字幕和声音要求", placeholder_en="Product, scene, camera, captions, and audio",
            ),
        ],
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
        required_capability="video",
        launch_policy="dry_run",
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


def build_deliverable_prompt(request: DeliverableRequest) -> str:
    """Build server-owned execution context; provider/model choice is intentionally absent."""

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
    }
    return (
        "You are executing a persisted Astra Deliverable Request. Treat the following JSON as "
        "the authoritative product brief. Do not choose or reveal a provider/model. Use only enabled "
        "tools, keep every artifact under workspace/deliverables/"
        f"{request.id}/, and never claim success until every output_contract file exists and validates. "
        "For presentation requests, create one structurally valid PPTX with convert_html_to_pptx and "
        "one matching PDF with convert_html_to_pdf; report both exact workspace paths.\n"
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
    return bool(assignment.enabled) if assignment is not None else bool(tool.is_default)


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


def _credit_estimate(workflow: WorkflowManifest, tier: str, spec: dict[str, Any]) -> dict[str, Any]:
    if workflow.work_type == "presentation":
        return {
            "mode": "usage_based",
            "minimum": None,
            "maximum": None,
            "billing_unit": "actual_usage",
        }
    if workflow.work_type == "poster":
        candidates = {"lite": 1, "pro": 3, "ultra": 5}[tier]
        credits = minimax_image_credits("image-01", images=candidates)
        return {
            "mode": "estimate",
            "minimum": credits,
            "maximum": credits,
            "billing_unit": f"{candidates}_candidate_images",
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
        credits = minimax_video_credits(profile.model, duration=duration, resolution=resolution)
        return {
            "mode": "estimate",
            "minimum": credits,
            "maximum": credits,
            "billing_unit": f"{duration}s_{resolution.lower()}_clip",
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
) -> dict[str, Any]:
    normalized_tier = str(tier or "lite").strip().lower()
    if normalized_tier not in SAAS_TIERS:
        raise DeliverableWorkflowError("invalid_tier", "Tier must be lite, pro, or ultra")
    normalized_spec = validate_workflow_spec(workflow, spec)
    reasons: list[str] = []

    try:
        await resolve_route(tenant_id, normalized_tier, "text")
    except QuotaExceeded as exc:
        reasons.append(str(getattr(exc, "quota_type", None) or "text_route_unavailable"))

    if workflow.required_capability == "presentation":
        if not await _presentation_tool_available(db, agent_id):
            reasons.append("presentation_tool_unavailable")
    elif workflow.required_capability in {"image", "video"}:
        entitlements = await get_tenant_entitlements(tenant_id)
        media = await get_agent_media_capabilities(
            db,
            agent_id=agent_id,
            entitlements=entitlements,
            tier=normalized_tier,
        )
        row = next((item for item in media if item["modality"] == workflow.required_capability), None)
        if row is None or not row["available"]:
            reasons.append(str((row or {}).get("reason") or "media_capability_unavailable"))

    if workflow.launch_policy == "dry_run":
        reasons.append("workflow_execution_not_enabled")
    hard_reasons = [reason for reason in reasons if reason != "workflow_execution_not_enabled"]
    return {
        "available": not hard_reasons,
        "launchable": not reasons and workflow.launch_policy == "agent_runtime",
        "reasons": reasons,
        "tier": normalized_tier,
        "normalized_spec": normalized_spec,
        "credit_estimate": _credit_estimate(workflow, normalized_tier, normalized_spec),
        "creates_reservation": False,
    }


@dataclass(slots=True)
class PreparedDeliverableLaunch:
    request: DeliverableRequest
    prompt: str


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
    if request.launch_message_id is None:
        preflight = await preflight_workflow(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            workflow=workflow,
            tier=request.tier,
            spec=request.spec,
        )
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
    return PreparedDeliverableLaunch(request=request, prompt=build_deliverable_prompt(request))


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
    if not isinstance(request, DeliverableRequest):
        return None
    if request.status in {"succeeded", "cancelled"}:
        return request
    if request.status == "failed" and request.current_stage == "changes_requested":
        return request
    normalized_lifecycle_status = str(lifecycle_status or "").strip().lower()
    if normalized_lifecycle_status == "completed" and request.work_type == "presentation":
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
        if (
            request.status == next_status
            and request.current_stage == next_stage
            and request.last_error_code == next_error_code
        ):
            return request
        request.status = next_status
        request.current_stage = next_stage
        request.completed_at = now or datetime.now(UTC) if next_status == "failed" else None
        request.last_error_code = next_error_code
        request.version += 1
        return request
    terminal_mapping = {
        # A completed Runtime only proves that the agent stopped normally. The
        # deliverable still needs an artifact revision and evaluator evidence.
        "completed": ("waiting_approval", "output_review"),
        "failed": ("failed", "failed"),
        "cancelled": ("cancelled", "cancelled"),
    }
    transition = terminal_mapping.get(normalized_lifecycle_status)
    if transition is None:
        return request
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
        return request
    request.status = next_status
    request.current_stage = next_stage
    request.completed_at = (
        now or datetime.now(UTC)
        if next_status in {"failed", "cancelled"}
        else None
    )
    request.last_error_code = next_error_code
    request.version += 1
    return request
