"""Add durable tenant purge jobs, holds, and minimal tombstones.

Revision ID: tenant_deletion_purge
Revises: identity_mfa
Create Date: 2026-08-16 12:00:00
"""

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "tenant_deletion_purge"
down_revision: str | Sequence[str] | None = "identity_mfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = {
    "tenant_deletion_jobs",
    "tenant_deletion_holds",
    "tenant_deletion_tombstones",
}


def _table_names() -> set[str]:
    bind = op.get_bind()
    return {
        table_name
        for table_name in _TABLES
        if bind.execute(
            sa.text("SELECT to_regclass(:qualified_name)"),
            {"qualified_name": f"public.{table_name}"},
        ).scalar_one_or_none()
        is not None
    }


def _ensure_complete_shape() -> bool:
    present = _TABLES & _table_names()
    if present and present != _TABLES:
        raise RuntimeError(
            "Tenant purge migration found partial tables: "
            + ", ".join(sorted(present))
        )
    return present == _TABLES


def _create_tables() -> None:
    op.create_table(
        "tenant_deletion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "table_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "storage_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('scheduled', 'dry_run_passed', 'purging', 'held', 'failed')",
            name="ck_tenant_deletion_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_tenant_deletion_jobs_attempt_count"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_deletion_jobs_tenant_id", "tenant_deletion_jobs", ["tenant_id"])
    op.create_index("ix_tenant_deletion_jobs_status", "tenant_deletion_jobs", ["status"])
    op.create_index("ix_tenant_deletion_jobs_eligible_at", "tenant_deletion_jobs", ["eligible_at"])
    op.create_index(
        "ix_tenant_deletion_jobs_status_eligible",
        "tenant_deletion_jobs",
        ["status", "eligible_at"],
    )

    op.create_table(
        "tenant_deletion_holds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hold_type", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("created_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("released_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("release_reason_code", sa.String(length=100), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "hold_type IN ('legal', 'operations')",
            name="ck_tenant_deletion_holds_type",
        ),
        sa.CheckConstraint("reason_code <> ''", name="ck_tenant_deletion_holds_reason"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_identity_id"],
            ["identities.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["released_by_identity_id"],
            ["identities.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_deletion_holds_tenant_id", "tenant_deletion_holds", ["tenant_id"])
    op.create_index(
        "uq_tenant_deletion_holds_active_type",
        "tenant_deletion_holds",
        ["tenant_id", "hold_type"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "tenant_deletion_tombstones",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name_digest", sa.String(length=64), nullable=False),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("table_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("storage_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_tenant_deletion_tombstones_schema_version",
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("receipt_hash"),
    )
    op.create_index(
        "ix_tenant_deletion_tombstones_purged_at",
        "tenant_deletion_tombstones",
        ["purged_at"],
    )


def _backfill_jobs() -> None:
    bind = op.get_bind()
    existing = {
        row[0]
        for row in bind.execute(sa.text("SELECT tenant_id FROM tenant_deletion_jobs"))
    }
    scheduled = list(
        bind.execute(
            sa.text(
                """
                SELECT id, deletion_scheduled_for
                  FROM tenants
                 WHERE deletion_requested_at IS NOT NULL
                   AND deletion_scheduled_for IS NOT NULL
                """
            )
        )
    )
    for tenant_id, eligible_at in scheduled:
        if tenant_id in existing:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO tenant_deletion_jobs (
                    id, tenant_id, status, eligible_at, attempt_count,
                    table_counts, storage_summary
                ) VALUES (
                    :id, :tenant_id, 'scheduled', :eligible_at, 0,
                    CAST('{}' AS jsonb), CAST('{}' AS jsonb)
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "eligible_at": eligible_at,
            },
        )


def upgrade() -> None:
    if not _ensure_complete_shape():
        _create_tables()
    _backfill_jobs()


def downgrade() -> None:
    tables = _table_names()
    for table_name in (
        "tenant_deletion_tombstones",
        "tenant_deletion_holds",
        "tenant_deletion_jobs",
    ):
        if table_name in tables:
            op.drop_table(table_name)
