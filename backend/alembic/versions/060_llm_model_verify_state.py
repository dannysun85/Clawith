"""add llm model verification state

Revision ID: llm_model_verify_state
Revises: add_title_to_agent_focus_items
Create Date: 2026-06-13

Adds verification_status, last_verified_at, last_error_code, last_error_message
to llm_models so model readiness is persistent and visible in Settings.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "llm_model_verify_state"
down_revision: Union[str, None] = "add_title_to_agent_focus_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _column_exists("llm_models", "verification_status"):
        op.add_column(
            "llm_models",
            sa.Column(
                "verification_status",
                sa.String(30),
                nullable=True,
                comment="unverified | verified | auth_failed | billing_blocked | region_mismatch | error",
            ),
        )
    if not _column_exists("llm_models", "last_verified_at"):
        op.add_column("llm_models", sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True))
    if not _column_exists("llm_models", "last_error_code"):
        op.add_column(
            "llm_models",
            sa.Column("last_error_code", sa.String(20), nullable=True, comment="e.g. 401, 402, 1004"),
        )
    if not _column_exists("llm_models", "last_error_message"):
        op.add_column("llm_models", sa.Column("last_error_message", sa.String(500), nullable=True))


def downgrade() -> None:
    for column_name in ("last_error_message", "last_error_code", "last_verified_at", "verification_status"):
        if _column_exists("llm_models", column_name):
            op.drop_column("llm_models", column_name)
