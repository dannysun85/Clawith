"""Fail-closed multi-reviewer evaluation for creative artifacts.

The existing blind-review service intentionally supports a lightweight single
reviewer pilot.  Formal commercial conclusions require a separate panel
contract so a single optimistic score, stale evidence, or missing audio/OCR
review cannot accidentally release an artifact.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.services.creative_blind_review import (
    BlindCandidateReviewSubmission,
    BlindReviewKey,
    BlindReviewPackage,
)
from app.services.creative_evaluation import (
    CreativeQualityEvaluation,
    CreativeScenario,
    HardGateObservation,
    QualityDimensionObservation,
    score_quality_evaluation,
)


EvidenceKind = Literal[
    "ocr",
    "frame_ocr",
    "human_visual",
    "human_audio",
    "human_av_sync",
    "document_semantic",
]
EvidenceStatus = Literal["complete", "partial", "unavailable"]
PanelStatus = Literal["blocked", "incomplete", "scored"]

_HUMAN_EVIDENCE_KINDS = frozenset(
    {"human_visual", "human_audio", "human_av_sync", "document_semantic"}
)
_PLACEHOLDER_MARKER = "replace-with-independent-receipt"
_AV_SYNC_CONTRACT_MARKERS = (
    "lip sync",
    "lip-sync",
    "synchronized dialogue",
    "synchronised dialogue",
    "口型同步",
    "同步对白",
)


class CreativeEvidenceReceipt(BaseModel):
    """Evidence bound to the immutable hashes of one masked candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_ref: str = Field(min_length=1)
    kind: EvidenceKind
    status: EvidenceStatus
    artifact_hashes: dict[str, str] = Field(min_length=1)
    source: str = Field(min_length=1)
    language_coverage: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


class BlindPanelCandidateSubmission(BaseModel):
    """One reviewer's judgment and evidence for one masked candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review: BlindCandidateReviewSubmission
    evidence_receipts: tuple[CreativeEvidenceReceipt, ...] = ()


class BlindPanelReviewerBatch(BaseModel):
    """All candidate reviews sealed by one independent reviewer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_receipt_ref: str = Field(min_length=1)
    candidates: tuple[BlindPanelCandidateSubmission, ...] = Field(min_length=1)


class BlindPanelSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    scenario_id: str
    reviewers: tuple[BlindPanelReviewerBatch, ...] = Field(min_length=1)


class BlindCandidatePanelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    reviewer_count: int
    panel_status: PanelStatus
    required_evidence_kinds: tuple[EvidenceKind, ...]
    complete_evidence_kinds: tuple[EvidenceKind, ...]
    missing_evidence_kinds: tuple[EvidenceKind, ...]
    disagreements: tuple[str, ...]
    evaluation: CreativeQualityEvaluation
    commercially_usable: bool


class RevealedBlindCandidatePanelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    candidate_id: str
    provider: str
    model: str | None = None
    reviewer_count: int
    panel_status: PanelStatus
    required_evidence_kinds: tuple[EvidenceKind, ...]
    complete_evidence_kinds: tuple[EvidenceKind, ...]
    missing_evidence_kinds: tuple[EvidenceKind, ...]
    disagreements: tuple[str, ...]
    evaluation: CreativeQualityEvaluation
    commercially_usable: bool


def required_evidence_kinds_for_modality(
    modality: str,
) -> tuple[EvidenceKind, ...]:
    """Return the minimum perceptual evidence for a formal commercial panel."""

    if modality == "image":
        return ("ocr", "human_visual")
    if modality == "video":
        return ("frame_ocr", "human_visual", "human_audio")
    if modality == "presentation":
        return ("document_semantic", "human_visual")
    raise ValueError(f"Unsupported creative modality: {modality}")


def required_evidence_kinds_for_scenario(
    scenario: CreativeScenario | BlindReviewPackage,
) -> tuple[EvidenceKind, ...]:
    """Add contract-specific evidence without making every video a lip-sync task."""

    kinds = list(required_evidence_kinds_for_modality(scenario.modality))
    if scenario.modality == "video":
        contract_text = "\n".join(
            (*scenario.requirements, *scenario.quality_dimensions)
        ).lower()
        if any(marker in contract_text for marker in _AV_SYNC_CONTRACT_MARKERS):
            kinds.append("human_av_sync")
    return tuple(kinds)


