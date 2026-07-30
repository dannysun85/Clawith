"""Prepare provider-masked review assets and structural observations."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.services.creative_artifact_evaluation import (
    CreativeArtifactContract,
    CreativeArtifactObservation,
    observe_creative_artifacts,
)
from app.services.creative_evaluation import (
    CandidateDescriptor,
    CreativeScenario,
    CreativeQualityEvaluation,
    HardGateObservation,
    QualityDimensionObservation,
    create_blind_comparison,
    score_quality_evaluation,
)


class BlindReviewSourceCandidate(BaseModel):
    """Private input containing provider identity and local artifact locations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    provider: str
    model: str | None = None
    artifacts: dict[str, Path] = Field(min_length=1)


class BlindReviewPublicCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    opaque_artifact_id: str
    artifacts: dict[str, str]
    structural_observation: CreativeArtifactObservation


class BlindReviewPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    masking_scope: Literal["manifest_and_filename_only"] = (
        "manifest_and_filename_only"
    )
    embedded_identity_review_required: bool = True
    masking_limitations: tuple[str, ...] = (
        "Binary metadata, visible watermarks, document text, and audio are not "
        "modified; reviewers must check them for source-identity leakage.",
    )
    scenario_id: str
    modality: str
    brief: str
    requirements: tuple[str, ...]
    hard_gates: tuple[str, ...]
    quality_dimensions: tuple[str, ...]
    candidates: tuple[BlindReviewPublicCandidate, ...]


class BlindReviewPrivateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    provider: str
    model: str | None = None
    source_artifacts: dict[str, str]


class BlindReviewKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    scenario_id: str
    candidates: dict[str, BlindReviewPrivateCandidate]


class BlindCandidateReviewSubmission(BaseModel):
    """Provider-free reviewer judgment that can be sealed before attribution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    reviewer_receipt_ref: str
    hard_gates: dict[str, HardGateObservation]
    dimensions: dict[str, QualityDimensionObservation]
    notes: tuple[str, ...] = ()


class BlindCandidateScoredReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    reviewer_receipt_ref: str
    evaluation: CreativeQualityEvaluation
    notes: tuple[str, ...] = ()


class RevealedBlindCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    candidate_id: str
    provider: str
    model: str | None = None
    evaluation: CreativeQualityEvaluation
    notes: tuple[str, ...] = ()


async def prepare_blind_review_package(
    scenario: CreativeScenario,
    contract: CreativeArtifactContract,
    candidates: Sequence[BlindReviewSourceCandidate],
    *,
    seed: int,
    public_assets_dir: Path,
) -> tuple[BlindReviewPackage, BlindReviewKey]:
    """Copy opaque review assets and keep provider attribution in a private key."""

    if scenario.modality != contract.modality:
        raise ValueError("Scenario and artifact contract modalities differ")
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("Candidate ids must be unique")
    blind_package, blind_key = create_blind_comparison(
        scenario,
        tuple(
            CandidateDescriptor(
                candidate_id=candidate.candidate_id,
                artifact_ref="private-artifact-set",
                provider=candidate.provider,
                model=candidate.model,
            )
            for candidate in candidates
        ),
        seed=seed,
    )
    public_assets_dir.mkdir(parents=True, exist_ok=True)
    public_candidates: list[BlindReviewPublicCandidate] = []
    private_candidates: dict[str, BlindReviewPrivateCandidate] = {}
    for masked in blind_package.candidates:
        descriptor = blind_key.candidates[masked.label]
        source = by_id[descriptor.candidate_id]
        observation = await observe_creative_artifacts(
            contract,
            source.artifacts,
        )
        public_artifacts: dict[str, str] = {}
        private_artifacts: dict[str, str] = {}
        for artifact_type, source_path in sorted(source.artifacts.items()):
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError(
                    f"Candidate artifact is not a regular file: {artifact_type}"
                )
            suffix = source_path.suffix.lower()
            destination_name = (
                f"{masked.opaque_artifact_id}-{artifact_type}{suffix}"
            )
            destination = public_assets_dir / destination_name
            shutil.copyfile(source_path, destination)
            destination.chmod(0o600)
            public_artifacts[artifact_type] = f"assets/{destination_name}"
            private_artifacts[artifact_type] = str(source_path)
        public_candidates.append(
            BlindReviewPublicCandidate(
                label=masked.label,
                opaque_artifact_id=masked.opaque_artifact_id,
                artifacts=public_artifacts,
                structural_observation=observation,
            )
        )
        private_candidates[masked.label] = BlindReviewPrivateCandidate(
            candidate_id=source.candidate_id,
            provider=source.provider,
            model=source.model,
            source_artifacts=private_artifacts,
        )
    package = BlindReviewPackage(
        scenario_id=scenario.scenario_id,
        modality=scenario.modality,
        brief=scenario.brief,
        requirements=scenario.requirements,
        hard_gates=scenario.hard_gates,
        quality_dimensions=scenario.quality_dimensions,
        candidates=tuple(public_candidates),
    )
    key = BlindReviewKey(
        scenario_id=scenario.scenario_id,
        candidates=private_candidates,
    )
    return package, key


def score_blind_review_submissions(
    scenario: CreativeScenario,
    package: BlindReviewPackage,
    submissions: Sequence[BlindCandidateReviewSubmission],
) -> tuple[BlindCandidateScoredReview, ...]:
    """Validate and score provider-free submissions without opening the key."""

    if package.scenario_id != scenario.scenario_id:
        raise ValueError("Review package and scenario differ")
    expected_labels = {candidate.label for candidate in package.candidates}
    by_label = {submission.label: submission for submission in submissions}
    if len(by_label) != len(submissions):
        raise ValueError("Review labels must be unique")
    if set(by_label) != expected_labels:
        raise ValueError("Every masked candidate must be reviewed exactly once")
    return tuple(
        BlindCandidateScoredReview(
            label=candidate.label,
            reviewer_receipt_ref=by_label[candidate.label].reviewer_receipt_ref,
            evaluation=score_quality_evaluation(
                scenario,
                hard_gates=by_label[candidate.label].hard_gates,
                dimensions=by_label[candidate.label].dimensions,
            ),
            notes=by_label[candidate.label].notes,
        )
        for candidate in package.candidates
    )


def reveal_scored_blind_reviews(
    scored_reviews: Sequence[BlindCandidateScoredReview],
    key: BlindReviewKey,
) -> tuple[RevealedBlindCandidateResult, ...]:
    """Join provider attribution only after all provider-free scores are sealed."""

    by_label: Mapping[str, BlindCandidateScoredReview] = {
        review.label: review for review in scored_reviews
    }
    if len(by_label) != len(scored_reviews):
        raise ValueError("Scored review labels must be unique")
    if set(by_label) != set(key.candidates):
        raise ValueError("Scored reviews and private key labels differ")
    return tuple(
        RevealedBlindCandidateResult(
            label=label,
            candidate_id=descriptor.candidate_id,
            provider=descriptor.provider,
            model=descriptor.model,
            evaluation=by_label[label].evaluation,
            notes=by_label[label].notes,
        )
        for label, descriptor in key.candidates.items()
    )


__all__ = [
    "BlindReviewKey",
    "BlindReviewPackage",
    "BlindReviewSourceCandidate",
    "BlindCandidateReviewSubmission",
    "BlindCandidateScoredReview",
    "RevealedBlindCandidateResult",
    "prepare_blind_review_package",
    "reveal_scored_blind_reviews",
    "score_blind_review_submissions",
]
