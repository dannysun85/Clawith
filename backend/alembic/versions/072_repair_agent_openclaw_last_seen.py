"""Repair agent OpenClaw last-seen column.

Revision ID: repair_agent_openclaw_last_seen
Revises: add_douyin_collab_publish_fields
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "repair_agent_openclaw_last_seen"
down_revision: Union[str, Sequence[str], None] = "add_douyin_collab_publish_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _table_exists("agents"):
        return

    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS openclaw_last_seen TIMESTAMPTZ")

    if _column_exists("agents", "astra_last_seen"):
        op.execute(
            "UPDATE agents "
            "SET openclaw_last_seen = astra_last_seen "
            "WHERE openclaw_last_seen IS NULL AND astra_last_seen IS NOT NULL"
        )


def downgrade() -> None:
    if _column_exists("agents", "openclaw_last_seen"):
        op.drop_column("agents", "openclaw_last_seen")
