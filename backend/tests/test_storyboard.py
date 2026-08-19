"""FR-V1~V4 contracts for the storyboard compiler and per-shot unit seams."""

from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services import storyboard as storyboard_module
from app.services.creative_briefs import compile_video_brief
from app.services.media_assets import MediaContractError
from app.services.prompt_compiler import (
    VIDEO_KEYFRAME_COMPILER_VERSION,
    VIDEO_SHOT_COMPILER_VERSION,
    compile_video_keyframe_prompt,
    compile_video_shot_prompt,
)
from app.services.storyboard import (
    ShotSpec,
    StoryboardError,
    compile_storyboard,
    keyframe_workspace_path,
    mark_video_v2_unit_submitted,
    reconcile_video_v2_units,
    resolve_video_v2_keyframe_unit,
    resolve_video_v2_shot_unit,
    shot_clip_workspace_path,
    storyboard_workspace_path,
    video_v2_keyframe_unit_key,
    video_v2_shot_unit_key,
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
        self.commit_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)


def _brief(**overrides):
    spec = {
        "channel": "social",
        "aspect_ratio": "9:16",
        "duration": "10",
        "audience": "25-35 岁都市白领",
        "language": "zh-CN",
        "audio_mode": "voiceover",
        "story": "成年演员在厨房台面拿起极光保温杯，倒水、旋盖、递给同事",
        "cta": "立即了解更多",
    }
    spec.update(overrides)
    brief, missing = compile_video_brief(
        "为新款极光保温杯制作抖音投放短视频",
        spec,
        [],
        tier="pro",
    )
    assert missing == ()
    assert brief is not None
    return brief


def _storyboard_payload(shots: int = 2, *, duration: int = 10, voiceover: str = "旁白文案") -> dict:
    per = duration // shots
    return {
        "shots": [
            {
                "shot_id": f"shot-{index:02d}",
                "duration_seconds": per,
                "visual": f"镜头 {index} 的画面描述",
                "camera": "固定机位",
                "subject_refs": [],
                "first_frame_ref": None,
                "last_frame_ref": None,
                "dialogue": "",
                "caption": f"字幕 {index}",
                "transition": "cut",
            }
            for index in range(1, shots + 1)
        ],
        "voiceover_script": voiceover,
    }


def test_storyboard_compiles_and_is_hash_stable() -> None:
    brief = _brief()
    first = compile_storyboard(brief, _storyboard_payload(), expected_shot_count=2)
    second = compile_storyboard(brief, _storyboard_payload(), expected_shot_count=2)
    assert first == second
    assert len(first.shots) == 2
    assert first.shots[0].shot_id == "shot-01"
    assert len(first.storyboard_sha256) == 64


def test_storyboard_rejects_count_and_duration_drift() -> None:
    brief = _brief()
    with pytest.raises(StoryboardError, match="exactly 2 shots, found 3"):
        compile_storyboard(brief, _storyboard_payload(3), expected_shot_count=2)
    payload = _storyboard_payload()
    payload["shots"][0]["duration_seconds"] = 6
    with pytest.raises(StoryboardError, match="sum to 11s"):
        compile_storyboard(brief, payload, expected_shot_count=2)
    too_long = {
        "shots": [
            {**_storyboard_payload()["shots"][0], "duration_seconds": 16},
            {**_storyboard_payload()["shots"][1], "shot_id": "shot-02", "duration_seconds": 5},
        ],
        "voiceover_script": "旁白",
    }
    with pytest.raises(StoryboardError, match="provider limit"):
        compile_storyboard(_brief(duration="21"), too_long, expected_shot_count=2)


def test_storyboard_enforces_shot_id_sequence() -> None:
    brief = _brief()
    payload = _storyboard_payload()
    payload["shots"][1]["shot_id"] = "shot-03"
    with pytest.raises(StoryboardError, match="shot-01..shot-02"):
        compile_storyboard(brief, payload, expected_shot_count=2)


