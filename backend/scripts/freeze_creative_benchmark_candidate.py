#!/usr/bin/env python3
"""Freeze a commercially reviewed creative benchmark candidate.

This command never calls a provider.  It only writes a content-addressed
manifest after every declared modality has a sealed, commercially usable
review panel and the source repository is a clean checkout at the requested
Git revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.audit_creative_benchmark_run import (  # noqa: E402
    BenchmarkRunAudit,
    audit_benchmark_run,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CandidateFreezeError(RuntimeError):
    """A candidate cannot be made immutable from the supplied evidence."""


class FrozenBenchmarkCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    candidate_sha256: str
    source_revision: str
    run_fingerprint_sha256: str
    expected_modalities: tuple[str, ...]
    scenario_ids: dict[str, str]
    modality_counts: dict[str, dict[str, int]]


def _git_source_state(repo_dir: Path) -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CandidateFreezeError(
            f"cannot inspect Git source state: {type(exc).__name__}"
        ) from exc
    return revision, not bool(status.strip())


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_audit(audit: BenchmarkRunAudit) -> None:
    if audit.status != "commercial_ready":
        raise CandidateFreezeError(
            f"benchmark audit is {audit.status}; sealed commercial review is required"
        )
    if not audit.run_fingerprint_sha256 or not _SHA256_RE.fullmatch(
        audit.run_fingerprint_sha256
    ):
        raise CandidateFreezeError("benchmark audit has no valid run fingerprint")
    if any(
        item.status != "commercial_ready"
        or item.candidate_count <= 0
        or item.commercially_usable_count != item.candidate_count
        for item in audit.modality_audits
    ):
        raise CandidateFreezeError(
            "every declared modality must have commercially usable sealed results"
        )


def build_candidate_manifest(
    audit: BenchmarkRunAudit,
    *,
    source_revision: str,
    expected_modalities: Sequence[str],
) -> FrozenBenchmarkCandidate:
    """Build a deterministic candidate identity from sealed audit evidence."""

    if not _GIT_SHA_RE.fullmatch(source_revision):
        raise CandidateFreezeError("source_revision must be a full 40-character Git SHA")
    _validate_audit(audit)
    normalized_modalities = tuple(dict.fromkeys(expected_modalities))
    if tuple(audit.expected_modalities) != normalized_modalities:
        raise CandidateFreezeError(
            "audit expected modalities differ from the freeze request"
        )
    scenario_ids = {
        item.modality: str(item.scenario_id)
        for item in audit.modality_audits
        if item.scenario_id
    }
    if set(scenario_ids) != set(normalized_modalities):
        raise CandidateFreezeError("every expected modality must have a scenario id")
    modality_counts = {
        item.modality: {
            "candidate_count": item.candidate_count,
            "verified_artifact_count": item.verified_artifact_count,
            "completed_reviewer_file_count": item.completed_reviewer_file_count,
            "formal_result_count": item.formal_result_count,
            "commercially_usable_count": item.commercially_usable_count,
        }
        for item in audit.modality_audits
    }
    content = {
        "schema_version": "1.0.0",
        "source_revision": source_revision,
        "run_fingerprint_sha256": audit.run_fingerprint_sha256,
        "expected_modalities": list(normalized_modalities),
        "scenario_ids": scenario_ids,
        "modality_counts": modality_counts,
    }
    return FrozenBenchmarkCandidate(
        candidate_sha256=_canonical_sha256(content),
        **content,
    )


def freeze_candidate(
    *,
    run_dir: Path,
    output: Path,
    source_revision: str,
    repo_dir: Path = REPO_DIR,
    expected_modalities: Sequence[str] = ("image", "video", "presentation"),
) -> FrozenBenchmarkCandidate:
    """Validate source and audit state, then create a new manifest once."""

    if output.exists():
        raise CandidateFreezeError(f"refusing to overwrite existing manifest: {output}")
    current_revision, clean = _git_source_state(repo_dir)
    if current_revision != source_revision:
        raise CandidateFreezeError(
            "requested source_revision does not match the current Git HEAD"
        )
    if not clean:
        raise CandidateFreezeError("source repository has uncommitted changes")
    audit = audit_benchmark_run(
        run_dir,
        expected_modalities=expected_modalities,
    )
    manifest = build_candidate_manifest(
        audit,
        source_revision=source_revision,
        expected_modalities=expected_modalities,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--repo-dir", type=Path, default=REPO_DIR)
    parser.add_argument(
        "--expected-modality",
        action="append",
        choices=("image", "video", "presentation"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = freeze_candidate(
            run_dir=args.run_dir,
            output=args.output,
            source_revision=args.source_revision,
            repo_dir=args.repo_dir,
            expected_modalities=(
                tuple(args.expected_modality)
                if args.expected_modality
                else ("image", "video", "presentation")
            ),
        )
    except CandidateFreezeError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
