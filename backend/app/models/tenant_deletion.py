"""Durable orchestration records for expired tenant deletion."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantDeletionJob(Base):
    """Re-entrant purge state while the tenant row still exists."""

    __tablename__ = "tenant_deletion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'dry_run_passed', 'purging', 'held', 'failed')",
            name="ck_tenant_deletion_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_tenant_deletion_jobs_attempt_count"),
        Index("ix_tenant_deletion_jobs_status_eligible", "status", "eligible_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), default="scheduled", nullable=False, index=True)
    eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    plan_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    table_counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    storage_summary: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class TenantDeletionHold(Base):
    """A legal or operational hold that blocks an otherwise eligible purge."""

    __tablename__ = "tenant_deletion_holds"
    __table_args__ = (
        CheckConstraint(
            "hold_type IN ('legal', 'operations')",
            name="ck_tenant_deletion_holds_type",
        ),
        CheckConstraint("reason_code <> ''", name="ck_tenant_deletion_holds_reason"),
        Index(
            "uq_tenant_deletion_holds_active_type",
            "tenant_id",
            "hold_type",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hold_type: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    released_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    release_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class TenantDeletionTombstone(Base):
    """Minimal non-customer receipt retained after the tenant is gone."""

    __tablename__ = "tenant_deletion_tombstones"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_tenant_deletion_tombstones_schema_version"),
    )

    # Deliberately no FK: the tenant row has been physically deleted.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deletion_requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    table_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    storage_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
