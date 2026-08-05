"""Deliverable contract validation, launch security, and estimate tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
import uuid

import pytest
from pydantic import ValidationError

from app.models.agent_run import AgentRun
from app.models.deliverable import DeliverableRequest
from app.schemas.deliverable import DeliverableInput
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    _credit_estimate,
    attach_deliverable_run,
    build_deliverable_prompt,
    list_agent_launchable_workflows,
    list_workflow_manifests,
    prepare_deliverable_launch,
    preflight_workflow,
    request_fingerprint,
    require_workflow,
    validate_workflow_spec,
    sync_deliverable_lifecycle,
)
from app.services.presentation_visual_policy import (
    presentation_brief_is_image_led,
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, value: object) -> None:
        self.value = value

    async def execute(self, _statement):
        return _ScalarResult(self.value)


class _SequenceSession:
    def __init__(self, *values: object) -> None:
        self.values = iter(values)

    async def execute(self, _statement):
        return _ScalarResult(next(self.values))


def _request(**overrides):
    tenant_id = overrides.pop("tenant_id", uuid.uuid4())
    user_id = overrides.pop("created_by_user_id", uuid.uuid4())
    agent_id = overrides.pop("agent_id", uuid.uuid4())
    session_id = overrides.pop("session_id", uuid.uuid4())
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "created_by_user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "agent_run_id": None,
        "launch_message_id": None,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "workflow_version": "1.0.0",
        "goal": "Create an investor presentation",
        "inputs": [{"type": "workspace_file", "path": "workspace/source.pdf"}],
        "spec": {
            "audience": "investors",
            "page_count": 8,
            "language": "en-US",
            "style": "professional",
        },
        "tier": "pro",
        "approval_policy": ["outline", "final"],
        "output_contract": ["pptx", "pdf"],
        "status": "ready",
        "current_stage": "brief_confirmed",
        "version": 1,
        "launched_at": None,
        "completed_at": None,
        "last_error_code": None,
    }
    values.update(overrides)
    return DeliverableRequest(**values)


def test_builtin_workflow_manifests_are_versioned_and_unique() -> None:
    workflows = list_workflow_manifests()

    assert [workflow.work_type for workflow in workflows] == [
        "presentation",
        "poster",
        "video",
    ]
    assert len({workflow.workflow_id for workflow in workflows}) == len(workflows)
    assert all(workflow.workflow_version == "1.0.0" for workflow in workflows)
    assert require_workflow("presentation").launch_policy == "agent_runtime"
    assert require_workflow("poster").launch_policy == "agent_runtime"
    assert require_workflow("video").launch_policy == "agent_runtime"


@pytest.mark.asyncio
async def test_launcher_lists_only_workflows_executable_by_this_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    available = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        available,
    )
    video_available = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.deliverable_workflows._video_post_production_tools_available",
        video_available,
    )
    image_tool_available = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        image_tool_available,
    )
    agent_id = uuid.uuid4()

    workflows = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=agent_id,
        tier="pro",
    )

    assert [workflow.work_type for workflow in workflows] == ["presentation", "poster", "video"]
    available.assert_awaited_once_with(ANY, agent_id)
    image_tool_available.assert_awaited_once_with(
        ANY,
        agent_id=agent_id,
        tool_name="generate_image_minimax",
    )
    video_available.assert_awaited_once_with(ANY, agent_id)


@pytest.mark.asyncio
async def test_launcher_hides_workflow_when_agent_lacks_its_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._video_post_production_tools_available",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=False),
    )

    workflows = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="ultra",
    )

    assert workflows == []


def test_presentation_spec_defaults_and_bounds_are_server_validated() -> None:
    workflow = require_workflow("presentation")
    spec = validate_workflow_spec(
        workflow,
        {"audience": "客户", "language": "zh-CN", "style": "简洁"},
    )

    assert spec["page_count"] == 8
    with pytest.raises(DeliverableWorkflowError, match="page_count"):
        validate_workflow_spec(
            workflow,
            {"audience": "客户", "page_count": 30, "language": "zh-CN", "style": "简洁"},
        )
    with pytest.raises(DeliverableWorkflowError, match="Unsupported spec fields"):
        validate_workflow_spec(
            workflow,
            {
                "audience": "客户",
                "language": "zh-CN",
                "style": "简洁",
                "provider": "forbidden",
            },
        )


def test_workspace_inputs_reject_traversal_and_absolute_paths() -> None:
    assert DeliverableInput(type="workspace_file", path="workspace/source.pdf").path == "workspace/source.pdf"

    for path in ("/tmp/source.pdf", "workspace/../secret", "source.pdf"):
        with pytest.raises(ValidationError):
            DeliverableInput(type="workspace_file", path=path)


def test_request_fingerprint_is_order_stable_and_sensitive_to_contract() -> None:
    first = request_fingerprint({"goal": "A", "spec": {"b": 2, "a": 1}})
    reordered = request_fingerprint({"spec": {"a": 1, "b": 2}, "goal": "A"})
    changed = request_fingerprint({"goal": "B", "spec": {"a": 1, "b": 2}})

    assert first == reordered
    assert first != changed


def test_execution_prompt_contains_contract_but_never_provider_selection() -> None:
    prompt = build_deliverable_prompt(_request())

    assert "builtin.presentation.v1" in prompt
    assert "workspace/deliverables/" in prompt
    assert '"tier": "pro"' in prompt
    assert "render_mode='hybrid_editable'" in prompt
    assert "every visible text node must remain fully inside its 1280x720 slide" in prompt
    assert "Use at least 16px computed font size" in prompt
    assert "data-clawith-text-role='metadata'" in prompt
    assert "one unique placeholder" in prompt
    assert "under 3500 characters" in prompt
    assert "exactly one placeholder per edit_file call" in prompt
    assert "outline.json" in prompt
    assert "slide_spec.json" in prompt
    assert "visual_plan_version='adaptive-v1'" in prompt
    assert "PRESENTATION_VISUAL_POLICY=" in prompt
    assert "do not repeat the same layout on consecutive slides" in prompt
    assert "editable_chart, editable_diagram, editable_table" in prompt
    assert "evidence may be one non-empty sentence or an array" in prompt
    assert "objects with a non-empty ref field" in prompt
    assert "[data-slide-title]" in prompt
    assert "The outline, slide_spec, and final visible slide titles must agree exactly" in prompt
    assert "expected_page_count=spec.page_count" in prompt
    assert "paper_width=13.333333" in prompt
    assert "paper_height=7.5" in prompt
    assert "exactly spec.page_count 16:9 pages" in prompt
    assert "Do not invent a brand" in prompt
    assert "one canonical hero asset first" in prompt
    assert "reference_image" in prompt
    assert '"required": false' in prompt
    assert "Do not invent star ratings, scores, percentages, ROI claims" in prompt
    assert "label the statement as a hypothesis to validate" in prompt
    assert '"provider"' not in prompt
    assert '"model"' not in prompt


def test_image_led_commercial_presentation_compiles_required_media_contract() -> None:
    request = _request(
        goal=(
            "制作一份高端智能保温杯新品发布提案，图文并茂，"
            "包含人物广告创意与三镜头故事板。"
        ),
        spec={
            "audience": "品牌与增长决策者",
            "page_count": 8,
            "language": "zh-CN",
            "style": "深海军蓝、暖金和银色的高端商业风",
        },
    )

    prompt = build_deliverable_prompt(request)

    assert '"required": true' in prompt
    assert '"asset_roles": ["product_hero", "people_lifestyle", "people_storyboard"]' in prompt
    assert '"minimum_distinct_images": 3' in prompt
    assert '"minimum_distinct_layouts": 4' in prompt
    assert '"minimum_image_slides": 4' in prompt
    assert '"minimum_picture_coverage_ratio": 0.35' in prompt
    assert '"maximum_uses_per_image": 3' in prompt
    assert "do not satisfy the image contract with tiny thumbnails" in prompt
    assert '"minimum_editable_compositions": 2' in prompt
    assert "generate_image_minimax exactly once" in prompt
    assert f"workspace/deliverables/{request.id}/assets/product_hero.png" in prompt
    assert f"workspace/deliverables/{request.id}/assets/people_lifestyle.png" in prompt
    assert f"workspace/deliverables/{request.id}/assets/people_storyboard.png" in prompt
    assert "pass its exact versioned output_path as reference_image" in prompt
    assert "Do not use emoji as a substitute" in prompt
    assert "stop without converting or claiming a commercial-quality deck" in prompt


def test_supplied_presentation_images_satisfy_media_roles_without_paid_generation() -> None:
    request = _request(
        goal=(
            "制作一份高端智能保温杯新品发布提案，图文并茂，"
            "包含人物广告创意与三镜头故事板。"
        ),
        inputs=[
            {
                "type": "workspace_file",
                "path": "workspace/deliverables/old/assets/product.png",
            },
            {
                "type": "workspace_file",
                "path": "workspace/uploads/lifestyle.webp",
            },
            {
                "type": "workspace_file",
                "path": "workspace/uploads/storyboard.jpg",
            },
        ],
        spec={
            "audience": "品牌与增长决策者",
            "page_count": 8,
            "language": "zh-CN",
            "style": "高端商业风",
        },
    )

    prompt = build_deliverable_prompt(request)

    assert '"generation_required_roles": []' in prompt
    assert '"role": "product_hero"' in prompt
    assert '"asset_ref": "../old/assets/product.png"' in prompt
    assert '"asset_ref": "../../uploads/lifestyle.webp"' in prompt
    assert '"minimum_distinct_images": 3' in prompt
    assert "All required media roles are satisfied by supplied assets" in prompt
    assert "Do not call generate_image_minimax for this presentation" in prompt
    assert "generate_image_minimax exactly once" not in prompt


@pytest.mark.asyncio
async def test_supplied_presentation_images_skip_unneeded_provider_preflight(
    monkeypatch,
) -> None:
    workflow = require_workflow("presentation")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    media_capabilities = AsyncMock()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        media_capabilities,
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        goal="制作一份图文并茂的新品发布 PPT",
        inputs=[
            {"type": "workspace_file", "path": "workspace/uploads/one.png"},
            {"type": "workspace_file", "path": "workspace/uploads/two.jpg"},
            {"type": "workspace_file", "path": "workspace/uploads/three.webp"},
        ],
        spec={
            "audience": "客户",
            "page_count": 8,
            "language": "zh-CN",
            "style": "商业风",
        },
    )

    assert result["available"] is True
    assert result["launchable"] is True
    assert result["reasons"] == []
    media_capabilities.assert_not_awaited()


def test_customer_launch_brief_is_image_led_without_literal_image_word() -> None:
    """Product launch decks must not silently fall back to sparse text cards."""

    request = _request(
        goal=(
            "为高端智能保温杯创建一份面向品牌与增长决策者的客户级上市方案，"
            "覆盖产品体验主张、外观与交互设计原则、人物广告创意与三镜头脚本、"
            "渠道物料与发布节奏。"
        ),
        spec={
            "audience": "品牌与增长决策者",
            "page_count": 8,
            "language": "zh-CN",
            "style": "premium_editorial",
        },
    )

    prompt = build_deliverable_prompt(request)

    assert '"required": true' in prompt
    assert '"asset_roles": ["product_hero", "people_lifestyle", "people_storyboard"]' in prompt
    assert '"minimum_picture_coverage_ratio": 0.35' in prompt


def test_explicit_native_only_presentation_does_not_require_images() -> None:
    request = _request(
        goal=(
            "为 ReefTotem 制作 6 页商业宣发 PPT，介绍图片、语音和视频业务流程。"
            "必须只使用PPT原生矢量形状、渐变、线条、图标和文字排版；"
            "不要调用任何图片、视频或语音生成工具，不使用外部素材。"
        ),
        spec={
            "audience": "客户决策者",
            "page_count": 6,
            "language": "zh-CN",
            "style": "深色科技商业风",
        },
    )

    prompt = build_deliverable_prompt(request)

    assert '"required": false' in prompt
    assert '"minimum_distinct_images": 0' in prompt
    assert '"minimum_image_slides": 0' in prompt
    assert '"minimum_picture_coverage_ratio": 0.0' in prompt
    assert "Do not call generate_image_minimax for this presentation" not in prompt


@pytest.mark.parametrize(
    "native_only_phrase",
    (
        "不要用图片，只做原生矢量",
        "无需图片，只用原生 shape",
        "不需要图片",
        "不调用图片、视频或语音生成工具",
        "严禁调用图片生成、视频生成或语音生成工具",
        "只使用 PPT 原生文字、形状、图表与线条",
        "Only native vector shapes, without images",
    ),
)
def test_common_native_only_phrases_override_positive_image_words(
    native_only_phrase: str,
) -> None:
    assert (
        presentation_brief_is_image_led(
            f"制作商业宣发 PPT，介绍图片业务；{native_only_phrase}",
            {"style": "商业风"},
        )
        is False
    )


def test_long_image_led_deck_scales_visual_budget_without_weakening_lite_contract() -> None:
    prompt = build_deliverable_prompt(
        _request(
            goal="制作一份图文并茂的 15 页商业发布方案",
            tier="lite",
            spec={
                "audience": "客户决策者",
                "page_count": 15,
                "language": "zh-CN",
                "style": "专业商业风",
            },
        )
    )

    assert '"minimum_distinct_images": 5' in prompt
    assert '"minimum_distinct_layouts": 8' in prompt
    assert '"minimum_image_slides": 8' in prompt
    assert '"minimum_picture_coverage_ratio": 0.35' in prompt
    assert '"maximum_uses_per_image": 3' in prompt
    assert '"minimum_editable_compositions": 3' in prompt


def test_video_workflow_compiles_people_led_voiceover_delivery_contract() -> None:
    request = _request(
        work_type="video",
        workflow_id="builtin.video.v1",
        spec={
            "channel": "social",
            "aspect_ratio": "9:16",
            "duration": "10",
            "audience": "都市白领",
            "language": "zh-CN",
            "audio_mode": "voiceover",
            "story": "人物在通勤场景中使用产品",
            "cta": "立即了解更多",
        },
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
    )

    prompt = build_deliverable_prompt(request)

    assert "people-led advertising video" in prompt
    assert "storyboard.json" in prompt
    assert "adult on-camera actor" in prompt
    assert "generate_image_minimax exactly once" in prompt
    assert f"workspace/deliverables/{request.id}/first_frame.png" in prompt
    assert "exact versioned workspace output_path" in prompt
    assert "first_frame_image=<exact returned first-frame output_path>" in prompt
    assert "generate_video_minimax" in prompt
    assert "duration=spec.duration" in prompt
    assert "server locks the current configured formal-tier quality profile" in prompt
    assert "allow_degraded_fallback=false" in prompt
    assert "generate_speech_minimax" in prompt
    assert "compose_video_audio" in prompt
    assert "Call each generation Tool exactly once" in prompt
    assert "scheduling a trigger" in prompt
    assert f"workspace/deliverables/{request.id}/final.mp4" in prompt
    assert prompt.index("generate_image_minimax") < prompt.index("generate_video_minimax")
    assert '"provider"' not in prompt
    assert '"model"' not in prompt


def test_ultra_video_prompt_delegates_quality_to_runtime_platform_profile() -> None:
    request = _request(
        work_type="video",
        workflow_id="builtin.video.v1",
        tier="ultra",
        spec={
            "channel": "social",
            "aspect_ratio": "9:16",
            "duration": "6",
            "audio_mode": "voiceover",
        },
        output_contract=["mp4"],
    )

    prompt = build_deliverable_prompt(request)

    assert "duration=spec.duration" in prompt
    assert "omit the resolution argument" in prompt
    assert "resolution='1080P'" not in prompt


def test_video_credit_estimate_includes_one_managed_first_frame() -> None:
    workflow = require_workflow("video")

    estimate = _credit_estimate(
        workflow,
        "pro",
        {
            "duration": "6",
            "aspect_ratio": "9:16",
        },
    )

    assert estimate == {
        "mode": "estimate",
        "minimum": 284,
        "maximum": 284,
        "billing_unit": "one_first_frame_plus_6s_768p_clip",
    }


@pytest.mark.asyncio
async def test_prepare_launch_enforces_exact_tenant_user_agent_and_session(monkeypatch) -> None:
    request = _request()
    message_id = uuid.uuid4()
    preflight = AsyncMock(return_value={"launchable": True, "reasons": []})
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        preflight,
    )
    prepared = await prepare_deliverable_launch(
        _Session(request),  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=message_id,
    )

    assert prepared.request is request
    assert request.launch_message_id == message_id
    assert request.status == "running"
    assert request.current_stage == "execution_queued"
    assert request.version == 2

    repeated = await prepare_deliverable_launch(
        _Session(request),  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=message_id,
    )
    assert repeated.request is request
    assert request.version == 2
    assert preflight.await_count == 1

    other = _request()
    with pytest.raises(DeliverableWorkflowError, match="not available in this chat"):
        await prepare_deliverable_launch(
            _Session(other),  # type: ignore[arg-type]
            request_id=other.id,
            tenant_id=other.tenant_id,
            user_id=uuid.uuid4(),
            agent_id=other.agent_id,
            session_id=other.session_id,
            message_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_prepare_launch_rechecks_capability_without_mutating_blocked_request(monkeypatch) -> None:
    request = _request()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(
            return_value={
                "launchable": False,
                "reasons": ["presentation_tool_unavailable"],
            }
        ),
    )

    with pytest.raises(DeliverableWorkflowError) as error:
        await prepare_deliverable_launch(
            _Session(request),  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )

    assert error.value.code == "deliverable_preflight_failed"
    assert "presentation_tool_unavailable" in str(error.value)
    assert request.status == "ready"
    assert request.launch_message_id is None
    assert request.version == 1


@pytest.mark.asyncio
async def test_prepare_launch_compiles_prompt_before_mutating_request(monkeypatch) -> None:
    request = _request()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.build_deliverable_prompt",
        lambda _request: (_ for _ in ()).throw(RuntimeError("prompt invalid")),
    )

    with pytest.raises(RuntimeError, match="prompt invalid"):
        await prepare_deliverable_launch(
            _Session(request),  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )

    assert request.status == "ready"
    assert request.launch_message_id is None
    assert request.version == 1


def test_poster_workflow_compiles_one_image_delivery_contract() -> None:
    request = _request(
        work_type="poster",
        workflow_id="builtin.poster.v1",
        spec={"channel": "social", "aspect_ratio": "3:4", "style": "commercial"},
        output_contract=["png"],
    )

    prompt = build_deliverable_prompt(request)

    assert "generate_image_minimax exactly once" in prompt
    assert f"workspace/deliverables/{request.id}/final.png" in prompt
    assert "reserve clean negative space" in prompt
    assert "never ask the image model to spell exact copy" in prompt
    assert '"provider"' not in prompt
    assert '"model"' not in prompt


def test_attach_run_is_idempotent_only_for_the_same_run() -> None:
    request = _request()
    prepared = SimpleNamespace(request=request)
    run_id = uuid.uuid4()
    launched_at = datetime.now(UTC)

    attach_deliverable_run(prepared, run_id=run_id, launched_at=launched_at)
    attach_deliverable_run(prepared, run_id=run_id, launched_at=launched_at)
    assert request.agent_run_id == run_id

    with pytest.raises(DeliverableWorkflowError, match="another run"):
        attach_deliverable_run(prepared, run_id=uuid.uuid4(), launched_at=launched_at)


@pytest.mark.asyncio
async def test_poster_preflight_is_available_and_launchable(monkeypatch) -> None:
    workflow = require_workflow("poster")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {
                    "modality": "image",
                    "available": True,
                    "reason": None,
                    "available_providers": ["volcengine_agent_plan", "minimax"],
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec={"channel": "social", "aspect_ratio": "3:4", "style": "commercial"},
    )

    assert result["available"] is True
    assert result["launchable"] is True
    assert result["reasons"] == []
    assert result["credit_estimate"] == {
        "mode": "estimate",
        "minimum": 4,
        "maximum": 4,
        "billing_unit": "one_final_image",
    }
    assert result["creates_reservation"] is False


@pytest.mark.asyncio
async def test_formal_poster_preflight_requires_confirmation_for_minimax_only_pool(
    monkeypatch,
) -> None:
    workflow = require_workflow("poster")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {
                    "modality": "image",
                    "available": True,
                    "reason": None,
                    "capability_status": "available",
                    "available_providers": ["minimax"],
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec={
            "channel": "social",
            "aspect_ratio": "3:4",
            "style": "commercial",
            "fallback_policy": "primary_only",
        },
    )

    assert result["available"] is True
    assert result["launchable"] is False
    assert result["capability_status"] == "degraded"
    assert result["reasons"] == ["degraded_route_requires_confirmation"]


@pytest.mark.asyncio
async def test_image_led_presentation_preflight_checks_the_image_route_before_launch(
    monkeypatch,
) -> None:
    workflow = require_workflow("presentation")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    media_capabilities = AsyncMock(
        return_value=[
            {
                "modality": "image",
                "available": False,
                "reason": "pool_unavailable",
                "capability_status": "unavailable",
                "next_action": "wait for a provider route",
            },
        ]
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        media_capabilities,
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        goal="制作一份图文并茂的新品发布 PPT",
        spec={
            "audience": "客户",
            "page_count": 8,
            "language": "zh-CN",
            "style": "商业风",
        },
    )

    assert result["available"] is False
    assert result["launchable"] is False
    assert result["capability_status"] == "unavailable"
    assert result["reasons"] == ["pool_unavailable"]
    media_capabilities.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_led_presentation_preflight_requires_explicit_degraded_quality(
    monkeypatch,
) -> None:
    workflow = require_workflow("presentation")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {
                    "modality": "image",
                    "available": True,
                    "reason": None,
                    "capability_status": "degraded",
                    "next_action": "confirm emergency quality",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        goal="制作一份图文并茂的新品发布 PPT",
        spec={
            "audience": "客户",
            "page_count": 8,
            "language": "zh-CN",
            "style": "商业风",
            "fallback_policy": "primary_only",
        },
    )

    assert result["available"] is True
    assert result["launchable"] is False
    assert result["capability_status"] == "degraded"
    assert result["reasons"] == ["degraded_route_requires_confirmation"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_policy", "expected_launchable", "expected_reasons"),
    [
        ("primary_only", False, ["degraded_route_requires_confirmation"]),
        ("allow_degraded", True, []),
    ],
)
async def test_visual_preflight_requires_explicit_confirmation_for_degraded_route(
    monkeypatch,
    fallback_policy,
    expected_launchable,
    expected_reasons,
) -> None:
    workflow = require_workflow("poster")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {
                    "modality": "image",
                    "available": True,
                    "reason": None,
                    "capability_status": "degraded",
                    "next_action": "confirm emergency quality",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec={
            "channel": "social",
            "aspect_ratio": "3:4",
            "style": "commercial",
            "fallback_policy": fallback_policy,
        },
    )

    assert result["available"] is True
    assert result["launchable"] is expected_launchable
    assert result["capability_status"] == "degraded"
    assert result["reasons"] == expected_reasons


def test_explicit_degraded_policy_is_compiled_into_formal_tool_contract() -> None:
    request = _request(
        work_type="poster",
        workflow_id="builtin.poster.v1",
        spec={
            "channel": "social",
            "aspect_ratio": "3:4",
            "style": "commercial",
            "fallback_policy": "allow_degraded",
        },
        output_contract=["png"],
    )

    assert "allow_degraded_fallback=true" in build_deliverable_prompt(request)
    assert "execution_strategy='commercial_quality'" in build_deliverable_prompt(request)
    assert "never overlay the same copy again" in build_deliverable_prompt(request)


def test_formal_poster_prompt_compiles_persisted_exact_copy_blocks() -> None:
    from app.services.poster_contract import poster_exact_copy_contract

    request = _request(
        work_type="poster",
        workflow_id="builtin.poster.v1",
        spec={
            "channel": "social",
            "aspect_ratio": "9:16",
            "style": "commercial",
            "exact_copy": "量化交易平台\n智能策略・实时信号\n从复杂市场中捕捉方向\n立即体验",
        },
        output_contract=["png"],
    )
    blocks, digest = poster_exact_copy_contract(request.spec)
    prompt = build_deliverable_prompt(request)

    assert [block["role"] for block in blocks] == [
        "title",
        "subtitle",
        "tagline",
        "cta",
    ]
    assert all(block["text"] in prompt for block in blocks)
    assert digest in prompt
    assert "server will reject any mismatch before a paid provider request" in prompt
    assert '"execution_strategy": "commercial_quality"' in prompt
    assert '"allow_degraded_fallback": false' in prompt
    assert "do not simplify a detailed brief into a flat gradient" in prompt


def test_formal_poster_detects_cta_before_company_footer() -> None:
    from app.services.poster_contract import poster_exact_copy_contract

    blocks, _digest = poster_exact_copy_contract(
        {
            "exact_copy": (
                "把 AI 公司真正运行起来\n"
                "数字员工 · 任务协作 · 成果审核\n"
                "从需求到商业成果，完整闭环\n"
                "立即体验 ReefTotem OPC\n"
                "深圳前海瑞孚图腾科技有限公司"
            )
        }
    )

    assert [block["role"] for block in blocks] == [
        "title",
        "subtitle",
        "tagline",
        "cta",
        "body",
    ]


@pytest.mark.asyncio
async def test_video_preflight_requires_the_first_frame_image_route(monkeypatch) -> None:
    workflow = require_workflow("video")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {
                    "modality": "video",
                    "available": True,
                    "capability_status": "available",
                },
                {
                    "modality": "image",
                    "available": False,
                    "reason": "image_pool_unavailable",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec={
            "channel": "social",
            "aspect_ratio": "9:16",
            "duration": "6",
            "audience": "都市白领",
            "language": "zh-CN",
            "audio_mode": "silent",
            "story": "真人使用产品",
            "fallback_policy": "primary_only",
        },
    )

    assert result["launchable"] is False
    assert result["available"] is False
    assert result["reasons"] == [
        "video_first_frame_image_capability_unavailable"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle_status", "expected_status", "expected_error"),
    [
        ("completed", "waiting_approval", None),
        ("failed", "failed", "slide_render_failed"),
        ("cancelled", "cancelled", None),
    ],
)
async def test_runtime_terminal_state_closes_the_linked_deliverable(
    lifecycle_status: str,
    expected_status: str,
    expected_error: str | None,
    monkeypatch,
) -> None:
    request = _request(status="running", current_stage="running", agent_run_id=uuid.uuid4())
    if lifecycle_status in {"completed", "cancelled"}:
        monkeypatch.setattr(
            "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
            AsyncMock(
                return_value=SimpleNamespace(
                    complete=lifecycle_status == "completed",
                    missing_types=(() if lifecycle_status == "completed" else ("pdf",)),
                    invalid_types=(),
                    unavailable_types=(),
                )
            ),
        )
    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status=lifecycle_status,
        lifecycle={"error": {"code": "slide_render_failed"}},
    )

    assert result is request
    assert request.status == expected_status
    expected_stage = "output_review" if expected_status == "waiting_approval" else expected_status
    assert request.current_stage == expected_stage
    assert request.last_error_code == expected_error
    assert (request.completed_at is None) is (expected_status == "waiting_approval")
    assert request.version == 2

    repeated = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status=lifecycle_status,
        lifecycle={"error": {"code": "slide_render_failed"}},
    )
    assert repeated is request
    assert request.version == 2


@pytest.mark.asyncio
async def test_cancelled_creative_request_can_recover_when_artifact_settles_late(
    monkeypatch,
) -> None:
    request = _request(
        work_type="poster",
        workflow_id="builtin.poster.v1",
        spec={"channel": "social", "aspect_ratio": "16:9", "style": "commercial"},
        output_contract=["png"],
        status="cancelled",
        current_stage="cancelled",
        agent_run_id=uuid.uuid4(),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=True,
                missing_types=(),
                invalid_types=(),
                unavailable_types=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="cancelled",
        lifecycle={},
    )

    assert result is request
    assert request.status == "waiting_approval"
    assert request.current_stage == "output_review"
    assert request.completed_at is None


@pytest.mark.asyncio
async def test_cancelled_runtime_keeps_verified_artifacts_reviewable(monkeypatch) -> None:
    request = _request(
        status="running",
        current_stage="running",
        agent_run_id=uuid.uuid4(),
        work_type="presentation",
        output_contract=["pptx", "pdf"],
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=True,
                missing_types=(),
                invalid_types=(),
                unavailable_types=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="cancelled",
    )

    assert result is request
    assert request.status == "waiting_approval"
    assert request.current_stage == "output_review"
    assert request.last_error_code is None
    assert request.completed_at is None


@pytest.mark.asyncio
async def test_followup_run_recovers_failed_deliverable_from_same_session(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    followup_run_id = uuid.uuid4()
    request = _request(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        status="failed",
        current_stage="artifact_verification_failed",
        last_error_code="deliverable_artifact_missing",
        agent_run_id=uuid.uuid4(),
    )
    followup_run = AgentRun(
        id=followup_run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        source_type="chat",
        goal="Repair the missing PDF",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=f"direct:{session_id}",
        graph_name="agent_runtime",
        graph_version="1",
        model_turn_limit=50,
        delivery_status="pending",
    )
    reconcile = AsyncMock(
        return_value=SimpleNamespace(
            complete=True,
            missing_types=(),
            invalid_types=(),
            unavailable_types=(),
            attempted_types=("pdf",),
            created_types=("pdf",),
            failure_codes=(),
        )
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        reconcile,
    )

    result = await sync_deliverable_lifecycle(
        _SequenceSession(None, followup_run, [request]),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        run_id=followup_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "waiting_approval"
    assert request.current_stage == "output_review"
    assert request.last_error_code is None
    assert request.completed_at is None
    reconcile.assert_awaited_once_with(
        ANY,
        request=request,
        run_id=followup_run_id,
    )


@pytest.mark.asyncio
async def test_followup_run_registers_new_revision_for_waiting_approval_request(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    followup_run_id = uuid.uuid4()
    request = _request(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        status="waiting_approval",
        current_stage="output_review",
        agent_run_id=uuid.uuid4(),
        version=4,
    )
    followup_run = AgentRun(
        id=followup_run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        source_type="chat",
        goal="Repair presentation layout",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=f"direct:{session_id}",
        graph_name="agent_runtime",
        graph_version="1",
        model_turn_limit=50,
        delivery_status="pending",
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=True,
                missing_types=(),
                invalid_types=(),
                unavailable_types=(),
                attempted_types=("pptx", "pdf"),
                created_types=("pptx", "pdf"),
                failure_codes=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _SequenceSession(None, followup_run, [request]),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        run_id=followup_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "waiting_approval"
    assert request.current_stage == "output_review"
    assert request.version == 5


@pytest.mark.asyncio
async def test_followup_failed_render_closes_previous_review_state(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    followup_run_id = uuid.uuid4()
    request = _request(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        status="waiting_approval",
        current_stage="output_review",
        agent_run_id=uuid.uuid4(),
        version=4,
    )
    followup_run = AgentRun(
        id=followup_run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        source_type="chat",
        goal="Repair presentation layout",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=f"direct:{session_id}",
        graph_name="agent_runtime",
        graph_version="1",
        model_turn_limit=50,
        delivery_status="pending",
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=False,
                missing_types=("pptx",),
                invalid_types=("pdf",),
                unavailable_types=(),
                attempted_types=("pptx", "pdf"),
                created_types=(),
                failure_codes=(("pdf", "presentation_visual_quality_failed"),),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _SequenceSession(None, followup_run, [request]),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        run_id=followup_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "failed"
    assert request.current_stage == "artifact_verification_failed"
    assert request.last_error_code == "presentation_visual_quality_failed"
    assert request.completed_at is not None
    assert request.version == 5


@pytest.mark.asyncio
async def test_unrelated_followup_run_does_not_mutate_deliverable(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    followup_run_id = uuid.uuid4()
    request = _request(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        status="waiting_approval",
        current_stage="output_review",
        agent_run_id=uuid.uuid4(),
        version=4,
    )
    followup_run = AgentRun(
        id=followup_run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        source_type="chat",
        goal="Answer a normal question",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=f"direct:{session_id}",
        graph_name="agent_runtime",
        graph_version="1",
        model_turn_limit=50,
        delivery_status="pending",
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=True,
                missing_types=(),
                invalid_types=(),
                unavailable_types=(),
                attempted_types=(),
                created_types=(),
                failure_codes=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _SequenceSession(None, followup_run, [request]),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        run_id=followup_run_id,
        lifecycle_status="completed",
    )

    assert result is None
    assert request.status == "waiting_approval"
    assert request.version == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_type", "output_contract", "missing_types"),
    [
        ("presentation", ["pptx", "pdf"], ("pdf",)),
        ("video", ["mp4"], ("mp4",)),
    ],
)
async def test_completed_runtime_fails_deliverable_when_required_artifact_is_missing(
    monkeypatch,
    work_type: str,
    output_contract: list[str],
    missing_types: tuple[str, ...],
) -> None:
    request = _request(
        status="running",
        current_stage="running",
        agent_run_id=uuid.uuid4(),
        work_type=work_type,
        output_contract=output_contract,
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=False,
                missing_types=missing_types,
                invalid_types=(),
                unavailable_types=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "failed"
    assert request.current_stage == "artifact_verification_failed"
    assert request.last_error_code == "deliverable_artifact_missing"
    assert request.completed_at is not None


@pytest.mark.asyncio
async def test_runtime_replay_never_regresses_an_approved_deliverable(monkeypatch) -> None:
    request = _request(
        status="succeeded",
        current_stage="delivered",
        agent_run_id=uuid.uuid4(),
        version=3,
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        reconcile,
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "succeeded"
    assert request.current_stage == "delivered"
    assert request.version == 3
    reconcile.assert_not_awaited()
