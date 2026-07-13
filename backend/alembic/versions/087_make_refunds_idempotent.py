"""Make incident and customer refunds idempotent.

Revision ID: make_refunds_idempotent
Revises: add_media_generation_tasks
Create Date: 2026-07-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "make_refunds_idempotent"
down_revision: Union[str, Sequence[str], None] = "add_media_generation_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_credit_transactions_idempotent_grants"


def _replace_index(predicate: str) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes("credit_transactions")}
    if INDEX_NAME in existing:
        op.drop_index(INDEX_NAME, table_name="credit_transactions")
    op.create_index(
        INDEX_NAME,
        "credit_transactions",
        ["tenant_id", "reason", "ref_type", "ref_id"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    _replace_index(
        "reason IN ('subscribe', 'topup', 'refund', 'refund_clawback') "
        "AND ref_type IS NOT NULL AND ref_id IS NOT NULL"
    )


def downgrade() -> None:
    _replace_index(
        "reason IN ('subscribe', 'topup', 'refund_clawback') "
        "AND ref_type IS NOT NULL AND ref_id IS NOT NULL"
    )
