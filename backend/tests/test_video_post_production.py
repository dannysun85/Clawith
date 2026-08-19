"""FR-V5: deterministic server-owned video post-production stage tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
from types import SimpleNamespace
import subprocess
import uuid

import pytest

from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services.creative_briefs import compile_video_brief
from app.services.storyboard import compile_storyboard
from app.services.video_post_production import (
    final_video_workspace_path,
    run_video_v2_post_production,
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

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)


class _FakeStorage:
    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = dict(payloads or {})
        self.writes: dict[str, bytes] = {}

    async def write_bytes(self, key: str, data: bytes, content_type=None):
        self.writes[key] = data

    async def write_bytes_if_match(self, key: str, data: bytes, condition=None, content_type=None):
        merged = {**self.payloads, **self.writes}
        if key in merged:
            return SimpleNamespace(ok=False)
        self.writes[key] = data
        return SimpleNamespace(ok=True)

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
        "audio_mode": "silent",
        "story": "成年演员拿起保温杯倒水递给同事",
        "cta": "",
        "caption_spec": "",
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
        agent_run_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        work_type="video",
        workflow_id="builtin.video.v2",
        workflow_version="2.0.0",
        goal="为新款保温杯制作抖音投放短视频",
        inputs=[],
        spec=spec if spec is not None else _spec(),
        tier="pro",
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
        status="running",
        current_stage="compose",
        version=4,
        contract_revision=1,
    )
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
        current_stage="compose",
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
    result_snapshot: dict | None = None,
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
        result_snapshot=result_snapshot or {},
        quality_evaluation={},
    )


def _pipeline_units(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    *,
    shots: int = 2,
    with_clips: bool = True,
) -> list[DeliverableExecutionUnit]:
    units: list[DeliverableExecutionUnit] = []
    for index in range(1, shots + 1):
        unit_key = f"shot-{index:02d}"
        snapshot = (
            {
                "clip_path": f"workspace/deliverables/{request.id}/shots/{unit_key}.mp4",
                "media_generation_task_id": str(uuid.uuid4()),
            }
            if with_clips
            else {}
        )
        units.append(
            _unit(
                request,
                execution,
                "shot_generate",
                unit_key,
                status="succeeded" if with_clips else "pending",
                result_snapshot=snapshot,
            )
        )
    units.append(_unit(request, execution, "edit_compose", "final"))
    units.append(_unit(request, execution, "caption_voice_music", "final"))
    units.append(_unit(request, execution, "package_qa", "final"))
    return units


def _brief(request: DeliverableRequest):
    brief, missing = compile_video_brief(request.goal, request.spec, request.inputs)
    assert missing == ()
    assert brief is not None
    return brief


def _storyboard(request: DeliverableRequest, shots: int = 2, *, voiceover_script: str = ""):
    return compile_storyboard(
        _brief(request),
        {
            "shots": [
                {
                    "shot_id": f"shot-{index:02d}",
                    "duration_seconds": 5,
                    "visual": f"镜头 {index}",
                    "camera": "固定机位",
                    "dialogue": "",
                    "caption": "",
                    "transition": "cut",
                }
                for index in range(1, shots + 1)
            ],
            "voiceover_script": voiceover_script,
        },
        expected_shot_count=shots,
    )


# ─── state-machine behavior (no ffmpeg needed) ──────────────────


async def test_missing_shot_clips_fail_the_stage_without_regeneration() -> None:
    request = _request()
    execution = _execution(request)
    units = _pipeline_units(request, execution, with_clips=False)
    session = _Session([], execution, units)
    storage = _FakeStorage()
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request),
        storage=storage,
    )
    assert handled is True
    assert request.status == "failed"
    assert request.current_stage == "compose_failed"
    assert request.last_error_code == "deliverable_shot_clips_missing"
    edit_unit = next(unit for unit in units if unit.stage_key == "edit_compose")
    assert edit_unit.status == "failed"
    # No package bytes were written and no artifact was registered.
    assert storage.writes == {}
    assert session.added == []


async def test_legacy_agent_composed_run_keeps_the_reconciliation_path() -> None:
    request = _request()
    legacy_execution_record = SimpleNamespace(
        result_metadata={"artifact_refs": ["workspace://x/final.mp4"]},
    )
    session = _Session([legacy_execution_record])
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request),
        storage=_FakeStorage(),
    )
    assert handled is False


async def test_succeeded_stage_replay_is_idempotent() -> None:
    request = _request()
    execution = _execution(request)
    units = _pipeline_units(request, execution)
    edit_unit = next(unit for unit in units if unit.stage_key == "edit_compose")
    edit_unit.status = "succeeded"
    edit_unit.result_snapshot = {"artifact_revision_id": str(uuid.uuid4())}
    session = _Session([], execution, units)
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request),
        storage=_FakeStorage(),
    )
    assert handled is True
    assert session.added == []
    assert request.status == "running"  # untouched


async def test_voiceover_mode_requires_the_synthesized_voiceover_asset() -> None:
    request = _request(spec=_spec(audio_mode="voiceover"))
    execution = _execution(request)
    units = _pipeline_units(request, execution)
    clips = {
        f"shots/shot-0{index}.mp4": b"clip-bytes"
        for index in (1, 2)
    }
    session = _Session([], execution, units, [])
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request, voiceover_script="保温杯，温暖一整天。"),
        storage=_FakeStorage(clips),
    )
    assert handled is True
    assert request.status == "failed"
    assert request.last_error_code == "deliverable_voiceover_missing"


async def test_non_v2_requests_are_untouched() -> None:
    request = _request(workflow_id="builtin.video.v1", workflow_version="1.0.0")
    session = _Session()
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=None,
        storage=_FakeStorage(),
    )
    assert handled is False
    assert session.execute_values == []


# ─── real deterministic compose (ffmpeg) ────────────────────────


_FFMPEG_AVAILABLE = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _make_clip(path: Path, *, width: int, height: int, seconds: int, audio: bool = False) -> bytes:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=24:duration={seconds}",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        *([] if audio else ["-an"]),
        *(["-c:a", "aac", "-shortest"] if audio else []),
        "-movflags", "+faststart",
        str(path),
    ]
    subprocess.run(command, check=True)
    return path.read_bytes()


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_silent_package_compose_is_deterministic_and_provider_free(tmp_path) -> None:
    request = _request()
    execution = _execution(request)
    units = _pipeline_units(request, execution)
    clip_one = _make_clip(tmp_path / "shot-01.mp4", width=720, height=1280, seconds=5)
    clip_two = _make_clip(tmp_path / "shot-02.mp4", width=720, height=1280, seconds=5)
    storage = _FakeStorage(
        {
            "shots/shot-01.mp4": clip_one,
            "shots/shot-02.mp4": clip_two,
        }
    )
    session = _Session([], execution, units, [])
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request),
        storage=storage,
    )
    assert handled is True
    assert request.status == "waiting_approval"
    assert request.current_stage == "output_review"

    artifacts = [item for item in session.added if isinstance(item, DeliverableArtifactRevision)]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.artifact_key == "mp4"
    assert artifact.workspace_path == final_video_workspace_path(request.id)
    final_bytes = storage.writes[
        next(key for key in storage.writes if key.endswith("final.mp4"))
    ]
    assert artifact.content_hash == hashlib.sha256(final_bytes).hexdigest()
    # Hash-bound receipt: the concat inputs are exactly the two shot clips.
    receipt = artifact.evaluation["post_production"]
    assert receipt["schema_version"] == "video-post-v1"
    assert receipt["concat"]["shot_sha256"] == [
        hashlib.sha256(clip_one).hexdigest(),
        hashlib.sha256(clip_two).hexdigest(),
    ]
    assert receipt["audio_mix"]["mode"] == "silent"
    assert receipt["cover"]["cover_sha256"]
    cover_key = next(key for key in storage.writes if key.endswith("cover.jpg"))
    assert storage.writes[cover_key][:2] == b"\xff\xd8"  # JPEG SOI

    edit_unit = next(unit for unit in units if unit.stage_key == "edit_compose")
    assert edit_unit.status == "succeeded"
    assert edit_unit.result_snapshot["post_production"]["shot_count"] == 2
    caption_unit = next(unit for unit in units if unit.stage_key == "caption_voice_music")
    assert caption_unit.status == "succeeded"


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_voiceover_mix_uses_the_existing_voiceover_asset(tmp_path) -> None:
    request = _request(spec=_spec(audio_mode="voiceover"))
    execution = _execution(request)
    units = _pipeline_units(request, execution)
    clip_one = _make_clip(tmp_path / "shot-01.mp4", width=720, height=1280, seconds=5)
    clip_two = _make_clip(tmp_path / "shot-02.mp4", width=720, height=1280, seconds=5)
    voiceover_path = tmp_path / "voiceover.mp3"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=9",
            str(voiceover_path),
        ],
        check=True,
    )
    voiceover_ref = (
        f"workspace://{request.agent_id}/workspace/deliverables/{request.id}/voiceover_ab12cd34ef56.mp3"
    )
    tts_execution = SimpleNamespace(result_metadata={"artifact_refs": [voiceover_ref]})
    storage = _FakeStorage(
        {
            "shots/shot-01.mp4": clip_one,
            "shots/shot-02.mp4": clip_two,
            "voiceover_ab12cd34ef56.mp3": voiceover_path.read_bytes(),
        }
    )
    session = _Session([], execution, units, [tts_execution], [])
    handled = await run_video_v2_post_production(
        session,
        request=request,
        brief=_brief(request),
        storyboard=_storyboard(request, voiceover_script="保温杯，温暖一整天。"),
        storage=storage,
    )
    assert handled is True
    assert request.status == "waiting_approval"
    artifacts = [item for item in session.added if isinstance(item, DeliverableArtifactRevision)]
    assert len(artifacts) == 1
    receipt = artifacts[0].evaluation["post_production"]
    assert receipt["audio_mix"]["voiceover_sha256"] == hashlib.sha256(
        voiceover_path.read_bytes()
    ).hexdigest()
    assert receipt["audio_mix"]["loudness"] == "alimiter-0.95"
    assert artifacts[0].evaluation["facts"]["audio_codec_name"] == "aac"
