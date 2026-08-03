#!/usr/bin/env python3
"""Verify that creative benchmark receipts are comparable without calling providers.

The provider benchmark runner and the external-artifact importer both record
the benchmark plan/case hashes.  This verifier makes that contract explicit:
every receipt must point to the exact plan, case, canonical brief and (when an
artifact exists) an unchanged file.  It never retries a provider, reads a key,
or changes benchmark data.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.creative_provider_benchmark import (  # noqa: E402
    benchmark_case_text,
    load_case,
    sha256_text,
)


ReceiptStatus = Literal["valid", "invalid"]
MODALITY_ARTIFACT_TYPES = {
    "image": {"image"},
    "video": {"mp4"},
    "presentation": {"pptx", "pdf"},
}


class ReceiptProvenanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_name: str
    provider: str | None = None
    case_key: str | None = None
    modality: str | None = None
    evidence_level: str | None = None
    artifact_verified: bool = False
    status: ReceiptStatus
    issues: tuple[str, ...] = ()


class BenchmarkReceiptProvenanceAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    benchmark_id: str
    benchmark_plan_sha256: str
    receipt_count: int
    required_providers: tuple[str, ...] = ()
    status: ReceiptStatus
    results: tuple[ReceiptProvenanceResult, ...]
    issues: tuple[str, ...] = ()
    checked_at: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("receipt must be a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("receipt root must be an object")
    return payload


def _artifact_path(value: object, *, artifact_root: Path | None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact_path must be a non-empty string")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and artifact_root is not None:
        candidate = artifact_root / candidate
    return candidate.resolve()


def _verify_artifact_set(
    *,
    paths: object,
    hashes: object,
    artifact_root: Path | None,
    expected_artifact_types: set[str] | None,
) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(paths, dict) or not isinstance(hashes, dict):
        return False, ("artifact_paths_and_hashes_must_be_objects",)
    if set(paths) != set(hashes):
        return False, ("artifact_paths_and_hashes_keys_mismatch",)
    if expected_artifact_types is not None and set(paths) != expected_artifact_types:
        return False, ("artifact_types_do_not_match_modality",)
    issues: list[str] = []
    verified = True
    for artifact_type in sorted(paths):
        try:
            artifact_path = _artifact_path(
                paths[artifact_type],
                artifact_root=artifact_root,
            )
            if artifact_path.is_symlink() or not artifact_path.is_file():
                raise ValueError("artifact is not a regular file")
            actual_hash = _sha256_file(artifact_path)
            expected_hash = hashes[artifact_type]
            if not isinstance(expected_hash, str) or actual_hash != expected_hash:
                raise ValueError("artifact hash differs")
        except (OSError, TypeError, ValueError):
            verified = False
            issues.append(f"artifact_sha256_mismatch:{artifact_type}")
    return verified, tuple(issues)


def _verify_one_receipt(
    *,
    plan_path: Path,
    plan_payload: dict[str, Any],
    plan_sha256: str,
    receipt_path: Path,
    artifact_root: Path | None,
) -> ReceiptProvenanceResult:
    issues: list[str] = []
    provider: str | None = None
    case_key: str | None = None
    modality: str | None = None
    evidence_level: str | None = None
    artifact_verified = False
    expected_artifact_types: set[str] | None = None
    try:
        receipt = _read_json(receipt_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return ReceiptProvenanceResult(
            receipt_name=receipt_path.name,
            status="invalid",
            issues=(f"invalid_receipt:{type(exc).__name__}",),
        )

    provider_value = receipt.get("provider")
    provider = str(provider_value).strip() if provider_value else None
    case_value = receipt.get("case_key")
    case_key = str(case_value).strip() if case_value else None
    modality_value = receipt.get("modality")
    modality = str(modality_value).strip().lower() if modality_value else None
    evidence_value = receipt.get("evidence_level")
    evidence_level = str(evidence_value).strip() if evidence_value else None

    if receipt.get("benchmark_id") != plan_payload.get("benchmark_id"):
        issues.append("benchmark_id_mismatch")
    if not case_key or case_key not in plan_payload.get("cases", {}):
        issues.append("unknown_case_key")
        case = None
    else:
        try:
            case = load_case(plan_path, case_key)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            issues.append("case_could_not_be_loaded")
            case = None

    if case is not None:
        expected_plan_hash = case["__benchmark_plan_sha256"]
        expected_case_hash = case["__benchmark_case_sha256"]
        if receipt.get("benchmark_plan_sha256") != expected_plan_hash:
            issues.append("benchmark_plan_sha256_mismatch")
        if receipt.get("benchmark_case_sha256") != expected_case_hash:
            issues.append("benchmark_case_sha256_mismatch")
        expected_modality = str(case.get("modality") or "").strip().lower()
        expected_artifact_types = MODALITY_ARTIFACT_TYPES.get(expected_modality)
        if modality != expected_modality:
            issues.append("modality_mismatch")
        expected_prompt_hash = sha256_text(benchmark_case_text(case))
        if receipt.get("prompt_sha256") != expected_prompt_hash:
            issues.append("prompt_sha256_mismatch")

    receipt_plan_hash = receipt.get("benchmark_plan_sha256")
    if receipt_plan_hash != plan_sha256:
        issues.append("receipt_plan_hash_does_not_match_plan_file")

    artifact_paths_value = receipt.get("artifact_paths")
    artifact_hashes_value = receipt.get("artifact_sha256")
    if artifact_paths_value is not None or (
        isinstance(artifact_hashes_value, dict)
    ):
        artifact_verified, artifact_issues = _verify_artifact_set(
            paths=artifact_paths_value,
            hashes=artifact_hashes_value,
            artifact_root=artifact_root,
            expected_artifact_types=expected_artifact_types,
        )
        issues.extend(artifact_issues)
    else:
        artifact_value = receipt.get("artifact_path")
        artifact_hash = receipt.get("artifact_sha256")
        if artifact_value is None:
            if artifact_hash not in (None, ""):
                issues.append("artifact_hash_without_artifact_path")
        else:
            try:
                artifact_path = _artifact_path(
                    artifact_value,
                    artifact_root=artifact_root,
                )
                if artifact_path.is_symlink() or not artifact_path.is_file():
                    raise ValueError("artifact is not a regular file")
                actual_hash = _sha256_file(artifact_path)
                if not isinstance(artifact_hash, str) or actual_hash != artifact_hash:
                    issues.append("artifact_sha256_mismatch")
                else:
                    artifact_verified = True
            except (OSError, TypeError, ValueError):
                issues.append("artifact_unavailable")

    return ReceiptProvenanceResult(
        receipt_name=receipt_path.name,
        provider=provider,
        case_key=case_key,
        modality=modality,
        evidence_level=evidence_level,
        artifact_verified=artifact_verified,
        status="invalid" if issues else "valid",
        issues=tuple(dict.fromkeys(issues)),
    )


def audit_benchmark_receipts(
    plan_path: Path,
    receipt_paths: Sequence[Path],
    *,
    artifact_root: Path | None = None,
    required_providers: Sequence[str] = (),
) -> BenchmarkReceiptProvenanceAudit:
    """Return a deterministic, provider-free provenance audit."""

    plan_path = plan_path.expanduser().resolve()
    plan_bytes = plan_path.read_bytes()
    plan_payload = json.loads(plan_bytes)
    if not isinstance(plan_payload, dict) or not isinstance(
        plan_payload.get("benchmark_id"), str
    ):
        raise ValueError("benchmark plan must contain a string benchmark_id")
    if not isinstance(plan_payload.get("cases"), dict):
        raise ValueError("benchmark plan must contain a cases object")
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()

    normalized_receipts = tuple(
        path.expanduser().resolve() for path in receipt_paths
    )
    results = tuple(
        _verify_one_receipt(
            plan_path=plan_path,
            plan_payload=plan_payload,
            plan_sha256=plan_sha256,
            receipt_path=path,
            artifact_root=artifact_root,
        )
        for path in normalized_receipts
    )
    issues: list[str] = []
    normalized_required_providers = tuple(
        dict.fromkeys(
            provider.strip().lower()
            for provider in required_providers
            if str(provider).strip()
        )
    )
    seen_pairs: set[tuple[str, str]] = set()
    case_providers: dict[str, set[str]] = {}
    for result in results:
        if result.provider and result.case_key:
            pair = (result.provider, result.case_key)
            if pair in seen_pairs:
                issues.append(
                    f"duplicate_provider_case:{result.provider}:{result.case_key}"
                )
            seen_pairs.add(pair)
            case_providers.setdefault(result.case_key, set()).add(
                result.provider.lower()
            )
    for case_key, providers in sorted(case_providers.items()):
        for provider in normalized_required_providers:
            if provider not in providers:
                issues.append(f"missing_provider_for_case:{case_key}:{provider}")
    issues.extend(
        f"{result.receipt_name}:{issue}"
        for result in results
        for issue in result.issues
    )
    return BenchmarkReceiptProvenanceAudit(
        benchmark_id=str(plan_payload["benchmark_id"]),
        benchmark_plan_sha256=plan_sha256,
        receipt_count=len(results),
        required_providers=normalized_required_providers,
        status="invalid" if issues else "valid",
        results=results,
        issues=tuple(dict.fromkeys(issues)),
        checked_at=datetime.now(UTC).isoformat(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Root for relative artifact_path values in receipts.",
    )
    parser.add_argument(
        "--required-provider",
        action="append",
        default=[],
        help="Require every observed case to contain this provider; repeatable.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit = audit_benchmark_receipts(
        args.plan,
        args.receipt,
        artifact_root=args.artifact_root,
        required_providers=args.required_provider,
    )
    rendered = audit.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if audit.status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
