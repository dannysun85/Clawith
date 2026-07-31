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
    executor_kind: Literal[
        "personal_assistant", "agent_employee", "temporary_expert", "group"
    ] = "personal_assistant"
    agent_id: uuid.UUID | None = None
    expert_role: str | None = Field(default=None, min_length=3, max_length=200)
    group_id: uuid.UUID | None = None
    group_session_id: uuid.UUID | None = None
    group_agent_participant_ids: list[uuid.UUID] = Field(
        default_factory=list,
        min_length=1,
        max_length=12,
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_executor(self) -> "WorkTaskDraft":
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
        return self


class WorkTaskPreflight(WorkTaskDraft):
    pass


class WorkTaskCreate(WorkTaskDraft):
    client_request_id: uuid.UUID
    confirmation_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class WorkTaskPreflightOut(BaseModel):
    confirmation_fingerprint: str
    capability_status: Literal["available", "degraded", "unavailable"]
    estimated_credits: int | None = None
    cost_note: str
    approval_required: bool
    reasons: list[str] = Field(default_factory=list)
    next_action: str | None = None
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


class WorkIndexOut(BaseModel):
    items: list[WorkItemOut]
    personal_assistant_agent_id: uuid.UUID | None = None
    next_cursor: str | None = None


class WorkTaskCreateOut(BaseModel):
    item: WorkItemOut
    created: bool


__all__ = [
    "WorkArtifactSummary",
    "WorkIndexOut",
    "WorkItemOut",
    "WorkTaskCreate",
    "WorkTaskCreateOut",
    "WorkTaskDraft",
    "WorkTaskPreflight",
    "WorkTaskPreflightOut",
]
