"""FR-V1~V7 pipeline tests: brief, preflight filter, approvals, QA, Credits."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.api import deliverables
from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.schemas.deliverable import DeliverableApprovalIn
from app.services import deliverable_workflows
from app.services.candidate_qa import evaluate_video_shot, evaluate_video_v2_package
from app.services.creative_briefs import compile_video_brief
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    build_deliverable_prompt,
    list_agent_launchable_workflows,
    preflight_workflow,
    prepare_deliverable_launch,
    require_workflow,
    validate_workflow_spec,
)
from app.services.storyboard import (
    compile_storyboard,
    video_v2_credit_reconciliation,
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
        "channel": "social",
        "aspect_ratio": "9:16",
        "duration": "10",
        "audience": "25-35 岁都市白领",
        "language": "zh-CN",
        "style": "commercial",
        "audio_mode": "voiceover",
        "story": "成年演员在厨房台面拿起极光保温杯，倒水、旋盖、递给同事",
        "cta": "立即了解更多",
        "shot_count": 2,
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
        work_type="video",
        workflow_id="builtin.video.v2",
        workflow_version="2.0.0",
        goal="为新款极光保温杯制作抖音投放短视频",
        inputs=[],
        spec=spec if spec is not None else _spec(),
        tier="pro",
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
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
    brief, missing = compile_video_brief(
        "为新款极光保温杯制作抖音投放短视频",
        _spec(**overrides),
        [],
        tier="pro",
    )
    assert missing == ()
    assert brief is not None
    return brief


def _approved_storyboard(shots: int = 2):
    return compile_storyboard(
        _brief(),
        {
            "shots": [
                {
                    "shot_id": f"shot-{index:02d}",
                    "duration_seconds": 5,
                    "visual": f"镜头 {index} 的画面描述",
                    "camera": "固定机位",
                    "dialogue": "",
                    "caption": f"字幕 {index}",
                    "transition": "cut",
                }
                for index in range(1, shots + 1)
            ],
            "voiceover_script": "极光保温杯，温暖一整天。",
        },
        expected_shot_count=shots,
    )


# ─── FR-V1 brief ────────────────────────────────────────────────


def test_video_brief_compiles_complete_spec() -> None:
    brief = _brief()
    assert brief.audio_mode == "voiceover"
    assert brief.duration_seconds == 10
    assert brief.language == "zh-CN"


def test_video_brief_reports_missing_and_never_invents() -> None:
    brief, missing = compile_video_brief("", {"style": "commercial"}, [])
    assert brief is None
    for field in ("purpose", "channel", "audience", "aspect_ratio", "language", "story", "duration", "audio_mode"):
        assert field in missing

    brief, missing = compile_video_brief(
        "goal",
        _spec(audio_mode="in_scene_dialogue"),
        [],
    )
    # In-scene dialogue without a script is a missing element, never padded.
    assert brief is None
    assert "dialogue_script" in missing

    brief, missing = compile_video_brief("goal", _spec(audio_mode="surround"), [])
    assert brief is None
    assert "audio_mode" in missing


# ─── manifest + rollout gating ──────────────────────────────────


def test_video_v2_manifest_registered_and_v1_untouched() -> None:
    assert deliverable_workflows.WORKFLOW_BY_TYPE["video"].workflow_id == "builtin.video.v1"
    v2 = require_workflow("video", "builtin.video.v2", "2.0.0")
    v1 = require_workflow("video", "builtin.video.v1", "1.0.0")
    v1_audio = next(field for field in v1.fields if field.key == "audio_mode")
    v2_audio = next(field for field in v2.fields if field.key == "audio_mode")
    # v1 keeps the two-option contract; v2 adds in-scene dialogue.
    assert v1_audio.options == ["voiceover", "silent"]
    assert v2_audio.options == ["in_scene_dialogue", "voiceover", "silent"]
    assert v2.approval_policy == ["storyboard", "final"]

    normalized = validate_workflow_spec(v2, _spec(audio_mode="in_scene_dialogue", dialogue_script="台词"))
    assert normalized["audio_mode"] == "in_scene_dialogue"
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v1, _spec(audio_mode="in_scene_dialogue"))
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v2, {**_spec(), "shot_count": 99})


@pytest.mark.asyncio
async def test_launchable_workflows_follow_the_video_v2_allowlist(monkeypatch) -> None:
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
        "app.services.deliverable_workflows.video_v2_workflow_allowed",
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
    assert "builtin.video.v1" in ids
    assert "builtin.video.v2" not in ids

    allowed["value"] = True
    listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    ids = [workflow.workflow_id for workflow in listing]
    # A partial rollout must keep v1 visible rather than exposing a v2 flow
    # that will dead-end at its mandatory storyboard approval.
    assert "builtin.video.v1" in ids
    assert "builtin.video.v2" not in ids

    stage_gate["value"] = True
    listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    ids = [workflow.workflow_id for workflow in listing]
    # Exactly one video workflow is ever listed for a canary account.
    assert "builtin.video.v2" in ids
    assert "builtin.video.v1" not in ids


# ─── preflight: brief gate + audio route filter + credits ───────


def _mock_video_preflight_capability(monkeypatch, video_providers) -> None:
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
                    "capability_status": "available",
                    "next_action": "ok",
                },
                {
                    "modality": "video",
                    "available": True,
                    "reason": None,
                    "available_providers": list(video_providers),
                    "capability_status": "available",
                    "next_action": "ok",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._video_post_production_tools_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.video_v2_workflow_allowed",
        lambda tenant_id, agent_id: True,
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.deliverable_stage_approvals_enabled",
        lambda: True,
    )


@pytest.mark.asyncio
async def test_in_scene_dialogue_requires_a_native_audio_route(monkeypatch) -> None:
    _mock_video_preflight_capability(monkeypatch, ["minimax"])
    workflow = require_workflow("video", "builtin.video.v2", "2.0.0")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(audio_mode="in_scene_dialogue", dialogue_script="这杯子保温一整天。"),
        goal="为新款极光保温杯制作抖音投放短视频",
    )
    # MiniMax Hailuo has no native audio track: the request must never launch
    # promising in-scene dialogue it cannot honor.
    assert result["launchable"] is False
    assert result["capability_status"] == "unavailable"
    assert "audio_mode_route_mismatch" in result["reasons"]


@pytest.mark.asyncio
async def test_video_v2_preflight_requires_the_stage_approval_gate(monkeypatch) -> None:
    _mock_video_preflight_capability(monkeypatch, ["minimax"])
    monkeypatch.setattr(
        "app.services.deliverable_workflows.deliverable_stage_approvals_enabled",
        lambda: False,
    )
    workflow = require_workflow("video", "builtin.video.v2", "2.0.0")

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(),
        goal="为新款极光保温杯制作抖音投放短视频",
    )

    assert result["available"] is False
    assert result["launchable"] is False
    assert "deliverable_stage_approvals_disabled" in result["reasons"]
    assert "阶段审批" in result["next_action"]


@pytest.mark.asyncio
async def test_in_scene_dialogue_launchable_with_native_audio_route(monkeypatch) -> None:
    _mock_video_preflight_capability(monkeypatch, ["volcengine_agent_plan", "minimax"])
    workflow = require_workflow("video", "builtin.video.v2", "2.0.0")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(audio_mode="in_scene_dialogue", dialogue_script="这杯子保温一整天。"),
        goal="为新款极光保温杯制作抖音投放短视频",
    )
    assert "audio_mode_route_mismatch" not in result["reasons"]
    assert result["launchable"] is True
    assert result["creative_brief"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_voiceover_v2_stays_launchable_on_minimax_only(monkeypatch) -> None:
    _mock_video_preflight_capability(monkeypatch, ["minimax"])
    workflow = require_workflow("video", "builtin.video.v2", "2.0.0")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(),
        goal="为新款极光保温杯制作抖音投放短视频",
    )
    assert "audio_mode_route_mismatch" not in result["reasons"]
    assert result["launchable"] is True
    estimate = result["credit_estimate"]
    assert estimate["shots"] == 2
    assert estimate["minimum"] == estimate["maximum"]
    assert estimate["minimum"] == estimate["per_shot_credits"] * 2
    assert estimate["per_shot_keyframe_credits"] > 0


@pytest.mark.asyncio
async def test_video_v2_preflight_gates_allowlist_and_brief(monkeypatch) -> None:
    _mock_video_preflight_capability(monkeypatch, ["minimax"])
    monkeypatch.setattr(
        "app.services.deliverable_workflows.video_v2_workflow_allowed",
        lambda tenant_id, agent_id: False,
    )
    workflow = require_workflow("video", "builtin.video.v2", "2.0.0")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_spec(),
        goal="为新款极光保温杯制作抖音投放短视频",
    )
    assert result["launchable"] is False
    assert "deliverable_video_v2_not_allowlisted" in result["reasons"]

    _mock_video_preflight_capability(monkeypatch, ["volcengine_agent_plan", "minimax"])
    # The manifest already fail-closes missing required fields; the brief
    # clarification seam catches the conditional contract the manifest cannot
    # express: in-scene dialogue without a dialogue script.
    spec = _spec(audio_mode="in_scene_dialogue")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=spec,
        goal="为新款极光保温杯制作抖音投放短视频",
    )
    assert result["launchable"] is False
    assert result["available"] is True
    assert "brief_missing:dialogue_script" in result["reasons"]
    assert "dialogue_script" in result["next_action"]
    assert result["creative_brief"]["status"] == "clarifying"


# ─── stage prompts ──────────────────────────────────────────────


def test_video_v2_storyboard_stage_prompt_forbids_paid_work() -> None:
    request = _request()
    prompt = build_deliverable_prompt(request, video_v2_stage="storyboard_draft")
    assert "storyboard-gated video pipeline" in prompt
    assert f"workspace/deliverables/{request.id}/storyboard.json" in prompt
    assert "exactly 2 entries" in prompt
    assert "summing to exactly 10" in prompt
    assert "Do not call any generation Tool" in prompt


def test_video_v2_shot_stage_prompt_is_verbatim_and_async() -> None:
    request = _request()
    storyboard = _approved_storyboard()
    prompt = build_deliverable_prompt(
        request,
        video_v2_stage="shot_generation",
        video_v2_storyboard=storyboard,
    )
    assert "prompts/shot-01.txt" in prompt
    assert "prompts/keyframe-shot-01.txt" in prompt
    assert "shots/shot-01.mp4" in prompt
    assert "duration=5" in prompt
    assert "wait_for_completion=false" in prompt
    assert "verbatim" in prompt
    assert "require_audio=false" in prompt


def test_video_v2_compose_stage_prompt_keeps_assembly_server_owned() -> None:
    request = _request()
    clips = [
        {"unit_key": "shot-01", "clip_path": f"workspace/deliverables/{request.id}/shots/shot-01_a.mp4"},
        {"unit_key": "shot-02", "clip_path": f"workspace/deliverables/{request.id}/shots/shot-02_b.mp4"},
    ]
    prompt = build_deliverable_prompt(
        request,
        video_v2_stage="compose",
        video_v2_storyboard=_approved_storyboard(),
        video_v2_shot_clips=clips,
    )
    # FR-V5: the agent never assembles; the only paid step is the voiceover.
    assert "video_paths=[" not in prompt
    assert "Never call compose_video_audio" in prompt
    assert prompt.index("shot-01_a.mp4") < prompt.index("shot-02_b.mp4")
    assert "generate_speech_minimax" in prompt
    assert "voiceover.mp3" in prompt

    silent_request = _request(spec=_spec(audio_mode="silent"))
    silent_prompt = build_deliverable_prompt(
        silent_request,
        video_v2_stage="compose",
        video_v2_storyboard=None,
        video_v2_shot_clips=clips,
    )
    assert "generate_speech_minimax" not in silent_prompt
    assert "video_paths=[" not in silent_prompt

    dialogue_request = _request(
        spec=_spec(audio_mode="in_scene_dialogue", dialogue_script="这杯子保温一整天。"),
    )
    dialogue_prompt = build_deliverable_prompt(
        dialogue_request,
        video_v2_stage="compose",
        video_v2_storyboard=None,
        video_v2_shot_clips=clips,
    )
    assert "video_paths=[" not in dialogue_prompt
    assert "generate_speech_minimax" not in dialogue_prompt


def test_v1_video_prompt_contract_is_unchanged() -> None:
    v1_request = _request(
        workflow_id="builtin.video.v1",
        workflow_version="1.0.0",
        spec={
            "channel": "social",
            "aspect_ratio": "9:16",
            "duration": "6",
            "audience": "潜在消费者",
            "language": "zh-CN",
            "audio_mode": "voiceover",
            "story": "产品故事",
        },
    )
    prompt = build_deliverable_prompt(v1_request)
    assert "storyboard-gated video pipeline" not in prompt
    assert "generate_video_minimax" in prompt
    assert "visual.mp4" in prompt


# ─── launch + continuation orchestration ────────────────────────


@pytest.mark.asyncio
async def test_first_launch_drafts_storyboard_without_paid_work(monkeypatch) -> None:
    request = _request()
    execution = _execution(request)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(return_value={"launchable": True, "reasons": []}),
    )
    db = _Session(
        request,
        execution,
        [_unit(request, execution, "shot_generate", f"shot-{index:02d}") for index in (1, 2)],
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
    assert request.current_stage == "storyboard_draft"
    assert "storyboard.json" in prepared.prompt
    # No prompt compilation receipts are written before storyboard approval.
    from app.models.deliverable import DeliverablePromptCompilation

    assert not any(isinstance(item, DeliverablePromptCompilation) for item in db.added)


@pytest.mark.asyncio
async def test_storyboard_revision_relaunches_planning_without_paid_shots(monkeypatch) -> None:
    request = _request()
    execution = _execution(request, kind="revision")
    execution.contract_snapshot = {
        "revision_stage": "storyboard",
        "target_units": [],
    }
    shot_units = [
        _unit(request, execution, "shot_generate", f"shot-{index:02d}")
        for index in (1, 2)
    ]
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(return_value={"launchable": True, "reasons": []}),
    )
    approval = AsyncMock(side_effect=AssertionError("planning revision must not require prior approval"))
    monkeypatch.setattr(
        "app.services.deliverable_workflows.storyboard_approved",
        approval,
    )
    db = _Session(request, execution, shot_units)

    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )

    assert request.current_stage == "storyboard_draft"
    assert "Do not call any generation Tool" in prepared.prompt
    approval.assert_not_awaited()
    assert not db.added


@pytest.mark.asyncio
async def test_continuation_requires_storyboard_approval(monkeypatch) -> None:
    request = _request(
        status="ready",
        current_stage="storyboard_approved",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.storyboard_approved",
        AsyncMock(return_value=False),
    )
    db = _Session(request, execution)
    with pytest.raises(DeliverableWorkflowError, match="must be approved before any paid shot"):
        await prepare_deliverable_launch(
            db,  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )
    # Zero media tasks and zero reservations can exist before approval.
    assert not db.added


@pytest.mark.asyncio
async def test_approved_continuation_compiles_shot_prompts(monkeypatch) -> None:
    request = _request(
        status="ready",
        current_stage="storyboard_approved",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    units = [
        _unit(request, execution, "shot_generate", f"shot-{index:02d}")
        for index in (1, 2)
    ] + [
        _unit(request, execution, "keyframe_pack", f"shot-{index:02d}")
        for index in (1, 2)
    ]
    storyboard_unit = _unit(request, execution, "storyboard", "video", status="succeeded")
    storyboard_unit.result_snapshot = {"storyboard": _approved_storyboard().model_dump(mode="json")}
    monkeypatch.setattr(
        "app.services.deliverable_workflows.storyboard_approved",
        AsyncMock(return_value=True),
    )
    storage = _FakeStorage()
    monkeypatch.setattr("app.services.prompt_compiler.get_storage_backend", lambda: storage)

    db = _Session(
        request,            # select request
        execution,          # current_execution
        storyboard_unit,    # load_latest_storyboard
        units,              # shot/keyframe units for compilation
        None, None, None, None,  # per-unit existing receipt lookups
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
    assert request.current_stage == "shot_generation"
    assert "wait_for_completion=false" in prepared.prompt
    from app.models.deliverable import DeliverablePromptCompilation

    receipts = [item for item in db.added if isinstance(item, DeliverablePromptCompilation)]
    assert len(receipts) == 4  # 2 shots + 2 keyframes
    assert {receipt.compiler_version for receipt in receipts} == {
        "video-shot-v1",
        "video-keyframe-v1",
    }
    assert len(storage.writes) == 4
    assert any("prompts/shot-01.txt" in key for key in storage.writes)
    assert any("prompts/keyframe-shot-02.txt" in key for key in storage.writes)


@pytest.mark.asyncio
async def test_storyboard_draft_resume_after_intake_run_crash(monkeypatch) -> None:
    request = _request(
        status="running",
        current_stage="storyboard_draft",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    execution.intake_run_id = uuid.uuid4()
    db = _Session(request, execution)
    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )
    # The crashed intake run is terminal (not found rows are terminal); the
    # storyboard draft stage re-runs with zero paid work.
    assert prepared.execution is execution
    assert "storyboard.json" in prepared.prompt
    assert request.current_stage == "storyboard_draft"
    assert request.agent_run_id is None


@pytest.mark.asyncio
async def test_compose_continuation_requires_complete_shots(monkeypatch) -> None:
    request = _request(
        status="ready",
        current_stage="compose_ready",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    execution = _execution(request)
    clip_units = []
    for index in (1, 2):
        unit = _unit(
            request,
            execution,
            "shot_generate",
            f"shot-{index:02d}",
            status="succeeded",
        )
        unit.result_snapshot = {
            "clip_path": f"workspace/deliverables/{request.id}/shots/shot-{index:02d}_v.mp4"
        }
        clip_units.append(unit)
    storyboard_unit = _unit(request, execution, "storyboard", "video", status="succeeded")
    storyboard_unit.result_snapshot = {"storyboard": _approved_storyboard().model_dump(mode="json")}

    db = _Session(
        request,
        execution,
        clip_units,          # succeeded shot units
        [*clip_units],       # execution_units for the expected count
        storyboard_unit,     # load_latest_storyboard
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
    assert request.current_stage == "compose"
    # FR-V5: the compose run only synthesizes the voiceover; assembly is the
    # server-owned deterministic post-production stage.
    assert "video_paths=[" not in prepared.prompt
    assert "Never call compose_video_audio" in prepared.prompt
    assert "generate_speech_minimax" in prepared.prompt

    # A missing clip fails closed before any compose run is created.
    broken_request = _request(
        status="ready",
        current_stage="compose_ready",
        agent_run_id=None,
        launch_message_id=uuid.uuid4(),
    )
    broken_execution = _execution(broken_request)
    broken_db = _Session(
        broken_request,
        broken_execution,
        clip_units[:1],
        clip_units,
    )
    with pytest.raises(DeliverableWorkflowError, match="shot clip"):
        await prepare_deliverable_launch(
            broken_db,  # type: ignore[arg-type]
            request_id=broken_request.id,
            tenant_id=broken_request.tenant_id,
            user_id=broken_request.created_by_user_id,
            agent_id=broken_request.agent_id,
            session_id=broken_request.session_id,
            message_id=uuid.uuid4(),
        )


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
async def test_v1_stage_approval_stays_409(monkeypatch) -> None:
    request = _request(
        workflow_id="builtin.video.v1",
        workflow_version="1.0.0",
        status="waiting_approval",
        current_stage="storyboard_review",
    )
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="storyboard", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_stage_approval_not_ready"


@pytest.mark.asyncio
async def test_v2_stage_approval_requires_the_flag(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="storyboard_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=False)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="storyboard", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_v2_storyboard_approval_releases_the_shot_gate(monkeypatch) -> None:
    request = _request(
        status="waiting_approval",
        current_stage="storyboard_review",
        agent_run_id=uuid.uuid4(),
    )
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)  # receipt idempotency lookup: no existing receipt
    result = await deliverables.record_deliverable_approval(
        request.id,
        _approval_input(request, stage="storyboard", action="approve"),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    assert result is request
    assert request.status == "ready"
    assert request.current_stage == "storyboard_approved"
    assert request.agent_run_id is None
    from app.models.deliverable import DeliverableApprovalReceipt

    receipts = [item for item in db.added if isinstance(item, DeliverableApprovalReceipt)]
    assert len(receipts) == 1
    assert receipts[0].stage == "storyboard"
    assert receipts[0].action == "approve"


@pytest.mark.asyncio
async def test_v2_storyboard_approval_replay_is_idempotent(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="storyboard_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    data = _approval_input(request, stage="storyboard", action="approve")
    fingerprint = deliverables.request_fingerprint(
        {
            "stage": data.stage,
            "action": data.action,
            "instruction": None,
            "target_units": [],
        }
    )
    existing = SimpleNamespace(request_fingerprint=fingerprint)
    db = _Session(existing)
    result = await deliverables.record_deliverable_approval(
        request.id,
        data,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    # A replayed client_action_id returns the current state without mutating it.
    assert result is request
    assert request.status == "waiting_approval"
    assert not db.added

    conflicting = _Session(SimpleNamespace(request_fingerprint="0" * 64))
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            data,
            user,  # type: ignore[arg-type]
            conflicting,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_v2_storyboard_gate_requires_the_review_state(monkeypatch) -> None:
    request = _request(status="ready", current_stage="storyboard_draft")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            _approval_input(request, stage="storyboard", action="approve"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_stage_approval_not_ready"


@pytest.mark.asyncio
async def test_storyboard_revision_records_planning_stage_without_targets(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="storyboard_review")
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
        stage="storyboard",
        action="request_changes",
        instruction="第二镜头改成更近的产品特写",
    )

    await deliverables.record_deliverable_approval(
        request.id,
        data,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert revision.await_args.kwargs["revision_stage"] == "storyboard"
    assert revision.await_args.kwargs["target_units"] == []


@pytest.mark.asyncio
async def test_storyboard_revision_rejects_production_targets(monkeypatch) -> None:
    request = _request(status="waiting_approval", current_stage="storyboard_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    db = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="storyboard",
        action="request_changes",
        instruction="调整第二个分镜",
        target_units=["shot-02"],
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


@pytest.mark.asyncio
async def test_shot_review_revision_entry_creates_targeted_revision(monkeypatch) -> None:
    request = _request(status="ready", current_stage="shot_review")
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
    failed_shot = _unit(request, execution, "shot_qa", "shot-02", status="failed")
    db = _Session(None, [failed_shot])
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="final",
        action="request_changes",
        instruction="重做第二个镜头，换一个场景",
        target_units=["shot-02"],
    )
    await deliverables.record_deliverable_approval(
        request.id,
        data,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    revision.assert_awaited_once()
    kwargs = revision.await_args.kwargs
    assert kwargs["target_units"] == ["shot-02"]
    assert kwargs["revision_stage"] == "shot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_units", "expected_code"),
    [
        ([], "deliverable_failed_shot_target_required"),
        (["shot-01"], "deliverable_failed_shot_target_invalid"),
    ],
)
async def test_shot_review_revision_rejects_empty_or_passed_targets(
    monkeypatch,
    target_units,
    expected_code,
) -> None:
    request = _request(status="ready", current_stage="shot_review")
    execution = _execution(request)
    user = _mock_approval_api(monkeypatch, request, execution, stage_approvals_enabled=True)
    failed_shot = _unit(request, execution, "shot_qa", "shot-02", status="failed")
    passed_shot = _unit(request, execution, "shot_qa", "shot-01", status="succeeded")
    db = _Session(None, [failed_shot, passed_shot])
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="final",
        action="request_changes",
        instruction="只重做失败镜头",
        target_units=target_units,
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.record_deliverable_approval(
            request.id,
            data,
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == expected_code


# ─── FR-V4 Credits reconciliation facts ─────────────────────────


@pytest.mark.asyncio
async def test_revision_charges_only_the_redone_shot() -> None:
    request = _request()
    execution_one = _execution(request)
    execution_two = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=2,
        kind="revision",
        status="running",
        current_stage="shot_generation",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=uuid.uuid4(),
        request_fingerprint="d" * 64,
    )
    shot_01 = _unit(request, execution_one, "shot_generate", "shot-01", status="succeeded")
    shot_02_initial = _unit(request, execution_one, "shot_generate", "shot-02", status="failed")
    shot_02_redo = _unit(request, execution_two, "shot_generate", "shot-02", status="succeeded")

    reservations = {}
    tasks = []
    for unit in (shot_01, shot_02_initial, shot_02_redo):
        reservation = SimpleNamespace(
            id=uuid.uuid4(),
            amount=30,
            status="finalized" if unit.status == "succeeded" else "released",
        )
        reservations[reservation.id] = reservation
        tasks.append(
            SimpleNamespace(
                id=uuid.uuid4(),
                deliverable_unit_id=unit.id,
                reservation_id=reservation.id,
            )
        )
    transactions = [
        SimpleNamespace(reason="consume", ref_type="reservation", ref_id=reservations_id)
        for reservations_id in (
            tasks[0].reservation_id,
            tasks[2].reservation_id,
        )
    ]
    db = _Session(
        [shot_01, shot_02_initial, shot_02_redo],
        [execution_one, execution_two],
        tasks,
        list(reservations.values()),
        transactions,
    )
    facts = await video_v2_credit_reconciliation(db, request=request)  # type: ignore[arg-type]

    # Exactly-once settlement per reservation, and the passed shot is never
    # re-charged: only the redone shot adds consume volume.
    assert facts["duplicate_consume_ref_ids"] == []
    assert facts["consumed_credits_total"] == 60
    assert facts["redo_consumed_credits"] == 30
    assert facts["redo_share"] == 0.5
    by_key = {}
    for shot in facts["shots"]:
        by_key.setdefault(shot["unit_key"], []).append(shot)
    passed_shot = by_key["shot-01"]
    assert len(passed_shot) == 1
    assert passed_shot[0]["consumed"] is True
    assert passed_shot[0]["consume_tx_count"] == 1
    redone = by_key["shot-02"]
    assert [shot["execution_kind"] for shot in redone] == ["initial", "revision"]
    # The failed initial attempt released its reservation instead of consuming.
    assert redone[0]["consumed"] is False
    assert redone[1]["consumed"] is True
    assert all(shot["consume_tx_count"] <= 1 for shot in facts["shots"])


# ─── FR-V7 provider-free video QA ───────────────────────────────

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _make_clip(path: Path, *, width: int, height: int, seconds: int, audio: bool = True) -> bytes:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:duration={seconds}:rate=24",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    else:
        command += ["-an"]
    command.append(str(path))
    subprocess.run(command, check=True, timeout=120)
    return path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_video_shot_qa_passes_a_contract_compliant_clip(tmp_path) -> None:
    data = _make_clip(tmp_path / "shot.mp4", width=720, height=1280, seconds=2)
    report = await evaluate_video_shot(
        data=data,
        unit_key="shot-01",
        artifact_path="workspace/deliverables/x/shots/shot-01.mp4",
        expected_aspect_ratio="9:16",
        expected_duration_seconds=2,
        require_audio=True,
    )
    assert report.status == "passed"
    assert report.artifact_sha256 == hashlib.sha256(data).hexdigest()
    checks = {check.name: check.status for check in report.checks}
    assert checks["artifact_decodable"] == "passed"
    assert checks["delivery_contract"] == "passed"
    assert checks["duration_match"] == "passed"
    assert checks["no_black_frames"] == "passed"


@pytest.mark.asyncio
@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_video_shot_qa_catches_aspect_and_frame_mismatch(tmp_path) -> None:
    landscape = _make_clip(tmp_path / "landscape.mp4", width=1280, height=720, seconds=2)
    report = await evaluate_video_shot(
        data=landscape,
        unit_key="shot-01",
        artifact_path="workspace/deliverables/x/shots/shot-01.mp4",
        expected_aspect_ratio="9:16",
        expected_duration_seconds=2,
        require_audio=True,
    )
    checks = {check.name: check.status for check in report.checks}
    assert checks["delivery_contract"] == "failed"
    assert report.status == "failed"

    portrait = _make_clip(tmp_path / "portrait.mp4", width=720, height=1280, seconds=2)
    from PIL import Image

    frame_path = tmp_path / "frame.png"
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(frame_path)
    report = await evaluate_video_shot(
        data=portrait,
        unit_key="shot-01",
        artifact_path="workspace/deliverables/x/shots/shot-01.mp4",
        expected_aspect_ratio="9:16",
        expected_duration_seconds=2,
        require_audio=True,
        first_frame_bytes=frame_path.read_bytes(),
    )
    checks = {check.name: check.status for check in report.checks}
    assert checks["first_frame_aspect_match"] == "failed"
    assert report.status == "failed"


@pytest.mark.asyncio
@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_video_shot_qa_fails_undecodable_bytes() -> None:
    report = await evaluate_video_shot(
        data=b"not an mp4 at all",
        unit_key="shot-03",
        artifact_path="workspace/deliverables/x/shots/shot-03.mp4",
        expected_aspect_ratio="9:16",
    )
    checks = {check.name: check.status for check in report.checks}
    assert checks["artifact_decodable"] == "failed"
    assert report.status == "failed"
    assert report.score == 0
    assert report.artifact_sha256 == hashlib.sha256(b"not an mp4 at all").hexdigest()


@pytest.mark.asyncio
@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_package_qa_hash_bound_shadow_then_enforcing(monkeypatch, tmp_path) -> None:
    data = _make_clip(tmp_path / "final.mp4", width=720, height=1280, seconds=6)
    from PIL import Image

    frame_path = tmp_path / "keyframe.png"
    Image.new("RGB", (720, 1280), (10, 20, 30)).save(frame_path)

    request = _request(spec=_spec(duration="6", shot_count=1))
    execution = _execution(request)
    package_unit = _unit(request, execution, "package_qa", "final")
    storage = _FakeStorage(
        {
            f"deliverables/{request.id}/final.mp4": data,
            f"deliverables/{request.id}/keyframes/shot-01.png": frame_path.read_bytes(),
        }
    )
    monkeypatch.setattr(
        "app.services.candidate_qa.get_storage_backend",
        lambda: storage,
    )
    monkeypatch.setattr(
        "app.services.candidate_qa.get_settings",
        lambda: SimpleNamespace(
            DELIVERABLE_CREATIVE_QA_ENFORCEMENT="shadow",
            DELIVERABLE_CREATIVE_QA_TENANT_IDS="",
            DELIVERABLE_CREATIVE_QA_AGENT_IDS="",
        ),
    )
    db = _Session(package_unit)
    report = await evaluate_video_v2_package(db, request=request, storage=storage)  # type: ignore[arg-type]
    assert report is not None
    assert report.status == "passed"
    assert report.artifact_sha256 == hashlib.sha256(data).hexdigest()
    # Shadow mode records the report without changing the unit lifecycle.
    assert package_unit.quality_evaluation["package_qa"]["artifact_sha256"] == report.artifact_sha256
    assert package_unit.quality_evaluation["enforcement"] == "shadow"
    assert package_unit.status == "pending"

    monkeypatch.setattr(
        "app.services.candidate_qa.get_settings",
        lambda: SimpleNamespace(
            DELIVERABLE_CREATIVE_QA_ENFORCEMENT="enforcing",
            DELIVERABLE_CREATIVE_QA_TENANT_IDS=str(request.tenant_id),
            DELIVERABLE_CREATIVE_QA_AGENT_IDS="",
        ),
    )
    db = _Session(package_unit)
    report = await evaluate_video_v2_package(db, request=request, storage=storage)  # type: ignore[arg-type]
    assert package_unit.status == "succeeded"

    broken_unit = _unit(request, execution, "package_qa", "final")
    broken_storage = _FakeStorage({f"deliverables/{request.id}/final.mp4": b"broken"})
    db = _Session(broken_unit)
    report = await evaluate_video_v2_package(db, request=request, storage=broken_storage)  # type: ignore[arg-type]
    assert report is not None and report.status == "failed"
    assert broken_unit.status == "failed"
    assert broken_unit.last_error_code == "package_qa_failed"