def create_reviewer_submission_template(
    package: BlindReviewPackage,
    *,
    reviewer_receipt_ref: str,
) -> BlindPanelReviewerBatch:
    """Create a provider-free, incomplete template for one real reviewer."""

    return BlindPanelReviewerBatch(
        reviewer_receipt_ref=reviewer_receipt_ref,
        candidates=tuple(
            BlindPanelCandidateSubmission(
                review=BlindCandidateReviewSubmission(
                    label=candidate.label,
                    reviewer_receipt_ref=(
                        f"{reviewer_receipt_ref}:{candidate.opaque_artifact_id}"
                    ),
                    hard_gates={
                        gate: HardGateObservation(
                            passed=None,
                            evidence=("Reviewer judgment required",),
                        )
                        for gate in package.hard_gates
                    },
                    dimensions={
                        dimension: QualityDimensionObservation(
                            score=None,
                            evidence=("Reviewer judgment required",),
                        )
                        for dimension in package.quality_dimensions
                    },
                    notes=(),
                ),
                evidence_receipts=(),
            )
            for candidate in package.candidates
        ),
    )


def _expected_artifact_hashes(
    package: BlindReviewPackage,
) -> dict[str, dict[str, str]]:
    return {
        candidate.label: {
            artifact_type: artifact.content_sha256
            for artifact_type, artifact in candidate.structural_observation.files.items()
        }
        for candidate in package.candidates
    }


def _validate_panel_shape(
    package: BlindReviewPackage,
    panel: BlindPanelSubmission,
    *,
    minimum_reviewers: int,
) -> dict[str, tuple[BlindPanelCandidateSubmission, ...]]:
    if minimum_reviewers < 2:
        raise ValueError("minimum_reviewers must be at least 2")
    if panel.scenario_id != package.scenario_id:
        raise ValueError("Panel submission and review package differ")
    if len(panel.reviewers) < minimum_reviewers:
        raise ValueError(
            f"Formal panel requires at least {minimum_reviewers} reviewers"
        )
    reviewer_refs = [item.reviewer_receipt_ref for item in panel.reviewers]
    if any(
        not reviewer_ref.strip()
        or reviewer_ref != reviewer_ref.strip()
        or _PLACEHOLDER_MARKER in reviewer_ref
        for reviewer_ref in reviewer_refs
    ):
        raise ValueError(
            "Panel reviewer receipt refs must be non-placeholder canonical refs"
        )
    if len(set(reviewer_refs)) != len(reviewer_refs):
        raise ValueError("Panel reviewer receipt refs must be unique")

    expected_labels = {candidate.label for candidate in package.candidates}
    by_label: defaultdict[str, list[BlindPanelCandidateSubmission]] = defaultdict(list)
    for reviewer in panel.reviewers:
        reviewer_labels = [candidate.review.label for candidate in reviewer.candidates]
        if len(set(reviewer_labels)) != len(reviewer_labels):
            raise ValueError("A reviewer submitted a candidate more than once")
        if set(reviewer_labels) != expected_labels:
            raise ValueError("Every reviewer must assess every masked candidate")
        for candidate in reviewer.candidates:
            expected_review_ref = (
                f"{reviewer.reviewer_receipt_ref}:{candidate.review.label}"
            )
            if candidate.review.reviewer_receipt_ref != expected_review_ref:
                raise ValueError(
                    "Candidate review receipt is not bound to its panel reviewer"
                )
            for receipt in candidate.evidence_receipts:
                if receipt.kind not in _HUMAN_EVIDENCE_KINDS:
                    continue
                independent_source = receipt.source == "independent_human_review"
                managed_source = (
                    receipt.source.startswith("managed_reviewer:")
                    and receipt.source != "managed_reviewer:"
                    and reviewer.reviewer_receipt_ref.startswith("managed-reviewer:")
                )
                if not (independent_source or managed_source):
                    raise ValueError(
                        "Human evidence must use an independent or managed reviewer source"
                    )
                if not receipt.receipt_ref.startswith(
                    f"{reviewer.reviewer_receipt_ref}:"
                ):
                    raise ValueError(
                        "Human evidence receipt is not bound to its panel reviewer"
                    )
            by_label[candidate.review.label].append(candidate)

    human_evidence_refs = [
        receipt.receipt_ref
        for reviewer in panel.reviewers
        for candidate in reviewer.candidates
        for receipt in candidate.evidence_receipts
        if receipt.kind in _HUMAN_EVIDENCE_KINDS
    ]
    if len(set(human_evidence_refs)) != len(human_evidence_refs):
        raise ValueError("Human evidence receipt refs must be unique")
    return {label: tuple(items) for label, items in by_label.items()}


