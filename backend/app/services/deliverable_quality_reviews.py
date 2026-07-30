"""Managed, identity-bound commercial review workflow for deliverables.

Clients submit individual reviewer judgments and trusted evidence inputs.  They
never submit a final quality-gate conclusion.  Astra binds every input to the
current immutable artifact hashes and produces the approval receipt server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping, Sequence

from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableQualityReviewAssignment,
    DeliverableQualityReviewEvidence,
    DeliverableRequest,
)
from app.schemas.deliverable import DeliverableQualityReviewSubmissionIn
from app.services.creative_artifact_evaluation import (
    CreativeArtifactObservation,
    ObservedArtifactFile,
)
from app.services.creative_blind_review import (
    BlindCandidateReviewSubmission,
    BlindReviewPackage,
    BlindReviewPublicCandidate,
)
from app.services.creative_evaluation import (
    CreativeScenario,
    HardGateObservation,
    QualityDimensionObservation,
)
from app.services.creative_review_panel import (
    BlindPanelCandidateSubmission,
    BlindPanelReviewerBatch,
    BlindPanelSubmission,
    CreativeEvidenceReceipt,
    required_evidence_kinds_for_modality,
    score_blind_review_panel,
)
from app.services.deliverable_quality_gate import (
    DeliverableQualityGateReceipt,
    attach_deliverable_quality_gate_receipt,
    blocked_quality_receipt_from_automated_evidence,
    quality_receipt_from_panel_result,
    quality_receipt_sha256,
)


MANAGED_REVIEW_SCHEMA_VERSION = "1.0.0"
_HUMAN_EVIDENCE_KINDS = frozenset(
    {"human_visual", "human_audio", "human_av_sync", "document_semantic"}
)
_MODALITY_HARD_GATES: dict[str, tuple[str, ...]] = {
    "image": (
        "artifact_decodable",
        "aspect_ratio_match",
        "fact_safety",
        "reference_identity_when_required",
        "no_unrequested_watermark",
    ),
    "video": (
        "artifact_decodable",
        "duration_and_aspect_match",
        "fact_safety",
        "audio_contract_match",
        "no_unrequested_watermark",
    ),
    "presentation": (
        "pptx_and_preview_valid",
        "page_count_and_aspect_match",
        "fact_safety",
        "no_text_overflow",
        "source_traceability",
        "editability",
    ),
}
_MODALITY_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "image": (
        "brief_adherence",
        "visual_hierarchy",
        "subject_quality",
        "brand_and_style_fit",
        "commercial_readiness",
    ),
    "video": (
        "brief_adherence",
        "story_and_pacing",
        "character_and_motion_consistency",
        "audio_visual_coherence",
        "commercial_readiness",
    ),
    "presentation": (
        "brief_adherence",
        "narrative_quality",
        "information_design",
        "visual_system_consistency",
        "commercial_readiness",
    ),
}
_MODALITY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "image": (
        "需求、画幅、主体身份、品牌事实和精确文案必须符合确认后的 brief",
        "不得出现未要求的平台水印、伪文字或无法解释的遮挡修补",
        "首轮产物必须能够直接用于指定商业渠道，或明确退回重做",
    ),
    "video": (
        "时长、画幅、镜头连续性、事实、音频模式和 CTA 必须符合确认后的 brief",
        "不得出现未要求的平台水印、黑帧、损坏帧或不可理解的音画结果",
        "人物同步对白任务必须单独检查听感与口型同步",
    ),
    "presentation": (
        "PPTX 与预览版本必须有效，页数、画幅和事实口径符合确认后的 brief",
        "不得出现文字溢出、不可读小字、无来源关键事实或虚构数据",
        "可编辑性合同必须与实际文件一致，不能用整页截图冒充可编辑交付",
    ),
}


class DeliverableQualityReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deliverable_modality(request: DeliverableRequest) -> str:
    modality = {
        "poster": "image",
        "video": "video",
        "presentation": "presentation",
    }.get(request.work_type)
    if modality is None:
        raise DeliverableQualityReviewError(
            "deliverable_quality_review_unsupported",
            "Only image, video, and presentation deliverables support commercial review",
        )
    return modality


def selected_artifact_hashes(
    artifacts: Sequence[DeliverableArtifactRevision],
) -> dict[str, str]:
    return {
        artifact.artifact_key: artifact.content_hash
        for artifact in artifacts
    }


def required_evidence_kinds(
    modality: str,
    *,
    require_av_sync: bool = False,
) -> tuple[str, ...]:
    kinds = list(required_evidence_kinds_for_modality(modality))
    if modality == "video" and require_av_sync and "human_av_sync" not in kinds:
        kinds.append("human_av_sync")
    return tuple(kinds)


def _video_requires_av_sync(request: DeliverableRequest) -> bool:
    audio_mode = str((request.spec or {}).get("audio_mode") or "").strip().lower()
    return audio_mode in {
        "synchronous_dialogue",
        "synchronized_dialogue",
        "lip_sync",
        "dialogue",
    }


def build_managed_review_contract(
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    review_id: str,
) -> tuple[CreativeScenario, BlindReviewPackage, dict[str, str]]:
    """Create one provider-neutral, immutable single-candidate review package."""

    modality = deliverable_modality(request)
    if not artifacts:
        raise DeliverableQualityReviewError(
            "deliverable_artifact_missing",
            "The complete artifact set is required before review can start",
        )
    hashes = selected_artifact_hashes(artifacts)
    hard_gates = _MODALITY_HARD_GATES[modality]
    dimensions = _MODALITY_DIMENSIONS[modality]
    scenario_id = f"deliverable-review:{review_id}"
    fingerprint = canonical_payload_sha256(
        {
            "schema_version": MANAGED_REVIEW_SCHEMA_VERSION,
            "request_id": str(request.id),
            "request_fingerprint": request.request_fingerprint,
            "artifact_hashes": hashes,
            "hard_gates": hard_gates,
            "quality_dimensions": dimensions,
        }
    )
    scenario = CreativeScenario(
        scenario_id=scenario_id,
        fingerprint=fingerprint,
        split="development",
        modality=modality,  # type: ignore[arg-type]
        language=str((request.spec or {}).get("language") or "unspecified"),
        industry="customer_deliverable",
        subject=request.goal,
        objective=str((request.spec or {}).get("objective") or "commercial_delivery"),
        channel=str((request.spec or {}).get("channel") or "customer_specified"),
        audience=str((request.spec or {}).get("audience") or "customer_specified"),
        style=str((request.spec or {}).get("style") or "customer_specified"),
        source_mode="managed_deliverable_artifacts",
        constraint_profile="confirmed_deliverable_contract",
        aspect_ratio=str((request.spec or {}).get("aspect_ratio") or "unspecified"),
        brief=request.goal,
        requirements=_MODALITY_REQUIREMENTS[modality],
        hard_gates=hard_gates,
        quality_dimensions=dimensions,
        metadata={
            "request_id": str(request.id),
            "workflow_id": request.workflow_id,
            "workflow_version": request.workflow_version,
            "require_av_sync": _video_requires_av_sync(request),
        },
    )
    files = {
        artifact.artifact_key: ObservedArtifactFile(
            artifact_type=artifact.artifact_type,
            content_sha256=artifact.content_hash,
            size_bytes=int(artifact.size_bytes or 0),
        )
        for artifact in artifacts
    }
    observation = CreativeArtifactObservation(
        modality=modality,  # type: ignore[arg-type]
        files=files,
        facts={"managed_deliverable_review": True},
        hard_gates={
            gate: HardGateObservation(
                passed=None,
                evidence=("Managed reviewer judgment required",),
            )
            for gate in hard_gates
        },
        warnings=(),
    )
    opaque_id = hashlib.sha256(
        f"{review_id}:{fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    package = BlindReviewPackage(
        scenario_id=scenario_id,
        modality=modality,
        brief=request.goal,
        requirements=_MODALITY_REQUIREMENTS[modality],
        hard_gates=hard_gates,
        quality_dimensions=dimensions,
        candidates=(
            BlindReviewPublicCandidate(
                label="candidate-a",
                opaque_artifact_id=opaque_id,
                artifacts={
                    artifact.artifact_key: f"managed-artifact:{artifact.id}"
                    for artifact in artifacts
                },
                structural_observation=observation,
            ),
        ),
    )
    return scenario, package, hashes


def review_creation_fingerprint(
    *,
    request: DeliverableRequest,
    artifact_hashes: Mapping[str, str],
    reviewer_user_ids: Sequence[str],
) -> str:
    return canonical_payload_sha256(
        {
            "schema_version": MANAGED_REVIEW_SCHEMA_VERSION,
            "request_id": str(request.id),
            "request_version": request.version,
            "request_fingerprint": request.request_fingerprint,
            "artifact_hashes": dict(artifact_hashes),
            "reviewer_user_ids": sorted(str(item) for item in reviewer_user_ids),
        }
    )


def reviewer_submission_fingerprint(
    submission: DeliverableQualityReviewSubmissionIn,
) -> str:
    return canonical_payload_sha256(
        submission.model_dump(mode="json", exclude={"expected_version"})
    )


def build_reviewer_batch(
    review: DeliverableQualityReview,
    assignment: DeliverableQualityReviewAssignment,
    submission: DeliverableQualityReviewSubmissionIn,
) -> BlindPanelReviewerBatch:
    """Validate a reviewer's full contract and add server-owned identity receipts."""

    scenario = CreativeScenario.model_validate(review.scenario)
    expected_gates = set(scenario.hard_gates)
    expected_dimensions = set(scenario.quality_dimensions)
    if set(submission.hard_gates) != expected_gates:
        raise DeliverableQualityReviewError(
            "deliverable_quality_review_gate_mismatch",
            "Every hard gate must be assessed exactly once",
        )
    if set(submission.dimensions) != expected_dimensions:
        raise DeliverableQualityReviewError(
            "deliverable_quality_review_dimension_mismatch",
            "Every quality dimension must be scored exactly once",
        )
    required_kinds = required_evidence_kinds(
        scenario.modality,
        require_av_sync=bool(scenario.metadata.get("require_av_sync")),
    )
    expected_human_kinds = {
        kind for kind in required_kinds if kind in _HUMAN_EVIDENCE_KINDS
    }
    if set(submission.human_evidence) != expected_human_kinds:
        raise DeliverableQualityReviewError(
            "deliverable_quality_review_evidence_mismatch",
            "Every required human evidence kind must be submitted exactly once",
        )

    artifact_hashes = dict(review.artifact_hashes)
    evidence_receipts = tuple(
        CreativeEvidenceReceipt(
            receipt_ref=(
                f"{assignment.reviewer_receipt_ref}:"
                f"{kind}:{submission.client_submission_id}"
            ),
            kind=kind,  # type: ignore[arg-type]
            status=evidence.status,
            artifact_hashes=artifact_hashes,
            source=f"managed_reviewer:{assignment.reviewer_user_id}",
            findings=tuple(evidence.findings),
        )
        for kind, evidence in sorted(submission.human_evidence.items())
    )
    return BlindPanelReviewerBatch(
        reviewer_receipt_ref=assignment.reviewer_receipt_ref,
        candidates=(
            BlindPanelCandidateSubmission(
                review=BlindCandidateReviewSubmission(
                    label="candidate-a",
                    reviewer_receipt_ref=(
                        f"{assignment.reviewer_receipt_ref}:candidate-a"
                    ),
                    hard_gates={
                        gate: HardGateObservation(
                            passed=observation.passed,
                            evidence=tuple(observation.evidence),
                        )
                        for gate, observation in submission.hard_gates.items()
                    },
                    dimensions={
                        dimension: QualityDimensionObservation(
                            score=observation.score,
                            evidence=tuple(observation.evidence),
                        )
                        for dimension, observation in submission.dimensions.items()
                    },
                    notes=tuple(submission.notes),
                ),
                evidence_receipts=evidence_receipts,
            ),
        ),
    )


