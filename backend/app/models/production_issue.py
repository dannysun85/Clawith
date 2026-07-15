"""Privacy-safe production issue rollups and individual occurrence events."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProductionIssue(Base):
    """One deduplicated, operator-managed production problem."""

    __tablename__ = "production_issues"
    __table_args__ = (
        Index("ix_production_issues_status_last_seen", "status", "last_seen_at"),
        Index("ix_production_issues_severity_last_seen", "severity", "last_seen_at"),
        CheckConstraint(
            "alert_epoch > 0",
            name="ck_production_issue_alert_epoch_positive",
        ),
        CheckConstraint(
            "alert_attempts >= 0",
            name="ck_production_issue_alert_attempts_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="error")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    route: Mapped[str | None] = mapped_column(String(500), nullable=True)
    operation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    alert_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    alert_last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    alert_notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProductionIssueEvent(Base):
    """One privacy-filtered occurrence linked to a rollup."""

    __tablename__ = "production_issue_events"
    __table_args__ = (
        Index("ix_production_issue_events_issue_created", "issue_id", "created_at"),
        Index("ix_production_issue_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_issues.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    route: Mapped[str | None] = mapped_column(String(500), nullable=True)
    operation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class ProductionIssueAlertDelivery(Base):
    """One durable, privacy-safe alert sink delivery for an issue epoch."""

    __tablename__ = "production_issue_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "issue_id",
            "alert_epoch",
            "sink",
            name="uq_production_issue_alert_delivery_epoch_sink",
        ),
        Index(
            "ix_production_issue_alert_delivery_due",
            "status",
            "next_attempt_at",
        ),
        CheckConstraint(
            "alert_epoch > 0",
            name="ck_production_issue_alert_delivery_epoch_positive",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_production_issue_alert_delivery_attempts_nonnegative",
        ),
        CheckConstraint(
            "(status = 'pending' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND delivered_at IS NULL) OR "
            "(status = 'delivering' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND delivered_at IS NOT NULL)",
            name="ck_production_issue_alert_delivery_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alert_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    sink: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
    )
    payload_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
