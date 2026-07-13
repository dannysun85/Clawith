"""Keep activity action enum aligned with runtime activity writers.

Revision ID: sync_activity_action_enum
Revises: add_trigger_execution_ledger
"""

from collections.abc import Sequence

from alembic import op


revision: str = "sync_activity_action_enum"
down_revision: str | None = "add_trigger_execution_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for value in ("agent_file_sent", "agent_file_received", "oneshot_task"):
        op.execute(
            f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum values in place.
    pass
