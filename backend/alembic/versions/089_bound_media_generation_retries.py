"""Bound consecutive media-generation recovery errors.

Revision ID: bound_media_generation_retries
Revises: seed_minimax_media_routes
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "bound_media_generation_retries"
down_revision: str | Sequence[str] | None = "seed_minimax_media_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("media_generation_tasks")
    }
    if "consecutive_error_count" not in columns:
        op.add_column(
            "media_generation_tasks",
            sa.Column(
                "consecutive_error_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )


def downgrade() -> None:
    # Keep recovery evidence during rollback. Older application versions ignore
    # the additive column and a later upgrade remains idempotent.
    pass
