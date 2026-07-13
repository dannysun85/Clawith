"""Default new credential-pool entries to unverified.

Revision ID: credential_unverified_default
Revises: add_media_generation_caps
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "credential_unverified_default"
down_revision: str | None = "add_media_generation_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "llm_credentials",
        "status",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default="unverified",
    )


def downgrade() -> None:
    op.alter_column(
        "llm_credentials",
        "status",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default="healthy",
    )
