"""Schemas for privacy-safe production issue reporting and operations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClientIssueReportIn(BaseModel):
    category: Literal["api", "runtime", "websocket"]
    severity: Literal["warning", "error"] = "error"
    error_code: str = Field(min_length=1, max_length=100)
    route: str | None = Field(default=None, max_length=500)
    operation: str | None = Field(default=None, max_length=100)
    agent_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ProductionIssueOut(BaseModel):
    id: uuid.UUID
    fingerprint: str
    category: str
    severity: str
    status: str
    source: str
    error_code: str | None = None
    summary: str
    route: str | None = None
    operation: str | None = None
    event_count: int
    affected_tenant_count: int = 0
    first_seen_at: datetime
    last_seen_at: datetime
    last_trace_id: str | None = None
    release_version: str | None = None
    last_metadata: dict | None = None
    alerted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ProductionIssueEventOut(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    trace_id: str | None = None
    severity: str
    route: str | None = None
    operation: str | None = None
    metadata_json: dict | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProductionIssueStatusIn(BaseModel):
    status: Literal["open", "acknowledged", "resolved", "ignored"]

    model_config = ConfigDict(extra="forbid")


class ProductionIssueSummaryOut(BaseModel):
    open_total: int
    open_warning: int
    open_error: int
    open_critical: int
    events_last_24h: int
    affected_tenants_last_24h: int