def test_storyboard_audio_mode_consistency() -> None:
    silent_brief = _brief(audio_mode="silent")
    with pytest.raises(StoryboardError, match="silent"):
        compile_storyboard(
            silent_brief,
            _storyboard_payload(voiceover="不该存在的旁白"),
            expected_shot_count=2,
        )
    silent_payload = _storyboard_payload(voiceover="")
    silent_board = compile_storyboard(silent_brief, silent_payload, expected_shot_count=2)
    assert silent_board.voiceover_script == ""

    voiceover_brief = _brief(audio_mode="voiceover")
    with pytest.raises(StoryboardError, match="voiceover_script"):
        compile_storyboard(voiceover_brief, silent_payload, expected_shot_count=2)

    dialogue_brief = _brief(
        audio_mode="in_scene_dialogue",
        dialogue_script="这杯子保温一整天。",
    )
    dialogue_payload = _storyboard_payload(voiceover="")
    dialogue_payload["shots"][0]["dialogue"] = "这杯子保温一整天。"
    board = compile_storyboard(dialogue_brief, dialogue_payload, expected_shot_count=2)
    assert board.shots[0].dialogue == "这杯子保温一整天。"
    with pytest.raises(StoryboardError, match="dialogue"):
        compile_storyboard(dialogue_brief, silent_payload, expected_shot_count=2)


def test_shot_spec_and_brief_fail_closed_on_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ShotSpec(
            shot_id="shot-01",
            duration_seconds=5,
            visual="画面",
            provider="minimax",  # type: ignore[call-arg]
        )


def test_workspace_paths_and_unit_key_parsing() -> None:
    request_id = uuid.uuid4()
    assert storyboard_workspace_path(request_id).endswith(f"{request_id}/storyboard.json")
    clip = shot_clip_workspace_path(request_id, "shot-01")
    assert video_v2_shot_unit_key(clip) == "shot-01"
    versioned = clip.replace(".mp4", "_abcdef123456.mp4")
    assert video_v2_shot_unit_key(versioned) == "shot-01"
    keyframe = keyframe_workspace_path(request_id, "shot-02")
    assert video_v2_keyframe_unit_key(keyframe) == "shot-02"
    assert video_v2_keyframe_unit_key(versioned.replace(".mp4", ".png")) is None
    assert video_v2_shot_unit_key("workspace/videos/other.mp4") is None
    assert video_v2_shot_unit_key(keyframe) is None


def test_shot_prompt_compilation_is_reproducible_and_goal_free() -> None:
    brief = _brief()
    board = compile_storyboard(brief, _storyboard_payload(), expected_shot_count=2)
    first = compile_video_shot_prompt(brief, board.shots[0], provider_target="minimax")
    second = compile_video_shot_prompt(brief, board.shots[0], provider_target="minimax")
    assert first == second
    assert first.compiler_version == VIDEO_SHOT_COMPILER_VERSION
    other_shot = compile_video_shot_prompt(brief, board.shots[1], provider_target="minimax")
    assert other_shot.prompt_sha256 != first.prompt_sha256
    # The raw goal/purpose text never reaches the compiled shot prompt; only
    # the approved storyboard shot and structured brief elements do.
    assert brief.purpose not in first.neutral_prompt
    assert brief.story not in first.neutral_prompt
    assert board.shots[0].visual in first.neutral_prompt
    with pytest.raises(ValueError, match="Unsupported video provider target"):
        compile_video_shot_prompt(brief, board.shots[0], provider_target="unknown")


def test_voiceover_shot_forbids_generated_speech() -> None:
    brief = _brief()
    board = compile_storyboard(brief, _storyboard_payload(), expected_shot_count=2)
    compiled = compile_video_shot_prompt(brief, board.shots[0], provider_target="minimax")
    assert "Do not generate any speech" in compiled.neutral_prompt
    assert compiled.provider_payload["first_frame_required"] is True

    dialogue_brief = _brief(
        audio_mode="in_scene_dialogue",
        dialogue_script="这杯子保温一整天。",
    )
    dialogue_payload = _storyboard_payload(voiceover="")
    dialogue_payload["shots"][0]["dialogue"] = "这杯子保温一整天。"
    dialogue_board = compile_storyboard(dialogue_brief, dialogue_payload, expected_shot_count=2)
    dialogue_compiled = compile_video_shot_prompt(
        dialogue_brief,
        dialogue_board.shots[0],
        provider_target="volcengine_agent_plan",
    )
    assert "Synchronized in-scene dialogue" in dialogue_compiled.neutral_prompt
    assert dialogue_compiled.provider_payload["generate_audio"] is True
    assert "Do not generate any speech" not in dialogue_compiled.neutral_prompt


