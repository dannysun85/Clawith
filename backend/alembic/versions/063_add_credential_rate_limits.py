"""Add client-side rate limit columns to llm_credentials.

Revision ID: add_credential_rate_limits
Revises: add_credential_pool
Create Date: 2026-07-08

Adds rpm_limit, tpm_limit, window_5h_limit columns for proactive client-side
rate limiting (prevents 429 cascades and provider bans). Drives Redis-backed
sliding-window filtering in the credential pool load balancer.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "add_credential_rate_limits"
down_revision: Union[str, None] = "add_credential_pool"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("llm_credentials")}
    if "rpm_limit" not in existing:
        op.add_column("llm_credentials", sa.Column("rpm_limit", sa.Integer(), nullable=True))
    if "tpm_limit" not in existing:
        op.add_column("llm_credentials", sa.Column("tpm_limit", sa.Integer(), nullable=True))
    if "window_5h_limit" not in existing:
        op.add_column("llm_credentials", sa.Column("window_5h_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_credentials", "window_5h_limit")
    op.drop_column("llm_credentials", "tpm_limit")
    op.drop_column("llm_credentials", "rpm_limit")
