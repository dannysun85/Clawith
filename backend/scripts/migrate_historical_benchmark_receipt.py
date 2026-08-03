#!/usr/bin/env python3
"""Create a provenance-bound copy of a legacy provider benchmark receipt.

Legacy receipts created before plan/case hashes were introduced can only be
carried forward when their canonical brief and stored Artifact hash still
match the current Benchmark plan.  This command never edits the source
receipt, never calls a Provider, and fails closed on any mismatch.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.creative_provider_benchmark import (  # noqa: E402
    benchmark_case_text,
    load_case,
    sha256_text,
)


class HistoricalReceiptMigrationError(ValueError):
    """The legacy receipt cannot be safely bound to the supplied plan."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_receipt(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise HistoricalReceiptMigrationError("source_receipt_must_be_regular_file")
    data = resolved.read_bytes()
    try:
        payload = json.loads(data)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalReceiptMigrationError("source_receipt_invalid_json") from exc
    if not isinstance(payload, dict):
        raise HistoricalReceiptMigrationError("source_receipt_root_must_be_object")
    return payload, hashlib.sha256(data).hexdigest()


def _resolve_artifact(value: object, *, artifact_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalReceiptMigrationError("source_artifact_path_missing")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = artifact_root / candidate
    return candidate.resolve()


def migrate_receipt(
    *,
    plan_path: Path,
    receipt_path: Path,
    output_path: Path,
    artifact_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    plan_path = plan_path.expanduser().resolve()
    receipt_path = receipt_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    artifact_root = (
        artifact_root.expanduser().resolve()
        if artifact_root is not None
        else REPO_DIR
    )
    receipt, source_receipt_sha256 = _read_receipt(receipt_path)
    if receipt.get("benchmark_plan_sha256") or receipt.get("benchmark_case_sha256"):
        raise HistoricalReceiptMigrationError("receipt_already_has_provenance")

    case_key = str(receipt.get("case_key") or "").strip()
    if not case_key:
        raise HistoricalReceiptMigrationError("source_case_key_missing")
    try:
        case = load_case(plan_path, case_key)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalReceiptMigrationError("benchmark_case_could_not_be_loaded") from exc

    if receipt.get("benchmark_id") != case["benchmark_id"]:
        raise HistoricalReceiptMigrationError("benchmark_id_mismatch")
    expected_modality = str(case.get("modality") or "").strip().lower()
    if str(receipt.get("modality") or "").strip().lower() != expected_modality:
        raise HistoricalReceiptMigrationError("modality_mismatch")
    if receipt.get("prompt_sha256") != sha256_text(benchmark_case_text(case)):
        raise HistoricalReceiptMigrationError("prompt_sha256_mismatch")
    if not str(receipt.get("provider") or "").strip():
        raise HistoricalReceiptMigrationError("provider_missing")

    artifact_path = _resolve_artifact(
        receipt.get("artifact_path"),
        artifact_root=artifact_root,
    )
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise HistoricalReceiptMigrationError("source_artifact_must_be_regular_file")
    if not isinstance(receipt.get("artifact_sha256"), str):
        raise HistoricalReceiptMigrationError("source_artifact_hash_missing")
    if _sha256_file(artifact_path) != receipt["artifact_sha256"]:
        raise HistoricalReceiptMigrationError("source_artifact_hash_mismatch")

    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"migration output already exists: {output_path}")
    migrated = dict(receipt)
    provider_receipt = migrated.get("provider_receipt")
    if isinstance(provider_receipt, dict) and "credential_id" in provider_receipt:
        credential_id = provider_receipt.pop("credential_id")
        provider_receipt["credential_id_sha256"] = sha256_text(
            str(credential_id)
        )
    migrated["benchmark_case_sha256"] = case["__benchmark_case_sha256"]
    migrated["benchmark_plan_sha256"] = case["__benchmark_plan_sha256"]
    migrated["evidence_level"] = "historical_receipt_provenance_bound"
    migrated["provenance_migration"] = {
        "method": "historical_receipt_provenance_bound",
        "source_receipt_sha256": source_receipt_sha256,
        "migrated_at": datetime.now(UTC).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_path.chmod(0o600)
    return output_path, migrated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Root for relative artifact_path values; defaults to the repository root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path, receipt = migrate_receipt(
        plan_path=args.plan,
        receipt_path=args.receipt,
        output_path=args.output,
        artifact_root=args.artifact_root,
    )
    print(
        json.dumps(
            {
                "case_key": receipt["case_key"],
                "evidence_level": receipt["evidence_level"],
                "output": str(output_path),
                "provider": receipt["provider"],
                "status": "migrated_without_provider_call",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
