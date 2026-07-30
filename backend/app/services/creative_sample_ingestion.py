"""Privacy-minimized ingestion for authorized real creative briefs.

Raw production identifiers and free text are accepted only as transient input.
Every exported candidate is pseudonymized, data-minimized, and forced into a
manual-review state before it may be used by an evaluation suite.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import re
from typing import Any, Literal, Mapping
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.creative_evaluation import CreativeModality


CreativeSourceKind = Literal["deliverable_request", "tool_execution"]
CreativeSampleReviewStatus = Literal[
    "pending_review",
    "approved",
    "rejected",
    "needs_clarification",
]


class AuthorizedCreativeBriefExport(BaseModel):
    """Transient, trusted export row. Never persist this model as a fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=200)
    source_kind: CreativeSourceKind
    modality: CreativeModality
    observed_at: datetime
    authorization_ref: str = Field(min_length=1, max_length=200)
    brief_text: str = Field(min_length=1, max_length=12_000)
    safe_spec: dict[str, Any] = Field(default_factory=dict)
    input_count: int = Field(default=0, ge=0)
    source_status: str = Field(default="unknown", min_length=1, max_length=64)

    @field_validator("source_status")
    @classmethod
    def validate_source_status(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]+", value):
            raise ValueError("source_status must be a normalized status token")
        return value


class AnonymizedCreativeBriefCandidate(BaseModel):
    """Persistable candidate that still requires a human privacy/content review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    sample_id: str
    source_ref_hmac: str
    source_kind: CreativeSourceKind
    modality: CreativeModality
    observed_month: str
    authorization_ref: str
    brief: str
    safe_spec: dict[str, Any]
    input_count_bucket: str
    source_status: str
    redactions: dict[str, int]
    omitted_spec_keys: tuple[str, ...]
    review_status: CreativeSampleReviewStatus = "pending_review"
    review_required: bool = True
    content_sha256: str


class CreativeBriefReviewDecision(BaseModel):
    """Explicit review receipt; approval cannot be inferred from ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str
    decision: Literal["approved", "rejected", "needs_clarification"]
    reviewer_receipt_ref: str = Field(min_length=1, max_length=200)
    reviewed_brief: str | None = Field(default=None, min_length=1, max_length=4_000)
    reviewed_spec: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = ()
    benchmark_cluster: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(values))
        for value in normalized:
            if not re.fullmatch(r"[a-z0-9_]+", value):
                raise ValueError("reason_codes must contain normalized tokens")
        return normalized

    @field_validator("benchmark_cluster")
    @classmethod
    def validate_benchmark_cluster(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-z0-9_-]+", value):
            raise ValueError("benchmark_cluster must be a normalized token")
        return value

    @model_validator(mode="after")
    def validate_decision_contract(self) -> CreativeBriefReviewDecision:
        if self.decision == "approved" and self.reviewed_brief is None:
            raise ValueError("Approved samples require reviewed_brief")
        if self.decision != "approved" and not self.reason_codes:
            raise ValueError("Non-approved samples require reason_codes")
        return self


class ReviewedCreativeBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    sample_id: str
    modality: CreativeModality
    authorization_ref: str
    brief: str
    safe_spec: dict[str, Any]
    input_count_bucket: str
    source_status: str
    review_status: CreativeSampleReviewStatus
    reviewer_receipt_ref: str
    review_reason_codes: tuple[str, ...]
    benchmark_cluster: str | None
    benchmark_eligible: bool
    content_sha256: str


