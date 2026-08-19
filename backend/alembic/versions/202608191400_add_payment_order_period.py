"""Add period column to payment_orders.

WeChat Pay Native orders finalize asynchronously via webhook, so the paid
period (monthly / yearly) must be persisted on the order at checkout time.
Previously finalize_order_in_session hardcoded a 30-day grant, which silently
under-delivered yearly subscriptions.

Revision ID: payment_order_period
Revises: deliverable_selection_receipts
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "payment_order_period"
down_revision: str | Sequence[str] | None = "deliverable_selection_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    # 001_initial_schema does create_all from current ORM metadata, so fresh
    # databases already carry this column; only backfill when it is missing.
    return column in {
        column_info["name"] for column_info in sa.inspect(op.get_bind()).get_columns(table)
    }


def upgrade() -> None:
    if not _column_exists("payment_orders", "period"):
        op.add_column(
            "payment_orders",
            sa.Column("period", sa.String(length=20), nullable=False, server_default="monthly"),
        )


def downgrade() -> None:
    op.drop_column("payment_orders", "period")
