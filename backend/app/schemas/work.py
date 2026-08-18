"""Contracts for the tenant-scoped task workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        "runtime_approval",
        "delivery_approval",
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
    "WorkTimelineEventOut",
]