def _managed_automated_receipt(
    evidence: DeliverableQualityReviewEvidence,
) -> CreativeEvidenceReceipt:
    return CreativeEvidenceReceipt.model_validate(evidence.receipt)


def _append_automated_evidence(
    reviewers: Sequence[BlindPanelReviewerBatch],
    evidence: Sequence[DeliverableQualityReviewEvidence],
) -> tuple[BlindPanelReviewerBatch, ...]:
    if not reviewers or not evidence:
        return tuple(reviewers)
    first = reviewers[0]
    candidate = first.candidates[0]
    enriched = candidate.model_copy(
        update={
            "evidence_receipts": (
                *candidate.evidence_receipts,
                *tuple(_managed_automated_receipt(item) for item in evidence),
            )
        }
    )
    return (
        first.model_copy(update={"candidates": (enriched,)}),
        *reviewers[1:],
    )


def finalize_managed_review(
    review: DeliverableQualityReview,
    artifacts: Sequence[DeliverableArtifactRevision],
    assignments: Sequence[DeliverableQualityReviewAssignment],
    evidence: Sequence[DeliverableQualityReviewEvidence],
    *,
    now: datetime | None = None,
) -> DeliverableQualityGateReceipt | None:
    """Seal a review when complete, or immediately persist exact blocking evidence."""

    if review.status != "open":
        if review.receipt is None:
            return None
        return DeliverableQualityGateReceipt.model_validate(review.receipt)
    current_hashes = selected_artifact_hashes(artifacts)
    if current_hashes != dict(review.artifact_hashes):
        review.status = "superseded"
        review.sealed_at = now or datetime.now(UTC)
        return None

    exact_prohibited_evidence = tuple(
        (item, finding)
        for item in evidence
        for receipt in (_managed_automated_receipt(item),)
        if receipt.status == "complete"
        for finding in receipt.findings
        if finding.startswith("prohibited_term_detected=")
    )
    if exact_prohibited_evidence:
        receipt = blocked_quality_receipt_from_automated_evidence(
            receipt_ref=f"managed-evidence:{review.id}:{review.version}",
            artifact_hashes=current_hashes,
            evidence_kind=exact_prohibited_evidence[0][0].kind,
            hard_gate_failures=("no_unrequested_watermark",),
            created_at=now,
        )
    else:
        if len(assignments) != review.assigned_reviewer_count:
            raise DeliverableQualityReviewError(
                "deliverable_quality_review_assignment_mismatch",
                "The persisted reviewer assignment count does not match the review",
            )
        if any(
            assignment.status != "submitted" or not assignment.submission
            for assignment in assignments
        ):
            return None
        scenario = CreativeScenario.model_validate(review.scenario)
        required_kinds = required_evidence_kinds(
            scenario.modality,
            require_av_sync=bool(scenario.metadata.get("require_av_sync")),
        )
        automated_kinds = {
            kind for kind in required_kinds if kind not in _HUMAN_EVIDENCE_KINDS
        }
        persisted_automated_kinds = {item.kind for item in evidence}
        if not automated_kinds <= persisted_automated_kinds:
            return None
        reviewer_batches = tuple(
            BlindPanelReviewerBatch.model_validate(assignment.submission)
            for assignment in assignments
        )
        panel = BlindPanelSubmission(
            scenario_id=scenario.scenario_id,
            reviewers=_append_automated_evidence(reviewer_batches, evidence),
        )
        results = score_blind_review_panel(
            scenario,
            BlindReviewPackage.model_validate(review.review_package),
            panel,
            minimum_reviewers=review.minimum_reviewers,
            required_evidence_kinds=required_kinds,  # type: ignore[arg-type]
        )
        if len(results) != 1:
            raise DeliverableQualityReviewError(
                "deliverable_quality_review_result_mismatch",
                "Managed deliverable review must produce exactly one candidate result",
            )
        receipt = quality_receipt_from_panel_result(
            results[0],
            artifact_hashes=current_hashes,
            receipt_ref=f"managed-panel:{review.id}:{review.version}",
            created_at=now,
        )

    attach_deliverable_quality_gate_receipt(artifacts, receipt)
    review.status = receipt.status
    review.receipt = receipt.model_dump(mode="json")
    review.receipt_sha256 = quality_receipt_sha256(receipt)
    review.sealed_at = now or datetime.now(UTC)
    return receipt


