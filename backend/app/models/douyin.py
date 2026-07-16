"""Douyin official OpenAPI account, token, task, and metric models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DouyinOAuthState(Base):
    """Short-lived OAuth state used to bind callbacks to a tenant/user."""

    __tablename__ = "douyin_oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    redirect_after: Mapped[str | None] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DouyinAccount(Base):
    """A tenant-owned Douyin account authorized via official OAuth."""

    __tablename__ = "douyin_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "open_id", name="uq_douyin_account_tenant_open_id"),
        Index("ix_douyin_accounts_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    primary_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    open_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    union_id: Mapped[str | None] = mapped_column(String(128), index=True)
    nickname: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    account_type: Mapped[str | None] = mapped_column(String(50))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    permission_status: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False, index=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DouyinToken(Base):
    """Encrypted user access token pair for one Douyin account."""

    __tablename__ = "douyin_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("douyin_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    refresh_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DouyinPublishJob(Base):
    """Approval-first publish task created by a Douyin Agent."""

    __tablename__ = "douyin_publish_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_douyin_publish_tenant_idempotency"),
        Index("ix_douyin_publish_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_douyin_publish_jobs_agent_status", "agent_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("douyin_accounts.id"), index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("approval_requests.id"), index=True)
    content_type: Mapped[str] = mapped_column(String(40), default="video", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    visibility: Mapped[str] = mapped_column(String(40), default="public_after_review", nullable=False)
    asset_refs: Mapped[list] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    publish_mode: Mapped[str] = mapped_column(String(40), default="collaborative_h5", nullable=False, index=True)
    approval_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    status: Mapped[str] = mapped_column(String(60), default="approval_required", nullable=False)
    external_item_id: Mapped[str | None] = mapped_column(String(200), index=True)
    external_video_id: Mapped[str | None] = mapped_column(String(200), index=True)
    share_id: Mapped[str | None] = mapped_column(String(200), index=True)
    share_state: Mapped[str | None] = mapped_column(String(200), index=True)
    share_schema_url: Mapped[str | None] = mapped_column(Text)
    share_nonce: Mapped[str | None] = mapped_column(String(80))
    share_signature: Mapped[str | None] = mapped_column(String(160))
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    official_error_code: Mapped[str | None] = mapped_column(String(80))
    official_log_id: Mapped[str | None] = mapped_column(String(160))
    redacted_request_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    response_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DouyinMetricSnapshot(Base):
    """Snapshot of account/video metrics with explicit freshness metadata."""

    __tablename__ = "douyin_metric_snapshots"
    __table_args__ = (
        Index("ix_douyin_metric_account_captured", "account_id", "captured_at"),
        Index("ix_douyin_metric_tenant_type", "tenant_id", "metric_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("douyin_accounts.id"), nullable=False, index=True)
    external_item_id: Mapped[str | None] = mapped_column(String(200), index=True)
    metric_type: Mapped[str] = mapped_column(String(40), default="account", nullable=False)
    source_api: Mapped[str] = mapped_column(String(160), default="official_openapi", nullable=False)
    data_freshness: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DouyinComment(Base):
    """Stored comment summary for moderation/reply planning."""

    __tablename__ = "douyin_comments"
    __table_args__ = (
        UniqueConstraint("account_id", "comment_id", name="uq_douyin_comment_account_comment"),
        Index("ix_douyin_comments_account_risk", "account_id", "risk_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("douyin_accounts.id"), nullable=False, index=True)
    external_item_id: Mapped[str | None] = mapped_column(String(200), index=True)
    comment_id: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text, default="")
    author_display: Mapped[str | None] = mapped_column(String(200))
    sentiment: Mapped[str | None] = mapped_column(String(40))
    intent: Mapped[str | None] = mapped_column(String(80))
    risk_level: Mapped[str] = mapped_column(String(40), default="normal", nullable=False)
    needs_reply: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DouyinOperation(Base):
    """Auditable external or pending Douyin operation."""

    __tablename__ = "douyin_operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_douyin_operation_tenant_idempotency"),
        Index("ix_douyin_operations_agent_status", "agent_id", "status"),
        Index("ix_douyin_operations_tenant_type", "tenant_id", "operation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("douyin_accounts.id"), index=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("approval_requests.id"), index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    operation_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_id: Mapped[str | None] = mapped_column(String(200), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    status: Mapped[str] = mapped_column(String(60), default="pending_approval", nullable=False)
    request_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    response_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    official_error_code: Mapped[str | None] = mapped_column(String(80))
    official_log_id: Mapped[str | None] = mapped_column(String(160))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
