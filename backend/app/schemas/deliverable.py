"""API contracts for durable deliverable requests."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeliverableInput(BaseModel):
    type: Literal["workspace_file"]
    path: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, max_length=255)

    model_config = ConfigDict(extra="forbid")

    @field_validator("path")
    @classmethod
    def validate_workspace_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized.startswith("workspace/") or ".." in normalized.split("/"):
            raise ValueError("path must be a safe workspace-relative reference")
        return normalized


class DeliverableRequestCreate(BaseModel):
    client_request_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    task_id: uuid.UUID | None = None
    work_type: Literal["presentation", "poster", "video", "report", "spreadsheet"]
    workflow_id: str = Field(min_length=1, max_length=120)
    workflow_version: str = Field(min_length=1, max_length=32)
    goal: str = Field(min_length=3, max_length=4000)
    inputs: list[DeliverableInput] = Field(default_factory=list, max_length=20)
    spec: dict[str, Any] = Field(default_factory=dict)
    tier: Literal["lite", "pro", "ultra"]
    approval_policy: list[str] | None = Field(default=None, max_length=10)
    output_contract: list[str] | None = Field(default=None, max_length=10)

    model_config = ConfigDict(extra="forbid")


class DeliverablePreflightIn(BaseModel):
    agent_id: uuid.UUID
    work_type: Literal["presentation", "poster", "video", "report", "spreadsheet"]
    workflow_id: str = Field(min_length=1, max_length=120)
    workflow_version: str = Field(min_length=1, max_length=32)
    goal: str = Field(default="", max_length=4000)
    inputs: list[DeliverableInput] = Field(default_factory=list, max_length=20)
    spec: dict[str, Any] = Field(default_factory=dict)
    tier: Literal["lite", "pro", "ultra"]

    model_config = ConfigDict(extra="forbid")


class DeliverableRequestUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    goal: str | None = Field(default=None, min_length=3, max_length=4000)
    inputs: list[DeliverableInput] | None = Field(default=None, max_length=20)
    spec: dict[str, Any] | None = None
    tier: Literal["lite", "pro", "ultra"] | None = None

    model_config = ConfigDict(extra="forbid")


class DeliverableActionIn(BaseModel):
    action: Literal["submit", "approve", "request_changes", "cancel"]
    expected_version: int = Field(ge=1)

    model_config = ConfigDict(extra="forbid")


def _validate_target_unit_keys(value: list[str]) -> list[str]:
    cleaned = [item.strip() for item in value]
    if any(
        not item
        or len(item) > 120
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in item
        )
        for item in cleaned
    ):
        raise ValueError("target_units must contain safe unit keys")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("target_units must be distinct")
    return cleaned


class DeliverableRevisionIn(BaseModel):
    expected_version: int = Field(ge=1)
    client_revision_id: uuid.UUID
    instruction: str = Field(min_length=3, max_length=4000)
    target_units: list[str] = Field(default_factory=list, max_length=50)

    model_config = ConfigDict(extra="forbid")

    @field_validator("target_units")
    @classmethod
    def validate_target_units(cls, value: list[str]) -> list[str]:
        return _validate_target_unit_keys(value)


class DeliverableApprovalIn(BaseModel):
    expected_version: int = Field(ge=1)
    client_action_id: uuid.UUID
    stage: Literal["brief", "outline", "composition", "storyboard", "final"]
    action: Literal["approve", "request_changes", "cancel"]
    instruction: str | None = Field(default=None, min_length=3, max_length=4000)
    target_units: list[str] = Field(default_factory=list, max_length=50)

    model_config = ConfigDict(extra="forbid")

    @field_validator("target_units")
    @classmethod
    def validate_target_units(cls, value: list[str]) -> list[str]:
        return _validate_target_unit_keys(value)


class DeliverableQualityReviewCreate(BaseModel):
    client_review_id: uuid.UUID
    expected_request_version: int = Field(ge=1)
    reviewer_user_ids: list[uuid.UUID] = Field(min_length=3, max_length=7)

    model_config = ConfigDict(extra="forbid")

    @field_validator("reviewer_user_ids")
    @classmethod
    def validate_distinct_reviewers(
        cls,
        value: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("reviewer_user_ids must contain distinct users")
        return value


class DeliverableReviewerHardGateIn(BaseModel):
    passed: bool
    evidence: list[str] = Field(min_length=1, max_length=12)

    model_config = ConfigDict(extra="forbid")

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(len(item) < 3 or len(item) > 500 for item in cleaned):
            raise ValueError("each evidence item must contain 3 to 500 characters")
        return cleaned


class DeliverableReviewerDimensionIn(BaseModel):
    score: float = Field(ge=1, le=5)
    evidence: list[str] = Field(min_length=1, max_length=12)

    model_config = ConfigDict(extra="forbid")

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(len(item) < 3 or len(item) > 500 for item in cleaned):
            raise ValueError("each evidence item must contain 3 to 500 characters")
        return cleaned


class DeliverableReviewerEvidenceIn(BaseModel):
    status: Literal["complete", "partial", "unavailable"]
    findings: list[str] = Field(min_length=1, max_length=30)

    model_config = ConfigDict(extra="forbid")

    @field_validator("findings")
    @classmethod
    def validate_findings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(len(item) < 3 or len(item) > 500 for item in cleaned):
            raise ValueError("each finding must contain 3 to 500 characters")
        return cleaned


class DeliverableQualityReviewSubmissionIn(BaseModel):
    client_submission_id: uuid.UUID
    expected_version: int = Field(ge=1)
    hard_gates: dict[str, DeliverableReviewerHardGateIn] = Field(min_length=1)
    dimensions: dict[str, DeliverableReviewerDimensionIn] = Field(min_length=1)
    human_evidence: dict[str, DeliverableReviewerEvidenceIn] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list, max_length=20)

    model_config = ConfigDict(extra="forbid")

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(len(item) > 1000 for item in cleaned):
            raise ValueError("each note must contain at most 1000 characters")
        return cleaned


class DeliverableQualityReviewEvidenceIn(BaseModel):
    client_evidence_id: uuid.UUID
    expected_version: int = Field(ge=1)
    kind: Literal["ocr", "frame_ocr"]
    status: Literal["complete", "partial", "unavailable"]
    source_ref: str = Field(min_length=3, max_length=500)
    findings: list[str] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        cleaned = value.strip()
        if "://" in cleaned or cleaned.startswith(("/", "~")) or ".." in cleaned.split("/"):
            raise ValueError("source_ref must be a private, relative evidence reference")
        return cleaned

    @field_validator("findings")
    @classmethod
    def validate_findings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(len(item) > 500 for item in cleaned):
            raise ValueError("each finding must contain at most 500 characters")
        return cleaned


class DeliverableQualityReviewerOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    role: str
    eligible: bool
    ineligible_reason: str | None = None


class DeliverableQualityReviewAssignmentOut(BaseModel):
    reviewer_user_id: uuid.UUID
    reviewer_display_name: str | None = None
    reviewer_role: str | None = None
    status: Literal["assigned", "submitted"]
    is_current_user: bool
    submitted_at: datetime | None = None


class DeliverableQualityReviewArtifactOut(BaseModel):
    id: uuid.UUID
    artifact_key: str
    artifact_type: str
    content_hash: str
    revision_number: int
    download_url: str


class DeliverableQualityReviewOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    modality: Literal["image", "video", "presentation"]
    status: Literal["open", "passed", "blocked", "incomplete", "superseded"]
    version: int
    minimum_reviewers: int
    assigned_reviewer_count: int
    submitted_reviewer_count: int
    artifact_hashes: dict[str, str]
    brief: str
    requirements: list[str]
    hard_gates: list[str]
    quality_dimensions: list[str]
    required_evidence_kinds: list[str]
    automated_evidence: list[dict[str, Any]]
    assignments: list[DeliverableQualityReviewAssignmentOut]
    artifacts: list[DeliverableQualityReviewArtifactOut]
    current_user_can_manage: bool
    current_user_can_submit: bool
    current_user_can_add_evidence: bool
    receipt_ref: str | None = None
    created_at: datetime
    sealed_at: datetime | None = None


class DeliverableArtifactOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    parent_revision_id: uuid.UUID | None = None
    execution_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None
    artifact_key: str
    artifact_type: str
    stage_key: str | None = None
    unit_key: str | None = None
    workspace_path: str
    mime_type: str | None = None
    content_hash: str
    size_bytes: int | None = None
    revision_number: int
    status: str
    evaluation: dict
    approved_by_user_id: uuid.UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeliverableApprovalReadinessOut(BaseModel):
    approvable: bool
    quality_gate_required: bool
    quality_status: Literal[
        "not_required",
        "pending",
        "passed",
        "blocked",
        "incomplete",
        "invalid",
    ]
    blockers: list[str] = Field(default_factory=list)
    receipt_ref: str | None = None


class CandidateQaSummaryOut(BaseModel):
    schema_version: str | None = None
    status: str | None = None
    score: int | None = None
    artifact_sha256: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    subject_similarity: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class DeliverableBriefOut(BaseModel):
    schema_version: str
    status: Literal["draft", "clarifying", "confirmed"]
    missing_fields: list[str] = Field(default_factory=list)
    brief_sha256: str | None = None
    candidate_count: int | None = None
    brief: dict[str, Any] | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class DeliverableClarificationIn(BaseModel):
    expected_version: int = Field(ge=1)
    answers: dict[str, Any] = Field(default_factory=dict, max_length=20)

    model_config = ConfigDict(extra="forbid")


class DeliverableExecutionUnitOut(BaseModel):
    id: uuid.UUID
    execution_id: uuid.UUID
    stage_key: str
    unit_key: str
    status: Literal[
        "pending",
        "running",
        "blocked",
        "reconciling",
        "succeeded",
        "failed",
        "cancelled",
        "superseded",
    ]
    dependency_hash: str
    attempt_count: int
    input_snapshot: dict[str, Any]
    result_snapshot: dict[str, Any]
    quality_evaluation: dict[str, Any]
    qa_summary: CandidateQaSummaryOut | None = None
    last_error_code: str | None = None
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeliverableApprovalReceiptOut(BaseModel):
    id: uuid.UUID
    execution_id: uuid.UUID
    actor_user_id: uuid.UUID
    client_action_id: uuid.UUID
    request_version: int
    stage: Literal["brief", "outline", "composition", "storyboard", "final"]
    action: Literal["approve", "request_changes", "cancel"]
    instruction: str | None = None
    target_units: list[str]
    receipt: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeliverableExecutionOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    execution_number: int
    kind: Literal["initial", "revision", "recovery"]
    status: Literal[
        "ready",
        "running",
        "blocked",
        "reconciling",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
    ]
    current_stage: str
    workflow_id: str
    workflow_version: str
    contract_snapshot: dict[str, Any]
    preflight_snapshot: dict[str, Any]
    revision_instruction: str | None = None
    blocked_reason: str | None = None
    last_error_code: str | None = None
    launched_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    units: list[DeliverableExecutionUnitOut] = Field(default_factory=list)
    approvals: list[DeliverableApprovalReceiptOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class DeliverableRequestOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by_user_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    agent_run_id: uuid.UUID | None = None
    current_execution_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    client_request_id: uuid.UUID
    work_type: str
    workflow_id: str
    workflow_version: str
    goal: str
    inputs: list
    spec: dict
    tier: str
    approval_policy: list
    output_contract: list
    status: str
    current_stage: str
    version: int
    contract_revision: int = 1
    latest_preflight: dict[str, Any] | None = None
    last_error_code: str | None = None
    launched_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    artifacts: list[DeliverableArtifactOut] = Field(default_factory=list)
    approval_readiness: DeliverableApprovalReadinessOut

    model_config = ConfigDict(from_attributes=True)
