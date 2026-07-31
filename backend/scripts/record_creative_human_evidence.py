#!/usr/bin/env python3
"""Bind one real human review receipt to the exact masked candidate hashes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_blind_review import BlindReviewPackage  # noqa: E402
from app.services.creative_review_panel import (  # noqa: E402
    CreativeEvidenceReceipt,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-package", type=Path, required=True)
    parser.add_argument("--candidate-label", required=True)
    parser.add_argument(
        "--kind",
        choices=(
            "human_visual",
            "human_audio",
            "human_av_sync",
            "document_semantic",
        ),
        required=True,
    )
    parser.add_argument(
        "--status",
        choices=("complete", "partial", "unavailable"),
        required=True,
    )
    parser.add_argument("--reviewer-receipt-ref", required=True)
    parser.add_argument("--finding", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package = BlindReviewPackage.model_validate_json(
        args.review_package.read_text(encoding="utf-8")
    )
    matches = [
        candidate
        for candidate in package.candidates
        if candidate.label == args.candidate_label
    ]
    if len(matches) != 1:
        raise ValueError("Candidate label does not resolve exactly once")
    if args.status == "complete" and not args.finding:
        raise ValueError("Complete human evidence requires at least one finding")
    candidate = matches[0]
    receipt = CreativeEvidenceReceipt(
        receipt_ref=(
            f"{args.reviewer_receipt_ref}:{args.candidate_label}:{args.kind}"
        ),
        kind=args.kind,
        status=args.status,
        artifact_hashes={
            artifact_type: artifact.content_sha256
            for artifact_type, artifact in candidate.structural_observation.files.items()
        },
        source="independent_human_review",
        findings=tuple(args.finding),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "candidate_label": args.candidate_label,
                "kind": receipt.kind,
                "output": str(args.output),
                "status": receipt.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
