"""Track provider quota failures per credential modality.

Revision ID: add_credential_modality_status
Revises: add_production_issue_monitoring
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "add_credential_modality_status"
down_revision: str | Sequence[str] | None = "add_production_issue_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("llm_credentials")}
    if "modality_status" not in columns:
        op.add_column(
            "llm_credentials",
            sa.Column(
                "modality_status",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    # Retain the field during application rollback. Older releases ignore it,
    # while keeping it prevents a rollback/redeploy cycle from losing the
    # provider quota isolation state.
    pass
