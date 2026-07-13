"""Disable proactive Agent heartbeat by default.

Revision ID: disable_heartbeat_default
Revises: align_minimax_text_tiers
Create Date: 2026-07-12

Existing heartbeat choices are reset once during upgrade because the previous
default enabled autonomous paid LLM work without an explicit user opt-in.
Explicit AgentTrigger rows are intentionally untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "disable_heartbeat_default"
down_revision: Union[str, Sequence[str], None] = "align_minimax_text_tiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE agents SET heartbeat_enabled = FALSE WHERE heartbeat_enabled IS TRUE")
    op.alter_column(
        "agents",
        "heartbeat_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.false(),
    )


def downgrade() -> None:
    # Do not silently re-enable paid autonomous work. Downgrade only restores
    # the legacy default for subsequently inserted rows.
    op.alter_column(
        "agents",
        "heartbeat_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
