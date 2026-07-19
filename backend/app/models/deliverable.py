"""Durable product contracts for user-requested deliverables and artifacts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeliverableRequest(Base):
    """A tenant-scoped, versioned work brief linked to at most one Agent run."""

    __tablename__ = "deliverable_requests"
    __table_args__ = (
        CheckConstraint(
            "work_type IN ('presentation', 'poster', 'video', 'report', 'spreadsheet')",
            name="ck_deliverable_requests_work_type",
        ),
        CheckConstraint(
            "tier IN ('lite', 'pro', 'ultra')",
            name="ck_deliverable_requests_tier",
        ),
        CheckConstraint(
            "status IN ('draft', 'ready', 'running', 'waiting_approval', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_deliverable_requests_status",
        ),
        CheckConstraint("version > 0", name="ck_deliverable_requests_version_positive"),
        UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "client_request_id",
            name="uq_deliverable_requests_client_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_deliverable_requests_tenant_id_id",
        ),
        UniqueConstraint("agent_run_id", name="uq_deliverable_requests_agent_run"),
        UniqueConstraint("launch_message_id", name="uq_deliverable_requests_launch_message"),
        Index(
            "ix_deliverable_requests_tenant_agent_created",
            "tenant_id",
            "agent_id",
            "created_at",
        ),
        Index(
            "ix_deliverable_requests_session_created",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_deliverable_requests_tenant", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_deliverable_requests_creator", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", name="fk_deliverable_requests_agent", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", name="fk_deliverable_requests_session", ondelete="CASCADE"),
        nullable=False,
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", name="fk_deliverable_requests_agent_run", ondelete="SET NULL"),
        nullable=True,
    )
    launch_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_messages.id", name="fk_deliverable_requests_launch_message", ondelete="SET NULL"),
        nullable=True,
    )
    client_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    work_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(120), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    spec: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_policy: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    output_contract: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ready", server_default=text("'ready'")
    )
    current_stage: Mapped[str] = mapped_column(
        String(64), nullable=False, default="brief_confirmed", server_default=text("'brief_confirmed'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DeliverableArtifactRevision(Base):
    """Metadata for one immutable binary/file revision stored outside the database."""

    __tablename__ = "deliverable_artifact_revisions"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="ck_deliverable_artifacts_revision_positive"),
        CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected', 'superseded')",
            name="ck_deliverable_artifacts_status",
        ),
        UniqueConstraint(
            "request_id",
            "artifact_key",
            "revision_number",
            name="uq_deliverable_artifacts_request_key_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_artifacts_tenant_request",
            ondelete="CASCADE",
        ),
        Index(
            "ix_deliverable_artifacts_request_created",
            "request_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_deliverable_artifacts_tenant", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "deliverable_artifact_revisions.id",
            name="fk_deliverable_artifacts_parent",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_deliverable_artifacts_approver", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_key: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="candidate", server_default=text("'candidate'")
    )
    evaluation: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
