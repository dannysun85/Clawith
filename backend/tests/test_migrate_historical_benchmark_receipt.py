from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from scripts.creative_provider_benchmark import benchmark_case_text, load_case
from scripts.migrate_historical_benchmark_receipt import (
    HistoricalReceiptMigrationError,
    migrate_receipt,
)
from scripts.verify_creative_benchmark_provenance import audit_benchmark_receipts


def _write_plan(path: Path) -> dict[str, object]:
    payload = {
        "benchmark_id": "historical-migration-test",
        "cases": {
            "image_case": {
                "modality": "image",
                "prompt": "same historical task",
                "aspect_ratio": "1:1",
            }
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return payload


def _write_artifact(path: Path) -> str:
    Image.new("RGB", (900, 900), color=(80, 40, 20)).save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_legacy_receipt(plan_path: Path, artifact_path: Path, path: Path) -> None:
    case = load_case(plan_path, "image_case")
    payload = {
        "artifact_path": artifact_path.name,
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "benchmark_id": case["benchmark_id"],
        "bytes": artifact_path.stat().st_size,
        "case_key": "image_case",
        "modality": "image",
        "prompt_sha256": hashlib.sha256(
            benchmark_case_text(case).encode("utf-8")
        ).hexdigest(),
        "provider": "minimax",
        "provider_receipt": {
            "credential_id": "credential-private-id",
            "model": "image-01",
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_migrate_historical_receipt_preserves_source_and_binds_provenance(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    source_path = tmp_path / "legacy.receipt.json"
    output_path = tmp_path / "migrated.receipt.json"
    _write_plan(plan_path)
    _write_artifact(artifact_path)
    _write_legacy_receipt(plan_path, artifact_path, source_path)
    source_bytes = source_path.read_bytes()

    _, migrated = migrate_receipt(
        plan_path=plan_path,
        receipt_path=source_path,
        output_path=output_path,
        artifact_root=tmp_path,
    )

    assert source_path.read_bytes() == source_bytes
    assert migrated["evidence_level"] == "historical_receipt_provenance_bound"
    assert "credential_id" not in migrated["provider_receipt"]
    assert len(migrated["provider_receipt"]["credential_id_sha256"]) == 64
    assert migrated["provenance_migration"]["source_receipt_sha256"] == hashlib.sha256(
        source_bytes
    ).hexdigest()
    assert output_path.stat().st_mode & 0o777 == 0o600
    audit = audit_benchmark_receipts(
        plan_path,
        [output_path],
        artifact_root=tmp_path,
    )
    assert audit.status == "valid"


def test_migrate_historical_receipt_rejects_prompt_or_artifact_changes(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    source_path = tmp_path / "legacy.receipt.json"
    _write_plan(plan_path)
    _write_artifact(artifact_path)
    _write_legacy_receipt(plan_path, artifact_path, source_path)

    artifact_path.write_bytes(b"changed")
    with pytest.raises(
        HistoricalReceiptMigrationError,
        match="source_artifact_hash_mismatch",
    ):
        migrate_receipt(
            plan_path=plan_path,
            receipt_path=source_path,
            output_path=tmp_path / "migrated.json",
            artifact_root=tmp_path,
        )


def test_migrate_historical_receipt_never_overwrites_existing_output(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    source_path = tmp_path / "legacy.receipt.json"
    output_path = tmp_path / "migrated.receipt.json"
    _write_plan(plan_path)
    _write_artifact(artifact_path)
    _write_legacy_receipt(plan_path, artifact_path, source_path)
    output_path.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        migrate_receipt(
            plan_path=plan_path,
            receipt_path=source_path,
            output_path=output_path,
            artifact_root=tmp_path,
        )