def build_managed_evidence_receipt(
    review: DeliverableQualityReview,
    *,
    kind: str,
    status: str,
    source_ref: str,
    findings: Sequence[str],
    receipt_ref: str,
) -> CreativeEvidenceReceipt:
    scenario = CreativeScenario.model_validate(review.scenario)
    expected = {
        value
        for value in required_evidence_kinds(
            scenario.modality,
            require_av_sync=bool(scenario.metadata.get("require_av_sync")),
        )
        if value not in _HUMAN_EVIDENCE_KINDS
    }
    if kind not in expected:
        raise DeliverableQualityReviewError(
            "deliverable_quality_review_evidence_kind_invalid",
            "Evidence kind does not match the deliverable modality",
        )
    return CreativeEvidenceReceipt(
        receipt_ref=receipt_ref,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        artifact_hashes=dict(review.artifact_hashes),
        source=f"managed_operator:{source_ref}",
        findings=tuple(findings),
    )


__all__ = [
    "DeliverableQualityReviewError",
    "build_managed_evidence_receipt",
    "build_managed_review_contract",
    "build_reviewer_batch",
    "canonical_payload_sha256",
    "deliverable_modality",
    "finalize_managed_review",
    "required_evidence_kinds",
    "review_creation_fingerprint",
    "reviewer_submission_fingerprint",
    "selected_artifact_hashes",
]
