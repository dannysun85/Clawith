#!/usr/bin/env python3
"""Audit one multi-modality creative benchmark run without calling providers.

The audit deliberately separates package readiness, provisional single-reviewer
scores, and formal multi-reviewer commercial results.  A copied or changed
artifact, provider leakage in a public manifest, an incomplete reviewer panel,
or a missing modality keeps the run from being reported as commercial-ready.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.creative_blind_review import (  # noqa: E402
    BlindReviewKey,
    BlindReviewPackage,
)
from app.services.creative_artifact_evaluation import (  # noqa: E402
    CreativeArtifactContract,
)
from app.services.creative_evaluation import CreativeScenario  # noqa: E402
from app.services.creative_review_panel import (  # noqa: E402
    BlindCandidatePanelResult,
    BlindPanelSubmission,
    BlindPanelReviewerBatch,
    required_evidence_kinds_for_scenario,
    score_blind_review_panel,
)


AuditStatus = Literal[
    "invalid",
    "awaiting_human_review",
    "evaluated_not_commercial",
    "commercial_ready",
]
_PLACEHOLDER_MARKER = "replace-with-independent-receipt"


class ModalityBenchmarkAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    modality: str
    scenario_id: str | None = None
    status: AuditStatus
    candidate_count: int = 0
    verified_artifact_count: int = 0
    reviewer_template_count: int = 0
    completed_reviewer_file_count: int = 0
    formal_result_count: int = 0
    commercially_usable_count: int = 0
    provisional_results_present: bool = False
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class BenchmarkRunAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    run_dir: str
    run_fingerprint_sha256: str | None = None
    status: AuditStatus
    expected_modalities: tuple[str, ...]
    modality_audits: tuple[ModalityBenchmarkAudit, ...]
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_fingerprint_sha256(
    run_dir: Path,
    modalities: Sequence[str],
) -> str:
    """Hash the content-addressed inputs of one benchmark run.

    The fingerprint is deliberately independent of the absolute temporary
    directory.  It includes the private attribution key as a digest only, so
    the audit output can identify a run without exposing provider identities or
    source paths.  This is a run-content identity, not a Git release SHA.
    """

    digest = hashlib.sha256()
    logical_paths: set[Path] = set()
    for modality in modalities:
        logical_paths.update(
            {
                Path(f"{modality}-batch-spec.json"),
                Path(modality) / "public" / "review-package.json",
                Path(modality) / "private" / "review-key.json",
            }
        )
        for relative_dir in (
            Path(modality) / "public" / "assets",
            Path(modality) / "panel",
            Path(modality) / "results",
        ):
            absolute_dir = run_dir / relative_dir
            if not absolute_dir.is_dir():
                continue
            logical_paths.update(
                path.relative_to(run_dir)
                for path in absolute_dir.rglob("*")
                if path.is_file() or path.is_symlink()
            )

    for relative_path in sorted(logical_paths, key=lambda item: item.as_posix()):
        encoded_path = relative_path.as_posix().encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        absolute_path = run_dir / relative_path
        if absolute_path.is_symlink():
            target = absolute_path.readlink().as_posix().encode("utf-8")
            digest.update(b"symlink")
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
            continue
        if not absolute_path.is_file():
            digest.update(b"missing")
            continue
        digest.update(b"file")
        digest.update(_sha256_file(absolute_path).encode("ascii"))
    return digest.hexdigest()


def _safe_public_asset(public_dir: Path, relative_ref: str) -> Path:
    relative_path = Path(relative_ref)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"unsafe public artifact path: {relative_ref}")
    root = public_dir.resolve()
    candidate = (public_dir / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"public artifact escapes review package: {relative_ref}"
        ) from exc
    return candidate


def _provider_identity_tokens(key: BlindReviewKey) -> tuple[str, ...]:
    tokens: set[str] = set()
    for candidate in key.candidates.values():
        for value in (candidate.candidate_id, candidate.provider, candidate.model):
            normalized = str(value or "").strip().lower()
            if len(normalized) >= 4:
                tokens.add(normalized)
    return tuple(sorted(tokens))


def _reviewer_file_state(
    path: Path,
) -> tuple[str, bool, BlindPanelReviewerBatch]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("reviewer submission must be a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenario_id = str(payload["scenario_id"])
    reviewer = BlindPanelReviewerBatch.model_validate(payload["reviewer"])
    placeholder = _PLACEHOLDER_MARKER in reviewer.reviewer_receipt_ref
    complete = all(
        all(
            observation.passed is not None
            for observation in candidate.review.hard_gates.values()
        )
        and all(
            observation.score is not None
            for observation in candidate.review.dimensions.values()
        )
        for candidate in reviewer.candidates
    )
    if complete and placeholder:
        raise ValueError(
            f"completed reviewer file still uses placeholder receipt: {path.name}"
        )
    if complete:
        reviewer = reviewer.model_copy(
            update={
                "candidates": tuple(
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
                    for candidate in reviewer.candidates
                )
            }
        )
    return scenario_id, complete, reviewer


def _audit_modality(run_dir: Path, modality: str) -> ModalityBenchmarkAudit:
    issues: list[str] = []
    notes: list[str] = []
    batch_path = run_dir / f"{modality}-batch-spec.json"
    package_path = run_dir / modality / "public" / "review-package.json"
    key_path = run_dir / modality / "private" / "review-key.json"
    required_paths = {
        "batch spec": batch_path,
        "public review package": package_path,
        "private attribution key": key_path,
    }
    for description, path in required_paths.items():
        if not path.is_file():
            issues.append(f"missing {description}: {path.name}")
    if issues:
        return ModalityBenchmarkAudit(
            modality=modality,
            status="invalid",
            issues=tuple(issues),
        )

    try:
        batch_payload = json.loads(batch_path.read_text(encoding="utf-8"))
        scenario = CreativeScenario.model_validate(batch_payload["scenario"])
        contract = CreativeArtifactContract.model_validate(batch_payload["contract"])
        package = BlindReviewPackage.model_validate_json(
            package_path.read_text(encoding="utf-8")
        )
        key = BlindReviewKey.model_validate_json(
            key_path.read_text(encoding="utf-8")
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return ModalityBenchmarkAudit(
            modality=modality,
            status="invalid",
            issues=(f"invalid benchmark metadata: {exc}",),
        )

    scenario_id = scenario.scenario_id
    if scenario.modality != modality:
        issues.append(
            f"batch modality mismatch: expected {modality}, got {scenario.modality}"
        )
    if package.modality != modality:
        issues.append(
            f"package modality mismatch: expected {modality}, got {package.modality}"
        )
    if contract.modality != scenario.modality:
        issues.append(
            "artifact contract modality differs from the benchmark scenario"
        )
    if contract.aspect_ratio != scenario.aspect_ratio:
        issues.append(
            "artifact contract aspect ratio differs from the benchmark scenario"
        )
    # The public package is what reviewers actually see.  Bind every
    # user-facing evaluation field to the same scenario used to produce the
    # candidate artifacts; checking only scenario_id would allow a package to
    # silently replace the brief or quality contract while keeping the same
    # identifier.
    package_contract_fields = (
        "brief",
        "requirements",
        "hard_gates",
        "quality_dimensions",
    )
    for field_name in package_contract_fields:
        if getattr(package, field_name) != getattr(scenario, field_name):
            issues.append(
                f"public review package {field_name} differs from the benchmark scenario"
            )
    if {scenario_id, package.scenario_id, key.scenario_id} != {scenario_id}:
        issues.append("scenario ids differ across batch, package, and private key")
    package_labels = {candidate.label for candidate in package.candidates}
    if package_labels != set(key.candidates):
        issues.append("public candidate labels and private attribution labels differ")
    expected_candidate_count = len(batch_payload.get("candidates", ()))
    if expected_candidate_count != len(package.candidates):
        issues.append(
            "batch candidate count and public candidate count differ: "
            f"{expected_candidate_count} != {len(package.candidates)}"
        )

    public_text = package_path.read_text(encoding="utf-8").lower()
    leaked_tokens = tuple(
        token for token in _provider_identity_tokens(key) if token in public_text
    )
    if leaked_tokens:
        issues.append(
            "public package leaks private candidate/provider/model identity: "
            + ", ".join(leaked_tokens)
        )

    verified_artifact_count = 0
    public_dir = package_path.parent
    for candidate in package.candidates:
        observed_files = candidate.structural_observation.files
        if set(candidate.artifacts) != set(observed_files):
            issues.append(
                f"candidate {candidate.label} artifact and observation keys differ"
            )
            continue
        for artifact_type, relative_ref in candidate.artifacts.items():
            try:
                artifact_path = _safe_public_asset(public_dir, relative_ref)
            except ValueError as exc:
                issues.append(str(exc))
                continue
            if artifact_path.is_symlink() or not artifact_path.is_file():
                issues.append(
                    f"candidate {candidate.label} missing regular {artifact_type} file"
                )
                continue
            expected_hash = observed_files[artifact_type].content_sha256
            actual_hash = _sha256_file(artifact_path)
            if actual_hash != expected_hash:
                issues.append(
                    f"candidate {candidate.label} {artifact_type} hash mismatch"
                )
                continue
            verified_artifact_count += 1

    reviewer_paths = sorted(
        (run_dir / modality / "panel").glob("reviewer-*/submission.json")
    )
    completed_reviewer_count = 0
    completed_reviewer_refs: set[str] = set()
    completed_reviewers: dict[str, BlindPanelReviewerBatch] = {}
    reviewer_scenario_ids: set[str] = set()
    for reviewer_path in reviewer_paths:
        try:
            reviewer_scenario_id, complete, reviewer = _reviewer_file_state(
                reviewer_path
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"invalid reviewer file {reviewer_path.name}: {exc}")
            continue
        reviewer_scenario_ids.add(reviewer_scenario_id)
        completed_reviewer_count += int(complete)
        if complete:
            if reviewer.reviewer_receipt_ref in completed_reviewers:
                issues.append("completed reviewer receipt refs must be unique")
            completed_reviewer_refs.add(reviewer.reviewer_receipt_ref)
            completed_reviewers[reviewer.reviewer_receipt_ref] = reviewer
    if len(reviewer_paths) < 3:
        issues.append("formal review requires at least 3 reviewer files")
    if reviewer_scenario_ids and reviewer_scenario_ids != {scenario_id}:
        issues.append("reviewer templates target a different scenario")

    results_dir = run_dir / modality / "results"
    provisional_results_present = (
        (results_dir / "sealed-scored-reviews.json").is_file()
        or (run_dir / f"{modality}-review-submissions.json").is_file()
    )
    if provisional_results_present:
        notes.append(
            "legacy/provisional scores are present but are not commercial evidence"
        )

    formal_result_path = results_dir / "sealed-panel-results.json"
    formal_panel_path = run_dir / modality / "panel" / "panel-submissions.json"
    formal_results: tuple[BlindCandidatePanelResult, ...] = ()
    if formal_result_path.is_symlink():
        issues.append("formal panel results must be a regular file")
    elif formal_result_path.is_file():
        formal_panel: BlindPanelSubmission | None = None
        try:
            formal_payload = json.loads(
                formal_result_path.read_text(encoding="utf-8")
            )
            formal_results = tuple(
                BlindCandidatePanelResult.model_validate(item)
                for item in formal_payload["results"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"invalid formal panel results: {exc}")
        else:
            if formal_payload.get("scenario_id") != scenario_id:
                issues.append("formal panel results target a different scenario")
            if formal_panel_path.is_symlink():
                issues.append("formal panel submissions must be a regular file")
            elif not formal_panel_path.is_file():
                issues.append(
                    "formal panel results are missing panel/panel-submissions.json"
                )
            else:
                try:
                    formal_panel = BlindPanelSubmission.model_validate_json(
                        formal_panel_path.read_text(encoding="utf-8")
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    issues.append(f"invalid formal panel submissions: {exc}")
            expected_artifact_hashes = {
                candidate.label: {
                    artifact_type: observation.content_sha256
                    for artifact_type, observation
                    in candidate.structural_observation.files.items()
                }
                for candidate in package.candidates
            }
            if formal_payload.get("batch_spec_sha256") != _sha256_file(
                batch_path
            ):
                issues.append(
                    "formal panel results are not bound to this batch spec"
                )
            if formal_payload.get("review_package_sha256") != _sha256_file(
                package_path
            ):
                issues.append(
                    "formal panel results are not bound to this review package"
                )
            if formal_payload.get("artifact_hashes") != expected_artifact_hashes:
                issues.append(
                    "formal panel results are not bound to current artifact hashes"
                )
            if formal_panel is not None:
                if formal_payload.get(
                    "panel_submissions_sha256"
                ) != _sha256_file(formal_panel_path):
                    issues.append(
                        "formal panel results are not bound to current panel submissions"
                    )
                panel_reviewer_refs = tuple(
                    reviewer.reviewer_receipt_ref
                    for reviewer in formal_panel.reviewers
                )
                if tuple(
                    formal_payload.get("reviewer_receipt_refs", ())
                ) != panel_reviewer_refs:
                    issues.append(
                        "formal panel results are not bound to panel reviewer receipts"
                    )
                if set(panel_reviewer_refs) != completed_reviewer_refs:
                    issues.append(
                        "formal panel reviewers differ from completed reviewer files"
                    )
                elif any(
                    reviewer != completed_reviewers[reviewer.reviewer_receipt_ref]
                    for reviewer in formal_panel.reviewers
                ):
                    issues.append(
                        "formal panel judgments differ from completed reviewer files"
                    )
                expected_evidence_kinds = (
                    required_evidence_kinds_for_scenario(scenario)
                )
                if tuple(
                    formal_payload.get("required_evidence_kinds", ())
                ) != expected_evidence_kinds:
                    issues.append(
                        "formal panel results use the wrong required evidence contract"
                    )
                minimum_reviewers = formal_payload.get("minimum_reviewers")
                if (
                    not isinstance(minimum_reviewers, int)
                    or isinstance(minimum_reviewers, bool)
                    or minimum_reviewers < 3
                ):
                    issues.append(
                        "formal panel minimum_reviewers must be an integer of at least 3"
                    )
                else:
                    try:
                        recomputed_results = score_blind_review_panel(
                            scenario,
                            package,
                            formal_panel,
                            minimum_reviewers=minimum_reviewers,
                            required_evidence_kinds=expected_evidence_kinds,
                        )
                    except ValueError as exc:
                        issues.append(
                            f"formal panel submissions fail validation: {exc}"
                        )
                    else:
                        if tuple(formal_results) != tuple(recomputed_results):
                            issues.append(
                                "formal panel results differ from recomputed panel scores"
                            )
            result_labels = {item.label for item in formal_results}
            if result_labels != package_labels:
                issues.append(
                    "formal panel results do not cover every masked candidate"
                )
            if any(item.reviewer_count < 3 for item in formal_results):
                issues.append("formal panel result has fewer than 3 reviewers")

    commercially_usable_count = sum(
        item.panel_status == "scored" and item.commercially_usable
        for item in formal_results
    )
    if issues:
        status: AuditStatus = "invalid"
    elif formal_results and commercially_usable_count == len(formal_results):
        status = "commercial_ready"
    elif formal_results:
        status = "evaluated_not_commercial"
    else:
        status = "awaiting_human_review"
        notes.append(
            "artifact hashes and reviewer templates are ready; formal human "
            "panel results are absent"
        )

    return ModalityBenchmarkAudit(
        modality=modality,
        scenario_id=scenario_id,
        status=status,
        candidate_count=len(package.candidates),
        verified_artifact_count=verified_artifact_count,
        reviewer_template_count=len(reviewer_paths),
        completed_reviewer_file_count=completed_reviewer_count,
        formal_result_count=len(formal_results),
        commercially_usable_count=commercially_usable_count,
        provisional_results_present=provisional_results_present,
        issues=tuple(issues),
        notes=tuple(notes),
    )


def audit_benchmark_run(
    run_dir: Path,
    *,
    expected_modalities: Sequence[str] = ("image", "video", "presentation"),
) -> BenchmarkRunAudit:
    normalized_modalities = tuple(dict.fromkeys(expected_modalities))
    if not normalized_modalities:
        raise ValueError("At least one expected modality is required")
    audits = tuple(
        _audit_modality(run_dir, modality) for modality in normalized_modalities
    )
    issues = tuple(
        f"{audit.modality}: {issue}"
        for audit in audits
        for issue in audit.issues
    )
    if issues:
        status: AuditStatus = "invalid"
    elif all(audit.status == "commercial_ready" for audit in audits):
        status = "commercial_ready"
    elif any(audit.status == "evaluated_not_commercial" for audit in audits):
        status = "evaluated_not_commercial"
    else:
        status = "awaiting_human_review"
    return BenchmarkRunAudit(
        run_dir=str(run_dir.resolve()),
        run_fingerprint_sha256=_run_fingerprint_sha256(
            run_dir,
            normalized_modalities,
        ),
        status=status,
        expected_modalities=normalized_modalities,
        modality_audits=audits,
        issues=issues,
        notes=(
            "Only sealed multi-reviewer panel results can produce "
            "commercial_ready.",
            "commercial_ready attests the reviewed artifacts only; cost, latency, "
            "and default-route eligibility require separate execution receipts.",
            "The audit does not call a provider or alter benchmark artifacts.",
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-modality",
        action="append",
        choices=("image", "video", "presentation"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-commercial-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit = audit_benchmark_run(
        args.run_dir,
        expected_modalities=(
            tuple(args.expected_modality)
            if args.expected_modality
            else ("image", "video", "presentation")
        ),
    )
    rendered = audit.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if audit.status == "invalid":
        return 1
    if args.require_commercial_ready and audit.status != "commercial_ready":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
