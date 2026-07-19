"""Align the durable task status enum with historical production data.

Revision ID: align_task_failed_status
Revises: merge_v111_astra_heads
Create Date: 2026-07-19 14:30:00
"""

from collections.abc import Sequence

from alembic import op


revision: str = "align_task_failed_status"
down_revision: str | Sequence[str] | None = "merge_v111_astra_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Some long-lived deployments already contain this value and historical
    # failed tasks. IF NOT EXISTS keeps their upgrade idempotent while making
    # fresh and older schemas agree with the ORM contract.
    op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'failed'")


def downgrade() -> None:
    # PostgreSQL cannot safely remove an enum label while rows may still use it.
    # Keeping the additive value is the only non-destructive downgrade.
    pass
