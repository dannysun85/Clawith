"""Add privacy-safe production issue monitoring tables.

Revision ID: add_production_issue_monitoring
Revises: bound_media_generation_retries
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "add_production_issue_monitoring"
down_revision: str | Sequence[str] | None = "bound_media_generation_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("production_issues"):
        op.create_table(
            "production_issues",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column("severity", sa.String(length=16), server_default="error", nullable=False),
            sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("summary", sa.String(length=500), nullable=False),
            sa.Column("route", sa.String(length=500), nullable=True),
            sa.Column("operation", sa.String(length=100), nullable=True),
            sa.Column("event_count", sa.Integer(), server_default="1", nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_trace_id", sa.String(length=64), nullable=True),
            sa.Column("release_version", sa.String(length=50), nullable=True),
            sa.Column("last_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("fingerprint", name="uq_production_issues_fingerprint"),
        )
        op.create_index("ix_production_issues_category", "production_issues", ["category"])
        op.create_index("ix_production_issues_status_last_seen", "production_issues", ["status", "last_seen_at"])
        op.create_index("ix_production_issues_severity_last_seen", "production_issues", ["severity", "last_seen_at"])

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("production_issue_events"):
        op.create_table(
            "production_issue_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("trace_id", sa.String(length=64), nullable=True),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("route", sa.String(length=500), nullable=True),
            sa.Column("operation", sa.String(length=100), nullable=True),
            sa.Column("metadata_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["issue_id"], ["production_issues.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_production_issue_events_created_at", "production_issue_events", ["created_at"])
        op.create_index("ix_production_issue_events_issue_created", "production_issue_events", ["issue_id", "created_at"])
        op.create_index("ix_production_issue_events_tenant_created", "production_issue_events", ["tenant_id", "created_at"])


def downgrade() -> None:
    # Retain operational evidence through application rollbacks.
    pass
