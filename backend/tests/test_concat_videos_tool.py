"""concat_videos quick-path tool: contract, governance, and real ffmpeg merge."""

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import agent_tools, tool_seeder
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.agent_tools import _concat_videos
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.media_assets import validate_generated_video
from app.services.multimodal_capability_matrix import CAPABILITY_MATRIX
from app.services.tool_capability_policy import (
    CENTRAL_CREDENTIAL_POOL_TOOL_NAMES,
    EXPLICIT_GRANT_TOOL_NAMES,
    GLOBAL_DEFAULT_MEDIA_TOOL_NAMES,
)


def _real_mp4(path: Path, *, size: str = "640x360", duration: float = 1.2) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the real media contract test")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=#205080:s={size}:d={duration}:r=24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def _write_clip(tmp_path: Path, rel: str, **kwargs) -> str:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _real_mp4(target, **kwargs)
    return rel


# ── Registration / gray-rollout semantics ────────────────────────────────


def test_concat_videos_is_a_product_default_local_tool_with_typed_runtime():
    definitions = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}
    persisted = {item["name"]: item for item in tool_seeder.BUILTIN_TOOLS}

    assert "concat_videos" in GLOBAL_DEFAULT_MEDIA_TOOL_NAMES
    assert "concat_videos" not in EXPLICIT_GRANT_TOOL_NAMES
    # Deterministic local post-production: zero provider credential, zero Credits.
    assert "concat_videos" not in CENTRAL_CREDENTIAL_POOL_TOOL_NAMES
    assert definitions["concat_videos"]["is_default"] is True
    assert persisted["concat_videos"]["is_default"] is True
    assert "concat_videos" in agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES
    assert definitions["concat_videos"]["readiness"] == "local"

    description = definitions["concat_videos"]["description"]
    assert "concatenate" in description
    assert "same-canvas" in description
    assert "compose_video_audio" in description
    schema = definitions["concat_videos"]["parameters_schema"]
    assert schema["required"] == ["video_paths"]
    assert schema["properties"]["video_paths"]["minItems"] == 2

    video_spec = next(row for row in CAPABILITY_MATRIX if row.key == "video")
    assert "concat_videos" in video_spec.entrypoint_tools
    assert "concat_videos" in video_spec.expected_default_tools


# ── Argument and path discipline ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_concat_videos_rejects_invalid_arguments(tmp_path):
    for arguments in (
        {},
        {"video_paths": []},
        {"video_paths": "workspace/videos/a.mp4"},
        {"video_paths": ["workspace/videos/a.mp4"]},
        {"video_paths": [f"workspace/videos/shot_{i:02d}.mp4" for i in range(13)]},
        {
            "video_paths": [
                "workspace/videos/a.mp4",
                "videos/a.mp4",  # canonicalizes to the same workspace path
            ]
        },
    ):
        result = await _concat_videos(uuid.uuid4(), tmp_path, arguments, typed=True)
        assert isinstance(result, ToolExecutionOutcome)
        assert result.status == "failed"
        assert result.error_code == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_concat_videos_rejects_paths_outside_the_workspace(tmp_path):
    result = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": ["workspace/videos/a.mp4", "../outside.mp4"]},
        typed=True,
    )

    assert isinstance(result, ToolExecutionOutcome)
    assert result.status == "failed"
    assert not (tmp_path / "workspace/videos").exists()


@pytest.mark.asyncio
async def test_concat_videos_rejects_missing_or_non_mp4_inputs(tmp_path):
    clip = _write_clip(tmp_path, "workspace/videos/shot_01.mp4")

    missing = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": [clip, "workspace/videos/missing.mp4"]},
        typed=True,
    )
    assert missing.status == "failed"

    not_mp4 = tmp_path / "workspace/videos/notes.txt"
    not_mp4.write_text("not a video", encoding="utf-8")
    wrong_suffix = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": [clip, "workspace/videos/notes.txt"]},
        typed=True,
    )
    assert wrong_suffix.status == "failed"


