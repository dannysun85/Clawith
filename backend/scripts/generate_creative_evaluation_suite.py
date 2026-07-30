#!/usr/bin/env python3
"""Generate a no-cost, provider-neutral creative evaluation suite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_evaluation import (  # noqa: E402
    generate_evaluation_bundle,
    verify_holdout_commitment,
)


DEFAULT_OUTPUT_DIR = REPO_DIR / "tmp/creative-evaluation/open-world-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument(
        "--modalities",
        nargs="+",
        choices=("image", "video", "presentation"),
        default=("image", "video", "presentation"),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    bundle = generate_evaluation_bundle(
        seed=args.seed,
        count=args.count,
        modalities=tuple(args.modalities),  # type: ignore[arg-type]
    )
    if not verify_holdout_commitment(bundle.holdout):
        raise RuntimeError("Holdout commitment verification failed.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "public-manifest.json"
    holdout_path = args.output_dir / "restricted-holdout.json"
    write_json(manifest_path, bundle.manifest.model_dump(mode="json"))
    write_json(holdout_path, bundle.holdout.model_dump(mode="json"))
    holdout_path.chmod(0o600)

    summary = {
        "seed": args.seed,
        "public_scenarios": len(bundle.manifest.public_scenarios),
        "holdout_scenarios": bundle.manifest.holdout_count,
        "holdout_commitment_sha256": bundle.manifest.holdout_commitment_sha256,
        "manifest_path": str(manifest_path),
        "holdout_path": str(holdout_path),
        "note": (
            "restricted-holdout.json is separated locally and chmod 0600. "
            "A production evaluator must store it behind independent access control."
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
