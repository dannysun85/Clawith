#!/usr/bin/env python3
"""Seal provider-free creative reviews, then optionally reveal attribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_blind_review import (  # noqa: E402
    BlindCandidateReviewSubmission,
    BlindReviewKey,
    BlindReviewPackage,
    reveal_scored_blind_reviews,
    score_blind_review_submissions,
)
from app.services.creative_evaluation import CreativeScenario  # noqa: E402


class BlindReviewSubmissionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    submissions: tuple[BlindCandidateReviewSubmission, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-spec", type=Path, required=True)
    parser.add_argument("--review-package", type=Path, required=True)
    parser.add_argument("--review-submissions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--private-key", type=Path)
    return parser.parse_args()


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def main() -> int:
    args = parse_args()
    batch_payload = json.loads(args.batch_spec.read_text(encoding="utf-8"))
    scenario = CreativeScenario.model_validate(batch_payload["scenario"])
    package = BlindReviewPackage.model_validate_json(
        args.review_package.read_text(encoding="utf-8")
    )
    submission_batch = BlindReviewSubmissionBatch.model_validate_json(
        args.review_submissions.read_text(encoding="utf-8")
    )
    if submission_batch.scenario_id != scenario.scenario_id:
        raise ValueError("Submission batch and scenario differ")

    scored = score_blind_review_submissions(
        scenario,
        package,
        submission_batch.submissions,
    )
    scored_path = args.output_dir / "sealed-scored-reviews.json"
    _write_private_json(
        scored_path,
        {
            "schema_version": "1.0.0",
            "scenario_id": scenario.scenario_id,
            "results": [item.model_dump(mode="json") for item in scored],
        },
    )

    revealed_path: Path | None = None
    if args.private_key is not None:
        key = BlindReviewKey.model_validate_json(
            args.private_key.read_text(encoding="utf-8")
        )
        revealed = reveal_scored_blind_reviews(scored, key)
        revealed_path = args.output_dir / "private-revealed-results.json"
        _write_private_json(
            revealed_path,
            {
                "schema_version": "1.0.0",
                "scenario_id": scenario.scenario_id,
                "results": [item.model_dump(mode="json") for item in revealed],
            },
        )

    print(
        json.dumps(
            {
                "candidate_count": len(scored),
                "revealed_results": (
                    str(revealed_path) if revealed_path is not None else None
                ),
                "sealed_results": str(scored_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