def test_keyframe_prompt_reuses_the_image_compiler() -> None:
    brief = _brief()
    board = compile_storyboard(brief, _storyboard_payload(), expected_shot_count=2)
    keyframe = compile_video_keyframe_prompt(
        brief,
        board.shots[0],
        provider_target="volcengine_agent_plan",
        quality_size="3K",
    )
    assert keyframe.compiler_version == VIDEO_KEYFRAME_COMPILER_VERSION
    assert keyframe.candidate_index == 1
    assert len(keyframe.prompt_sha256) == 64
    again = compile_video_keyframe_prompt(
        brief,
        board.shots[0],
        provider_target="volcengine_agent_plan",
        quality_size="3K",
    )
    assert again == keyframe


def _v2_request(**overrides) -> DeliverableRequest:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "client_request_id": uuid.uuid4(),
        "request_fingerprint": "a" * 64,
        "work_type": "video",
        "workflow_id": "builtin.video.v2",
        "workflow_version": "2.0.0",
        "goal": "为新款极光保温杯制作抖音投放短视频",
        "inputs": [],
        "spec": {
            "channel": "social",
            "aspect_ratio": "9:16",
            "duration": "10",
            "audience": "25-35 岁都市白领",
            "language": "zh-CN",
            "audio_mode": "voiceover",
            "story": "成年演员在厨房台面拿起极光保温杯，倒水、旋盖、递给同事",
        },
        "tier": "pro",
        "approval_policy": ["storyboard", "final"],
        "output_contract": ["mp4"],
        "status": "ready",
        "current_stage": "storyboard_approved",
        "version": 1,
        "contract_revision": 1,
    }
    request = DeliverableRequest(**values)
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def _execution(request: DeliverableRequest) -> DeliverableExecution:
    execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=1,
        kind="initial",
        status="running",
        current_stage="shot_generation",
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
    media_generation_task_id: uuid.UUID | None = None,
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
        media_generation_task_id=media_generation_task_id,
    )


def _approval_receipt(request: DeliverableRequest) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        stage="storyboard",
        action="approve",
    )


def _compilation(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    unit: DeliverableExecutionUnit,
    compiler_version: str,
    prompt_path: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        execution_id=execution.id,
        unit_id=unit.id,
        compiler_version=compiler_version,
        compiled_prompt_path=prompt_path,
    )


class _FakeStorage:
    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = dict(payloads or {})

    async def read_bytes(self, key: str) -> bytes:
        for suffix, payload in self.payloads.items():
            if key.endswith(suffix):
                return payload
        raise FileNotFoundError(key)

    async def read_text(self, key: str, **_kwargs) -> str:
        return (await self.read_bytes(key)).decode("utf-8")


def _binding_sessions(
    request: DeliverableRequest,
    *,
    approved: bool,
    prompt: bytes = b"compiled prompt",
    with_task: bool = False,
    duration_seconds: int = 5,
):
    execution = _execution(request)
    unit = _unit(
        request,
        execution,
        "shot_generate",
        "shot-01",
        media_generation_task_id=uuid.uuid4() if with_task else None,
    )
    receipt = _approval_receipt(request) if approved else None
    storyboard_unit = _unit(request, execution, "storyboard", "video", status="succeeded")
    storyboard_unit.result_snapshot = {
        "storyboard": compile_storyboard(
            _brief(),
            _storyboard_payload(),
            expected_shot_count=2,
        ).model_dump(mode="json")
    }
    prompt_path = f"workspace/deliverables/{request.id}/prompts/shot-01.txt"
    compilation = _compilation(request, execution, unit, VIDEO_SHOT_COMPILER_VERSION, prompt_path)
    session = _Session(
        request,          # select request
        execution,        # select execution
        unit,             # select unit
        receipt,          # storyboard approval lookup
        storyboard_unit,  # load_latest_storyboard
        compilation,      # compilation receipt
    )
    storage = _FakeStorage({"prompts/shot-01.txt": prompt})
    return session, storage, unit


@pytest.mark.asyncio
async def test_shot_binding_returns_none_for_non_v2_requests(monkeypatch) -> None:
    v1_request = _v2_request(workflow_id="builtin.video.v1", workflow_version="1.0.0")
    session = _Session(v1_request)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    binding = await resolve_video_v2_shot_unit(
        tenant_id=v1_request.tenant_id,
        agent_id=v1_request.agent_id,
        request_id=v1_request.id,
        save_path=shot_clip_workspace_path(v1_request.id, "shot-01"),
        prompt="anything",
    )
    assert binding is None
    # No further lookups happen for non-v2 requests.
    assert not session.execute_values


