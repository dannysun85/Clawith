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


class DeliverableArtifactOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    parent_revision_id: uuid.UUID | None = None
    artifact_key: str
    artifact_type: str
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

class DeliverableRequestOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by_user_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    agent_run_id: uuid.UUID | None = None
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
    last_error_code: str | None = None
    launched_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    artifacts: list[DeliverableArtifactOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
