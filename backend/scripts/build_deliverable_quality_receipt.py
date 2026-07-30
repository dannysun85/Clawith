#!/usr/bin/env python3
"""Convert exact automated failure evidence into a hash-bound quality receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_review_panel import CreativeEvidenceReceipt  # noqa: E402
from app.services.deliverable_quality_gate import (  # noqa: E402
    blocked_quality_receipt_from_automated_evidence,
    quality_gate_evaluation_payload,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def build_blocked_payload(
    evidence: CreativeEvidenceReceipt,
) -> dict[str, object]:
    if evidence.status != "complete":
        raise ValueError("Only complete automated evidence can issue a blocked receipt")
    exact_findings = tuple(
        finding
        for finding in evidence.findings
        if finding.startswith("prohibited_term_detected=")
    )
    if not exact_findings:
        raise ValueError(
            "Evidence has no exact prohibited-term finding; absence or possible matches cannot block"
        )
    receipt = blocked_quality_receipt_from_automated_evidence(
        receipt_ref=f"quality:{evidence.receipt_ref}",
        artifact_hashes=evidence.artifact_hashes,
        evidence_kind=evidence.kind,
        hard_gate_failures=("no_unrequested_watermark",),
    )
    return quality_gate_evaluation_payload(receipt)


def main() -> int:
    args = parse_args()
    evidence = CreativeEvidenceReceipt.model_validate_json(
        args.evidence_receipt.read_text(encoding="utf-8")
    )
    payload = build_blocked_payload(evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    receipt = payload["receipt"]
    assert isinstance(receipt, dict)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "receipt_ref": receipt["receipt_ref"],
                "status": receipt["status"],
                "artifact_hashes": receipt["artifact_hashes"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
