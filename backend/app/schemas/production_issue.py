"""Schemas for privacy-safe production issue reporting and operations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SAFE_DIAGNOSTIC_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
SAFE_DIAGNOSTIC_ROUTE = r"^/[A-Za-z0-9_./:{}@%+-]*$"
SAFE_RELEASE_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$"


class ClientIssueMetadata(BaseModel):
    """Strict browser-generated diagnostic shape with no free-form text."""

    component: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=SAFE_DIAGNOSTIC_IDENTIFIER,
    )
    file: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=SAFE_DIAGNOSTIC_IDENTIFIER,
    )
    status_code: int | None = Field(default=None, ge=100, le=599)
    close_code: int | None = Field(default=None, ge=0, le=4999)
    line: int | None = Field(default=None, ge=0, le=10_000_000)
    column: int | None = Field(default=None, ge=0, le=10_000_000)
    release_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        pattern=SAFE_RELEASE_VERSION,
    )
    origin_host: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=SAFE_DIAGNOSTIC_IDENTIFIER,
    )
    visibility_state: Literal["visible", "hidden", "prerender", "unknown"] | None = None
    lifecycle_state: Literal["active", "ending"] | None = None
    online: bool | None = None
    signal_kind: Literal[
        "fetch_rejected",
        "http_response",
        "runtime_exception",
        "websocket_close",
    ] | None = None

    model_config = ConfigDict(extra="forbid")


class ClientIssueReportIn(BaseModel):
    category: Literal["api", "runtime", "websocket"]
    severity: Literal["warning", "error"] = "error"
    error_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=SAFE_DIAGNOSTIC_IDENTIFIER,
    )
    route: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        pattern=SAFE_DIAGNOSTIC_ROUTE,
    )
    operation: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=SAFE_DIAGNOSTIC_IDENTIFIER,
    )
    agent_id: uuid.UUID | None = None
    metadata: ClientIssueMetadata = Field(default_factory=ClientIssueMetadata)

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
    alert_epoch: int = 1
    alert_attempts: int = 0
    alert_next_attempt_at: datetime | None = None
    alert_last_error_code: str | None = None
    alert_notification_sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution_reason: str | None = None
    auto_resolved: bool = False

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
