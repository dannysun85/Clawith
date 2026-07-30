#!/usr/bin/env python3
"""Assemble independently completed reviewer files into one sealed panel input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_review_panel import (  # noqa: E402
    BlindPanelReviewerBatch,
    BlindPanelSubmission,
)


_PLACEHOLDER_MARKER = "replace-with-independent-receipt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reviewer-submission",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load_reviewer(path: Path) -> tuple[str, BlindPanelReviewerBatch]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenario_id = str(payload["scenario_id"])
    reviewer = BlindPanelReviewerBatch.model_validate(payload["reviewer"])
    if _PLACEHOLDER_MARKER in reviewer.reviewer_receipt_ref:
        raise ValueError("Reviewer receipt placeholder must be replaced")
    normalized_candidates = []
    for candidate in reviewer.candidates:
        if any(
            observation.passed is None
            for observation in candidate.review.hard_gates.values()
        ):
            raise ValueError("Every hard-gate judgment must be completed")
        if any(
            observation.score is None
            for observation in candidate.review.dimensions.values()
        ):
            raise ValueError("Every quality-dimension judgment must be completed")
        normalized_candidates.append(
            candidate.model_copy(
                update={
                    "review": candidate.review.model_copy(
                        update={
                            "reviewer_receipt_ref": (
                                f"{reviewer.reviewer_receipt_ref}:"
                                f"{candidate.review.label}"
                            )
                        }
                    )
                }
            )
        )
    return (
        scenario_id,
        reviewer.model_copy(update={"candidates": tuple(normalized_candidates)}),
    )


def main() -> int:
    args = parse_args()
    if len(args.reviewer_submission) < 3:
        raise ValueError("Formal panel assembly requires at least 3 reviewers")
    loaded = tuple(_load_reviewer(path) for path in args.reviewer_submission)
    scenario_ids = {scenario_id for scenario_id, _reviewer in loaded}
    if len(scenario_ids) != 1:
        raise ValueError("Reviewer submissions target different scenarios")
    reviewers = tuple(reviewer for _scenario_id, reviewer in loaded)
    reviewer_refs = [reviewer.reviewer_receipt_ref for reviewer in reviewers]
    if len(set(reviewer_refs)) != len(reviewer_refs):
        raise ValueError("Reviewer receipt refs must be unique")
    panel = BlindPanelSubmission(
        scenario_id=scenario_ids.pop(),
        reviewers=reviewers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(panel.model_dump_json(indent=2) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "reviewer_count": len(reviewers),
                "scenario_id": panel.scenario_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
