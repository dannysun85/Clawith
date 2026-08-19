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


def upgrade() -> None:
    op.add_column(
        "payment_orders",
        sa.Column("period", sa.String(length=20), nullable=False, server_default="monthly"),
    )


def downgrade() -> None:
    op.drop_column("payment_orders", "period")
