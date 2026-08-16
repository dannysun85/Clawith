"""Add explicit lifecycle state for retained personal assistants.

Revision ID: legacy_assistant_lifecycle
Revises: tenant_deletion_purge
Create Date: 2026-08-17 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "legacy_assistant_lifecycle"
down_revision: str | Sequence[str] | None = "tenant_deletion_purge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_agents_legacy_assistant_state"


def _column_names() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("agents")
    }


def _check_constraint_names() -> set[str]:
    return {
        str(constraint.get("name"))
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("agents")
        if constraint.get("name")
    }


def upgrade() -> None:
    if "legacy_assistant_state" not in _column_names():
        op.add_column(
            "agents",
            sa.Column("legacy_assistant_state", sa.String(length=16), nullable=True),
        )
    if _CONSTRAINT not in _check_constraint_names():
        op.create_check_constraint(
            _CONSTRAINT,
            "agents",
            "legacy_assistant_state IS NULL OR "
            "legacy_assistant_state IN ('archived', 'converted')",
        )


def downgrade() -> None:
    if _CONSTRAINT in _check_constraint_names():
        op.drop_constraint(_CONSTRAINT, "agents", type_="check")
    if "legacy_assistant_state" in _column_names():
        op.drop_column("agents", "legacy_assistant_state")
