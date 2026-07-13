"""Add llm_credentials table (account pool, provider-scoped).

Revision ID: add_credential_pool
Revises: add_subscription_tables
Create Date: 2026-07-06

Restores the API-key account pool (previously removed). Provider-scoped (one
account serves multiple models/modalities of a provider, e.g. a MiniMax code-plan
account can call text/voice/image/video). Drives load-balanced invocation +
real-time monitoring. See SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §7.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_credential_pool"
down_revision: Union[str, Sequence[str], None] = "add_subscription_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "llm_credentials" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        "llm_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("api_key_encrypted", sa.String(length=1024), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(), nullable=True),  # ["text","voice","image","video"]
        sa.Column("daily_quota", sa.Integer(), nullable=True),  # per-account daily cap
        sa.Column("used_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="healthy"),
        # healthy / degraded / quota_exceeded / disabled
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),  # weighted round-robin
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),  # higher = used first
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        # null = platform pool (shared across tenants)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_credentials_provider", "llm_credentials", ["provider"])
    op.create_index("ix_llm_credentials_tenant_id", "llm_credentials", ["tenant_id"])
    # composite index for the load-balancer's hot query: provider + status + enabled
    op.create_index(
        "ix_llm_credentials_pool_lookup",
        "llm_credentials",
        ["provider", "status", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_credentials_pool_lookup", table_name="llm_credentials")
    op.drop_index("ix_llm_credentials_tenant_id", table_name="llm_credentials")
    op.drop_index("ix_llm_credentials_provider", table_name="llm_credentials")
    op.drop_table("llm_credentials")