_SAFE_SPEC_KEYS: dict[CreativeModality, frozenset[str]] = {
    "image": frozenset(
        {
            "aspect_ratio",
            "channel",
            "language",
            "style",
            "reference_count",
            "exact_copy_required",
        }
    ),
    "video": frozenset(
        {
            "aspect_ratio",
            "audio_mode",
            "channel",
            "duration",
            "language",
            "resolution",
            "style",
            "reference_count",
        }
    ),
    "presentation": frozenset(
        {
            "aspect_ratio",
            "editability_contract",
            "language",
            "page_count",
            "style",
            "source_count",
        }
    ),
}
_SAFE_SCALAR_TYPES = (str, int, float, bool)
_REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "api_token",
        re.compile(
            r"(?i)\b(?:ark|sk|ak|token|secret|key)[-_][A-Za-z0-9_-]{12,}\b"
        ),
    ),
    (
        "email",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    ),
    (
        "url",
        re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>'\"]+"),
    ),
    (
        "uuid",
        re.compile(
            r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
        ),
    ),
    (
        "ipv4",
        re.compile(
            r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)"
            r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b"
        ),
    ),
    (
        "cn_identity_number",
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    ),
    (
        "workspace_path",
        re.compile(
            r"(?i)(?:workspace|deliverable_artifacts)/[^\s<>'\"]+"
        ),
    ),
)
_REDACTION_MARKER = "[REDACTED:{kind}]"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hmac_sha256(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _input_count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 4:
        return "2-4"
    return "5+"


def sanitize_creative_brief_text(value: str) -> tuple[str, dict[str, int]]:
    """Apply deterministic high-confidence redactions.

    This is not a complete anonymizer. The caller must retain the manual review
    gate because company, product, person, and campaign names can remain.
    """

    sanitized = unicodedata.normalize("NFKC", value)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", sanitized)
    redactions: dict[str, int] = {}
    for kind, pattern in _REDACTION_PATTERNS:
        sanitized, count = pattern.subn(
            _REDACTION_MARKER.format(kind=kind),
            sanitized,
        )
        if count:
            redactions[kind] = count
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if len(sanitized) > 4_000:
        sanitized = sanitized[:3_997].rstrip() + "..."
        redactions["truncated"] = redactions.get("truncated", 0) + 1
    return sanitized, redactions


def _minimize_spec(
    modality: CreativeModality,
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    allowed = _SAFE_SPEC_KEYS[modality]
    minimized: dict[str, Any] = {}
    omitted: list[str] = []
    for key, value in spec.items():
        normalized_key = str(key)
        if (
            normalized_key not in allowed
            or not isinstance(value, _SAFE_SCALAR_TYPES)
            or isinstance(value, str)
            and len(value) > 120
        ):
            omitted.append(normalized_key)
            continue
        minimized[normalized_key] = value
    return dict(sorted(minimized.items())), tuple(sorted(set(omitted)))


def anonymize_creative_brief(
    source: AuthorizedCreativeBriefExport,
    *,
    pseudonym_key: bytes,
) -> AnonymizedCreativeBriefCandidate:
    """Return a data-minimized candidate with no raw production identifiers."""

    if len(pseudonym_key) < 32:
        raise ValueError("pseudonym_key must contain at least 32 bytes")
    source_ref = _hmac_sha256(
        pseudonym_key,
        f"{source.source_kind}:{source.source_id}",
    )
    sample_id = f"real-{source.modality}-{source_ref[:16]}"
    brief, redactions = sanitize_creative_brief_text(source.brief_text)
    if not brief:
        raise ValueError("Brief is empty after sanitization")
    safe_spec, omitted_spec_keys = _minimize_spec(
        source.modality,
        source.safe_spec,
    )
    content_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "brief": brief,
                "modality": source.modality,
                "safe_spec": safe_spec,
            }
        ).encode("utf-8")
    ).hexdigest()
    return AnonymizedCreativeBriefCandidate(
        sample_id=sample_id,
        source_ref_hmac=source_ref,
        source_kind=source.source_kind,
        modality=source.modality,
        observed_month=source.observed_at.strftime("%Y-%m"),
        authorization_ref=source.authorization_ref,
        brief=brief,
        safe_spec=safe_spec,
        input_count_bucket=_input_count_bucket(source.input_count),
        source_status=source.source_status,
        redactions=redactions,
        omitted_spec_keys=omitted_spec_keys,
        content_sha256=content_sha256,
    )


