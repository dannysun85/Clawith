"""add agent error diagnostics

Revision ID: add_agent_error_diagnostics
Revises: disable_unsigned_webhooks
"""

from collections.abc import Sequence

from alembic import op


revision: str = "add_agent_error_diagnostics"
down_revision: str | None = "disable_unsigned_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``001_initial_schema`` builds from current model metadata on a fresh DB,
    # so these columns may already exist when this historical migration runs.
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_error TEXT")
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "last_error_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS last_error_at")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS last_error")