def _validate_evidence_hashes(
    submissions: Sequence[BlindPanelCandidateSubmission],
    expected_hashes: Mapping[str, str],
) -> None:
    for submission in submissions:
        for receipt in submission.evidence_receipts:
            if receipt.artifact_hashes != expected_hashes:
                raise ValueError(
                    "Evidence receipt artifact hashes do not match the masked candidate"
                )


def _aggregate_hard_gate(
    gate: str,
    submissions: Sequence[BlindPanelCandidateSubmission],
) -> tuple[HardGateObservation, str | None]:
    observations = [submission.review.hard_gates.get(gate) for submission in submissions]
    complete_observations = [
        observation for observation in observations if observation is not None
    ]
    states = {
        observation.passed
        for observation in complete_observations
    }
    evidence = tuple(
        item
        for observation in observations
        if observation is not None
        for item in observation.evidence
    )
    if len(complete_observations) == len(submissions) and states == {True}:
        return HardGateObservation(passed=True, evidence=evidence), None
    if len(complete_observations) == len(submissions) and states == {False}:
        return HardGateObservation(passed=False, evidence=evidence), None
    return (
        HardGateObservation(
            passed=None,
            evidence=(*evidence, f"Panel disagreement or missing judgment: {gate}"),
        ),
        gate,
    )


def _aggregate_dimension(
    dimension: str,
    submissions: Sequence[BlindPanelCandidateSubmission],
) -> tuple[QualityDimensionObservation, str | None]:
    observations = [
        submission.review.dimensions.get(dimension) for submission in submissions
    ]
    scores = [
        observation.score
        for observation in observations
        if observation is not None and observation.score is not None
    ]
    evidence = tuple(
        item
        for observation in observations
        if observation is not None
        for item in observation.evidence
    )
    if len(scores) != len(submissions):
        return (
            QualityDimensionObservation(
                score=None,
                evidence=(*evidence, f"Panel judgment missing: {dimension}"),
            ),
            dimension,
        )
    if max(scores) - min(scores) > 1.5:
        return (
            QualityDimensionObservation(
                score=None,
                evidence=(*evidence, f"Panel score spread exceeds 1.5: {dimension}"),
            ),
            dimension,
        )
    return (
        QualityDimensionObservation(
            score=round(sum(scores) / len(scores), 3),
            evidence=evidence,
        ),
        None,
    )


def _complete_evidence_kinds(
    submissions: Sequence[BlindPanelCandidateSubmission],
    required_kinds: Sequence[EvidenceKind],
) -> tuple[EvidenceKind, ...]:
    complete: list[EvidenceKind] = []
    for kind in required_kinds:
        if kind in _HUMAN_EVIDENCE_KINDS:
            present_for_every_reviewer = all(
                any(
                    receipt.kind == kind and receipt.status == "complete"
                    for receipt in submission.evidence_receipts
                )
                for submission in submissions
            )
            if present_for_every_reviewer:
                complete.append(kind)
            continue
        if any(
            receipt.kind == kind and receipt.status == "complete"
            for submission in submissions
            for receipt in submission.evidence_receipts
        ):
            complete.append(kind)
    return tuple(complete)


def _exact_prohibited_evidence(
    submissions: Sequence[BlindPanelCandidateSubmission],
) -> tuple[str, ...]:
    return tuple(
        finding
        for submission in submissions
        for receipt in submission.evidence_receipts
        if receipt.kind in {"ocr", "frame_ocr"}
        and receipt.status == "complete"
        for finding in receipt.findings
        if finding.startswith("prohibited_term_detected=")
    )


