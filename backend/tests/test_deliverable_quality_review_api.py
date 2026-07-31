"""API boundary tests for managed deliverable quality reviews."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.api import deliverables
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableQualityReviewAssignment,
    DeliverableRequest,
)
from app.models.user import Identity, User
from app.schemas.deliverable import (
    DeliverableQualityReviewCreate,
    DeliverableQualityReviewSubmissionIn,
    DeliverableReviewerDimensionIn,
    DeliverableReviewerEvidenceIn,
    DeliverableReviewerHardGateIn,
)
from app.services.deliverable_quality_reviews import (
    build_managed_review_contract,
    reviewer_submission_fingerprint,
)


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        return self.value if isinstance(self.value, (list, tuple)) else [self.value]


class _SequencedSession:
    def __init__(
        self,
        *execute_values: object | None,
        get_value: object | None = None,
    ) -> None:
        self.execute_values = list(execute_values)
        self.get_value = get_value

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def get(self, _model, _key):
        return self.get_value


def _request() -> DeliverableRequest:
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="f" * 64,
        work_type="presentation",
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
        goal="Create a commercial launch deck",
        inputs=[],
        spec={"aspect_ratio": "16:9"},
        tier="pro",
        approval_policy=["final"],
        output_contract=["pptx", "pdf"],
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
            content_hash=character * 64,
            size_bytes=1024,
            revision_number=1,
            status="candidate",
            evaluation={"verified": True},
        )
        for artifact_type, character in (("pptx", "a"), ("pdf", "b"))
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
        created_by_user_id=request.created_by_user_id,
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
        created_at=datetime.now(UTC),
    )


def _user(
    *,
    tenant_id: uuid.UUID,
    role: str = "member",
    identity_id: uuid.UUID | None = None,
) -> User:
    identity = Identity(
        id=identity_id or uuid.uuid4(),
        is_active=True,
        is_platform_admin=False,
    )
    return User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        identity_id=identity.id,
        display_name="Independent reviewer",
        role=role,
        is_active=True,
        identity=identity,
    )


def _submission(
    review: DeliverableQualityReview,
    *,
    client_submission_id: uuid.UUID | None = None,
) -> DeliverableQualityReviewSubmissionIn:
    return DeliverableQualityReviewSubmissionIn(
        client_submission_id=client_submission_id or uuid.uuid4(),
        expected_version=review.version,
        hard_gates={
            gate: DeliverableReviewerHardGateIn(
                passed=True,
                evidence=[f"Checked {gate} against the immutable artifact"],
            )
            for gate in review.scenario["hard_gates"]
        },
        dimensions={
            dimension: DeliverableReviewerDimensionIn(
                score=5,
                evidence=[f"Checked {dimension} against the confirmed brief"],
            )
            for dimension in review.scenario["quality_dimensions"]
        },
        human_evidence={
            kind: DeliverableReviewerEvidenceIn(
                status="complete",
                findings=[f"Completed independent {kind} inspection"],
            )
            for kind in ("document_semantic", "human_visual")
        },
        notes=["Independent review completed"],
    )


def test_changed_artifact_hash_supersedes_a_sealed_review_without_erasing_receipt() -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    review.status = "passed"
    review.receipt = {"receipt_ref": "panel:historical-pass"}
    replacement = list(artifacts)
    replacement[0].content_hash = "9" * 64
    sealed_at = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    changed = deliverables._supersede_review_for_changed_artifacts(  # noqa: SLF001
        review,
        tuple(replacement),
        now=sealed_at,
    )

    assert changed is True
    assert review.status == "superseded"
    assert review.sealed_at == sealed_at
    assert review.version == 2
    assert review.receipt == {"receipt_ref": "panel:historical-pass"}


@pytest.mark.asyncio
async def test_unassigned_same_tenant_user_cannot_discover_review() -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    assigned_user = _user(tenant_id=request.tenant_id)
    assignment = DeliverableQualityReviewAssignment(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        review_id=review.id,
        reviewer_user_id=assigned_user.id,
        reviewer_identity_id=assigned_user.identity_id,
        reviewer_display_name=assigned_user.display_name,
        reviewer_role=assigned_user.role,
        reviewer_receipt_ref=f"managed-reviewer:{review.id}:{assigned_user.identity_id}",
        status="assigned",
    )
    unassigned_user = _user(tenant_id=request.tenant_id)
    db = _SequencedSession(review, [assignment], [], get_value=request)

    with pytest.raises(HTTPException) as error:
        await deliverables._review_access(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            review_id=review.id,
            user=unassigned_user,
        )

    assert error.value.status_code == 404
    assert error.value.detail == "Quality review not found"


@pytest.mark.asyncio
async def test_review_creation_rejects_two_memberships_for_one_physical_identity(
    monkeypatch,
) -> None:
    request = _request()
    artifacts = _artifacts(request)
    manager = _user(tenant_id=request.tenant_id, role="org_admin")
    shared_identity_id = uuid.uuid4()
    reviewers = (
        _user(tenant_id=request.tenant_id, identity_id=shared_identity_id),
        _user(tenant_id=request.tenant_id, identity_id=shared_identity_id),
        _user(tenant_id=request.tenant_id),
    )
    data = DeliverableQualityReviewCreate(
        client_review_id=uuid.uuid4(),
        expected_request_version=request.version,
        reviewer_user_ids=[reviewer.id for reviewer in reviewers],
    )
    db = _SequencedSession(None, None, reviewers)
    monkeypatch.setattr(
        deliverables,
        "_manageable_request",
        AsyncMock(return_value=request),
    )
    monkeypatch.setattr(
        deliverables,
        "_request_artifacts",
        AsyncMock(return_value=artifacts),
    )
    monkeypatch.setattr(
        deliverables,
        "_ensure_quality_review_allowlisted",
        lambda _request: None,
    )
    monkeypatch.setattr(
        deliverables,
        "deliverable_approval_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(quality_status="pending"),
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.create_deliverable_quality_review(
            request.id,
            data,
            manager,
            db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert error.value.detail == "Every reviewer must have a distinct physical identity"


@pytest.mark.asyncio
async def test_exact_reviewer_submission_replay_is_idempotent(monkeypatch) -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    reviewer = _user(tenant_id=request.tenant_id)
    data = _submission(review)
    assignment = DeliverableQualityReviewAssignment(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        review_id=review.id,
        reviewer_user_id=reviewer.id,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_display_name=reviewer.display_name,
        reviewer_role=reviewer.role,
        reviewer_receipt_ref=f"managed-reviewer:{review.id}:{reviewer.identity_id}",
        status="submitted",
        client_submission_id=data.client_submission_id,
        submission_fingerprint=reviewer_submission_fingerprint(data),
        submission={"sealed": True},
    )
    expected = object()
    monkeypatch.setattr(
        deliverables,
        "_review_access",
        AsyncMock(return_value=(review, request, artifacts, (assignment,), ())),
    )
    monkeypatch.setattr(deliverables, "_quality_review_out", lambda **_kwargs: expected)

    result = await deliverables.submit_deliverable_quality_review(
        review.id,
        data,
        reviewer,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert result is expected


@pytest.mark.asyncio
async def test_reviewer_cannot_replace_a_sealed_submission(monkeypatch) -> None:
    request = _request()
    artifacts = _artifacts(request)
    review = _review(request, artifacts)
    reviewer = _user(tenant_id=request.tenant_id)
    original = _submission(review)
    assignment = DeliverableQualityReviewAssignment(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        review_id=review.id,
        reviewer_user_id=reviewer.id,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_display_name=reviewer.display_name,
        reviewer_role=reviewer.role,
        reviewer_receipt_ref=f"managed-reviewer:{review.id}:{reviewer.identity_id}",
        status="submitted",
        client_submission_id=original.client_submission_id,
        submission_fingerprint=reviewer_submission_fingerprint(original),
        submission={"sealed": True},
    )
    monkeypatch.setattr(
        deliverables,
        "_review_access",
        AsyncMock(return_value=(review, request, artifacts, (assignment,), ())),
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.submit_deliverable_quality_review(
            review.id,
            _submission(review),
            reviewer,
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert error.value.detail == "This reviewer has already sealed a submission"


@pytest.mark.asyncio
async def test_non_admin_cannot_attest_automated_evidence() -> None:
    user = _user(tenant_id=uuid.uuid4())

    with pytest.raises(HTTPException) as error:
        await deliverables.add_deliverable_quality_review_evidence(
            uuid.uuid4(),
            SimpleNamespace(),  # type: ignore[arg-type]
            user,
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 403
    assert error.value.detail == "Admin access required"