@pytest.mark.asyncio
async def test_shot_binding_requires_storyboard_approval(monkeypatch) -> None:
    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=False)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    with pytest.raises(MediaContractError, match="deliverable_storyboard_approval_required"):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=shot_clip_workspace_path(request.id, "shot-01"),
            prompt="compiled prompt",
            duration_seconds=5,
            has_first_frame=True,
        )


@pytest.mark.asyncio
async def test_shot_binding_fail_closed_on_path_and_prompt(monkeypatch) -> None:
    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    with pytest.raises(MediaContractError, match="shots/shot-NN"):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path="workspace/videos/freeform.mp4",
            prompt="compiled prompt",
        )

    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    with pytest.raises(MediaContractError, match="verbatim"):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=shot_clip_workspace_path(request.id, "shot-01"),
            prompt="rewritten by the model",
            duration_seconds=5,
            has_first_frame=True,
        )


@pytest.mark.asyncio
async def test_shot_binding_enforces_first_frame_for_non_169(monkeypatch) -> None:
    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    with pytest.raises(
        MediaContractError,
        match="media_video_requires_first_frame_for_aspect_ratio",
    ):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=shot_clip_workspace_path(request.id, "shot-01"),
            prompt="compiled prompt",
            duration_seconds=5,
            has_first_frame=False,
        )

    # A 16:9 shot may submit text-to-video without a first frame.
    request_169 = _v2_request(
        spec={**_v2_request().spec, "aspect_ratio": "16:9"},
    )
    session, storage, _unit_row = _binding_sessions(request_169, approved=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    binding = await resolve_video_v2_shot_unit(
        tenant_id=request_169.tenant_id,
        agent_id=request_169.agent_id,
        request_id=request_169.id,
        save_path=shot_clip_workspace_path(request_169.id, "shot-01"),
        prompt="compiled prompt",
        duration_seconds=5,
        has_first_frame=False,
    )
    assert binding is not None
    assert binding.unit_key == "shot-01"


@pytest.mark.asyncio
async def test_shot_binding_rejects_resubmission_and_duration_drift(monkeypatch) -> None:
    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=True, with_task=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    with pytest.raises(MediaContractError, match="already has a durable media task"):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=shot_clip_workspace_path(request.id, "shot-01"),
            prompt="compiled prompt",
        )

    request = _v2_request()
    session, storage, _unit_row = _binding_sessions(request, approved=True)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    with pytest.raises(MediaContractError, match="approved duration"):
        await resolve_video_v2_shot_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=shot_clip_workspace_path(request.id, "shot-01"),
            prompt="compiled prompt",
            duration_seconds=6,
            has_first_frame=True,
        )


@pytest.mark.asyncio
async def test_keyframe_binding_uses_the_keyframe_namespace(monkeypatch) -> None:
    request = _v2_request()
    execution = _execution(request)
    unit = _unit(request, execution, "keyframe_pack", "shot-01")
    prompt_path = f"workspace/deliverables/{request.id}/prompts/keyframe-shot-01.txt"
    compilation = _compilation(request, execution, unit, VIDEO_KEYFRAME_COMPILER_VERSION, prompt_path)
    session = _Session(
        request,
        execution,
        unit,
        _approval_receipt(request),
        compilation,
    )
    storage = _FakeStorage({"prompts/keyframe-shot-01.txt": b"keyframe prompt"})
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    monkeypatch.setattr(storyboard_module, "get_storage_backend", lambda: storage)
    binding = await resolve_video_v2_keyframe_unit(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        request_id=request.id,
        save_path=keyframe_workspace_path(request.id, "shot-01"),
        prompt="keyframe prompt",
    )
    assert binding is not None and binding.unit_id == unit.id


@pytest.mark.asyncio
async def test_mark_submitted_is_idempotent(monkeypatch) -> None:
    request = _v2_request()
    execution = _execution(request)
    unit = _unit(request, execution, "shot_generate", "shot-01")
    task_id = uuid.uuid4()

    session = _Session(unit)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    binding = storyboard_module.VideoV2UnitBinding(
        execution_id=execution.id,
        unit_id=unit.id,
        unit_key="shot-01",
    )
    await mark_video_v2_unit_submitted(binding, tenant_id=request.tenant_id, media_task_id=task_id)
    assert unit.media_generation_task_id == task_id
    assert unit.status == "running"
    assert unit.attempt_count == 1

    other_task = uuid.uuid4()
    session = _Session(unit)
    monkeypatch.setattr(storyboard_module, "async_session", lambda: session)
    await mark_video_v2_unit_submitted(binding, tenant_id=request.tenant_id, media_task_id=other_task)
    # A second submission marker never rebinds the unit to another task.
    assert unit.media_generation_task_id == task_id
    assert unit.attempt_count == 1


