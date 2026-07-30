#!/usr/bin/env python3
"""Collect private OCR evidence for an image or sampled video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_evidence_collection import (  # noqa: E402
    collect_ocr_evidence,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-type", choices=("image", "video"), required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-language", action="append", default=[])
    parser.add_argument("--prohibited-term", action="append", default=[])
    parser.add_argument("--minimum-confidence", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    receipt = collect_ocr_evidence(
        artifact_type=args.artifact_type,
        path=args.artifact,
        expected_languages=args.expected_language,
        prohibited_terms=args.prohibited_term,
        minimum_confidence=args.minimum_confidence,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        receipt.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
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
