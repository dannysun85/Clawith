"""Persist confirmed work statements before task execution.

Revision ID: task_confirmation_contract
Revises: backfill_private_assistant
Create Date: 2026-08-01 11:00:00

The workbench remains an intake/read-model layer. These additive fields keep
the user-confirmed business contract on the existing Task without creating a
second execution state machine. Legacy tasks remain readable with a general
work type and an empty statement; only the workbench create API requires new
confirmation evidence.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "task_confirmation_contract"
down_revision: str | Sequence[str] | None = "backfill_private_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("tasks")
    }
    expected = {
        "work_type",
        "work_statement",
        "confirmation_fingerprint",
        "confirmed_at",
    }
    present = expected & columns
    if present == expected:
        # Fresh installs build the current Task shape in the bootstrap
        # revision.  Historical databases reach this revision without any of
        # these fields, so only the complete current shape is safe to adopt.
        return
    if present:
        raise RuntimeError("Partial Task confirmation schema requires manual repair")

    op.add_column(
        "tasks",
        sa.Column("work_type", sa.String(length=32), nullable=False, server_default="general"),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "work_statement",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("confirmation_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "confirmed_at")
    op.drop_column("tasks", "confirmation_fingerprint")
    op.drop_column("tasks", "work_statement")
    op.drop_column("tasks", "work_type")
