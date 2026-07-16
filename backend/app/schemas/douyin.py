"""Pydantic schemas for Douyin official OpenAPI integration."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DouyinCapabilityOut(BaseModel):
    key: str
    label: str
    required_scopes: list[str]
    status: str


class DouyinAccountOut(BaseModel):
    id: uuid.UUID
    open_id: str
    nickname: str | None = None
    avatar_url: str | None = None
    status: str
    scopes: list[str] = Field(default_factory=list)
    permission_status: dict = Field(default_factory=dict)
    capabilities: list[DouyinCapabilityOut] = Field(default_factory=list)
    authorized_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None

    model_config = {"from_attributes": True}


class DouyinStatusOut(BaseModel):
    configured: bool
    status: str
    message: str
    required_scopes: list[str]
    accounts: list[DouyinAccountOut]
    primary_account_id: uuid.UUID | None = None
    callback_url: str | None = None


class DouyinOAuthStartRequest(BaseModel):
    redirect_after: str | None = "/enterprise#douyin"


class DouyinOAuthStartOut(BaseModel):
    status: str
    authorization_url: str | None = None
    message: str
    state_expires_at: datetime | None = None


class DouyinPublishJobCreate(BaseModel):
    agent_id: uuid.UUID
    account_id: uuid.UUID | None = None
    content_type: str = "video"
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    hashtags: list[str] = Field(default_factory=list)
    visibility: str = "public_after_review"
    asset_refs: list[dict] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    idempotency_key: str | None = None


class DouyinPublishJobOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None
    content_type: str
    title: str
    body: str
    hashtags: list = Field(default_factory=list)
    visibility: str
    asset_refs: list = Field(default_factory=list)
    idempotency_key: str
    publish_mode: str
    approval_status: str
    status: str
    external_item_id: str | None = None
    external_video_id: str | None = None
    share_id: str | None = None
    share_state: str | None = None
    share_schema_url: str | None = None
    share_expires_at: datetime | None = None
    official_error_code: str | None = None
    official_log_id: str | None = None
    redacted_request_summary: dict = Field(default_factory=dict)
    response_summary: dict = Field(default_factory=dict)
    scheduled_at: datetime | None = None
    confirmed_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DouyinOperationCreate(BaseModel):
    agent_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    operation_type: str = Field(min_length=1, max_length=80)
    target_id: str | None = None
    request_summary: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class DouyinCommentReplyRequest(BaseModel):
    agent_id: uuid.UUID
    account_id: uuid.UUID | None = None
    comment_id: str
    reply_text: str = Field(min_length=1, max_length=600)
    item_id: str | None = None
    idempotency_key: str | None = None


class DouyinOperationOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None
    operation_type: str
    target_id: str | None = None
    idempotency_key: str
    approval_required: bool
    approval_status: str
    status: str
    request_summary: dict = Field(default_factory=dict)
    response_summary: dict = Field(default_factory=dict)
    official_error_code: str | None = None
    official_log_id: str | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DouyinMetricSnapshotOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    external_item_id: str | None = None
    metric_type: str
    source_api: str
    data_freshness: str
    metrics_json: dict
    captured_at: datetime

    model_config = {"from_attributes": True}


class DouyinCommentOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    external_item_id: str | None = None
    comment_id: str
    parent_comment_id: str | None = None
    content: str
    author_display: str | None = None
    sentiment: str | None = None
    intent: str | None = None
    risk_level: str
    needs_reply: bool
    last_seen_at: datetime | None = None

    model_config = {"from_attributes": True}


class DouyinAgentDashboardOut(BaseModel):
    configured: bool
    account: DouyinAccountOut | None = None
    publish_jobs: list[DouyinPublishJobOut] = Field(default_factory=list)
    operations: list[DouyinOperationOut] = Field(default_factory=list)
    metric_snapshots: list[DouyinMetricSnapshotOut] = Field(default_factory=list)
    comments: list[DouyinCommentOut] = Field(default_factory=list)
    message: str
