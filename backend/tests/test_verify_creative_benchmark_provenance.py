from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from scripts.creative_provider_benchmark import benchmark_case_text, load_case
from scripts.verify_creative_benchmark_provenance import (
    audit_benchmark_receipts,
)


def _write_plan(path: Path) -> dict[str, object]:
    payload = {
        "benchmark_id": "provenance-test",
        "cases": {
            "image_case": {
                "modality": "image",
                "prompt": "same task",
                "aspect_ratio": "1:1",
            }
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return payload


def _write_image(path: Path) -> str:
    Image.new("RGB", (900, 900), color=(12, 34, 56)).save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_receipt(plan_path: Path, artifact_path: Path) -> dict[str, object]:
    case = load_case(plan_path, "image_case")
    return {
        "artifact_path": artifact_path.name,
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "benchmark_id": case["benchmark_id"],
        "benchmark_case_sha256": case["__benchmark_case_sha256"],
        "benchmark_plan_sha256": case["__benchmark_plan_sha256"],
        "case_key": "image_case",
        "modality": "image",
        "prompt_sha256": hashlib.sha256(
            benchmark_case_text(case).encode("utf-8")
        ).hexdigest(),
        "provider": "doubao",
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_provenance_audit_accepts_exact_plan_case_and_artifact_hashes(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    receipt_path = tmp_path / "doubao.receipt.json"
    _write_plan(plan_path)
    _write_image(artifact_path)
    _write_receipt(receipt_path, _valid_receipt(plan_path, artifact_path))

    audit = audit_benchmark_receipts(
        plan_path,
        [receipt_path],
        artifact_root=tmp_path,
    )

    assert audit.status == "valid"
    assert audit.receipt_count == 1
    assert audit.results[0].artifact_verified is True
    assert audit.results[0].provider == "doubao"


def test_provenance_audit_accepts_external_multi_artifact_receipt(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    pptx_path = tmp_path / "candidate.pptx"
    preview_path = tmp_path / "candidate.pdf"
    receipt_path = tmp_path / "external.receipt.json"
    plan_path.write_text(
        json.dumps(
            {
                "benchmark_id": "provenance-test",
                "cases": {
                    "presentation_case": {
                        "modality": "presentation",
                        "goal": "same deck task",
                        "slides": 1,
                        "aspect_ratio": "16:9",
                    }
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    pptx_path.write_bytes(b"pptx")
    preview_path.write_bytes(b"preview")
    case = load_case(plan_path, "presentation_case")
    _write_receipt(
        receipt_path,
        {
            "artifact_paths": {
                "pptx": str(pptx_path),
                "pdf": str(preview_path),
            },
            "artifact_sha256": {
                "pptx": hashlib.sha256(pptx_path.read_bytes()).hexdigest(),
                "pdf": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
            },
            "benchmark_id": case["benchmark_id"],
            "benchmark_case_sha256": case["__benchmark_case_sha256"],
            "benchmark_plan_sha256": case["__benchmark_plan_sha256"],
            "case_key": "presentation_case",
            "modality": "presentation",
            "prompt_sha256": hashlib.sha256(
                benchmark_case_text(case).encode("utf-8")
            ).hexdigest(),
            "provider": "doubao",
            "evidence_level": "external_artifact_imported",
        },
    )

    audit = audit_benchmark_receipts(plan_path, [receipt_path])

    assert audit.status == "valid"
    assert audit.results[0].artifact_verified is True


def test_provenance_audit_rejects_changed_artifact_and_plan_hash(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    receipt_path = tmp_path / "provider.receipt.json"
    _write_plan(plan_path)
    _write_image(artifact_path)
    receipt = _valid_receipt(plan_path, artifact_path)
    receipt["benchmark_plan_sha256"] = "f" * 64
    _write_receipt(receipt_path, receipt)
    artifact_path.write_bytes(b"changed")

    audit = audit_benchmark_receipts(
        plan_path,
        [receipt_path],
        artifact_root=tmp_path,
    )

    assert audit.status == "invalid"
    assert "provider.receipt.json:benchmark_plan_sha256_mismatch" in audit.issues
    assert "provider.receipt.json:artifact_sha256_mismatch" in audit.issues


def test_provenance_audit_rejects_legacy_receipt_without_provenance(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    receipt_path = tmp_path / "legacy.receipt.json"
    _write_plan(plan_path)
    artifact_hash = _write_image(artifact_path)
    _write_receipt(
        receipt_path,
        {
            "artifact_path": artifact_path.name,
            "artifact_sha256": artifact_hash,
            "benchmark_id": "provenance-test",
            "case_key": "image_case",
            "modality": "image",
            "provider": "minimax",
        },
    )

    audit = audit_benchmark_receipts(
        plan_path,
        [receipt_path],
        artifact_root=tmp_path,
    )

    assert audit.status == "invalid"
    assert any(
        issue.endswith(":benchmark_plan_sha256_mismatch")
        for issue in audit.issues
    )
    assert any(
        issue.endswith(":prompt_sha256_mismatch")
        for issue in audit.issues
    )


def test_provenance_audit_rejects_duplicate_provider_case_pair(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    first_path = tmp_path / "first.receipt.json"
    second_path = tmp_path / "second.receipt.json"
    _write_plan(plan_path)
    _write_image(artifact_path)
    receipt = _valid_receipt(plan_path, artifact_path)
    _write_receipt(first_path, receipt)
    _write_receipt(second_path, receipt)

    audit = audit_benchmark_receipts(
        plan_path,
        [first_path, second_path],
        artifact_root=tmp_path,
    )

    assert audit.status == "invalid"
    assert "duplicate_provider_case:doubao:image_case" in audit.issues


def test_provenance_audit_can_require_provider_parity_per_case(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    first_path = tmp_path / "doubao.receipt.json"
    second_path = tmp_path / "minimax.receipt.json"
    _write_plan(plan_path)
    _write_image(artifact_path)
    first = _valid_receipt(plan_path, artifact_path)
    second = dict(first)
    second["provider"] = "minimax"
    _write_receipt(first_path, first)
    _write_receipt(second_path, second)

    audit = audit_benchmark_receipts(
        plan_path,
        [first_path, second_path],
        artifact_root=tmp_path,
        required_providers=("doubao", "minimax"),
    )

    assert audit.status == "valid"
    assert audit.required_providers == ("doubao", "minimax")


def test_provenance_audit_reports_missing_required_provider_for_case(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    artifact_path = tmp_path / "candidate.png"
    receipt_path = tmp_path / "doubao.receipt.json"
    _write_plan(plan_path)
    _write_image(artifact_path)
    _write_receipt(receipt_path, _valid_receipt(plan_path, artifact_path))

    audit = audit_benchmark_receipts(
        plan_path,
        [receipt_path],
        artifact_root=tmp_path,
        required_providers=("doubao", "minimax"),
    )

    assert audit.status == "invalid"
    assert "missing_provider_for_case:image_case:minimax" in audit.issues
