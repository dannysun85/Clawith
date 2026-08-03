#!/usr/bin/env python3
"""Import an externally generated creative artifact into a guarded benchmark.

The live Provider benchmark script intentionally owns paid generation.  This
command is the complementary, provider-free path for artifacts produced by a
web product such as Doubao: it binds the file to the exact benchmark plan and
case, copies it into an isolated output directory, and records only hashes and
machine-observable delivery facts.  It never calls a Provider and never marks
an artifact commercially usable.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_artifact_evaluation import (  # noqa: E402
    CreativeArtifactContract,
    observe_creative_artifacts,
)
from scripts.creative_provider_benchmark import (  # noqa: E402
    BenchmarkContractError,
    benchmark_case_text,
    load_case,
    sha256_bytes,
    sha256_text,
)


EXTERNAL_PROVIDERS = ("doubao",)
MODALITY_ARTIFACT_TYPES = {
    "image": ("image",),
    "video": ("mp4",),
    "presentation": ("pptx", "pdf"),
}


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    slug = slug.strip(".-")
    if not slug:
        raise ValueError("provider and case names must contain safe characters")
    return slug[:120]


def _regular_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    if resolved.stat().st_size <= 0:
        raise ValueError(f"{label} must not be empty")
    return resolved


def _contract_for_case(
    case: Mapping[str, Any],
    *,
    minimum_picture_coverage_ratio: float | None,
) -> CreativeArtifactContract:
    modality = str(case.get("modality") or "").strip().lower()
    if modality not in MODALITY_ARTIFACT_TYPES:
        raise BenchmarkContractError("unsupported_external_artifact_modality")
    if modality == "image":
        return CreativeArtifactContract(
            modality="image",
            aspect_ratio=str(case["aspect_ratio"]),
            editable_required=False,
            preview_required=True,
        )
    if modality == "video":
        return CreativeArtifactContract(
            modality="video",
            aspect_ratio=str(case["aspect_ratio"]),
            duration_seconds=float(case["duration_seconds"]),
            audio_required=True,
            editable_required=False,
            preview_required=True,
        )
    return CreativeArtifactContract(
        modality="presentation",
        aspect_ratio=str(case.get("aspect_ratio") or "16:9"),
        page_count=int(case["slides"]),
        editable_required=True,
        preview_required=True,
        minimum_picture_coverage_ratio=minimum_picture_coverage_ratio,
    )


def _artifact_paths_for_case(
    case: Mapping[str, Any],
    supplied: Mapping[str, Path | None],
) -> dict[str, Path]:
    modality = str(case.get("modality") or "").strip().lower()
    required = MODALITY_ARTIFACT_TYPES.get(modality)
    if required is None:
        raise BenchmarkContractError("unsupported_external_artifact_modality")
    artifacts: dict[str, Path] = {}
    for artifact_type in required:
        value = supplied.get(artifact_type)
        if value is None:
            raise BenchmarkContractError(
                f"missing_external_{artifact_type}_artifact"
            )
        suffixes = {
            "image": {".png", ".jpg", ".jpeg", ".webp"},
            "mp4": {".mp4"},
            "pptx": {".pptx"},
            "pdf": {".pdf"},
        }[artifact_type]
        resolved = _regular_file(value, label=artifact_type)
        if resolved.suffix.lower() not in suffixes:
            raise ValueError(f"{artifact_type} has an unexpected file type")
        artifacts[artifact_type] = resolved
    return artifacts


def _copy_artifacts(
    artifacts: Mapping[str, Path],
    *,
    output_dir: Path,
    provider: str,
    case_key: str,
) -> dict[str, str]:
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for artifact_type, source in sorted(artifacts.items()):
        destination = artifact_dir / (
            f"{_safe_slug(provider)}-{_safe_slug(case_key)}-{artifact_type}"
            f"{source.suffix.lower()}"
        )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(
                f"external benchmark artifact already exists: {destination}"
            )
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        copied[artifact_type] = str(destination)
    return copied


async def import_external_artifact(
    *,
    provider: str,
    case: dict[str, Any],
    artifacts: Mapping[str, Path],
    output_dir: Path,
    minimum_picture_coverage_ratio: float | None = None,
    source_reference: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in EXTERNAL_PROVIDERS:
        raise ValueError(f"unsupported external provider: {provider}")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    case_key = str(case["case_key"])
    plan_hash = case.get("__benchmark_plan_sha256")
    case_hash = case.get("__benchmark_case_sha256")
    if not plan_hash or not case_hash:
        raise BenchmarkContractError("benchmark_provenance_missing")

    contract = _contract_for_case(
        case,
        minimum_picture_coverage_ratio=minimum_picture_coverage_ratio,
    )
    source_paths = _artifact_paths_for_case(
        case,
        {key: value for key, value in artifacts.items()},
    )
    copied = _copy_artifacts(
        source_paths,
        output_dir=output_dir,
        provider=normalized_provider,
        case_key=case_key,
    )
    copied_paths = {key: Path(value) for key, value in copied.items()}
    observation = await observe_creative_artifacts(contract, copied_paths)
    completed_at = datetime.now(UTC).isoformat()
    source_hashes = {
        artifact_type: sha256_bytes(path.read_bytes())
        for artifact_type, path in source_paths.items()
    }
    output_hashes = {
        artifact_type: sha256_bytes(path.read_bytes())
        for artifact_type, path in copied_paths.items()
    }
    source_reference_hash = sha256_text(source_reference)
    stem = f"{_safe_slug(normalized_provider)}-{_safe_slug(case_key)}"
    receipt_path = output_dir / f"{stem}.external.receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        raise FileExistsError(f"external benchmark receipt already exists: {receipt_path}")
    receipt: dict[str, Any] = {
        "artifact_paths": {
            artifact_type: str(path)
            for artifact_type, path in copied_paths.items()
        },
        "artifact_sha256": output_hashes,
        "benchmark_case_sha256": case_hash,
        "benchmark_id": case["benchmark_id"],
        "benchmark_plan_sha256": plan_hash,
        "case_key": case_key,
        "completed_at": completed_at,
        "cost_guardrail": {
            "generation_performed": False,
            "max_generations_per_provider": 0,
            "existing_successful_generations": 0,
        },
        "evidence_level": "external_artifact_imported",
        "modality": case["modality"],
        "prompt_sha256": sha256_text(benchmark_case_text(case)),
        "provider": normalized_provider,
        "provider_receipt": {
            "artifact_supplied": True,
            "acceptance_observed": False,
            "source_reference_sha256": source_reference_hash,
            "status": "imported_external_artifact",
        },
        "source_artifact_sha256": source_hashes,
        "structural_observation": observation.model_dump(mode="json"),
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    return receipt_path, receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=EXTERNAL_PROVIDERS, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--mp4", type=Path)
    parser.add_argument("--pptx", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--minimum-picture-coverage-ratio",
        type=float,
        help="Optional image-led PPT mean coverage gate, e.g. 0.35.",
    )
    parser.add_argument(
        "--source-reference",
        help="Optional source receipt or page reference; only its SHA-256 is stored.",
    )
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    case = load_case(args.plan.resolve(), args.case)
    receipt_path, receipt = await import_external_artifact(
        provider=args.provider,
        case=case,
        artifacts={
            "image": args.image,
            "mp4": args.mp4,
            "pptx": args.pptx,
            "pdf": args.pdf,
        },
        output_dir=args.output_dir,
        minimum_picture_coverage_ratio=args.minimum_picture_coverage_ratio,
        source_reference=args.source_reference,
    )
    print(
        json.dumps(
            {
                "provider": receipt["provider"],
                "case_key": receipt["case_key"],
                "modality": receipt["modality"],
                "receipt_path": str(receipt_path),
                "status": receipt["provider_receipt"]["status"],
                "hard_gates": {
                    key: value["passed"]
                    for key, value in receipt["structural_observation"]["hard_gates"].items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(parse_args())))
