"""Viewer-scoped contracts for the Company Overview workforce topology."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, Field


class WorkforceTopologyNodeOut(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: str | None = None
    role_description: str = ""
    status: str
    last_active_at: datetime | None = None
    tokens_used_today: int = 0
    cache_read_tokens_today: int = 0
    max_tokens_per_day: int | None = None
    is_expired: bool = False
    is_system: bool = False


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
    action_type: str
    summary: str
    created_at: datetime


class WorkforceTopologyOut(BaseModel):
    company_id: uuid.UUID
    company_name: str
    window_hours: int = Field(ge=1, le=168)
    generated_at: datetime
    nodes: list[WorkforceTopologyNodeOut] = Field(default_factory=list)
    relationship_edges: list[WorkforceTopologyRelationshipEdgeOut] = Field(default_factory=list)
    activity_edges: list[WorkforceTopologyActivityEdgeOut] = Field(default_factory=list)
    recent_activities: list[WorkforceTopologyActivityOut] = Field(default_factory=list)


__all__ = [
    "WorkforceTopologyActivityEdgeOut",
    "WorkforceTopologyActivityOut",
    "WorkforceTopologyNodeOut",
    "WorkforceTopologyOut",
    "WorkforceTopologyRelationshipEdgeOut",
]
