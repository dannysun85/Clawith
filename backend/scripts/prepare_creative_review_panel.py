#!/usr/bin/env python3
"""Prepare separate provider-free submission templates for real reviewers."""

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
    create_reviewer_submission_template,
    required_evidence_kinds_for_scenario,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-package", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reviewer-count", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.reviewer_count < 3:
        raise ValueError("Formal panel preparation requires at least 3 reviewers")
    package = BlindReviewPackage.model_validate_json(
        args.review_package.read_text(encoding="utf-8")
    )
    required_evidence = required_evidence_kinds_for_scenario(package)
    output_paths: list[str] = []
    for index in range(1, args.reviewer_count + 1):
        reviewer_ref = f"reviewer-{index:02d}-replace-with-independent-receipt"
        template = create_reviewer_submission_template(
            package,
            reviewer_receipt_ref=reviewer_ref,
        )
        reviewer_dir = args.output_dir / f"reviewer-{index:02d}"
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        output_path = reviewer_dir / "submission.json"
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "scenario_id": package.scenario_id,
                    "required_evidence_kinds": required_evidence,
                    "instructions": (
                        "Replace the reviewer receipt with an independently issued "
                        "receipt; review every candidate without provider attribution; "
                        "replace all null judgments; attach exact-hash evidence receipts."
                    ),
                    "reviewer": template.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        output_path.chmod(0o600)
        output_paths.append(str(output_path))
    print(
        json.dumps(
            {
                "reviewer_count": args.reviewer_count,
                "submission_templates": output_paths,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
