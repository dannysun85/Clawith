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


_BOOTSTRAP_COLUMNS = {
    "deliverable_quality_reviews": {
        "id",
        "tenant_id",
        "request_id",
        "created_by_user_id",
        "client_review_id",
        "request_fingerprint",
        "modality",
        "status",
        "minimum_reviewers",
        "assigned_reviewer_count",
        "artifact_hashes",
        "scenario",
        "review_package",
        "receipt",
        "receipt_sha256",
        "version",
        "sealed_at",
        "created_at",
        "updated_at",
    },
    "deliverable_quality_review_assignments": {
        "id",
        "tenant_id",
        "review_id",
        "reviewer_user_id",
        "reviewer_identity_id",
        "reviewer_display_name",
        "reviewer_role",
        "reviewer_receipt_ref",
        "status",
        "client_submission_id",
        "submission_fingerprint",
        "submission",
        "submitted_at",
        "created_at",
    },
    "deliverable_quality_review_evidence": {
        "id",
        "tenant_id",
        "review_id",
        "submitted_by_user_id",
        "client_evidence_id",
        "evidence_fingerprint",
        "receipt_ref",
        "kind",
        "status",
        "source_ref",
        "receipt",
        "created_at",
    },
}

_BOOTSTRAP_INDEXES = {
    "deliverable_quality_reviews": {
        "uq_deliverable_quality_reviews_open_request",
        "ix_deliverable_quality_reviews_tenant_created",
    },
    "deliverable_quality_review_assignments": {
        "uq_deliverable_quality_review_submission_client_identity",
        "ix_deliverable_quality_review_assignments_reviewer",
    },
    "deliverable_quality_review_evidence": {
        "ix_deliverable_quality_review_evidence_review",
    },
}

_BOOTSTRAP_UNIQUES = {
    "deliverable_quality_reviews": {
        "uq_deliverable_quality_reviews_client_identity",
        "uq_deliverable_quality_reviews_tenant_id_id",
    },
    "deliverable_quality_review_assignments": {
        "uq_deliverable_quality_review_assignment_user",
        "uq_deliverable_quality_review_assignment_identity",
        "uq_deliverable_quality_review_assignment_receipt",
    },
    "deliverable_quality_review_evidence": {
        "uq_deliverable_quality_review_evidence_kind",
        "uq_deliverable_quality_review_evidence_client_identity",
        "uq_deliverable_quality_review_evidence_receipt",
    },
}

_BOOTSTRAP_FOREIGN_KEYS = {
    "deliverable_quality_reviews": {
        "fk_deliverable_quality_reviews_tenant",
        "fk_deliverable_quality_reviews_creator",
        "fk_deliverable_quality_reviews_tenant_request",
    },
    "deliverable_quality_review_assignments": {
        "fk_deliverable_quality_review_assignments_tenant",
        "fk_deliverable_quality_review_assignments_user",
        "fk_deliverable_quality_review_assignments_identity",
        "fk_deliverable_quality_review_assignments_tenant_review",
    },
    "deliverable_quality_review_evidence": {
        "fk_deliverable_quality_review_evidence_tenant",
        "fk_deliverable_quality_review_evidence_user",
        "fk_deliverable_quality_review_evidence_tenant_review",
    },
}

_BOOTSTRAP_CHECKS = {
    "deliverable_quality_reviews": {
        "ck_deliverable_quality_reviews_status",
        "ck_deliverable_quality_reviews_modality",
        "ck_deliverable_quality_reviews_minimum_reviewers",
        "ck_deliverable_quality_reviews_assigned_reviewers",
        "ck_deliverable_quality_reviews_version_positive",
    },
    "deliverable_quality_review_assignments": {
        "ck_deliverable_quality_review_assignments_status",
    },
    "deliverable_quality_review_evidence": {
        "ck_deliverable_quality_review_evidence_kind",
        "ck_deliverable_quality_review_evidence_status",
    },
}


def _precreated_quality_review_state(
    schema: dict[str, dict[str, set[str]]],
) -> bool:
    expected_tables = set(_BOOTSTRAP_COLUMNS)
    present_tables = set(schema) & expected_tables
    if not present_tables:
        return False
    if present_tables != expected_tables:
        raise RuntimeError(
            "Partial deliverable quality-review bootstrap schema; missing "
            + ", ".join(sorted(expected_tables - present_tables))
        )

    requirements = {
        "columns": _BOOTSTRAP_COLUMNS,
        "indexes": _BOOTSTRAP_INDEXES,
        "uniques": _BOOTSTRAP_UNIQUES,
        "foreign_keys": _BOOTSTRAP_FOREIGN_KEYS,
        "checks": _BOOTSTRAP_CHECKS,
    }
    for object_type, expected_by_table in requirements.items():
        for table_name, required in expected_by_table.items():
            missing = required - schema[table_name].get(object_type, set())
            if missing:
                raise RuntimeError(
                    f"Incomplete {table_name} bootstrap {object_type}: "
                    + ", ".join(sorted(missing))
                )
    for table_name in expected_tables:
        if schema[table_name].get("primary_key") != {"id"}:
            raise RuntimeError(
                f"Contradictory {table_name} bootstrap primary key"
            )
    return True


def _inspected_bootstrap_schema() -> dict[str, dict[str, set[str]]]:
    inspector = sa.inspect(op.get_bind())
    expected_tables = set(_BOOTSTRAP_COLUMNS)
    present_tables = set(inspector.get_table_names()) & expected_tables
    return {
        table_name: {
            "columns": {
                column["name"] for column in inspector.get_columns(table_name)
            },
            "indexes": {
                index["name"]
                for index in inspector.get_indexes(table_name)
                if index.get("name")
            },
            "uniques": {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint.get("name")
            },
            "foreign_keys": {
                constraint["name"]
                for constraint in inspector.get_foreign_keys(table_name)
                if constraint.get("name")
            },
            "checks": {
                constraint["name"]
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name")
            },
            "primary_key": set(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns", []
                )
            ),
        }
        for table_name in present_tables
    }


def upgrade() -> None:
    if _precreated_quality_review_state(_inspected_bootstrap_schema()):
        return

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
