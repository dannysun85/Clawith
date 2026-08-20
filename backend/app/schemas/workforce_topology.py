"""Viewer-scoped contracts for the digital employee workforce projection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, Field


class WorkforceTopologyWorkOut(BaseModel):
    id: uuid.UUID
    title: str
    summary: str
    stage: Literal[
        "executing",
        "review",
        "approval",
        "blocked",
        "completed",
    ]
    active_count: int = Field(ge=0)
    recently_completed_count: int = Field(ge=0)
    deep_link: str
    updated_at: datetime


class WorkforceTopologyExecutionOut(BaseModel):
    """Latest company-visible execution fact, separate from Agent health."""

    id: uuid.UUID
    run_id: uuid.UUID | None = None
    source_type: Literal[
        "direct_chat",
        "group",
        "a2a",
        "task",
        "trigger",
        "heartbeat",
        "deliverable",
        "media",
    ]
    status: Literal[
        "queued",
        "running",
        "waiting_user",
        "waiting_agent",
        "waiting_external",
        "completed",
        "failed",
        "cancelled",
    ]
    phase: str | None = None
    title: str
    summary: str
    details_visible: bool = False
    active_count: int = Field(ge=0)
    recently_finished_count: int = Field(ge=0)
    deep_link: str
    updated_at: datetime


class WorkforceTopologyNodeOut(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: str | None = None
    role_description: str = ""
    status: str
    last_active_at: datetime | None = None
    tokens_used_today: int | None = None
    cache_read_tokens_today: int | None = None
    max_tokens_per_day: int | None = None
    is_expired: bool = False
    is_system: bool = False
    visibility: Literal["company", "private", "custom"] = "company"
    can_manage: bool = False
    execution: WorkforceTopologyExecutionOut | None = None
    work: WorkforceTopologyWorkOut | None = None


class WorkforceTopologyRelationshipEdgeOut(BaseModel):
    id: uuid.UUID
    source_agent_id: uuid.UUID
    target_agent_id: uuid.UUID
    relation: str
    updated_at: datetime | None = None


class WorkforceTopologyActivityEdgeOut(BaseModel):
    agent_a_id: uuid.UUID
    agent_b_id: uuid.UUID
    interaction_count: int = Field(ge=1)
    last_activity_at: datetime


class WorkforceTopologyActivityOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    summary: str
    created_at: datetime


class WorkforceTopologyScopeOut(BaseModel):
    """Stable visibility semantics for the three topology data layers."""

    execution: Literal["company_visible_redacted"] = "company_visible_redacted"
    work: Literal["viewer_owned"] = "viewer_owned"
    analytics: Literal["governor_or_managed"] = "governor_or_managed"


class WorkforceTopologyOut(BaseModel):
    company_id: uuid.UUID
    company_name: str
    window_hours: int = Field(ge=1, le=168)
    generated_at: datetime
    scope_contract: WorkforceTopologyScopeOut = Field(
        default_factory=WorkforceTopologyScopeOut
    )
    nodes: list[WorkforceTopologyNodeOut] = Field(default_factory=list)
    relationship_edges: list[WorkforceTopologyRelationshipEdgeOut] = Field(default_factory=list)
    activity_edges: list[WorkforceTopologyActivityEdgeOut] = Field(default_factory=list)
    recent_activities: list[WorkforceTopologyActivityOut] = Field(default_factory=list)


__all__ = [
    "WorkforceTopologyActivityEdgeOut",
    "WorkforceTopologyActivityOut",
    "WorkforceTopologyExecutionOut",
    "WorkforceTopologyNodeOut",
    "WorkforceTopologyOut",
    "WorkforceTopologyRelationshipEdgeOut",
    "WorkforceTopologyScopeOut",
    "WorkforceTopologyWorkOut",
]
