#!/usr/bin/env python3
"""Run one bounded real-provider creative benchmark without exposing secrets.

This script is intentionally provider-level evidence. It does not replace the
durable Agent Tool and browser business-flow gates.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.models.tenant  # noqa: E402,F401 - registers the credential tenant FK
from app.services.media_provider_routing import (  # noqa: E402
    MINIMAX_PROVIDER,
    minimax_video_requires_first_frame,
    prepare_media_provider,
)
from app.services.llm.load_balancer import NoCredentialAvailable  # noqa: E402
from app.services.volcengine_agent_plan import (  # noqa: E402
    IMAGE_MODEL as VOLCENGINE_IMAGE_MODEL,
    VIDEO_MODEL as VOLCENGINE_VIDEO_MODEL,
    VolcengineAgentPlanError,
    VolcengineAgentPlanRejected,
    create_video_task as create_volcengine_video_task,
    download_video as download_volcengine_video,
    generate_image as generate_volcengine_image,
    image_size_for_aspect_ratio,
    normalized_video_status as normalized_volcengine_video_status,
    query_video_task as query_volcengine_video_task,
    video_url_from_response as volcengine_video_url_from_response,
)
from app.services.media_assets import (  # noqa: E402
    validate_generated_image,
    validate_generated_video,
    validate_image_delivery_contract,
    validate_video_delivery_contract,
)


DEFAULT_PLAN = (
    REPO_DIR / "tmp/creative-benchmark/2026-07-26-agent-plan/benchmark-plan.json"
)
DEFAULT_OUTPUT_DIR = REPO_DIR / "tmp/creative-benchmark/2026-07-26-agent-plan"
TERMINAL_VIDEO_STATES = {"Success", "Fail"}
VOLCENGINE_VIDEO_MODELS = (
    "doubao-seedance-2.0",
    "doubao-seedance-2.0-fast",
    "doubao-seedance-2.0-mini",
)


class BenchmarkContractError(ValueError):
    """A case/provider pair that would spend quota without meeting the brief."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BenchmarkCostGuardrailError(BenchmarkContractError):
    """A bounded benchmark was already consumed for this provider/case."""

    def __init__(self, code: str, *, snapshot: dict[str, int | None]):
        super().__init__(code)
        self.snapshot = snapshot


class BenchmarkArtifactValidationError(BenchmarkContractError):
    """A provider returned bytes that failed the benchmark delivery contract."""

    def __init__(self, code: str, *, provider_receipt: dict[str, Any]):
        super().__init__(code)
        self.provider_receipt = provider_receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("volcengine_agent_plan", "minimax"), required=True)
    parser.add_argument(
        "--case",
        required=True,
        help="Case key from the benchmark plan.",
    )
    parser.add_argument("--saas-tier", choices=("lite", "pro", "ultra"), default="ultra")
    parser.add_argument(
        "--volcengine-video-model",
        choices=VOLCENGINE_VIDEO_MODELS,
        help=(
            "Benchmark-only explicit model selection. Production routing must "
            "continue to use the reviewed provider capability matrix."
        ),
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--reference-image",
        type=Path,
        help="Optional local image reference for an image-generation case.",
    )
    parser.add_argument(
        "--first-frame-image",
        type=Path,
        help="Optional local first-frame image for a video-generation case.",
    )
    parser.add_argument("--poll-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--confirm-paid-provider-call",
        action="store_true",
        help=(
            "Explicitly authorize this one bounded real-provider generation. "
            "Without this flag the command fails closed before provider selection."
        ),
    )
    return parser.parse_args()


