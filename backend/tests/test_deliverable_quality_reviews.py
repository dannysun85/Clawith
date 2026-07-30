from __future__ import annotations

from datetime import UTC, datetime
import uuid

from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableQualityReviewAssignment,
    DeliverableQualityReviewEvidence,
    DeliverableRequest,
)
from app.schemas.deliverable import (
    DeliverableQualityReviewSubmissionIn,
    DeliverableReviewerDimensionIn,
    DeliverableReviewerEvidenceIn,
    DeliverableReviewerHardGateIn,
)
from app.services.deliverable_quality_reviews import (
    build_managed_evidence_receipt,
    build_managed_review_contract,
    build_reviewer_batch,
    finalize_managed_review,
    reviewer_submission_fingerprint,
)


def _request(work_type: str = "presentation") -> DeliverableRequest:
    output_contract = ["pptx", "pdf"] if work_type == "presentation" else ["mp4"]
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="f" * 64,
        work_type=work_type,
        workflow_id=f"builtin.{work_type}.v1",
        workflow_version="1.0.0",
        goal="Create a commercially usable customer deliverable",
        inputs=[],
        spec={"aspect_ratio": "16:9"},
        tier="pro",
        approval_policy=["final"],
        output_contract=output_contract,
        status="waiting_approval",
        current_stage="output_review",
        version=3,
    )


def _artifacts(
    request: DeliverableRequest,
) -> tuple[DeliverableArtifactRevision, ...]:
    return tuple(
        DeliverableArtifactRevision(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            artifact_key=artifact_type,
            artifact_type=artifact_type,
            workspace_path=f"workspace/deliverables/{request.id}/result.{artifact_type}",
            content_hash=chr(ord("a") + index) * 64,
            size_bytes=1024 + index,
            revision_number=1,
            status="candidate",
            evaluation={"verified": True},
        )
        for index, artifact_type in enumerate(request.output_contract)
    )


def _review(
    request: DeliverableRequest,
    artifacts: tuple[DeliverableArtifactRevision, ...],
) -> DeliverableQualityReview:
    review_id = uuid.uuid4()
    scenario, package, hashes = build_managed_review_contract(
        request,
        artifacts,
        review_id=str(review_id),
    )
    return DeliverableQualityReview(
        id=review_id,
        tenant_id=request.tenant_id,
        request_id=request.id,
        created_by_user_id=uuid.uuid4(),
        client_review_id=uuid.uuid4(),
        request_fingerprint="e" * 64,
        modality=scenario.modality,
        status="open",
        minimum_reviewers=3,
        assigned_reviewer_count=3,
        artifact_hashes=hashes,
        scenario=scenario.model_dump(mode="json"),
        review_package=package.model_dump(mode="json"),
        version=1,
    )


def _assignments(
    review: DeliverableQualityReview,
) -> tuple[DeliverableQualityReviewAssignment, ...]:
    return tuple(
        DeliverableQualityReviewAssignment(
            id=uuid.uuid4(),
            tenant_id=review.tenant_id,
            review_id=review.id,
            reviewer_user_id=uuid.uuid4(),
            reviewer_identity_id=uuid.uuid4(),
            reviewer_display_name=f"Reviewer {index}",
            reviewer_role="member",
            reviewer_receipt_ref=f"managed-reviewer:{review.id}:{index}",
            status="assigned",
        )
        for index in range(3)
    )


def _submission(
    review: DeliverableQualityReview,
    *,
    score: float = 5,
    hard_gate_value: bool = True,
) -> DeliverableQualityReviewSubmissionIn:
    scenario = review.scenario
    human_kinds = {
        "image": ("human_visual",),
        "video": ("human_visual", "human_audio"),
        "presentation": ("document_semantic", "human_visual"),
    }[review.modality]
    return DeliverableQualityReviewSubmissionIn(
        client_submission_id=uuid.uuid4(),
        expected_version=review.version,
        hard_gates={
            gate: DeliverableReviewerHardGateIn(
                passed=hard_gate_value,
                evidence=[f"Observed gate {gate} against the artifact"],
            )
            for gate in scenario["hard_gates"]
        },
        dimensions={
            dimension: DeliverableReviewerDimensionIn(
                score=score,
                evidence=[f"Observed dimension {dimension} against the brief"],
            )
            for dimension in scenario["quality_dimensions"]
        },
        human_evidence={
            kind: DeliverableReviewerEvidenceIn(
                status="complete",
                findings=[f"Completed independent {kind} inspection"],
            )
            for kind in human_kinds
        },
        notes=["Independent managed review completed"],
    )


