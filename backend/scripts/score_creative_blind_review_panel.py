#!/usr/bin/env python3
"""Score a formal multi-reviewer creative panel, then optionally reveal routes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_blind_review import (  # noqa: E402
    BlindReviewKey,
    BlindReviewPackage,
)
from app.services.creative_evaluation import CreativeScenario  # noqa: E402
from app.services.creative_review_panel import (  # noqa: E402
    BlindPanelSubmission,
    required_evidence_kinds_for_scenario,
    reveal_panel_results,
    score_blind_review_panel,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-spec", type=Path, required=True)
    parser.add_argument("--review-package", type=Path, required=True)
    parser.add_argument("--panel-submissions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--minimum-reviewers", type=int, default=3)
    parser.add_argument(
        "--required-evidence-kind",
        action="append",
        choices=(
            "ocr",
            "frame_ocr",
            "human_visual",
            "human_audio",
            "human_av_sync",
            "document_semantic",
        ),
    )
    return parser.parse_args()


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    batch_payload = json.loads(args.batch_spec.read_text(encoding="utf-8"))
    scenario = CreativeScenario.model_validate(batch_payload["scenario"])
    package = BlindReviewPackage.model_validate_json(
        args.review_package.read_text(encoding="utf-8")
    )
    panel = BlindPanelSubmission.model_validate_json(
        args.panel_submissions.read_text(encoding="utf-8")
    )
    required_evidence_kinds = list(required_evidence_kinds_for_scenario(scenario))
    for kind in args.required_evidence_kind or ():
        if kind not in required_evidence_kinds:
            required_evidence_kinds.append(kind)
    required_evidence_kinds_tuple = tuple(required_evidence_kinds)
    results = score_blind_review_panel(
        scenario,
        package,
        panel,
        minimum_reviewers=args.minimum_reviewers,
        required_evidence_kinds=required_evidence_kinds_tuple,
    )
    sealed_path = args.output_dir / "sealed-panel-results.json"
    _write_private_json(
        sealed_path,
        {
            "schema_version": "1.1.0",
            "scenario_id": scenario.scenario_id,
            "minimum_reviewers": args.minimum_reviewers,
            "batch_spec_sha256": _sha256_file(args.batch_spec),
            "review_package_sha256": _sha256_file(args.review_package),
            "panel_submissions_sha256": _sha256_file(args.panel_submissions),
            "reviewer_receipt_refs": [
                reviewer.reviewer_receipt_ref for reviewer in panel.reviewers
            ],
            "required_evidence_kinds": required_evidence_kinds_tuple,
            "artifact_hashes": {
                candidate.label: {
                    artifact_type: observation.content_sha256
                    for artifact_type, observation
                    in candidate.structural_observation.files.items()
                }
                for candidate in package.candidates
            },
            "results": [item.model_dump(mode="json") for item in results],
        },
    )

    revealed_path: Path | None = None
    if args.private_key is not None:
        key = BlindReviewKey.model_validate_json(
            args.private_key.read_text(encoding="utf-8")
        )
        revealed = reveal_panel_results(results, key)
        revealed_path = args.output_dir / "private-revealed-panel-results.json"
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
                "candidate_count": len(results),
                "formal_scored_count": sum(
                    item.panel_status == "scored" for item in results
                ),
                "commercially_usable_count": sum(
                    item.commercially_usable for item in results
                ),
                "revealed_results": (
                    str(revealed_path) if revealed_path is not None else None
                ),
                "sealed_results": str(sealed_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
