#!/usr/bin/env python3
"""Create a local provider-masked review package from a private batch spec."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_artifact_evaluation import (  # noqa: E402
    CreativeArtifactContract,
)
from app.services.creative_blind_review import (  # noqa: E402
    BlindReviewSourceCandidate,
    prepare_blind_review_package,
)
from app.services.creative_evaluation import CreativeScenario  # noqa: E402


class BlindReviewBatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    scenario: CreativeScenario
    contract: CreativeArtifactContract
    candidates: tuple[BlindReviewSourceCandidate, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    spec = BlindReviewBatchSpec.model_validate_json(
        args.batch_spec.read_text(encoding="utf-8")
    )
    public_dir = args.output_dir / "public"
    private_dir = args.output_dir / "private"
    package, key = await prepare_blind_review_package(
        spec.scenario,
        spec.contract,
        spec.candidates,
        seed=spec.seed,
        public_assets_dir=public_dir / "assets",
    )
    private_dir.mkdir(parents=True, exist_ok=True)
    package_path = public_dir / "review-package.json"
    key_path = private_dir / "review-key.json"
    package_path.write_text(
        json.dumps(
            package.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    key_path.write_text(
        json.dumps(
            key.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    package_path.chmod(0o600)
    key_path.chmod(0o600)
    print(
        json.dumps(
            {
                "candidate_count": len(package.candidates),
                "private_key": str(key_path),
                "public_package": str(package_path),
                "scenario_id": package.scenario_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