# ── Media contract failures fail closed with actionable codes ────────────


@pytest.mark.asyncio
async def test_concat_videos_rejects_undecodable_clips(tmp_path):
    clip = _write_clip(tmp_path, "workspace/videos/shot_01.mp4")
    broken = tmp_path / "workspace/videos/shot_02.mp4"
    broken.write_bytes(b"not an mp4 payload")

    result = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": [clip, "workspace/videos/shot_02.mp4"]},
        typed=True,
    )

    assert result.status == "failed"
    assert result.error_code == "video_concat_media_contract_invalid"
    assert "shot" in result.result_summary.lower() or "MP4" in result.result_summary


@pytest.mark.asyncio
async def test_concat_videos_fails_closed_on_canvas_mismatch(tmp_path):
    first = _write_clip(tmp_path, "workspace/videos/shot_01.mp4", size="640x360")
    second = _write_clip(tmp_path, "workspace/videos/shot_02.mp4", size="320x240")

    result = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": [first, second]},
        typed=True,
    )

    assert result.status == "failed"
    assert result.error_code == "video_concat_media_contract_invalid"
    # The failure must name the conflicting canvases instead of rescaling.
    assert "640x360" in result.result_summary
    assert "320x240" in result.result_summary
    assert not (tmp_path / "workspace/videos/concat_video.mp4").exists()


# ── Real ffmpeg merge ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concat_videos_merges_same_canvas_clips_into_browser_safe_output(
    tmp_path,
):
    first = _write_clip(tmp_path, "workspace/videos/shot_01.mp4", duration=1.2)
    second = _write_clip(tmp_path, "workspace/videos/shot_02.mp4", duration=0.8)
    credit_reservation = AsyncMock(
        side_effect=AssertionError("concat_videos must never reserve Credits")
    )

    with patch(
        "app.services.credit_service.reserve_credits_in_session",
        credit_reservation,
    ):
        result = await _concat_videos(
            uuid.uuid4(),
            tmp_path,
            {
                "video_paths": [first, second],
                "save_path": "workspace/videos/final.mp4",
            },
            typed=True,
        )

    assert result.status == "succeeded"
    credit_reservation.assert_not_awaited()
    output = tmp_path / "workspace/videos/final.mp4"
    assert output.is_file()
    info = await validate_generated_video(output.read_bytes(), label="concat output")
    assert info.codec_name == "h264"
    assert info.pixel_format == "yuv420p"
    assert info.fast_start is True
    assert (info.width, info.height) == (640, 360)
    # Output duration is the sum of the shot durations (ffmpeg timestamp slack).
    assert abs(info.duration_seconds - 2.0) < 0.4

    receipt = json.loads(result.result_summary)
    assert receipt["status"] == "succeeded"
    assert receipt["workspace_path"] == "workspace/videos/final.mp4"
    assert receipt["mode"] == "concat"
    assert receipt["shot_count"] == 2
    assert len(receipt["shot_sha256"]) == 2
    assert receipt["fast_start"] is True
    assert result.artifact_refs
    assert result.metadata["operation"] == "video_concat"


@pytest.mark.asyncio
async def test_concat_videos_rejects_overlong_packages(tmp_path, monkeypatch):
    first = _write_clip(tmp_path, "workspace/videos/shot_01.mp4")
    second = _write_clip(tmp_path, "workspace/videos/shot_02.mp4")
    monkeypatch.setattr(
        agent_tools,
        "CONCAT_VIDEOS_MAX_TOTAL_DURATION_SECONDS",
        1.0,
    )

    result = await _concat_videos(
        uuid.uuid4(),
        tmp_path,
        {"video_paths": [first, second]},
        typed=True,
    )

    assert result.status == "failed"
    assert result.error_code == "video_concat_duration_exceeds_limit"