def score_blind_review_panel(
    scenario: CreativeScenario,
    package: BlindReviewPackage,
    panel: BlindPanelSubmission,
    *,
    minimum_reviewers: int = 3,
    required_evidence_kinds: Sequence[EvidenceKind] | None = None,
) -> tuple[BlindCandidatePanelResult, ...]:
    """Aggregate independent reviews without converting disagreement into a pass."""

    if package.scenario_id != scenario.scenario_id:
        raise ValueError("Review package and scenario differ")
    by_label = _validate_panel_shape(
        package,
        panel,
        minimum_reviewers=minimum_reviewers,
    )
    expected_hashes = _expected_artifact_hashes(package)
    required_kinds = tuple(
        required_evidence_kinds
        or required_evidence_kinds_for_scenario(scenario)
    )

    results: list[BlindCandidatePanelResult] = []
    for candidate in package.candidates:
        submissions = by_label[candidate.label]
        _validate_evidence_hashes(
            submissions,
            expected_hashes[candidate.label],
        )
        hard_gates: dict[str, HardGateObservation] = {}
        dimensions: dict[str, QualityDimensionObservation] = {}
        disagreements: list[str] = []
        for gate in scenario.hard_gates:
            observation, disagreement = _aggregate_hard_gate(gate, submissions)
            if gate == "no_unrequested_watermark":
                exact_prohibited_evidence = _exact_prohibited_evidence(submissions)
                if exact_prohibited_evidence:
                    observation = HardGateObservation(
                        passed=False,
                        evidence=(
                            *observation.evidence,
                            *exact_prohibited_evidence,
                        ),
                    )
                    disagreement = None
            hard_gates[gate] = observation
            if disagreement is not None:
                disagreements.append(f"hard_gate:{disagreement}")
        for dimension in scenario.quality_dimensions:
            observation, disagreement = _aggregate_dimension(
                dimension,
                submissions,
            )
            dimensions[dimension] = observation
            if disagreement is not None:
                disagreements.append(f"dimension:{disagreement}")

        evaluation = score_quality_evaluation(
            scenario,
            hard_gates=hard_gates,
            dimensions=dimensions,
        )
        complete_kinds = _complete_evidence_kinds(submissions, required_kinds)
        missing_kinds = tuple(
            kind for kind in required_kinds if kind not in complete_kinds
        )
        if evaluation.status == "blocked":
            panel_status: PanelStatus = "blocked"
        elif (
            evaluation.status != "scored"
            or disagreements
            or missing_kinds
        ):
            panel_status = "incomplete"
        else:
            panel_status = "scored"
        results.append(
            BlindCandidatePanelResult(
                label=candidate.label,
                reviewer_count=len(panel.reviewers),
                panel_status=panel_status,
                required_evidence_kinds=required_kinds,
                complete_evidence_kinds=complete_kinds,
                missing_evidence_kinds=missing_kinds,
                disagreements=tuple(disagreements),
                evaluation=evaluation,
                commercially_usable=(
                    panel_status == "scored" and evaluation.commercially_usable
                ),
            )
        )
    return tuple(results)


def reveal_panel_results(
    panel_results: Sequence[BlindCandidatePanelResult],
    key: BlindReviewKey,
) -> tuple[RevealedBlindCandidatePanelResult, ...]:
    by_label = {result.label: result for result in panel_results}
    if len(by_label) != len(panel_results):
        raise ValueError("Panel result labels must be unique")
    if set(by_label) != set(key.candidates):
        raise ValueError("Panel results and private key labels differ")
    return tuple(
        RevealedBlindCandidatePanelResult(
            label=label,
            candidate_id=descriptor.candidate_id,
            provider=descriptor.provider,
            model=descriptor.model,
            reviewer_count=result.reviewer_count,
            panel_status=result.panel_status,
            required_evidence_kinds=result.required_evidence_kinds,
            complete_evidence_kinds=result.complete_evidence_kinds,
            missing_evidence_kinds=result.missing_evidence_kinds,
            disagreements=result.disagreements,
            evaluation=result.evaluation,
            commercially_usable=result.commercially_usable,
        )
        for label, descriptor in key.candidates.items()
        for result in (by_label[label],)
    )


__all__ = [
    "BlindCandidatePanelResult",
    "BlindPanelCandidateSubmission",
    "BlindPanelReviewerBatch",
    "BlindPanelSubmission",
    "CreativeEvidenceReceipt",
    "EvidenceKind",
    "RevealedBlindCandidatePanelResult",
    "create_reviewer_submission_template",
    "required_evidence_kinds_for_modality",
    "required_evidence_kinds_for_scenario",
    "reveal_panel_results",
    "score_blind_review_panel",
]
