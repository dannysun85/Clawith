"""Add explicit default tools to Agent templates.

Revision ID: agent_template_default_tools
Revises: add_deliverable_workbench
"""

from collections.abc import Sequence

from alembic import op


revision: str = "agent_template_default_tools"
down_revision: str | Sequence[str] | None = "add_deliverable_workbench"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_templates "
        "ADD COLUMN IF NOT EXISTS default_tools JSON DEFAULT '[]'::json"
    )
    op.execute(
        "UPDATE agent_templates SET default_tools = '[]'::json "
        "WHERE default_tools IS NULL"
    )
    op.execute(
        "ALTER TABLE agent_templates ALTER COLUMN default_tools SET NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_templates DROP COLUMN IF EXISTS default_tools"
    )