def load_case(plan_path: Path, case_key: str) -> dict[str, Any]:
    plan_bytes = plan_path.read_bytes()
    payload = json.loads(plan_bytes)
    case = payload["cases"][case_key]
    loaded_case = {
        "benchmark_id": payload["benchmark_id"],
        "case_key": case_key,
        **case,
    }
    # Keep benchmark provenance separate from the public prompt payload.  The
    # hashes are persisted in receipts, while the plan itself never enters a
    # provider request and no secret-bearing value is copied into the receipt.
    loaded_case["__benchmark_plan_sha256"] = sha256_bytes(plan_bytes)
    loaded_case["__benchmark_case_sha256"] = sha256_text(
        json.dumps(
            loaded_case,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return loaded_case


def benchmark_generation_limit(plan_path: Path, modality: str) -> int | None:
    """Return the plan-owned generation limit for a direct media case.

    The benchmark plan is the cost authority.  A missing or malformed limit
    must fail closed before credential selection, so rerunning a paid case
    cannot silently become an unbounded experiment.
    """

    normalized_modality = str(modality or "").strip().lower()
    if normalized_modality not in {"image", "video"}:
        return None
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    guardrail = payload.get("cost_guardrail")
    if not isinstance(guardrail, dict):
        raise BenchmarkContractError("missing_cost_guardrail")
    retries = guardrail.get("automatic_quality_retries")
    if retries != 0:
        raise BenchmarkContractError("automatic_quality_retries_must_be_zero")
    raw_limit = guardrail.get(f"{normalized_modality}_generations_per_provider")
    if (
        not isinstance(raw_limit, int)
        or isinstance(raw_limit, bool)
        or raw_limit < 1
    ):
        raise BenchmarkContractError(
            f"invalid_{normalized_modality}_generation_limit"
        )
    return raw_limit


def existing_successful_generation_count(
    output_dir: Path,
    *,
    benchmark_id: str,
    case_key: str,
    provider: str,
) -> int:
    """Count successful receipts for one exact plan/provider/case tuple."""

    count = 0
    for receipt_path in output_dir.glob(f"{provider}-{case_key}*.receipt.json"):
        if receipt_path.is_symlink() or not receipt_path.is_file():
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            receipt.get("benchmark_id") == benchmark_id
            and receipt.get("case_key") == case_key
            and receipt.get("provider") == provider
            and receipt.get("artifact_path")
        ):
            count += 1
    return count


def enforce_benchmark_cost_guardrail(
    plan_path: Path,
    *,
    case: dict[str, Any],
    provider: str,
    output_dir: Path,
) -> dict[str, int | None]:
    """Fail before provider selection when the bounded plan is exhausted."""

    limit = benchmark_generation_limit(plan_path, str(case.get("modality")))
    existing = existing_successful_generation_count(
        output_dir,
        benchmark_id=str(case["benchmark_id"]),
        case_key=str(case["case_key"]),
        provider=provider,
    )
    snapshot: dict[str, int | None] = {
        "max_generations_per_provider": limit,
        "existing_successful_generations": existing,
    }
    if limit is not None and existing >= limit:
        raise BenchmarkCostGuardrailError(
            "cost_guardrail_exhausted",
            snapshot=snapshot,
        )
    return snapshot


def require_paid_provider_call_authorization(confirmed: bool) -> None:
    """Require an explicit per-run authorization before selecting a Provider.

    The benchmark plan limits the number of generations, but a cost limit is
    not user authorization.  Keep the default CLI path provider-free so a
    copied command, retry, or automated continuation cannot spend credits
    without an explicit human decision for this run.
    """

    if not confirmed:
        raise BenchmarkContractError(
            "explicit_paid_provider_call_authorization_required"
        )


def enforce_direct_provider_benchmark_contract(
    case: dict[str, Any],
    *,
    paid_call_confirmed: bool,
) -> None:
    """Validate the direct-media boundary before selecting a Provider."""

    modality = str(case.get("modality") or "").strip().lower()
    if modality not in {"image", "video"}:
        raise BenchmarkContractError("presentation_requires_artifact_pair")
    require_paid_provider_call_authorization(paid_call_confirmed)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def benchmark_case_text(case: dict[str, Any]) -> str:
    """Return the canonical brief text used by provider receipts.

    Image/video cases carry a prompt, while the PPT case is an Agent/Artifact
    brief with a story and acceptance contract instead.  The receipt must be
    hashable for both shapes and must never crash while recording a guarded
    rejection.
    """

    prompt = case.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return json.dumps(
        {
            key: value
            for key, value in case.items()
            if not key.startswith("__")
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def validate_benchmark_artifact(
    case: dict[str, Any],
    artifact_bytes: bytes,
) -> dict[str, Any]:
    """Validate provider output before it enters a benchmark receipt.

    The benchmark intentionally does not transcode or repair provider output:
    it records the raw artifact and its delivery properties so a comparison
    cannot hide a provider defect behind Astra's later compatibility layer.
    """

    modality = str(case.get("modality") or "").strip().lower()
    if modality == "image":
        width, height = validate_generated_image(artifact_bytes)
        validate_image_delivery_contract(
            width,
            height,
            expected_aspect_ratio=case.get("aspect_ratio"),
        )
        return {
            "kind": "image",
            "width": width,
            "height": height,
            "aspect_ratio": case.get("aspect_ratio"),
        }
    if modality == "video":
        info = await validate_generated_video(
            artifact_bytes,
            label="Benchmark provider video",
            require_browser_safe=False,
        )
        validate_video_delivery_contract(
            info,
            expected_duration_seconds=case.get("duration_seconds"),
            expected_aspect_ratio=case.get("aspect_ratio"),
        )
        return {
            "kind": "video",
            "width": info.width,
            "height": info.height,
            "duration_seconds": round(info.duration_seconds, 3),
            "codec_name": info.codec_name,
            "pixel_format": info.pixel_format,
            "audio_codec_name": info.audio_codec_name,
            "fast_start": info.fast_start,
            "browser_safe": (
                info.codec_name == "h264"
                and info.pixel_format in {"yuv420p", "yuvj420p"}
                and info.audio_codec_name in {None, "aac", "mp3"}
                and info.fast_start
            ),
        }
    raise BenchmarkContractError("unsupported_benchmark_artifact_modality")


def agent_plan_image_size(case: dict[str, Any], quality: str | None) -> str:
    return image_size_for_aspect_ratio(
        quality or "2K",
        str(case["aspect_ratio"]),
    )


def save_receipt(output_dir: Path, stem: str, receipt: dict[str, Any]) -> Path:
    receipt_path = output_dir / f"{stem}.receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(receipt_path, 0o600)
    return receipt_path


def failure_receipt_stem(stem: str, error: Exception) -> str:
    """Keep a guardrail rejection from overwriting a prior success receipt."""

    if isinstance(error, BenchmarkCostGuardrailError):
        return f"{stem}.guardrail-blocked"
    return stem


def display_path(path: Path) -> str:
    """Return a stable receipt path without requiring output under the repo.

    Benchmark runs are often written to an isolated temporary directory.  A
    failed preflight must still emit its structured receipt and exit with the
    contract error, rather than raising a secondary ``relative_to`` error
    merely because that directory is outside the checkout.
    """

    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_DIR))
    except ValueError:
        return str(resolved)


def provider_failure_receipt(error: Exception) -> dict[str, Any]:
    """Return provider evidence without persisting messages or secrets."""

    receipt: dict[str, Any] = {
        "error_type": type(error).__name__,
        "provider_accepted": False,
        "status": (
            "rejected_before_acceptance"
            if isinstance(error, VolcengineAgentPlanRejected)
            else "failed_before_artifact"
        ),
    }
    if isinstance(error, VolcengineAgentPlanError):
        receipt["provider_error_code"] = error.provider_code
        receipt["provider_http_status"] = error.http_status
    if isinstance(error, NoCredentialAvailable):
        receipt["credential_unavailable_reason"] = error.reason_code.value
    if isinstance(error, BenchmarkContractError):
        receipt["benchmark_contract_error"] = error.code
    if isinstance(error, BenchmarkCostGuardrailError):
        receipt["cost_guardrail"] = error.snapshot
    if isinstance(error, BenchmarkArtifactValidationError):
        receipt["provider_accepted"] = True
        receipt["status"] = "provider_artifact_contract_failed"
        receipt["provider_receipt"] = error.provider_receipt
    return receipt


def output_stem(
    provider: str,
    case_key: str,
    *,
    explicit_volcengine_video_model: str | None,
) -> str:
    stem = f"{provider}-{case_key}"
    if provider == "volcengine_agent_plan" and explicit_volcengine_video_model:
        safe_model = "".join(
            char if char.isalnum() or char in {".", "-"} else "-"
            for char in explicit_volcengine_video_model
        )
        return f"{stem}-{safe_model}"
    return stem


async def generate_image(
    *,
    provider: str,
    saas_tier: str,
    case: dict[str, Any],
    reference_image_path: Path | None,
) -> tuple[bytes, dict[str, Any]]:
    from app.services.media_assets import image_reference_for_provider

    prepared = await prepare_media_provider(
        provider,
        modality="image",
        saas_tier=saas_tier,
        minimax_model="image-01",
    )
    accepted_reference_hash: str | None = None
    reference_image = None
    reference_image_sha256 = None
    if reference_image_path is not None:
        resolved_reference = reference_image_path.resolve()
        reference_image = image_reference_for_provider(
            resolved_reference.parent,
            resolved_reference.name,
            label="Benchmark reference image",
        )
        reference_image_sha256 = sha256_bytes(resolved_reference.read_bytes())

    async def record_acceptance(reference: str | None) -> None:
        nonlocal accepted_reference_hash
        accepted_reference_hash = sha256_text(reference)

    if provider == "volcengine_agent_plan":
        requested_quality = prepared.size or "2K"
        requested_size = agent_plan_image_size(case, requested_quality)
        image_bytes = await generate_volcengine_image(
            api_key=prepared.api_key,
            base_url=prepared.base_url,
            prompt=case["prompt"],
            model=prepared.model,
            size=requested_size,
            reference_image=reference_image,
            on_provider_accepted=record_acceptance,
        )
    else:
        requested_quality = None
        requested_size = None
        from app.services.agent_tools import _generate_image_minimax

        image_bytes = await _generate_image_minimax(
            api_key=prepared.api_key,
            base_url=prepared.base_url,
            model=prepared.model,
            prompt=case["prompt"],
            aspect_ratio=case["aspect_ratio"],
            reference_image=reference_image,
            on_provider_accepted=record_acceptance,
        )
    return image_bytes, {
        "credential_id": str(prepared.credential_id),
        "model": prepared.model,
        "provider_acceptance_reference_sha256": accepted_reference_hash,
        "provider_plan_tier": prepared.plan_tier,
        "reference_image_sha256": reference_image_sha256,
        "requested_quality": requested_quality,
        "requested_size": requested_size,
    }


async def generate_video(
    *,
    provider: str,
    saas_tier: str,
    case: dict[str, Any],
    timeout_seconds: int,
    volcengine_video_model: str | None = None,
    first_frame_image_path: Path | None = None,
) -> tuple[bytes, dict[str, Any]]:
    minimax_model = "MiniMax-Hailuo-2.3"
    if (
        provider == MINIMAX_PROVIDER
        and minimax_video_requires_first_frame(
            str(case["aspect_ratio"]),
            first_frame_image_path,
        )
    ):
        raise BenchmarkContractError(
            "minimax_non_16_9_video_requires_first_frame"
        )
    prepared = await prepare_media_provider(
        provider,
        modality="video",
        saas_tier=saas_tier,
        minimax_model=minimax_model,
    )
    duration = int(case["duration_seconds"])
    started = time.monotonic()
    first_frame_image = None
    first_frame_image_sha256 = None
    if first_frame_image_path is not None:
        from app.services.media_assets import image_reference_for_provider

        resolved_frame = first_frame_image_path.resolve()
        first_frame_image = image_reference_for_provider(
            resolved_frame.parent,
            resolved_frame.name,
            label="Benchmark first-frame image",
            require_video_dimensions=True,
        )
        first_frame_image_sha256 = sha256_bytes(resolved_frame.read_bytes())

    if provider == "volcengine_agent_plan":
        requested_model = volcengine_video_model or prepared.model
        task_id = await create_volcengine_video_task(
            api_key=prepared.api_key,
            base_url=prepared.base_url,
            prompt=case["prompt"],
            model=requested_model,
            duration=duration,
            resolution=prepared.resolution or "720p",
            ratio=case["aspect_ratio"],
            first_frame_image=first_frame_image,
        )
        status_payload: dict[str, Any] = {}
        while time.monotonic() - started < timeout_seconds:
            status_payload = await query_volcengine_video_task(
                api_key=prepared.api_key,
                base_url=prepared.base_url,
                task_id=task_id,
            )
            status = normalized_volcengine_video_status(status_payload)
            if status in TERMINAL_VIDEO_STATES:
                break
            await asyncio.sleep(8)
        else:
            raise TimeoutError("Volcengine video task did not reach a terminal state")
        if normalized_volcengine_video_status(status_payload) != "Success":
            raise RuntimeError("Volcengine video task reached a failed terminal state")
        video_url = volcengine_video_url_from_response(status_payload)
        if not video_url:
            raise RuntimeError("Volcengine video task succeeded without a downloadable result")
        video_bytes = await download_volcengine_video(video_url)
        status = "Success"
    else:
        from app.services.agent_tools import (
            _minimax_create_video_task,
            _minimax_download_file,
            _minimax_query_video_task,
            _minimax_retrieve_file_download_url,
            _minimax_video_file_id,
            _minimax_video_status,
        )

        task_id = await _minimax_create_video_task(
            api_key=prepared.api_key,
            base_url=prepared.base_url,
            model=prepared.model,
            prompt=case["prompt"],
            duration=duration,
            resolution="768P",
            first_frame_image=first_frame_image,
            prompt_optimizer=True,
        )
        status_payload = {}
        while time.monotonic() - started < timeout_seconds:
            status_payload = await _minimax_query_video_task(
                prepared.api_key,
                prepared.base_url,
                task_id,
            )
            status = _minimax_video_status(status_payload)
            if status in {"Success", "Fail"}:
                break
            await asyncio.sleep(8)
        else:
            raise TimeoutError("MiniMax video task did not reach a terminal state")
        if status != "Success":
            raise RuntimeError("MiniMax video task reached a failed terminal state")
        file_id = _minimax_video_file_id(status_payload)
        if not file_id:
            raise RuntimeError("MiniMax video task succeeded without a file id")
        video_url = await _minimax_retrieve_file_download_url(
            prepared.api_key,
            prepared.base_url,
            file_id,
        )
        video_bytes = await _minimax_download_file(video_url)

    return video_bytes, {
        "credential_id": str(prepared.credential_id),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model": (
            requested_model
            if provider == "volcengine_agent_plan"
            else prepared.model
        ),
        "first_frame_image_sha256": first_frame_image_sha256,
        "provider_plan_tier": prepared.plan_tier,
        "provider_task_id_sha256": sha256_text(task_id),
        "requested_duration_seconds": duration,
        "requested_resolution": prepared.resolution if provider == "volcengine_agent_plan" else "768P",
        "status": status,
    }


async def main() -> None:
    args = parse_args()
    case = load_case(args.plan.resolve(), args.case)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem(
        args.provider,
        args.case,
        explicit_volcengine_video_model=args.volcengine_video_model,
    )
    started_at = datetime.now(UTC)
    cost_guardrail: dict[str, int | None] = {
        "max_generations_per_provider": None,
        "existing_successful_generations": 0,
    }

    try:
        cost_guardrail = enforce_benchmark_cost_guardrail(
            args.plan.resolve(),
            case=case,
            provider=args.provider,
            output_dir=output_dir,
        )
        enforce_direct_provider_benchmark_contract(
            case,
            paid_call_confirmed=args.confirm_paid_provider_call,
        )
        if case["modality"] == "image":
            artifact_bytes, provider_receipt = await generate_image(
                provider=args.provider,
                saas_tier=args.saas_tier,
                case=case,
                reference_image_path=args.reference_image,
            )
            extension = "png"
        elif case["modality"] == "video":
            artifact_bytes, provider_receipt = await generate_video(
                provider=args.provider,
                saas_tier=args.saas_tier,
                case=case,
                timeout_seconds=args.poll_timeout_seconds,
                volcengine_video_model=args.volcengine_video_model,
                first_frame_image_path=args.first_frame_image,
            )
            extension = "mp4"
        try:
            artifact_contract = await validate_benchmark_artifact(
                case,
                artifact_bytes,
            )
        except Exception as exc:
            raise BenchmarkArtifactValidationError(
                f"{case['modality']}_artifact_contract_invalid",
                provider_receipt=provider_receipt,
            ) from exc
    except Exception as error:
        if isinstance(error, BenchmarkCostGuardrailError):
            cost_guardrail = error.snapshot
        failed_at = datetime.now(UTC)
        requested_model = None
        if (
            args.provider == "volcengine_agent_plan"
            and isinstance(error, VolcengineAgentPlanError)
        ):
            requested_model = (
                VOLCENGINE_IMAGE_MODEL
                if case["modality"] == "image"
                else args.volcengine_video_model or VOLCENGINE_VIDEO_MODEL
            )
        receipt = {
            "artifact_path": None,
            "artifact_sha256": None,
            "benchmark_id": case["benchmark_id"],
            "benchmark_case_sha256": case.get("__benchmark_case_sha256"),
            "benchmark_plan_sha256": case.get("__benchmark_plan_sha256"),
            "bytes": 0,
            "case_key": case["case_key"],
            "completed_at": failed_at.isoformat(),
            "cost_guardrail": cost_guardrail,
            "modality": case["modality"],
            "prompt_sha256": sha256_text(benchmark_case_text(case)),
            "provider": args.provider,
            "provider_receipt": {
                **provider_failure_receipt(error),
                **(
                    {"requested_model": requested_model}
                    if requested_model is not None
                    else {}
                ),
            },
            "saas_tier": args.saas_tier,
            "started_at": started_at.isoformat(),
        }
        receipt_path = save_receipt(
            output_dir,
            failure_receipt_stem(stem, error),
            receipt,
        )
        print(
            json.dumps(
                {
                    "provider": args.provider,
                    "receipt_path": display_path(receipt_path),
                    "status": receipt["provider_receipt"]["status"],
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None

    artifact_path = output_dir / f"{stem}.{extension}"
    artifact_path.write_bytes(artifact_bytes)
    os.chmod(artifact_path, 0o600)
    completed_at = datetime.now(UTC)
    receipt = {
        "artifact_path": display_path(artifact_path),
        "artifact_sha256": sha256_bytes(artifact_bytes),
        "benchmark_id": case["benchmark_id"],
        "benchmark_case_sha256": case.get("__benchmark_case_sha256"),
        "benchmark_plan_sha256": case.get("__benchmark_plan_sha256"),
        "bytes": len(artifact_bytes),
        "case_key": case["case_key"],
        "completed_at": completed_at.isoformat(),
        "cost_guardrail": cost_guardrail,
        "modality": case["modality"],
        "prompt_sha256": sha256_text(benchmark_case_text(case)),
        "provider": args.provider,
        "artifact_contract": artifact_contract,
        "provider_receipt": provider_receipt,
        "saas_tier": args.saas_tier,
        "started_at": started_at.isoformat(),
    }
    receipt_path = save_receipt(output_dir, stem, receipt)
    print(
        json.dumps(
            {
                "artifact_path": receipt["artifact_path"],
                "bytes": receipt["bytes"],
                "provider": args.provider,
                "receipt_path": display_path(receipt_path),
                "status": "succeeded",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
