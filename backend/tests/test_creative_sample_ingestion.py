from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest
from pydantic import ValidationError

from app.services.creative_sample_ingestion import (
    AnonymizedCreativeBriefCandidate,
    AuthorizedCreativeBriefExport,
    CreativeBriefReviewDecision,
    anonymize_creative_brief,
    apply_creative_brief_review,
    apply_creative_brief_review_batch,
)


def _source(**overrides):
    values = {
        "source_id": "0ca67738-3859-47ac-b45d-a2de5f616d1b",
        "source_kind": "tool_execution",
        "modality": "video",
        "observed_at": datetime(2026, 7, 27, 8, 30, tzinfo=UTC),
        "authorization_ref": "customer-creative-audit-2026-07-27",
        "brief_text": (
            "给 alice@example.com 制作 6 秒广告，素材在 "
            "workspace/assets/customer-a.png，联系电话 13800138000。"
        ),
        "safe_spec": {
            "duration": 6,
            "resolution": "768P",
            "aspect_ratio": "9:16",
            "prompt": "must never survive",
            "save_path": "workspace/output.mp4",
        },
        "input_count": 2,
        "source_status": "succeeded",
    }
    values.update(overrides)
    return AuthorizedCreativeBriefExport(**values)


def test_anonymization_removes_raw_identity_and_minimizes_spec() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"k" * 32)
    serialized = candidate.model_dump_json()

    assert "0ca67738-3859-47ac-b45d-a2de5f616d1b" not in serialized
    assert "alice@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "workspace/assets/customer-a.png" not in serialized
    assert candidate.review_status == "pending_review"
    assert candidate.review_required is True
    assert candidate.safe_spec == {
        "aspect_ratio": "9:16",
        "duration": 6,
        "resolution": "768P",
    }
    assert candidate.omitted_spec_keys == ("prompt", "save_path")
    assert candidate.input_count_bucket == "2-4"
    assert candidate.observed_month == "2026-07"


def test_pseudonym_is_stable_only_for_same_private_key() -> None:
    source = _source()

    first = anonymize_creative_brief(source, pseudonym_key=b"a" * 32)
    second = anonymize_creative_brief(source, pseudonym_key=b"a" * 32)
    rotated = anonymize_creative_brief(source, pseudonym_key=b"b" * 32)

    assert first.sample_id == second.sample_id
    assert first.sample_id != rotated.sample_id
    assert first.source_ref_hmac != rotated.source_ref_hmac


def test_raw_export_rejects_unknown_fields() -> None:
    payload = json.loads(_source().model_dump_json())
    payload["tenant_id"] = "must-not-enter-contract"

    with pytest.raises(ValidationError):
        AuthorizedCreativeBriefExport.model_validate(payload)


def test_ingestion_cannot_approve_a_sample() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"k" * 32)

    assert candidate.review_status == "pending_review"
    assert "reviewer" not in type(candidate).model_fields


def test_explicit_review_can_approve_cleaned_content() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"k" * 32)
    reviewed = apply_creative_brief_review(
        candidate,
        CreativeBriefReviewDecision(
            sample_id=candidate.sample_id,
            decision="approved",
            reviewer_receipt_ref="privacy-review-0001",
            reviewed_brief="制作一条 6 秒竖屏产品广告，强调真实使用场景。",
            reviewed_spec={
                "duration": 6,
                "resolution": "768P",
                "aspect_ratio": "9:16",
            },
            benchmark_cluster="generic-product-video",
        ),
    )

    assert reviewed.review_status == "approved"
    assert reviewed.benchmark_eligible is True
    assert reviewed.benchmark_cluster == "generic-product-video"
    assert reviewed.reviewer_receipt_ref == "privacy-review-0001"
    assert reviewed.brief.startswith("制作一条 6 秒")


def test_review_rejects_remaining_high_confidence_sensitive_data() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"k" * 32)

    with pytest.raises(
        ValueError,
        match="still contains high-confidence sensitive data",
    ):
        apply_creative_brief_review(
            candidate,
            CreativeBriefReviewDecision(
                sample_id=candidate.sample_id,
                decision="approved",
                reviewer_receipt_ref="privacy-review-0002",
                reviewed_brief="联系 bob@example.com 后制作视频。",
            ),
        )


def test_review_can_request_clarification_without_benchmark_eligibility() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"k" * 32)
    reviewed = apply_creative_brief_review(
        candidate,
        CreativeBriefReviewDecision(
            sample_id=candidate.sample_id,
            decision="needs_clarification",
            reviewer_receipt_ref="privacy-review-0003",
            reason_codes=("missing_reference_asset",),
            benchmark_cluster="brand-product-video",
        ),
    )

    assert reviewed.review_status == "needs_clarification"
    assert reviewed.benchmark_eligible is False
    assert reviewed.review_reason_codes == ("missing_reference_asset",)


def test_non_approved_review_requires_reason_codes() -> None:
    with pytest.raises(ValidationError, match="require reason_codes"):
        CreativeBriefReviewDecision(
            sample_id="real-video-example",
            decision="rejected",
            reviewer_receipt_ref="privacy-review-0004",
        )


def test_batch_review_requires_exactly_one_decision_per_candidate() -> None:
    first = anonymize_creative_brief(_source(), pseudonym_key=b"a" * 32)
    second = anonymize_creative_brief(
        _source(source_id="another-source"),
        pseudonym_key=b"a" * 32,
    )
    decision = CreativeBriefReviewDecision(
        sample_id=first.sample_id,
        decision="needs_clarification",
        reviewer_receipt_ref="privacy-review-0005",
        reason_codes=("missing_reference_asset",),
    )

    with pytest.raises(ValueError, match="exactly one decision"):
        apply_creative_brief_review_batch([first, second], [decision])


def test_batch_review_rejects_duplicate_candidate_ids() -> None:
    candidate = anonymize_creative_brief(_source(), pseudonym_key=b"a" * 32)
    duplicate = AnonymizedCreativeBriefCandidate.model_validate(
        candidate.model_dump()
    )
    decision = CreativeBriefReviewDecision(
        sample_id=candidate.sample_id,
        decision="needs_clarification",
        reviewer_receipt_ref="privacy-review-0006",
        reason_codes=("missing_reference_asset",),
    )

    with pytest.raises(ValueError, match="duplicate sample_id"):
        apply_creative_brief_review_batch(
            [candidate, duplicate],
            [decision],
        )
