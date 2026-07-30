"""Add managed, identity-bound deliverable quality review batches.

Revision ID: add_deliverable_quality_reviews
Revises: seed_agent_plan_text_routes
Create Date: 2026-07-27 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_deliverable_quality_reviews"
down_revision: str | Sequence[str] | None = "seed_agent_plan_text_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliverable_quality_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("modality", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'open'"), nullable=False),
        sa.Column("minimum_reviewers", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("assigned_reviewer_count", sa.Integer(), nullable=False),
        sa.Column("artifact_hashes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("scenario", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("review_package", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'passed', 'blocked', 'incomplete', 'superseded')", name="ck_deliverable_quality_reviews_status"),
        sa.CheckConstraint("modality IN ('image', 'video', 'presentation')", name="ck_deliverable_quality_reviews_modality"),
        sa.CheckConstraint("minimum_reviewers >= 3", name="ck_deliverable_quality_reviews_minimum_reviewers"),
        sa.CheckConstraint("assigned_reviewer_count >= minimum_reviewers", name="ck_deliverable_quality_reviews_assigned_reviewers"),
        sa.CheckConstraint("version > 0", name="ck_deliverable_quality_reviews_version_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_deliverable_quality_reviews_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_deliverable_quality_reviews_creator", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "request_id"], ["deliverable_requests.tenant_id", "deliverable_requests.id"], name="fk_deliverable_quality_reviews_tenant_request", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "request_id", "client_review_id", name="uq_deliverable_quality_reviews_client_identity"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_deliverable_quality_reviews_tenant_id_id"),
    )
    op.create_index("uq_deliverable_quality_reviews_open_request", "deliverable_quality_reviews", ["request_id"], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.create_index("ix_deliverable_quality_reviews_tenant_created", "deliverable_quality_reviews", ["tenant_id", "created_at"], unique=False)

    op.create_table(
        "deliverable_quality_review_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_display_name", sa.String(length=100), nullable=False),
        sa.Column("reviewer_role", sa.String(length=32), nullable=False),
        sa.Column("reviewer_receipt_ref", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'assigned'"), nullable=False),
        sa.Column("client_submission_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submission_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("submission", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('assigned', 'submitted')", name="ck_deliverable_quality_review_assignments_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_deliverable_quality_review_assignments_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], name="fk_deliverable_quality_review_assignments_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewer_identity_id"], ["identities.id"], name="fk_deliverable_quality_review_assignments_identity", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "review_id"], ["deliverable_quality_reviews.tenant_id", "deliverable_quality_reviews.id"], name="fk_deliverable_quality_review_assignments_tenant_review", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "reviewer_user_id", name="uq_deliverable_quality_review_assignment_user"),
        sa.UniqueConstraint("review_id", "reviewer_identity_id", name="uq_deliverable_quality_review_assignment_identity"),
        sa.UniqueConstraint("reviewer_receipt_ref", name="uq_deliverable_quality_review_assignment_receipt"),
    )
    op.create_index("uq_deliverable_quality_review_submission_client_identity", "deliverable_quality_review_assignments", ["tenant_id", "reviewer_user_id", "client_submission_id"], unique=True, postgresql_where=sa.text("client_submission_id IS NOT NULL"))
    op.create_index("ix_deliverable_quality_review_assignments_reviewer", "deliverable_quality_review_assignments", ["tenant_id", "reviewer_user_id", "status"], unique=False)

    op.create_table(
        "deliverable_quality_review_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitted_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=240), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('ocr', 'frame_ocr')", name="ck_deliverable_quality_review_evidence_kind"),
        sa.CheckConstraint("status IN ('complete', 'partial', 'unavailable')", name="ck_deliverable_quality_review_evidence_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_deliverable_quality_review_evidence_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["users.id"], name="fk_deliverable_quality_review_evidence_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "review_id"], ["deliverable_quality_reviews.tenant_id", "deliverable_quality_reviews.id"], name="fk_deliverable_quality_review_evidence_tenant_review", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "kind", name="uq_deliverable_quality_review_evidence_kind"),
        sa.UniqueConstraint("tenant_id", "submitted_by_user_id", "client_evidence_id", name="uq_deliverable_quality_review_evidence_client_identity"),
        sa.UniqueConstraint("receipt_ref", name="uq_deliverable_quality_review_evidence_receipt"),
    )
    op.create_index("ix_deliverable_quality_review_evidence_review", "deliverable_quality_review_evidence", ["review_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_deliverable_quality_review_evidence_review", table_name="deliverable_quality_review_evidence")
    op.drop_table("deliverable_quality_review_evidence")
    op.drop_index("ix_deliverable_quality_review_assignments_reviewer", table_name="deliverable_quality_review_assignments")
    op.drop_index("uq_deliverable_quality_review_submission_client_identity", table_name="deliverable_quality_review_assignments")
    op.drop_table("deliverable_quality_review_assignments")
    op.drop_index("ix_deliverable_quality_reviews_tenant_created", table_name="deliverable_quality_reviews")
    op.drop_index("uq_deliverable_quality_reviews_open_request", table_name="deliverable_quality_reviews")
    op.drop_table("deliverable_quality_reviews")
