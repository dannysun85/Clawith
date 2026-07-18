"""Converge the Astra security and upstream v1.11 Runtime branches.

Revision ID: merge_v111_astra_heads
Revises: sso_password_login, add_experience_revision_drafts
Create Date: 2026-07-19 10:00:00

Both parents are additive release histories that must remain independently
addressable. The merged Runtime serializes accepted Trigger Runs through its
scheduling lane, while the legacy worker still needs a database fence for
leased executions. Narrow the historical unique index to that legacy lease
contract while establishing one release head.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "merge_v111_astra_heads"
down_revision: tuple[str, str] = (
    "sso_password_login",
    "add_experience_revision_drafts",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_trigger_executions_processing_agent",
        table_name="trigger_executions",
    )
    op.create_index(
        "uq_trigger_executions_processing_agent",
        "trigger_executions",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing' AND lease_owner IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_trigger_executions_processing_agent",
        table_name="trigger_executions",
    )
    op.create_index(
        "uq_trigger_executions_processing_agent",
        "trigger_executions",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )
