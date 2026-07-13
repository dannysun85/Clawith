"""Persist the SaaS model selection for first-party chat sessions.

Revision ID: add_chat_session_model_selection
Revises: sync_activity_action_enum
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "add_chat_session_model_selection"
down_revision: str | None = "sync_activity_action_enum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``001_initial_schema`` creates tables from current model metadata on a
    # fresh installation, so these columns can already exist when history is
    # replayed. Historical databases still need the explicit additions.
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("chat_sessions")
    }
    if "model_tier" not in columns:
        op.add_column("chat_sessions", sa.Column("model_tier", sa.String(length=20), nullable=True))
    if "model_modality" not in columns:
        op.add_column("chat_sessions", sa.Column("model_modality", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "model_modality")
    op.drop_column("chat_sessions", "model_tier")