def _seal_assignments(
    review: DeliverableQualityReview,
    assignments: tuple[DeliverableQualityReviewAssignment, ...],
    *,
    scores: tuple[float, float, float] = (5, 5, 5),
) -> None:
    for assignment, score in zip(assignments, scores, strict=True):
        submission = _submission(review, score=score)
        batch = build_reviewer_batch(review, assignment, submission)
        assignment.client_submission_id = submission.client_submission_id
        assignment.submission_fingerprint = reviewer_submission_fingerprint(submission)
        assignment.submission = batch.model_dump(mode="json")
        assignment.status = "submitted"
        assignment.submitted_at = datetime.now(UTC)


def _automated_evidence(
    review: DeliverableQualityReview,
    *,
    kind: str,
    findings: tuple[str, ...] = (),
) -> DeliverableQualityReviewEvidence:
    receipt_ref = f"managed-evidence:{review.id}:{kind}"
    receipt = build_managed_evidence_receipt(
        review,
        kind=kind,
        status="complete",
        source_ref=f"private/evidence/{review.id}/{kind}.json",
        findings=findings,
        receipt_ref=receipt_ref,
    )
    return DeliverableQualityReviewEvidence(
        id=uuid.uuid4(),
        tenant_id=review.tenant_id,
        review_id=review.id,
        submitted_by_user_id=uuid.uuid4(),
        client_evidence_id=uuid.uuid4(),
        evidence_fingerprint="d" * 64,
        receipt_ref=receipt_ref,
        kind=kind,
        status="complete",
        source_ref=f"private/evidence/{review.id}/{kind}.json",
        receipt=receipt.model_dump(mode="json"),
    )


def test_three_identity_bound_presentation_reviews_issue_server_pass() -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assignments = _assignments(review)
    _seal_assignments(review, assignments)

    receipt = finalize_managed_review(
        review,
        artifacts,
        assignments,
        (),
        now=datetime(2026, 7, 27, tzinfo=UTC),
    )

    assert receipt is not None
    assert receipt.status == "passed"
    assert receipt.reviewer_count == 3
    assert review.status == "passed"
    assert review.receipt_sha256 is not None
    assert all("quality_gate" in artifact.evaluation for artifact in artifacts)


def test_video_review_waits_for_frame_ocr_before_sealing() -> None:
    request = _request("video")
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assignments = _assignments(review)
    _seal_assignments(review, assignments)

    assert finalize_managed_review(review, artifacts, assignments, ()) is None
    assert review.status == "open"

    receipt = finalize_managed_review(
        review,
        artifacts,
        assignments,
        (_automated_evidence(review, kind="frame_ocr"),),
    )

    assert receipt is not None
    assert receipt.status == "passed"


def test_exact_managed_ocr_finding_blocks_without_human_pass() -> None:
    request = _request("video")
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assignments = _assignments(review)
    evidence = _automated_evidence(
        review,
        kind="frame_ocr",
        findings=("prohibited_term_detected=豆包",),
    )

    receipt = finalize_managed_review(
        review,
        artifacts,
        assignments,
        (evidence,),
    )

    assert receipt is not None
    assert receipt.source == "automated_evidence"
    assert receipt.status == "blocked"
    assert receipt.hard_gate_failures == ("no_unrequested_watermark",)
    assert review.status == "blocked"


def test_panel_disagreement_is_sealed_incomplete_not_majority_pass() -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assignments = _assignments(review)
    _seal_assignments(review, assignments, scores=(5, 5, 3))

    receipt = finalize_managed_review(review, artifacts, assignments, ())

    assert receipt is not None
    assert receipt.status == "incomplete"
    assert receipt.commercially_usable is False
    assert any(item.startswith("dimension:") for item in receipt.disagreements)


def test_artifact_revision_change_supersedes_open_review_without_receipt() -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assignments = _assignments(review)
    artifacts[0].content_hash = "9" * 64

    receipt = finalize_managed_review(review, artifacts, assignments, ())

    assert receipt is None
    assert review.status == "superseded"
    assert review.receipt is None
