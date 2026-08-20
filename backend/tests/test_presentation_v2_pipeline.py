"""FR-P1~P5/P8 pipeline tests: brief, outline gate, semantic QA, rollout."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.api import deliverables
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableCreativeBrief,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.schemas.deliverable import DeliverableApprovalIn
from app.services import deliverable_workflows
from app.services.creative_briefs import (
    PRESENTATION_BRIEF_SCHEMA_VERSION,
    PRESENTATION_V2_WORKFLOW_ID,
    compile_presentation_brief,
)
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    build_deliverable_prompt,
    list_agent_launchable_workflows,
    preflight_workflow,
    prepare_deliverable_launch,
    require_workflow,
    validate_workflow_spec,
)
from app.services.media_assets import MediaContractError
from app.services.presentation_pipeline import (
    DeckOutlineError,
    advance_presentation_v2_after_run,
    compile_deck_outline,
    parse_slide_spec_slides,
    resolve_presentation_v2_image_gate,
    resolve_presentation_v2_render_gate,
)


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, *execute_values: object | None) -> None:
        self.execute_values = list(execute_values)
        self.added: list[object] = []
        self.flush_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1

    async def get(self, _model, _key):
        return None

    def add(self, value: object) -> None:
        self.added.append(value)


class _FakeStorage:
    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = dict(payloads or {})
        self.writes: dict[str, bytes] = {}

    async def write_bytes(self, key: str, data: bytes, content_type=None):
        self.writes[key] = data

    async def read_bytes(self, key: str) -> bytes:
        merged = {**self.payloads, **self.writes}
        for suffix, payload in merged.items():
            if key.endswith(suffix):
                return payload
        raise FileNotFoundError(key)

    async def read_text(self, key: str, **_kwargs) -> str:
        return (await self.read_bytes(key)).decode("utf-8")


def _spec(**overrides) -> dict:
    spec = {
        "audience": "潜在客户采购负责人",
        "scenario": "client_proposal",
        "page_count": 8,
        "language": "zh-CN",
        "style": "professional",
        "key_points": "保温时长 24 小时\n内胆采用 316 不锈钢",
        "editability_contract": "editable",
        "fallback_policy": "primary_only",
    }
    spec.update(overrides)
    return spec


def _request(*, spec: dict | None = None, **overrides) -> DeliverableRequest:
    request = DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        work_type="presentation",
        workflow_id=PRESENTATION_V2_WORKFLOW_ID,
        workflow_version="2.0.0",
        goal="为极光保温杯制作客户提案演示",
        inputs=[],
        spec=spec if spec is not None else _spec(),
        tier="pro",
        approval_policy=["outline", "final"],
        output_contract=["pptx"],
        status="ready",
        current_stage="brief_confirmed",
        version=1,
        contract_revision=1,
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def _execution(request: DeliverableRequest, *, kind: str = "initial") -> DeliverableExecution:
    execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=1,
        kind=kind,
        status="ready",
        current_stage="brief_confirmed",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=request.client_request_id,
        request_fingerprint="b" * 64,
    )
    request.current_execution_id = execution.id
    return execution


def _unit(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    stage_key: str,
    unit_key: str,
    *,
    status: str = "pending",
) -> DeliverableExecutionUnit:
    return DeliverableExecutionUnit(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        stage_key=stage_key,
        unit_key=unit_key,
        status=status,
        dependency_hash="c" * 64,
        attempt_count=0,
        input_snapshot={},
        result_snapshot={},
        quality_evaluation={},
    )


def _brief(**overrides):
    brief, missing = compile_presentation_brief(
        "为极光保温杯制作客户提案演示",
        _spec(**overrides),
        [],
        output_contract=("pptx",),
    )
    assert missing == ()
    assert brief is not None
    return brief


def _outline_payload(page_count: int = 8) -> dict:
    return {
        "deck_title": "极光保温杯客户提案",
        "audience": "潜在客户采购负责人",
        "core_message": "用可验证的保温数据赢得采购",
        "one_sentence_claim": "极光保温杯以可验证的 24 小时保温赢得采购信任",
        "storyline": ["痛点", "证据", "方案", "行动"],
        "slides": [
            {
                "slide_id": f"slide-{index:02d}",
                "purpose": f"第 {index} 页目的",
                "headline": f"第 {index} 页标题",
                "evidence": ["保温时长 24 小时"],
                "visual_intent": "图表或排版",
            }
            for index in range(1, page_count + 1)
        ],
    }


def _slide_spec_payload(page_count: int = 8, **slide_overrides) -> dict:
    layouts = ["cover", "metrics", "split", "process", "matrix", "timeline", "table", "closing"]
    slides = []
    for index in range(1, page_count + 1):
        slide = {
            "slide_id": f"slide-{index:02d}",
            "headline": f"第 {index} 页标题",
            "layout": layouts[(index - 1) % len(layouts)],
            "body_points": ["保温时长 24 小时"],
            "visual_asset": "图表",
            "source_refs": ["src-01"],
            "slide_type": "content",
            "visual_kind": "editable_chart",
            "asset_ref": "",
        }
        if index == 2:
            slide.update(slide_overrides)
        slides.append(slide)
    return {
        "visual_plan_version": "adaptive-v1",
        "visual_policy": {
            "minimum_distinct_layouts": 4,
            "minimum_distinct_images": 0,
            "minimum_image_slides": 0,
            "maximum_uses_per_image": 0,
            "minimum_editable_compositions": 2,
        },
        "slides": slides,
    }


# ─── FR-P1 brief ────────────────────────────────────────────────


def test_presentation_brief_compiles_complete_spec() -> None:
    brief = _brief()
    assert brief.page_count == 8
    assert brief.language == "zh-CN"
    assert brief.editability_contract == "editable"
    assert brief.required_points == ("保温时长 24 小时", "内胆采用 316 不锈钢")
    assert brief.output_contract == ("pptx",)

    visual = _brief(editability_contract="visual_fidelity")
    assert visual.editability_contract == "visual_fidelity"


def test_presentation_brief_reports_missing_and_never_invents() -> None:
    brief, missing = compile_presentation_brief("", {"style": "professional"}, [])
    assert brief is None
    for field in ("purpose", "audience", "scenario", "page_count", "language", "key_points"):
        assert field in missing

    brief, missing = compile_presentation_brief("goal", _spec(page_count=99), [])
    assert brief is None
    assert "page_count" in missing

    brief, missing = compile_presentation_brief("goal", _spec(editability_contract="fancy"), [])
    assert brief is None
    assert "editability_contract" in missing


# ─── manifest + rollout gating ──────────────────────────────────


def test_presentation_v2_manifest_registered_and_v1_untouched() -> None:
    assert (
        deliverable_workflows.WORKFLOW_BY_TYPE["presentation"].workflow_id
        == "builtin.presentation.v1"
    )
    v1 = require_workflow("presentation", "builtin.presentation.v1", "1.0.0")
    v2 = require_workflow("presentation", "builtin.presentation.v2", "2.0.0")
    # v1 keeps its exact field contract; v2 adds the new brief elements.
    assert [field.key for field in v1.fields] == [
        "audience",
        "page_count",
        "language",
        "style",
        "key_points",
        "fallback_policy",
    ]
    assert v2.approval_policy == ["outline", "final"]
    assert v2.output_contract == ["pptx"]
    v2_keys = {field.key for field in v2.fields}
    assert {"scenario", "brand_theme", "source_urls", "editability_contract"} <= v2_keys

    normalized = validate_workflow_spec(v2, _spec(editability_contract="hybrid"))
    assert normalized["editability_contract"] == "hybrid"
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v1, _spec())
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v2, _spec(editability_contract="pixel_perfect"))


@pytest.mark.asyncio
async def test_launchable_workflows_follow_the_presentation_v2_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._video_post_production_tools_available",
        AsyncMock(return_value=True),
    )
    allowed = {"value": False}
    stage_gate = {"value": False}
    monkeypatch.setattr(
        "app.services.deliverable_workflows.presentation_v2_workflow_allowed",
        lambda tenant_id, agent_id: allowed["value"],
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.deliverable_stage_approvals_enabled",
        lambda: stage_gate["value"],
    )

    listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    ids = [workflow.workflow_id for workflow in listing]
    # Rollout off: the v1 presentation contract is the only one listed.
    assert "builtin.presentation.v1" in ids
    assert PRESENTATION_V2_WORKFLOW_ID not in ids

    allowed["value"] = True
    listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    ids = [workflow.workflow_id for workflow in listing]
    assert "builtin.presentation.v1" in ids
    assert PRESENTATION_V2_WORKFLOW_ID not in ids

    stage_gate["value"] = True
    listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    ids = [workflow.workflow_id for workflow in listing]
    assert PRESENTATION_V2_WORKFLOW_ID in ids
    assert "builtin.presentation.v1" not in ids


# ─── preflight: allowlist + brief clarification seam ────────────


def _mock_presentation_preflight(monkeypatch, *, allowed: bool = True) -> None:
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.presentation_v2_workflow_allowed",
        lambda tenant_id, agent_id: allowed,
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.deliverable_stage_approvals_enabled",
        lambda: True,
    )


@pytest.mark.asyncio
async def test_presentation_v2_preflight_gates_allowlist_and_brief(monkeypatch) -> None:
    workflow = require_workflow("presentation", PRESENTATION_V2_WORKFLOW_ID, "2.0.0")
    _mock_presentation_preflight(monkeypatch, allowed=False)
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(),
        goal="为极光保温杯制作客户提案演示",
    )
    assert result["launchable"] is False
    assert "deliverable_presentation_v2_not_allowlisted" in result["reasons"]

    _mock_presentation_preflight(monkeypatch, allowed=True)
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(),
        goal="为极光保温杯制作客户提案演示",
    )
    assert result["launchable"] is True
    assert result["creative_brief"]["status"] == "confirmed"
    assert result["creative_brief"]["schema_version"] == PRESENTATION_BRIEF_SCHEMA_VERSION

    # The manifest fail-closes missing required spec fields before the brief
    # compiler runs; the brief seam reports conditional gaps it cannot see.
    spec = _spec()
    del spec["key_points"]
    with pytest.raises(DeliverableWorkflowError, match="key_points"):
        validate_workflow_spec(workflow, spec)


# ─── outline compile ────────────────────────────────────────────


def test_deck_outline_compiles_and_is_hash_stable() -> None:
    brief = _brief()
    first = compile_deck_outline(brief, _outline_payload())
    second = compile_deck_outline(brief, _outline_payload())
    assert first == second
    assert len(first.slides) == 8
    assert first.slides[0].slide_id == "slide-01"
    assert len(first.outline_sha256) == 64


def test_deck_outline_rejects_drift() -> None:
    brief = _brief()
    with pytest.raises(DeckOutlineError, match="exactly 8 slides, found 7"):
        compile_deck_outline(brief, _outline_payload(7))
    payload = _outline_payload()
    payload["slides"][1]["slide_id"] = "slide-03"
    with pytest.raises(DeckOutlineError, match="slide-01..slide-08"):
        compile_deck_outline(brief, payload)
    missing_claim = {key: value for key, value in _outline_payload().items() if key != "one_sentence_claim"}
    with pytest.raises(DeckOutlineError, match="one_sentence_claim"):
        compile_deck_outline(brief, missing_claim)
    no_storyline = {**_outline_payload(), "storyline": []}
    with pytest.raises(DeckOutlineError, match="storyline"):
        compile_deck_outline(brief, no_storyline)


def test_parse_slide_spec_fail_closed() -> None:
    slides = parse_slide_spec_slides(_slide_spec_payload(), expected_page_count=8)
    assert len(slides) == 8
    with pytest.raises(DeckOutlineError, match="exactly 8 slides"):
        parse_slide_spec_slides(_slide_spec_payload(7), expected_page_count=8)
    bad = _slide_spec_payload()
    bad["slides"][0]["source_refs"] = "src-01"
    with pytest.raises(DeckOutlineError, match="source_refs must be an array"):
        parse_slide_spec_slides(bad, expected_page_count=8)


# ─── launch + continuation orchestration ────────────────────────


@pytest.mark.asyncio
async def test_first_launch_drafts_outline_without_paid_work(monkeypatch) -> None:
    request = _request()
    execution = _execution(request)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(return_value={"launchable": True, "reasons": []}),
    )
    db = _Session(
        request,
        execution,
        None,  # brief row lookup for the inventory projection
    )
    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )
    assert request.status == "running"
    assert request.current_stage == "outline_draft"
    assert "outline.json" in prepared.prompt
    assert "SOURCE_INVENTORY=" in prepared.prompt
    assert "call no generation or conversion Tool" in prepared.prompt
    # No prompt compilation receipts are written before outline approval.
    assert not any(isinstance(item, DeliverablePromptCompilation) for item in db.added)


@pytest.mark.asyncio
async def test_outline_revision_relaunches_planning_even_with_stale_targets(monkeypatch) -> None:
    request = _request()
    execution = _execution(request, kind="revision")
    execution.contract_snapshot = {
        "revision_stage": "outline",
        "target_units": ["slide-02"],
    }
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(return_value={"launchable": True, "reasons": []}),
    )
    db = _Session(request, execution, None)

    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )

    assert request.current_stage == "outline_draft"
    assert "call no generation or conversion Tool" in prepared.prompt
    assert "slide_revision" not in prepared.prompt
    assert not db.added


@pytest.mark.asyncio
async def test_continuation_requires_outline_approval(monkeypatch) -> None:
    request = _request(
        status="ready",
        current_stage="outline_approved",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.outline_approved",
        AsyncMock(return_value=False),
    )
    db = _Session(request, execution)
    with pytest.raises(DeliverableWorkflowError, match="must be approved before any rendering"):
        await prepare_deliverable_launch(
            db,  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )
    assert not db.added


@pytest.mark.asyncio
async def test_approved_continuation_enters_slide_render(monkeypatch) -> None:
    request = _request(
        status="ready",
        current_stage="outline_approved",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    outline_unit = _unit(request, execution, "outline", "deck", status="succeeded")
    outline_unit.result_snapshot = {
        "outline": compile_deck_outline(_brief(), _outline_payload()).model_dump(mode="json")
    }
    db = _Session(
        request,
        execution,
        SimpleNamespace(),  # outline approval receipt
        outline_unit,  # load_latest_outline
        None,          # brief row lookup for the inventory projection
    )
    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )
    assert request.status == "running"
    assert request.current_stage == "slide_render"
    assert "convert_html_to_pptx" in prepared.prompt
    assert "render_mode='hybrid_editable'" in prepared.prompt

    visual_request = _request(
        spec=_spec(editability_contract="visual_fidelity"),
        status="ready",
        current_stage="outline_approved",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    visual_execution = _execution(visual_request)
    visual_outline_unit = _unit(visual_request, visual_execution, "outline", "deck", status="succeeded")
    visual_outline_unit.result_snapshot = outline_unit.result_snapshot
    visual_db = _Session(
        visual_request,
        visual_execution,
        SimpleNamespace(),  # outline approval receipt
        visual_outline_unit,
        None,
    )
    visual_prepared = await prepare_deliverable_launch(
        visual_db,  # type: ignore[arg-type]
        request_id=visual_request.id,
        tenant_id=visual_request.tenant_id,
        user_id=visual_request.created_by_user_id,
        agent_id=visual_request.agent_id,
        session_id=visual_request.session_id,
        message_id=uuid.uuid4(),
    )
    assert "render_mode='visual'" in visual_prepared.prompt


@pytest.mark.asyncio
async def test_outline_draft_resume_after_intake_run_crash(monkeypatch) -> None:
    request = _request(
        status="running",
        current_stage="outline_draft",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    execution.intake_run_id = uuid.uuid4()
    db = _Session(request, execution, None)
    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )
    assert prepared.execution is execution
    assert "outline.json" in prepared.prompt
    assert request.current_stage == "outline_draft"
    assert request.agent_run_id is None


# ─── outline stage terminal projection ──────────────────────────


@pytest.mark.asyncio
async def test_outline_draft_terminal_projection_parks_for_review() -> None:
    request = _request(status="running", current_stage="outline_draft")
    execution = _execution(request)
    units = [
        _unit(request, execution, "source_inventory", "deck"),
        _unit(request, execution, "outline", "deck"),
        _unit(request, execution, "slide_spec", "deck"),
        _unit(request, execution, "slide_render", "slide-01"),
        _unit(request, execution, "semantic_qa", "deck"),
    ]
    storage = _FakeStorage(
        {
            "outline.json": json.dumps(_outline_payload(), ensure_ascii=False).encode("utf-8"),
            "slide_spec.json": json.dumps(_slide_spec_payload(), ensure_ascii=False).encode("utf-8"),
        }
    )
    db = _Session(units)
    handled = await advance_presentation_v2_after_run(
        db,  # type: ignore[arg-type]
        request=request,
        run_id=uuid.uuid4(),
        storage=storage,
    )
    assert handled is True
    assert request.status == "waiting_approval"
    assert request.current_stage == "outline_review"
    outline_unit = next(unit for unit in units if unit.stage_key == "outline")
    assert outline_unit.status == "succeeded"
    assert outline_unit.result_snapshot["outline"]["one_sentence_claim"]
    inventory_unit = next(unit for unit in units if unit.stage_key == "source_inventory")
    assert inventory_unit.status == "succeeded"
    assert inventory_unit.result_snapshot["source_inventory"]
    render_unit = next(unit for unit in units if unit.stage_key == "slide_render")
    # Rendering stays pending until the customer approves the outline.
    assert render_unit.status == "pending"


@pytest.mark.asyncio
async def test_outline_draft_terminal_projection_fails_closed() -> None:
    request = _request(status="running", current_stage="outline_draft")
    execution = _execution(request)
    units = [_unit(request, execution, "outline", "deck")]
    db = _Session(units)
    handled = await advance_presentation_v2_after_run(
        db,  # type: ignore[arg-type]
        request=request,
        run_id=uuid.uuid4(),
        storage=_FakeStorage(),
    )
    assert handled is True
    assert request.status == "failed"
    assert request.current_stage == "outline_invalid"
    assert request.last_error_code == "deliverable_outline_missing"

    cancelled = _request(status="running", current_stage="outline_draft")
    _execution(cancelled)
    handled = await advance_presentation_v2_after_run(
        _Session([]),  # type: ignore[arg-type]
        request=cancelled,
        run_id=uuid.uuid4(),
        lifecycle_status="cancelled",
        storage=_FakeStorage(),
    )
    assert handled is True
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_slide_render_stage_records_semantic_qa_and_defers() -> None:
    request = _request(status="running", current_stage="slide_render")
    execution = _execution(request)
    semantic_unit = _unit(request, execution, "semantic_qa", "deck")
    brief_row = DeliverableCreativeBrief(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        modality="presentation",
        schema_version=PRESENTATION_BRIEF_SCHEMA_VERSION,
        status="confirmed",
        brief=_brief().model_dump(mode="json"),
        source_inventory=[],
        missing_fields=[],
        brief_sha256="d" * 64,
    )
    storage = _FakeStorage(
        {
            "slide_spec.json": json.dumps(
                _slide_spec_payload(body_points=["市场占有率 48%，行业第一"]),
                ensure_ascii=False,
            ).encode("utf-8"),
        }
    )
    db = _Session(brief_row, semantic_unit)
    handled = await advance_presentation_v2_after_run(
        db,  # type: ignore[arg-type]
        request=request,
        run_id=uuid.uuid4(),
        storage=storage,
    )
    # The production stage defers to the standard artifact reconciliation.
    assert handled is False
    report = semantic_unit.quality_evaluation["semantic_qa"]
    assert report["schema_version"] == "semantic-qa-v1"
    assert report["status"] == "failed"
    assert len(report["artifact_sha256"]) == 64
    # Shadow mode (the default) never changes unit lifecycle state.
    assert semantic_unit.status == "pending"
    assert semantic_unit.quality_evaluation["enforcement"] == "shadow"


# ─── stage approval API semantics ───────────────────────────────


def _approval_input(request: DeliverableRequest, *, stage: str, action: str) -> DeliverableApprovalIn:
    return DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage=stage,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
    )


def _mock_approval_api(
    monkeypatch,
    request: DeliverableRequest,
    execution: DeliverableExecution,
    *,
    stage_approvals_enabled: bool,
):
    user = SimpleNamespace(id=request.created_by_user_id, tenant_id=request.tenant_id)
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        deliverables,
        "get_settings",
        lambda: SimpleNamespace(
            DELIVERABLE_STAGE_APPROVALS_ENABLED=stage_approvals_enabled,
        ),
    )
    monkeypatch.setattr(
        deliverables,
        "ensure_execution_shadow",
        AsyncMock(return_value=execution),
    )
    monkeypatch.setattr(deliverables, "project_execution_lifecycle", AsyncMock())
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(side_effect=lambda _db, req: req))
    return user


@pytest.mark.asyncio
async def test_v1_presentation_outline_approval_stays_409(monkeypatch) -> None:
    request = _request(
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
        status="waiting_approval",
        current_stage="outline_review",
    )
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="outline", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_stage_approval_not_ready"


@pytest.mark.asyncio
async def test_v2_outline_approval_requires_the_flag(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="outline_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=False)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="outline", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_v2_outline_approval_releases_the_render_gate(monkeypatch) -> None:
    request = _request(
        status="waiting_approval",
        current_stage="outline_review",
        agent_run_id=uuid.uuid4(),
    )
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)  # receipt idempotency lookup: no existing receipt
    result = await deliverables.record_deliverable_approval(
        request.id,
        _approval_input(request, stage="outline", action="approve"),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    assert result is request
    assert request.status == "ready"
    assert request.current_stage == "outline_approved"
    assert request.agent_run_id is None
    receipts = [item for item in db.added if isinstance(item, DeliverableApprovalReceipt)]
    assert len(receipts) == 1
    assert receipts[0].stage == "outline"
    assert receipts[0].action == "approve"


@pytest.mark.asyncio
async def test_v2_outline_gate_requires_the_review_state(monkeypatch) -> None:
    request = _request(status="running", current_stage="outline_draft")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="outline", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_stage_approval_not_ready"


@pytest.mark.asyncio
async def test_outline_revision_records_planning_stage_without_targets(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="outline_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    next_execution = SimpleNamespace(id=uuid.uuid4())
    revision = AsyncMock(return_value=(next_execution, True))
    monkeypatch.setattr(deliverables, "create_revision_execution", revision)
    monkeypatch.setattr(
        deliverables,
        "_supersede_quality_reviews_for_revision",
        AsyncMock(return_value=()),
    )
    db = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="outline",
        action="request_changes",
        instruction="调整第 2 页逻辑，但先重新确认大纲",
    )

    await deliverables.record_deliverable_approval(
        request.id,
        data,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert revision.await_args.kwargs["revision_stage"] == "outline"
    assert revision.await_args.kwargs["target_units"] == []


@pytest.mark.asyncio
async def test_outline_revision_rejects_slide_targets_before_approval(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="outline_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="outline",
        action="request_changes",
        instruction="重新调整大纲",
        target_units=["slide-02"],
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            data,
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "deliverable_revision_target_not_allowed"


# ─── paid-work tool gates ───────────────────────────────────────


@pytest.mark.asyncio
async def test_render_gate_blocks_conversion_before_outline_approval(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="outline_review")
    db = _Session(request, None)  # request lookup, approval receipt lookup
    monkeypatch.setattr("app.services.presentation_pipeline.async_session", lambda: db)
    target = f"workspace/deliverables/{request.id}/final.pptx"
    with pytest.raises(MediaContractError, match="deliverable_outline_approval_required"):
        await resolve_presentation_v2_render_gate(
            agent_id=request.agent_id,
            target_path=target,
            output_format="pptx",
        )


@pytest.mark.asyncio
async def test_render_gate_returns_editability_mode_and_inventory(monkeypatch) -> None:
    request = _request(spec=_spec(editability_contract="visual_fidelity"))
    execution = _execution(request)
    receipt = SimpleNamespace()
    brief_row = DeliverableCreativeBrief(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        modality="presentation",
        schema_version=PRESENTATION_BRIEF_SCHEMA_VERSION,
        status="confirmed",
        brief=_brief(editability_contract="visual_fidelity").model_dump(mode="json"),
        source_inventory=[
            {
                "source_id": "src-01",
                "kind": "brief",
                "path": "",
                "url": "",
                "sha256": "e" * 64,
                "extracted_facts": ["保温时长 24 小时"],
                "registered_by": "user",
            }
        ],
        missing_fields=[],
        brief_sha256="d" * 64,
    )
    db = _Session(request, receipt, brief_row)
    monkeypatch.setattr("app.services.presentation_pipeline.async_session", lambda: db)
    gate = await resolve_presentation_v2_render_gate(
        agent_id=request.agent_id,
        target_path=f"workspace/deliverables/{request.id}/final.pptx",
        output_format="pptx",
    )
    assert gate is not None
    assert gate.expected_render_mode == "visual"
    assert gate.source_inventory_entries[0]["source_id"] == "src-01"

    # Non-v2 requests and unmanaged paths skip the gate entirely.
    v1_request = _request(workflow_id="builtin.presentation.v1", workflow_version="1.0.0")
    v1_db = _Session(v1_request)
    monkeypatch.setattr("app.services.presentation_pipeline.async_session", lambda: v1_db)
    assert await resolve_presentation_v2_render_gate(
        agent_id=v1_request.agent_id,
        target_path=f"workspace/deliverables/{v1_request.id}/final.pptx",
        output_format="pptx",
    ) is None
    assert await resolve_presentation_v2_render_gate(
        agent_id=uuid.uuid4(),
        target_path="workspace/scratch/notes.pptx",
        output_format="pptx",
    ) is None


@pytest.mark.asyncio
async def test_image_gate_blocks_paid_deck_imagery_before_approval(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="outline_review")
    db = _Session(request, None)  # request lookup, approval receipt lookup
    monkeypatch.setattr("app.services.presentation_pipeline.async_session", lambda: db)
    save_path = f"workspace/deliverables/{request.id}/assets/product_hero.png"
    with pytest.raises(MediaContractError, match="deliverable_outline_approval_required"):
        await resolve_presentation_v2_image_gate(
            agent_id=request.agent_id,
            save_path=save_path,
        )

    # v1 decks and non-deck paths are untouched by the gate.
    v1_request = _request(workflow_id="builtin.presentation.v1", workflow_version="1.0.0")
    v1_db = _Session(v1_request)
    monkeypatch.setattr("app.services.presentation_pipeline.async_session", lambda: v1_db)
    assert await resolve_presentation_v2_image_gate(
        agent_id=v1_request.agent_id,
        save_path=f"workspace/deliverables/{v1_request.id}/assets/product_hero.png",
    ) is False
    assert await resolve_presentation_v2_image_gate(
        agent_id=uuid.uuid4(),
        save_path="workspace/scratch/hero.png",
    ) is False


# ─── prompt contract ────────────────────────────────────────────


def test_v2_prompt_stages_and_v1_prompt_untouched() -> None:
    request = _request()
    outline_prompt = build_deliverable_prompt(
        request,
        presentation_v2_stage="outline_draft",
        presentation_v2_source_inventory=[
            {"source_id": "src-01", "kind": "brief", "extracted_facts": ["保温时长 24 小时"]}
        ],
    )
    assert "SOURCE_INVENTORY=" in outline_prompt
    assert "src-01" in outline_prompt
    assert "convert_html_to_pptx" not in outline_prompt
    assert "generate_image_minimax" not in outline_prompt

    render_prompt = build_deliverable_prompt(
        request,
        presentation_v2_stage="slide_render",
    )
    assert "convert_html_to_pptx" in render_prompt
    assert "render_mode='hybrid_editable'" in render_prompt
    assert "data_slide" in render_prompt

    # The v1 presentation prompt keeps its exact historical contract.
    v1_request = _request(
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
    )
    v1_prompt = build_deliverable_prompt(v1_request)
    assert "SOURCE_INVENTORY=" not in v1_prompt
    assert "render_mode='hybrid_editable'" in v1_prompt
    assert "visual_plan_version='adaptive-v1'" in v1_prompt


def test_v2_visual_policy_carries_extended_fields_v1_does_not() -> None:
    v2_request = _request()
    v2_policy = deliverable_workflows._presentation_visual_policy(v2_request, ())
    assert v2_policy["version"] == "adaptive-v1"
    assert v2_policy["minimum_body_font_size_px"] == 16
    assert v2_policy["minimum_contrast_ratio"] == 4.5
    assert v2_policy["data_slide_editability"] == "editable_required"

    v1_request = _request(
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
    )
    v1_policy = deliverable_workflows._presentation_visual_policy(v1_request, ())
    assert "minimum_body_font_size_px" not in v1_policy
    assert "minimum_contrast_ratio" not in v1_policy
