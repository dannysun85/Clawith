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


class DeliverableQualityReview(Base):
    """One immutable-artifact review batch managed by the Astra control plane."""

    __tablename__ = "deliverable_quality_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'passed', 'blocked', 'incomplete', 'superseded')",
            name="ck_deliverable_quality_reviews_status",
        ),
        CheckConstraint(
            "modality IN ('image', 'video', 'presentation')",
            name="ck_deliverable_quality_reviews_modality",
        ),
        CheckConstraint(
            "minimum_reviewers >= 3",
            name="ck_deliverable_quality_reviews_minimum_reviewers",
        ),
        CheckConstraint(
            "assigned_reviewer_count >= minimum_reviewers",
            name="ck_deliverable_quality_reviews_assigned_reviewers",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_deliverable_quality_reviews_version_positive",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            "client_review_id",
            name="uq_deliverable_quality_reviews_client_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_deliverable_quality_reviews_tenant_id_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_quality_reviews_tenant_request",
            ondelete="CASCADE",
        ),
        Index(
            "uq_deliverable_quality_reviews_open_request",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index(
            "ix_deliverable_quality_reviews_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenants.id",
            name="fk_deliverable_quality_reviews_tenant",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="fk_deliverable_quality_reviews_creator",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    client_review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    modality: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="open", server_default=text("'open'")
    )
    minimum_reviewers: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    assigned_reviewer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_hashes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    scenario: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    review_package: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    receipt: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DeliverableQualityReviewAssignment(Base):
    """A reviewer identity bound to exactly one sealed panel submission."""

    __tablename__ = "deliverable_quality_review_assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('assigned', 'submitted')",
            name="ck_deliverable_quality_review_assignments_status",
        ),
        UniqueConstraint(
            "review_id",
            "reviewer_user_id",
            name="uq_deliverable_quality_review_assignment_user",
        ),
        UniqueConstraint(
            "review_id",
            "reviewer_identity_id",
            name="uq_deliverable_quality_review_assignment_identity",
        ),
        UniqueConstraint(
            "reviewer_receipt_ref",
            name="uq_deliverable_quality_review_assignment_receipt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "review_id"],
            ["deliverable_quality_reviews.tenant_id", "deliverable_quality_reviews.id"],
            name="fk_deliverable_quality_review_assignments_tenant_review",
            ondelete="CASCADE",
        ),
        Index(
            "uq_deliverable_quality_review_submission_client_identity",
            "tenant_id",
            "reviewer_user_id",
            "client_submission_id",
            unique=True,
            postgresql_where=text("client_submission_id IS NOT NULL"),
            sqlite_where=text("client_submission_id IS NOT NULL"),
        ),
        Index(
            "ix_deliverable_quality_review_assignments_reviewer",
            "tenant_id",
            "reviewer_user_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenants.id",
            name="fk_deliverable_quality_review_assignments_tenant",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="fk_deliverable_quality_review_assignments_user",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reviewer_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "identities.id",
            name="fk_deliverable_quality_review_assignments_identity",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reviewer_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reviewer_role: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_receipt_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="assigned", server_default=text("'assigned'")
    )
    client_submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    submission_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeliverableQualityReviewEvidence(Base):
    """Trusted operator evidence bound by the server to one review snapshot."""

    __tablename__ = "deliverable_quality_review_evidence"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ocr', 'frame_ocr')",
            name="ck_deliverable_quality_review_evidence_kind",
        ),
        CheckConstraint(
            "status IN ('complete', 'partial', 'unavailable')",
            name="ck_deliverable_quality_review_evidence_status",
        ),
        UniqueConstraint(
            "review_id",
            "kind",
            name="uq_deliverable_quality_review_evidence_kind",
        ),
        UniqueConstraint(
            "tenant_id",
            "submitted_by_user_id",
            "client_evidence_id",
            name="uq_deliverable_quality_review_evidence_client_identity",
        ),
        UniqueConstraint(
            "receipt_ref",
            name="uq_deliverable_quality_review_evidence_receipt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "review_id"],
            ["deliverable_quality_reviews.tenant_id", "deliverable_quality_reviews.id"],
            name="fk_deliverable_quality_review_evidence_tenant_review",
            ondelete="CASCADE",
        ),
        Index(
            "ix_deliverable_quality_review_evidence_review",
            "review_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "tenants.id",
            name="fk_deliverable_quality_review_evidence_tenant",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="fk_deliverable_quality_review_evidence_user",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    client_evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    receipt: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
