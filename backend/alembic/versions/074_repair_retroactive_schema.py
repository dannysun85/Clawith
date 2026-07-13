"""Repair schema changes that were inserted behind already-deployed heads.

Revision ID: repair_retroactive_schema
Revises: seed_saas_mvp_catalog
Create Date: 2026-07-11

Some deployments had already been stamped beyond the revisions that later
introduced model verification, social-account auth_type, and the AgentBay
session ledger. Alembic correctly skipped those newly inserted historical
revisions. This head migration reapplies their DDL idempotently.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "repair_retroactive_schema"
down_revision: Union[str, Sequence[str], None] = "seed_saas_mvp_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _repair_llm_model_verification_columns() -> None:
    if not _table_exists("llm_models"):
        return
    if not _column_exists("llm_models", "verification_status"):
        op.add_column("llm_models", sa.Column("verification_status", sa.String(30), nullable=True))
    if not _column_exists("llm_models", "last_verified_at"):
        op.add_column("llm_models", sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True))
    if not _column_exists("llm_models", "last_error_code"):
        op.add_column("llm_models", sa.Column("last_error_code", sa.String(20), nullable=True))
    if not _column_exists("llm_models", "last_error_message"):
        op.add_column("llm_models", sa.Column("last_error_message", sa.String(500), nullable=True))


def _repair_social_account_auth_type() -> None:
    table_name = "company_social_account_capabilities"
    if not _table_exists(table_name):
        return
    if not _column_exists(table_name, "auth_type"):
        op.add_column(table_name, sa.Column("auth_type", sa.String(40), nullable=False, server_default=""))
        op.alter_column(table_name, "auth_type", server_default=None)
    index_name = op.f("ix_company_social_account_capabilities_auth_type")
    if not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, ["auth_type"])


def _repair_agentbay_session_ledger() -> None:
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
        index_name = op.f(f"ix_{table_name}_{column}")
        if not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, [column])


def upgrade() -> None:
    _repair_llm_model_verification_columns()
    _repair_social_account_auth_type()
    _repair_agentbay_session_ledger()


def downgrade() -> None:
    # This is a repair for schema that may have been created by earlier
    # revisions. A downgrade cannot safely determine ownership, so retain it.
    pass
