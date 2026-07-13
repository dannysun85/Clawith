"""Add Douyin collaborative publish package fields.

Revision ID: add_douyin_collab_publish_fields
Revises: subscription_billing_hardening
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_douyin_collab_publish_fields"
down_revision: Union[str, Sequence[str], None] = "subscription_billing_hardening"
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
    table_name = "douyin_publish_jobs"
    if not _table_exists(table_name):
        return

    if not _column_exists(table_name, "publish_mode"):
        op.add_column(
            table_name,
            sa.Column("publish_mode", sa.String(length=40), nullable=False, server_default="collaborative_h5"),
        )
    if not _column_exists(table_name, "share_id"):
        op.add_column(table_name, sa.Column("share_id", sa.String(length=200), nullable=True))
    if not _column_exists(table_name, "share_state"):
        op.add_column(table_name, sa.Column("share_state", sa.String(length=200), nullable=True))
    if not _column_exists(table_name, "share_schema_url"):
        op.add_column(table_name, sa.Column("share_schema_url", sa.Text(), nullable=True))
    if not _column_exists(table_name, "share_nonce"):
        op.add_column(table_name, sa.Column("share_nonce", sa.String(length=80), nullable=True))
    if not _column_exists(table_name, "share_signature"):
        op.add_column(table_name, sa.Column("share_signature", sa.String(length=160), nullable=True))
    if not _column_exists(table_name, "share_expires_at"):
        op.add_column(table_name, sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True))
    if not _column_exists(table_name, "confirmed_at"):
        op.add_column(table_name, sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))

    if _column_exists(table_name, "publish_mode") and not _index_exists(table_name, "ix_douyin_publish_jobs_publish_mode"):
        op.create_index("ix_douyin_publish_jobs_publish_mode", table_name, ["publish_mode"])
    if _column_exists(table_name, "share_id") and not _index_exists(table_name, "ix_douyin_publish_jobs_share_id"):
        op.create_index("ix_douyin_publish_jobs_share_id", table_name, ["share_id"])
    if _column_exists(table_name, "share_state") and not _index_exists(table_name, "ix_douyin_publish_jobs_share_state"):
        op.create_index("ix_douyin_publish_jobs_share_state", table_name, ["share_state"])
    if _column_exists(table_name, "share_expires_at") and not _index_exists(table_name, "ix_douyin_publish_jobs_share_expires_at"):
        op.create_index("ix_douyin_publish_jobs_share_expires_at", table_name, ["share_expires_at"])


def downgrade() -> None:
    table_name = "douyin_publish_jobs"
    if not _table_exists(table_name):
        return

    for index_name in (
        "ix_douyin_publish_jobs_share_expires_at",
        "ix_douyin_publish_jobs_share_state",
        "ix_douyin_publish_jobs_share_id",
        "ix_douyin_publish_jobs_publish_mode",
    ):
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    for column_name in (
        "confirmed_at",
        "share_expires_at",
        "share_signature",
        "share_nonce",
        "share_schema_url",
        "share_state",
        "share_id",
        "publish_mode",
    ):
        if _column_exists(table_name, column_name):
            op.drop_column(table_name, column_name)