def apply_creative_brief_review(
    candidate: AnonymizedCreativeBriefCandidate,
    decision: CreativeBriefReviewDecision,
) -> ReviewedCreativeBrief:
    """Apply an explicit review decision without mutating the source candidate."""

    if candidate.sample_id != decision.sample_id:
        raise ValueError("Review decision does not match candidate")
    if decision.decision == "approved":
        assert decision.reviewed_brief is not None
        reviewed_brief, redactions = sanitize_creative_brief_text(
            decision.reviewed_brief
        )
        if redactions:
            raise ValueError(
                "Reviewed brief still contains high-confidence sensitive data"
            )
        reviewed_spec, omitted = _minimize_spec(
            candidate.modality,
            decision.reviewed_spec or candidate.safe_spec,
        )
        if omitted:
            raise ValueError(
                "Reviewed spec contains fields outside the safe allowlist"
            )
        content_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "brief": reviewed_brief,
                    "modality": candidate.modality,
                    "safe_spec": reviewed_spec,
                }
            ).encode("utf-8")
        ).hexdigest()
        return ReviewedCreativeBrief(
            sample_id=candidate.sample_id,
            modality=candidate.modality,
            authorization_ref=candidate.authorization_ref,
            brief=reviewed_brief,
            safe_spec=reviewed_spec,
            input_count_bucket=candidate.input_count_bucket,
            source_status=candidate.source_status,
            review_status="approved",
            reviewer_receipt_ref=decision.reviewer_receipt_ref,
            review_reason_codes=decision.reason_codes,
            benchmark_cluster=decision.benchmark_cluster,
            benchmark_eligible=True,
            content_sha256=content_sha256,
        )
    return ReviewedCreativeBrief(
        sample_id=candidate.sample_id,
        modality=candidate.modality,
        authorization_ref=candidate.authorization_ref,
        brief=candidate.brief,
        safe_spec=candidate.safe_spec,
        input_count_bucket=candidate.input_count_bucket,
        source_status=candidate.source_status,
        review_status=decision.decision,
        reviewer_receipt_ref=decision.reviewer_receipt_ref,
        review_reason_codes=decision.reason_codes,
        benchmark_cluster=decision.benchmark_cluster,
        benchmark_eligible=False,
        content_sha256=candidate.content_sha256,
    )


def apply_creative_brief_review_batch(
    candidates: list[AnonymizedCreativeBriefCandidate],
    decisions: list[CreativeBriefReviewDecision],
) -> list[ReviewedCreativeBrief]:
    """Apply a complete one-to-one review batch.

    Partial or duplicate decision sets are rejected so a caller cannot silently
    treat an unreviewed customer brief as benchmark-ready.
    """

    candidates_by_id = {candidate.sample_id: candidate for candidate in candidates}
    decisions_by_id = {decision.sample_id: decision for decision in decisions}
    if len(candidates_by_id) != len(candidates):
        raise ValueError("Candidate batch contains duplicate sample_id values")
    if len(decisions_by_id) != len(decisions):
        raise ValueError("Decision batch contains duplicate sample_id values")
    missing = sorted(candidates_by_id.keys() - decisions_by_id.keys())
    unexpected = sorted(decisions_by_id.keys() - candidates_by_id.keys())
    if missing or unexpected:
        raise ValueError(
            "Review batch must contain exactly one decision per candidate; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return [
        apply_creative_brief_review(
            candidate,
            decisions_by_id[candidate.sample_id],
        )
        for candidate in candidates
    ]


__all__ = [
    "AnonymizedCreativeBriefCandidate",
    "AuthorizedCreativeBriefExport",
    "CreativeBriefReviewDecision",
    "ReviewedCreativeBrief",
    "anonymize_creative_brief",
    "apply_creative_brief_review",
    "apply_creative_brief_review_batch",
    "sanitize_creative_brief_text",
]