@pytest.mark.asyncio
async def test_reconcile_advances_units_and_parks_failed_shots(monkeypatch) -> None:
    request = _v2_request(status="running", current_stage="shot_generation")
    execution = _execution(request)
    shot_ok = _unit(request, execution, "shot_generate", "shot-01", status="running")
    shot_bad = _unit(request, execution, "shot_generate", "shot-02", status="running")
    keyframe_unit = _unit(request, execution, "keyframe_pack", "shot-01", status="running")
    qa_unit = _unit(request, execution, "shot_qa", "shot-01")
    task_ok = SimpleNamespace(
        id=uuid.uuid4(),
        deliverable_unit_id=shot_ok.id,
        status="succeeded",
        output_path=f"workspace/deliverables/{request.id}/shots/shot-01.mp4",
    )
    task_bad = SimpleNamespace(
        id=uuid.uuid4(),
        deliverable_unit_id=shot_bad.id,
        status="failed",
        output_path="",
    )
    task_keyframe = SimpleNamespace(
        id=uuid.uuid4(),
        deliverable_unit_id=keyframe_unit.id,
        status="succeeded",
        output_path=f"workspace/deliverables/{request.id}/keyframes/shot-01.png",
    )
    shot_ok.media_generation_task_id = task_ok.id
    shot_bad.media_generation_task_id = task_bad.id
    keyframe_unit.media_generation_task_id = task_keyframe.id

    qa_calls: list[str] = []

    async def _fake_qa(_db, *, shot_unit, **_kwargs) -> None:
        qa_calls.append(shot_unit.unit_key)

    monkeypatch.setattr(storyboard_module, "_run_shot_qa", _fake_qa)
    db = _Session(
        execution,
        [shot_ok, shot_bad, keyframe_unit, qa_unit],
        [task_ok, task_bad, task_keyframe],
    )
    advanced = await reconcile_video_v2_units(db, request=request)  # type: ignore[arg-type]
    assert advanced == 3
    assert shot_ok.status == "succeeded"
    assert shot_ok.result_snapshot["clip_path"].endswith("shots/shot-01.mp4")
    assert keyframe_unit.status == "succeeded"
    assert keyframe_unit.result_snapshot["keyframe_path"].endswith("keyframes/shot-01.png")
    # FR-V3: one failed shot never drags the sibling down.
    assert shot_bad.status == "failed"
    assert shot_bad.last_error_code == "media_task_failed"
    assert qa_calls == ["shot-01"]
    # All shots terminal with one failure -> the request parks for a targeted
    # revision instead of failing wholesale.
    assert request.status == "ready"
    assert request.current_stage == "shot_review"
    assert request.agent_run_id is None

    # Idempotent: a second pass over terminal tasks advances nothing.
    db = _Session(
        execution,
        [shot_ok, shot_bad, keyframe_unit, qa_unit],
        [task_ok, task_bad, task_keyframe],
    )
    assert await reconcile_video_v2_units(db, request=request) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reconcile_all_succeeded_moves_to_compose_ready(monkeypatch) -> None:
    request = _v2_request(status="running", current_stage="shot_generation")
    execution = _execution(request)
    shots = [
        _unit(request, execution, "shot_generate", f"shot-{index:02d}", status="running")
        for index in (1, 2)
    ]
    qa_units = [
        _unit(request, execution, "shot_qa", f"shot-{index:02d}")
        for index in (1, 2)
    ]
    tasks = []
    for shot in shots:
        task = SimpleNamespace(
            id=uuid.uuid4(),
            deliverable_unit_id=shot.id,
            status="succeeded",
            output_path=f"workspace/deliverables/{request.id}/shots/{shot.unit_key}.mp4",
        )
        shot.media_generation_task_id = task.id
        tasks.append(task)

    async def _fake_qa(_db, **_kwargs) -> None:
        return None

    monkeypatch.setattr(storyboard_module, "_run_shot_qa", _fake_qa)
    db = _Session(execution, [*shots, *qa_units], tasks)
    advanced = await reconcile_video_v2_units(db, request=request)  # type: ignore[arg-type]
    assert advanced == 2
    assert request.current_stage == "compose_ready"
    assert request.status == "ready"
