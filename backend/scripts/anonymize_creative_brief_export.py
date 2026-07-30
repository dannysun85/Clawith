#!/usr/bin/env python3
"""Convert a JSONL stream of authorized briefs into pending-review candidates.

Raw rows may be read from stdin, allowing a read-only production query to feed
this process without writing raw customer text to local disk.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
from typing import TextIO


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_sample_ingestion import (  # noqa: E402
    AuthorizedCreativeBriefExport,
    anonymize_creative_brief,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="-",
        help="JSONL input path or '-' for stdin. Raw input is never persisted.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--pseudonym-key-file",
        type=Path,
        help=(
            "File containing at least 32 private bytes. Omit only for a "
            "single-export ephemeral key that cannot deduplicate future cycles."
        ),
    )
    return parser.parse_args()


def _open_input(value: str) -> tuple[TextIO, bool]:
    if value == "-":
        return sys.stdin, False
    return Path(value).open("r", encoding="utf-8"), True


def _read_key(path: Path | None) -> tuple[bytes, str]:
    if path is None:
        return secrets.token_bytes(32), "single_export_ephemeral"
    key = path.read_bytes()
    if len(key) < 32:
        raise SystemExit("pseudonym key file must contain at least 32 bytes")
    return key, "stable_private_key"


def main() -> int:
    args = parse_args()
    pseudonym_key, dedupe_scope = _read_key(args.pseudonym_key_file)
    input_stream, should_close = _open_input(args.input)
    candidates = []
    try:
        for line_number, line in enumerate(input_stream, start=1):
            if not line.strip():
                continue
            try:
                source = AuthorizedCreativeBriefExport.model_validate_json(line)
                candidate = anonymize_creative_brief(
                    source,
                    pseudonym_key=pseudonym_key,
                )
            except Exception as exc:
                raise SystemExit(
                    f"Invalid authorized export row at line {line_number}: "
                    f"{type(exc).__name__}"
                ) from exc
            candidates.append(candidate)
    finally:
        if should_close:
            input_stream.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0.0",
        "dedupe_scope": dedupe_scope,
        "review_status": "pending_review",
        "candidate_count": len(candidates),
        "candidates": [
            candidate.model_dump(mode="json") for candidate in candidates
        ],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "dedupe_scope": dedupe_scope,
                "output": str(args.output),
                "review_status": "pending_review",
                "raw_rows_persisted": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
