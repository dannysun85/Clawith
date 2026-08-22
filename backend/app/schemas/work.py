"""Contracts for the tenant-scoped task workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkResultLengthContract(BaseModel):
    """A deterministic result-size boundary confirmed before execution."""

    unit: Literal["characters", "cjk_characters", "words"] = "characters"
    minimum: int | None = Field(default=None, ge=1, le=6000)
    maximum: int | None = Field(default=None, ge=1, le=6000)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_bounds(self) -> "WorkResultLengthContract":
        if self.minimum is None and self.maximum is None:
            raise ValueError("at least one result length boundary is required")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum result length cannot exceed maximum")
        safe_inline_limits = {
            "characters": 6000,
            "cjk_characters": 1200,
            "words": 1200,
        }
        requested_limit = max(
            value for value in (self.minimum, self.maximum) if value is not None
        )
        if requested_limit > safe_inline_limits[self.unit]:
            raise ValueError(
                "inline result length exceeds the safe model boundary; use a formal Deliverable"
            )
        return self


class WorkAcceptanceContract(BaseModel):
    """Human-confirmed criteria plus deterministic Runtime checks."""

    version: Literal[1] = 1
    criteria: list[str] = Field(
        default_factory=lambda: [
            "The result directly addresses the confirmed objective and is usable for the next business action."
        ],
        min_length=1,
        max_length=12,
    )
    required_sections: list[str] = Field(default_factory=list, max_length=12)
    forbidden_terms: list[str] = Field(default_factory=list, max_length=20)
    result_language: Literal["auto", "zh-CN", "en"] = "auto"
    length: WorkResultLengthContract | None = None
    evidence_required: bool = False
    owner_review_required: bool = True

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def normalize_contract(self) -> "WorkAcceptanceContract":
        def normalized(values: list[str], *, field_name: str) -> list[str]:
            clean = list(dict.fromkeys(value.strip() for value in values if value.strip()))
            if field_name == "criteria" and not clean:
                raise ValueError("at least one acceptance criterion is required")
            if any(len(value) > 300 for value in clean):
                raise ValueError(f"{field_name} entries cannot exceed 300 characters")
            return clean

        self.criteria = normalized(self.criteria, field_name="criteria")
        self.required_sections = normalized(
            self.required_sections,
            field_name="required_sections",
        )
        self.forbidden_terms = normalized(
            self.forbidden_terms,
            field_name="forbidden_terms",
        )
        return self


class WorkTaskDraft(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    intent: str = Field(min_length=3, max_length=4000)
    work_type: Literal["general", "image", "video", "presentation", "document"] = "general"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    routing_mode: Literal["auto", "manual"] = "auto"
    executor_kind: Literal[
        "personal_assistant", "agent_employee", "temporary_expert", "group"
    ] | None = None
    agent_id: uuid.UUID | None = None
    expert_role: str | None = Field(default=None, min_length=3, max_length=200)
    group_id: uuid.UUID | None = None
    group_session_id: uuid.UUID | None = None
    group_agent_participant_ids: list[uuid.UUID] = Field(
        default_factory=list,
        min_length=1,
        max_length=12,
    )
    source_kind: Literal["workbench", "group_message"] = "workbench"
    source_group_id: uuid.UUID | None = None
    source_session_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None
    source_message_cursor: str | None = Field(default=None, max_length=500)
    acceptance_contract: WorkAcceptanceContract = Field(
        default_factory=WorkAcceptanceContract
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def preserve_manual_executor_compatibility(cls, value: Any) -> Any:
        """Treat legacy requests with an executor_kind as explicit manual routing."""

        if isinstance(value, dict) and "routing_mode" not in value:
            value = dict(value)
            value["routing_mode"] = "manual" if value.get("executor_kind") is not None else "auto"
        return value

    @model_validator(mode="after")
    def validate_executor(self) -> "WorkTaskDraft":
        if self.routing_mode == "manual" and self.executor_kind is None:
            raise ValueError("executor_kind is required for manual routing")
        if self.routing_mode == "auto" and self.executor_kind is not None:
            raise ValueError("executor_kind is only valid for manual routing")
        if self.routing_mode == "auto" and (
            self.agent_id is not None
            or self.expert_role is not None
            or self.group_id is not None
            or self.group_session_id is not None
            or self.group_agent_participant_ids
        ):
            raise ValueError("executor override fields are only valid for manual routing")
        if self.executor_kind == "agent_employee" and self.agent_id is None:
            raise ValueError("agent_id is required for an Agent employee")
        if self.executor_kind == "temporary_expert" and not self.expert_role:
            raise ValueError("expert_role is required for a temporary expert")
        if self.executor_kind == "group":
            if self.group_id is None or self.group_session_id is None:
                raise ValueError("group_id and group_session_id are required for a Group")
            if not self.group_agent_participant_ids:
                raise ValueError("at least one Agent participant is required for a Group")
            if len(set(self.group_agent_participant_ids)) != len(
                self.group_agent_participant_ids
            ):
                raise ValueError("Group Agent participants must be unique")
        elif (
            self.group_id is not None
            or self.group_session_id is not None
            or self.group_agent_participant_ids
        ):
            raise ValueError("Group fields are only valid for a Group executor")
        if self.source_kind == "group_message":
            if self.executor_kind != "group" or self.routing_mode != "manual":
                raise ValueError(
                    "A Group message source requires a manually confirmed Group executor"
                )
            if (
                self.source_group_id is None
                or self.source_session_id is None
                or self.source_message_id is None
            ):
                raise ValueError(
                    "source_group_id, source_session_id and source_message_id are required for a Group message"
                )
            if (
                self.source_group_id != self.group_id
                or self.source_session_id != self.group_session_id
            ):
                raise ValueError(
                    "Group message source must match the selected Group and session"
                )
        elif (
            self.source_group_id is not None
            or self.source_session_id is not None
            or self.source_message_id is not None
            or self.source_message_cursor is not None
        ):
            raise ValueError(
                "Source message fields are only valid for a Group message source"
            )
        return self


class WorkTaskPreflight(WorkTaskDraft):
    pass


class WorkTaskCreate(WorkTaskDraft):
    client_request_id: uuid.UUID
    confirmation_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class WorkExecutorProposalOut(BaseModel):
    policy_version: str
    chosen_executor_kind: Literal[
        "personal_assistant", "agent_employee", "temporary_expert", "group"
    ]
    agent_id: uuid.UUID
    agent_name: str
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    candidates_considered: list[dict[str, Any]] = Field(default_factory=list)
    capability_snapshot: dict[str, Any] = Field(default_factory=dict)
    fallback: dict[str, Any] | None = None


class WorkTaskPreflightOut(BaseModel):
    confirmation_fingerprint: str
    capability_status: Literal["available", "degraded", "unavailable"]
    estimated_credits: int | None = None
    cost_note: str
    approval_required: bool
    reasons: list[str] = Field(default_factory=list)
    next_action: str | None = None
    executor_proposal: WorkExecutorProposalOut
    work_statement: dict[str, Any]


class WorkArtifactSummary(BaseModel):
    id: uuid.UUID
    artifact_type: str
    status: str
    workspace_path: str
    revision_number: int

    model_config = ConfigDict(from_attributes=True)


class WorkItemOut(BaseModel):
    id: uuid.UUID
    kind: Literal["task", "deliverable"]
    title: str
    intent: str
    origin_type: str
    executor_kind: str
    executor_snapshot: dict = Field(default_factory=dict)
    work_statement: dict[str, Any] = Field(default_factory=dict)
    formal_delivery_spec: dict[str, Any] = Field(default_factory=dict)
    confirmed_at: datetime | None = None
    agent_id: uuid.UUID
    agent_name: str
    task_id: uuid.UUID | None = None
    task_status: str | None = None
    priority: str | None = None
    run_id: uuid.UUID | None = None
    execution_status: str
    deliverable_id: uuid.UUID | None = None
    work_type: str | None = None
    deliverable_status: str | None = None
    artifact_status: str | None = None
    review_status: str | None = None
    result_review_status: Literal[
        "not_required", "pending", "approved", "request_changes"
    ] = "not_required"
    approval_status: str | None = None
    delivery_status: str
    delivery_mode: Literal["task_only", "formal_deliverable"]
    user_stage: str
    artifacts: list[WorkArtifactSummary] = Field(default_factory=list)
    latest_update: str | None = None
    latest_update_at: datetime | None = None
    deep_link: str
    formal_delivery_link: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkStatusAxesOut(BaseModel):
    """Independent product states; no single enum may overwrite domain truth."""

    execution: Literal[
        "not_started", "queued", "running", "waiting", "completed", "failed", "cancelled"
    ]
    artifact: Literal["missing", "candidate", "approved", "rejected", "superseded"]
    quality: Literal["not_required", "open", "passed", "blocked", "incomplete", "superseded"]
    runtime_approval: Literal[
        "not_required",
        "pending",
        "approved",
        "rejected",
        "executing",
        "succeeded",
        "failed",
        "ambiguous",
    ]
    delivery_approval: Literal[
        "not_required", "pending", "approved", "request_changes", "cancelled"
    ]
    delivery: Literal[
        "not_requested", "pending", "reconciling", "delivered", "failed", "cancelled"
    ]


class WorkNextActionOut(BaseModel):
    id: str
    task_id: uuid.UUID | None = None
    kind: Literal[
        "quality_review",
        "task_result_review",
        "runtime_approval",
        "delivery_approval",
        "tool_reconciliation",
        "task_recovery",
        "delivery_recovery",
    ]
    status: Literal["open"] = "open"
    title: str
    reason_code: str
    source_type: str
    source_id: str
    action_url: str
    created_at: datetime
    due_at: datetime | None = None
    version: str | None = None


class WorkToolReconciliation(BaseModel):
    client_request_id: uuid.UUID
    outcome: Literal["applied", "not_applied"]
    note: str = Field(min_length=1, max_length=2_000)


class WorkToolReconciliationOut(BaseModel):
    task_id: uuid.UUID
    run_id: uuid.UUID
    execution_id: uuid.UUID
    execution_status: Literal["succeeded", "failed"]
    command_id: uuid.UUID
    created: bool
    result_summary: str


class GroupTaskParticipantOut(BaseModel):
    agent_id: uuid.UUID
    agent_name: str
    responsibility: Literal["primary_owner", "collaborator"]


class GroupTaskRunSummaryOut(BaseModel):
    """Collaboration-safe execution state plus independent notification delivery."""

    id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    parent_run_id: uuid.UUID | None = None
    root_run_id: uuid.UUID | None = None
    run_kind: str
    latest_event: str | None = Field(
        default=None,
        description="Latest Runtime lifecycle event; delivery receipts are excluded.",
    )
    delivery_status: str = Field(
        description="Delivery status of the Runtime outcome notification, not execution status.",
    )
    created_at: datetime
    updated_at: datetime


class GroupTaskSummaryOut(BaseModel):
    """Group-side read projection of one authoritative formal Task."""

    task_id: uuid.UUID
    title: str
    intent: str
    task_status: str
    user_stage: str
    status_axes: WorkStatusAxesOut
    primary_owner_agent_id: uuid.UUID
    primary_owner_agent_name: str
    participants: list[GroupTaskParticipantOut] = Field(default_factory=list)
    runs: list[GroupTaskRunSummaryOut] = Field(default_factory=list)
    group_id: uuid.UUID
    group_session_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None
    source_message_cursor: str | None = None
    latest_update: str | None = None
    latest_update_at: datetime | None = None
    next_actions: list[WorkNextActionOut] = Field(default_factory=list)
    work_link: str
    group_link: str | None = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class WorkTimelineEventOut(BaseModel):
    id: str
    type: str
    occurred_at: datetime
    source_type: str
    source_id: str
    status: str | None = None
    title: str
    summary: str | None = None
    actor_type: str | None = None
    actor_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkRunSummaryOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID | None = None
    parent_run_id: uuid.UUID | None = None
    root_run_id: uuid.UUID | None = None
    run_kind: str
    latest_event: str | None = Field(
        default=None,
        description="Latest Runtime lifecycle event; delivery receipts are excluded.",
    )
    delivery_status: str = Field(
        description="Delivery status of the Runtime outcome notification, not execution status.",
    )
    created_at: datetime
    updated_at: datetime


class WorkTaskResultReviewReceiptOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    actor_user_id: uuid.UUID
    action: Literal["approve", "request_changes"]
    comment: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkDeliverableSummaryOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    work_type: str
    status: str
    current_stage: str
    current_execution_id: uuid.UUID | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkArtifactDetailOut(WorkArtifactSummary):
    request_id: uuid.UUID
    execution_id: uuid.UUID | None = None
    artifact_key: str
    mime_type: str | None = None
    content_hash: str
    created_at: datetime


class WorkReviewSummaryOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    status: str
    modality: str
    minimum_reviewers: int
    assigned_reviewer_count: int
    current_user_assignment_status: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkApprovalSummaryOut(BaseModel):
    id: uuid.UUID
    kind: Literal["runtime", "delivery"]
    source_id: str
    status: str
    action_type: str
    execution_status: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class WorkTaskDetailOut(BaseModel):
    detail_scope: Literal["full", "collaboration"] = "full"
    summary: WorkItemOut
    status_axes: WorkStatusAxesOut
    timeline: list[WorkTimelineEventOut] = Field(default_factory=list)
    next_actions: list[WorkNextActionOut] = Field(default_factory=list)
    runs: list[WorkRunSummaryOut] = Field(default_factory=list)
    task_result_reviews: list[WorkTaskResultReviewReceiptOut] = Field(
        default_factory=list
    )
    deliverables: list[WorkDeliverableSummaryOut] = Field(default_factory=list)
    artifacts: list[WorkArtifactDetailOut] = Field(default_factory=list)
    reviews: list[WorkReviewSummaryOut] = Field(default_factory=list)
    approvals: list[WorkApprovalSummaryOut] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)


class WorkInboxOut(BaseModel):
    items: list[WorkNextActionOut]
    next_cursor: str | None = None


class WorkInboxCountOut(BaseModel):
    count: int


class WorkTaskRetry(BaseModel):
    client_request_id: uuid.UUID


class WorkTaskRetryOut(BaseModel):
    item: WorkItemOut
    run_id: uuid.UUID
    created: bool


class WorkTaskResultReview(BaseModel):
    run_id: uuid.UUID
    action: Literal["approve", "request_changes"]
    comment: str | None = Field(default=None, max_length=2000)
    client_request_id: uuid.UUID

    @model_validator(mode="after")
    def require_change_reason(self) -> "WorkTaskResultReview":
        self.comment = self.comment.strip() if self.comment else None
        if self.action == "request_changes" and not self.comment:
            raise ValueError("a change request must explain what needs to change")
        return self


class WorkTaskResultReviewOut(BaseModel):
    receipt: WorkTaskResultReviewReceiptOut
    created: bool


class WorkIndexOut(BaseModel):
    items: list[WorkItemOut]
    personal_assistant_agent_id: uuid.UUID | None = None
    next_cursor: str | None = None


class WorkTaskCreateOut(BaseModel):
    item: WorkItemOut
    created: bool


__all__ = [
    "GroupTaskParticipantOut",
    "GroupTaskRunSummaryOut",
    "GroupTaskSummaryOut",
    "WorkApprovalSummaryOut",
    "WorkArtifactDetailOut",
    "WorkArtifactSummary",
    "WorkDeliverableSummaryOut",
    "WorkExecutorProposalOut",
    "WorkInboxCountOut",
    "WorkInboxOut",
    "WorkIndexOut",
    "WorkItemOut",
    "WorkNextActionOut",
    "WorkReviewSummaryOut",
    "WorkRunSummaryOut",
    "WorkStatusAxesOut",
    "WorkTaskDetailOut",
    "WorkTaskCreate",
    "WorkTaskCreateOut",
    "WorkTaskDraft",
    "WorkTaskPreflight",
    "WorkTaskPreflightOut",
    "WorkTaskRetry",
    "WorkTaskRetryOut",
    "WorkToolReconciliation",
    "WorkToolReconciliationOut",
    "WorkTimelineEventOut",
]
