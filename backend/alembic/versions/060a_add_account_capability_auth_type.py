"""Add auth_type to company_social_account_capabilities.

Revision ID: add_account_capability_auth_type
Revises: llm_model_verify_state
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "add_account_capability_auth_type"
down_revision: Union[str, Sequence[str], None] = "llm_model_verify_state"
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


def upgrade() -> None:
    table_name = "company_social_account_capabilities"
    if not _table_exists(table_name):
        return

    if not _column_exists(table_name, "auth_type"):
        op.add_column(table_name, sa.Column("auth_type", sa.String(40), nullable=False, server_default=""))
        op.alter_column(table_name, "auth_type", server_default=None)

    index_name = op.f("ix_company_social_account_capabilities_auth_type")
    if _column_exists(table_name, "auth_type") and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, ["auth_type"])


def downgrade() -> None:
    table_name = "company_social_account_capabilities"
    if not _table_exists(table_name):
        return

    index_name = op.f("ix_company_social_account_capabilities_auth_type")
    if _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)
    if _column_exists(table_name, "auth_type"):
        op.drop_column(table_name, "auth_type")
