"""Add AgentBay session ledger.

Revision ID: add_agentbay_session_ledger
Revises: add_account_capability_auth_type
Create Date: 2026-06-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "add_agentbay_session_ledger"
down_revision: Union[str, Sequence[str], None] = "add_account_capability_auth_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    table_name = "agentbay_session_ledger"
    if not _table_exists(table_name):
        op.create_table(
            table_name,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("chat_session_id", sa.String(length=160), nullable=True),
            sa.Column("provider_session_id", sa.String(length=200), nullable=True),
            sa.Column("image_type", sa.String(length=40), nullable=False),
            sa.Column("purpose", sa.String(length=60), nullable=False),
            sa.Column("platform", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("close_reason", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    for column in (
        "tenant_id",
        "agent_id",
        "user_id",
        "chat_session_id",
        "provider_session_id",
        "image_type",
        "purpose",
        "platform",
        "status",
        "close_reason",
        "started_at",
        "last_used_at",
        "closed_at",
    ):
        index_name = op.f(f"ix_agentbay_session_ledger_{column}")
        if not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, [column])


def downgrade() -> None:
    table_name = "agentbay_session_ledger"
    if not _table_exists(table_name):
        return

    for column in (
        "closed_at",
        "last_used_at",
        "started_at",
        "close_reason",
        "status",
        "platform",
        "purpose",
        "image_type",
        "provider_session_id",
        "chat_session_id",
        "user_id",
        "agent_id",
        "tenant_id",
    ):
        index_name = op.f(f"ix_agentbay_session_ledger_{column}")
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
    op.drop_table(table_name)
