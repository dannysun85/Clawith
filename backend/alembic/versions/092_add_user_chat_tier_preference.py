"""Persist the user's first-party chat model tier across Agents.

Revision ID: add_user_chat_tier_preference
Revises: add_credential_modality_status
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "add_user_chat_tier_preference"
down_revision: str | Sequence[str] | None = "add_credential_modality_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "preferred_chat_tier" not in columns:
        op.add_column(
            "users",
            sa.Column("preferred_chat_tier", sa.String(length=20), nullable=True),
        )
    if "preferred_chat_tier_revision" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "preferred_chat_tier_revision",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )

    inspector = sa.inspect(op.get_bind())
    check_names = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints("users")
    }
    if "ck_users_preferred_chat_tier" not in check_names:
        op.create_check_constraint(
            "ck_users_preferred_chat_tier",
            "users",
            "preferred_chat_tier IS NULL OR preferred_chat_tier IN ('lite', 'pro', 'ultra')",
        )


def downgrade() -> None:
    # Keep the preference through application rollback. Older releases simply
    # ignore the extra nullable column, and retaining it avoids surprising
    # users after a rollback/redeploy cycle.
    pass
