#!/usr/bin/env python3
"""Inspect local image, video, or presentation artifacts without Provider calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_artifact_evaluation import (  # noqa: E402
    CreativeArtifactContract,
    observe_creative_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modality",
        choices=("image", "video", "presentation"),
        required=True,
    )
    parser.add_argument("--aspect-ratio", required=True)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--page-count", type=int)
    parser.add_argument("--audio-required", action="store_true")
    parser.add_argument(
        "--reference-identity-required",
        action="store_true",
    )
    parser.add_argument(
        "--editable-required",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--preview-required",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--image", type=Path)
    parser.add_argument("--mp4", type=Path)
    parser.add_argument("--pptx", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    artifacts = {
        key: value
        for key, value in {
            "image": args.image,
            "mp4": args.mp4,
            "pptx": args.pptx,
            "pdf": args.pdf,
        }.items()
        if value is not None
    }
    observation = await observe_creative_artifacts(
        CreativeArtifactContract(
            modality=args.modality,
            aspect_ratio=args.aspect_ratio,
            duration_seconds=args.duration_seconds,
            page_count=args.page_count,
            audio_required=args.audio_required,
            reference_identity_required=args.reference_identity_required,
            editable_required=args.editable_required,
            preview_required=args.preview_required,
        ),
        artifacts,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            observation.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "modality": observation.modality,
                "output": str(args.output),
                "hard_gates": {
                    key: value.passed
                    for key, value in observation.hard_gates.items()
                },
                "warnings": observation.warnings,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
