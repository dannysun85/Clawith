from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from pydantic import ValidationError

from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.services.deliverable_quality_gate import (
    DeliverableQualityGateError,
    DeliverableQualityGateReceipt,
    attach_deliverable_quality_gate_receipt,
    blocked_quality_receipt_from_automated_evidence,
    creative_quality_gate_required_for_request,
    deliverable_approval_readiness,
    quality_gate_evaluation_payload,
)


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
        spec={},
        tier="pro",
        approval_policy=["outline", "final"],
        output_contract=["pptx", "pdf"],
        status="waiting_approval",
        current_stage="output_review",
        version=3,
    )


def _artifacts(request: DeliverableRequest) -> tuple[DeliverableArtifactRevision, ...]:
    return tuple(
        DeliverableArtifactRevision(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            artifact_key=artifact_type,
            artifact_type=artifact_type,
            workspace_path=f"workspace/deliverables/{request.id}/result.{artifact_type}",
            content_hash=content_hash,
            revision_number=1,
            status="candidate",
            evaluation={"verified": True},
        )
        for artifact_type, content_hash in (("pptx", "a" * 64), ("pdf", "b" * 64))
    )


def _passed_receipt(
    artifacts: tuple[DeliverableArtifactRevision, ...],
) -> DeliverableQualityGateReceipt:
    return DeliverableQualityGateReceipt(
        receipt_ref="panel:scenario-1:candidate-a",
        source="blind_review_panel",
        status="passed",
        artifact_hashes={
            artifact.artifact_key: artifact.content_hash for artifact in artifacts
        },
        reviewer_count=3,
        required_evidence_kinds=("document_semantic", "human_visual"),
        complete_evidence_kinds=("document_semantic", "human_visual"),
        commercially_usable=True,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


def test_default_off_preserves_legacy_approval_but_required_gate_is_pending() -> None:
    request = _request()
    artifacts = _artifacts(request)

    legacy = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=False,
    )
    required = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=True,
    )

    assert legacy.approvable is True
    assert legacy.quality_status == "not_required"
    assert required.approvable is False
    assert required.quality_status == "pending"
    assert required.blockers == ("deliverable_creative_quality_review_required",)


def test_gate_requires_explicit_tenant_or_agent_allowlist() -> None:
    request = _request()

    assert creative_quality_gate_required_for_request(
        request,
        enabled=True,
        tenant_ids="",
        agent_ids="",
    ) is False
    assert creative_quality_gate_required_for_request(
        request,
        enabled=True,
        tenant_ids=str(request.tenant_id),
        agent_ids="",
    ) is True
    assert creative_quality_gate_required_for_request(
        request,
        enabled=True,
        tenant_ids="",
        agent_ids=str(request.agent_id),
    ) is True
    assert creative_quality_gate_required_for_request(
        request,
        enabled=False,
        tenant_ids=str(request.tenant_id),
        agent_ids=str(request.agent_id),
    ) is False


def test_invalid_gate_allowlist_fails_loudly_when_enabled() -> None:
    request = _request()

    with pytest.raises(ValueError, match="invalid UUID"):
        creative_quality_gate_required_for_request(
            request,
            enabled=True,
            tenant_ids="not-a-uuid",
            agent_ids="",
        )


def test_explicit_automated_failure_blocks_even_when_rollout_flag_is_off() -> None:
    request = _request()
    artifacts = _artifacts(request)
    receipt = blocked_quality_receipt_from_automated_evidence(
        receipt_ref="frame-ocr:artifact-1",
        artifact_hashes={
            artifact.artifact_key: artifact.content_hash for artifact in artifacts
        },
        evidence_kind="frame_ocr",
        hard_gate_failures=("no_unrequested_watermark",),
    )
    attach_deliverable_quality_gate_receipt(artifacts, receipt)

    readiness = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=False,
    )

    assert readiness.approvable is False
    assert readiness.quality_status == "blocked"
    assert readiness.blockers == ("deliverable_creative_quality_blocked",)


def test_receipt_is_rejected_after_artifact_hash_changes() -> None:
    request = _request()
    artifacts = _artifacts(request)
    attach_deliverable_quality_gate_receipt(artifacts, _passed_receipt(artifacts))
    artifacts[1].content_hash = "c" * 64

    readiness = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=True,
    )

    assert readiness.approvable is False
    assert readiness.quality_status == "invalid"
    assert readiness.blockers == (
        "deliverable_creative_quality_receipt_hash_mismatch",
    )


def test_tampered_receipt_digest_is_invalid() -> None:
    request = _request()
    artifacts = _artifacts(request)
    payload = quality_gate_evaluation_payload(_passed_receipt(artifacts))
    payload["receipt_sha256"] = "0" * 64
    for artifact in artifacts:
        artifact.evaluation = {"verified": True, "quality_gate": payload}

    readiness = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=True,
    )

    assert readiness.quality_status == "invalid"
    assert readiness.blockers == ("deliverable_creative_quality_receipt_invalid",)


def test_attach_refuses_receipt_for_a_different_artifact_set() -> None:
    request = _request()
    artifacts = _artifacts(request)
    receipt = _passed_receipt(artifacts).model_copy(
        update={"artifact_hashes": {"pptx": "d" * 64, "pdf": "e" * 64}}
    )

    with pytest.raises(DeliverableQualityGateError) as error:
        attach_deliverable_quality_gate_receipt(artifacts, receipt)

    assert error.value.code == "deliverable_creative_quality_receipt_hash_mismatch"


def test_automated_or_understaffed_review_cannot_issue_a_pass() -> None:
    base = {
        "receipt_ref": "invalid-pass",
        "status": "passed",
        "artifact_hashes": {"mp4": "a" * 64},
        "required_evidence_kinds": ("frame_ocr", "human_visual", "human_audio"),
        "complete_evidence_kinds": ("frame_ocr", "human_visual", "human_audio"),
        "commercially_usable": True,
    }

    with pytest.raises(ValidationError):
        DeliverableQualityGateReceipt(
            **base,
            source="automated_evidence",
            reviewer_count=0,
        )
    with pytest.raises(ValidationError):
        DeliverableQualityGateReceipt(
            **base,
            source="blind_review_panel",
            reviewer_count=2,
        )
