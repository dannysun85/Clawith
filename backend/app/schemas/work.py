"""Contracts for the tenant-scoped task workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkTaskCreate(BaseModel):
    client_request_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    intent: str = Field(min_length=3, max_length=4000)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    executor_kind: Literal[
        "personal_assistant", "agent_employee", "temporary_expert"
    ] = "personal_assistant"
    agent_id: uuid.UUID | None = None
    expert_role: str | None = Field(default=None, min_length=3, max_length=200)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_executor(self) -> "WorkTaskCreate":
        if self.executor_kind == "agent_employee" and self.agent_id is None:
            raise ValueError("agent_id is required for an Agent employee")
        if self.executor_kind == "temporary_expert" and not self.expert_role:
            raise ValueError("expert_role is required for a temporary expert")
        return self


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
    agent_id: uuid.UUID
    agent_name: str
    task_id: uuid.UUID | None = None
    task_status: str | None = None
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
    deep_link: str
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
]
