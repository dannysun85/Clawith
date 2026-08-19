"""Add candidate-selection receipts for v2 deliverables (FR-I6).

Revision ID: deliverable_selection_receipts
Revises: creative_brief_receipts
Create Date: 2026-08-19 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "deliverable_selection_receipts"
down_revision: str | Sequence[str] | None = "creative_brief_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_TABLE = "deliverable_selection_receipts"


def _jsonb() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _json_default(literal: str) -> sa.TextClause:
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{literal}'::jsonb")
    return sa.text(f"'{literal}'")


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _NEW_TABLE in _existing_tables():
        return

    op.create_table(
        "deliverable_selection_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("selected_unit_key", sa.String(length=120), nullable=False),
        sa.Column("candidate_scores", _jsonb(), server_default=_json_default("[]"), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("cost_breakdown", _jsonb(), server_default=_json_default("{}"), nullable=False),
        sa.Column("actor", sa.String(length=24), nullable=False),
        sa.Column("client_selection_id", sa.UUID(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "actor IN ('auto', 'user')",
            name="ck_deliverable_selection_receipts_actor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_selection_receipts_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_selection_receipts_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["deliverable_executions.id"],
            name="fk_deliverable_selection_receipts_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_deliverable_selection_receipts_actor",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_selection_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "client_selection_id",
            name="uq_deliverable_selection_receipts_client",
        ),
    )
    op.create_index(
        "ix_deliverable_selection_receipts_request_created",
        "deliverable_selection_receipts",
        ["request_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    if _NEW_TABLE in _existing_tables():
        op.drop_index(
            "ix_deliverable_selection_receipts_request_created",
            table_name="deliverable_selection_receipts",
        )
        op.drop_table("deliverable_selection_receipts")
