#!/usr/bin/env python3
"""Apply a complete manual-review decision set to creative brief candidates."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_sample_ingestion import (  # noqa: E402
    AnonymizedCreativeBriefCandidate,
    CreativeBriefReviewDecision,
    apply_creative_brief_review_batch,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_list(path: Path, key: str) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{path} must contain a {key!r} list")
    return values


def main() -> None:
    args = parse_args()
    candidates = [
        AnonymizedCreativeBriefCandidate.model_validate(value)
        for value in load_list(args.candidates, "candidates")
    ]
    decisions = [
        CreativeBriefReviewDecision.model_validate(value)
        for value in load_list(args.decisions, "decisions")
    ]
    reviewed = apply_creative_brief_review_batch(candidates, decisions)
    counts = Counter(item.review_status for item in reviewed)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "source_candidates": args.candidates.name,
                "source_decisions": args.decisions.name,
                "counts": dict(sorted(counts.items())),
                "reviewed": [item.model_dump(mode="json") for item in reviewed],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(output, 0o600)
    print(
        json.dumps(
            {
                "counts": dict(sorted(counts.items())),
                "output": str(output),
                "reviewed_count": len(reviewed),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
