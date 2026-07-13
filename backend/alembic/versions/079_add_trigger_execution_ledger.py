"""Add the durable trigger execution ledger.

Revision ID: add_trigger_execution_ledger
Revises: scope_skills_by_tenant
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "add_trigger_execution_ledger"
down_revision: str | None = "scope_skills_by_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``initial_schema`` builds fresh databases from current metadata, while
    # historical databases need this explicit migration.
    if inspect(op.get_bind()).has_table("trigger_executions"):
        return

    op.create_table(
        "trigger_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_text", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["agent_triggers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trigger_id",
            "idempotency_key",
            name="uq_trigger_execution_idempotency",
        ),
    )
    op.create_index(
        "ix_trigger_executions_agent_id",
        "trigger_executions",
        ["agent_id"],
    )
    op.create_index(
        "ix_trigger_executions_trigger_id",
        "trigger_executions",
        ["trigger_id"],
    )
    op.create_index(
        "ix_trigger_executions_status_scheduled",
        "trigger_executions",
        ["status", "scheduled_at"],
    )


def downgrade() -> None:
    op.drop_table("trigger_executions")
