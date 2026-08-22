"""Add immutable owner review receipts for Work task results.

Revision ID: task_result_reviews
Revises: manual_order_decisions
Create Date: 2026-08-21 17:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "task_result_reviews"
down_revision: str | Sequence[str] | None = "manual_order_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "task_result_review_receipts"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE_NAME in _table_names():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('approve', 'request_changes')",
            name="ck_task_result_review_receipts_action",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_task_result_review_receipts_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_task_result_review_receipts_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_result_review_receipts_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_task_result_review_receipts_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_result_review_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "run_id",
            name="uq_task_result_review_receipts_attempt",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "client_request_id",
            name="uq_task_result_review_receipts_request",
        ),
    )
    op.create_index(
        "ix_task_result_review_receipts_task_created",
        TABLE_NAME,
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    if TABLE_NAME in _table_names():
        op.drop_table(TABLE_NAME)
