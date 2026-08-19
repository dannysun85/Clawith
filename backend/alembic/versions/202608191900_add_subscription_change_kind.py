"""Add change_kind to payment_orders and scheduled downgrade to subscriptions.

Checkout now classifies plan purchases (new / renew / period_switch / upgrade /
downgrade). Downgrades no longer apply immediately (which would discard paid
higher-tier time); they are scheduled on the subscription and take effect at
period_end, applied by the subscription lifecycle daemon.

Revision ID: subscription_change_kind
Revises: payment_order_period
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "subscription_change_kind"
down_revision: str | Sequence[str] | None = "payment_order_period"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    return column in {
        column_info["name"] for column_info in sa.inspect(op.get_bind()).get_columns(table)
    }


def _constraint_exists(table: str, name: str) -> bool:
    return name in {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table)
    }


def upgrade() -> None:
    # 001_initial_schema does create_all from current ORM metadata, so fresh
    # databases already carry these columns; only backfill what is missing.
    if not _column_exists("payment_orders", "change_kind"):
        op.add_column(
            "payment_orders",
            sa.Column("change_kind", sa.String(length=20), nullable=True),
        )
    if not _column_exists("subscriptions", "scheduled_plan_id"):
        op.add_column(
            "subscriptions",
            sa.Column("scheduled_plan_id", sa.UUID(), nullable=True),
        )
    if not _column_exists("subscriptions", "scheduled_period"):
        op.add_column(
            "subscriptions",
            sa.Column("scheduled_period", sa.String(length=20), nullable=True),
        )
    if _column_exists("subscriptions", "scheduled_plan_id") and not _constraint_exists(
        "subscriptions", "fk_subscriptions_scheduled_plan_id"
    ):
        op.create_foreign_key(
            "fk_subscriptions_scheduled_plan_id",
            "subscriptions",
            "plans",
            ["scheduled_plan_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint("fk_subscriptions_scheduled_plan_id", "subscriptions", type_="foreignkey")
    op.drop_column("subscriptions", "scheduled_period")
    op.drop_column("subscriptions", "scheduled_plan_id")
    op.drop_column("payment_orders", "change_kind")
