"""Add durable receipts for paid effects, refunds, and provider close retries.

Revision ID: billing_effect_receipts
Revises: backfill_deliv_audit_tenant
Create Date: 2026-08-21 10:30:00

The columns are additive so the previous release can continue to run during a
blue/green rollback. Historical paid subscription orders are conservatively
queued for an idempotent effects reconciliation; no payment, subscription, or
Credit state is changed by the migration itself.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "billing_effect_receipts"
down_revision: str | Sequence[str] | None = "backfill_deliv_audit_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name) if index.get("name")}


def _foreign_key_for_columns(table_name: str, columns: list[str]) -> str | None:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name):
        if foreign_key.get("constrained_columns") == columns:
            name = foreign_key.get("name")
            return str(name) if name else None
    return None


def _add_missing_columns(table_name: str, columns: list[sa.Column]) -> None:
    existing = _column_names(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    _add_missing_columns(
        "payment_orders",
        [
            sa.Column(
                "refunded_amount_cents",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "refunded_credits",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "paid_effects_status",
                sa.String(length=20),
                server_default="not_applicable",
                nullable=False,
            ),
            sa.Column(
                "paid_effects_attempts",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column("paid_effects_error", sa.Text(), nullable=True),
            sa.Column("paid_effects_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("paid_effects_applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "provider_close_status",
                sa.String(length=20),
                server_default="not_requested",
                nullable=False,
            ),
            sa.Column(
                "provider_close_attempts",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column("provider_close_error", sa.Text(), nullable=True),
            sa.Column(
                "provider_close_last_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "provider_close_next_retry_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        ],
    )
    _add_missing_columns(
        "billing_webhook_events",
        [sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True)],
    )
    if _foreign_key_for_columns("billing_webhook_events", ["order_id"]) is None:
        op.create_foreign_key(
            "fk_billing_webhook_events_order_id_payment_orders",
            "billing_webhook_events",
            "payment_orders",
            ["order_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_billing_webhook_events_order_id" not in _index_names("billing_webhook_events"):
        op.create_index(
            "ix_billing_webhook_events_order_id",
            "billing_webhook_events",
            ["order_id"],
            unique=False,
        )
    if "ix_payment_orders_paid_effects_reconcile" not in _index_names("payment_orders"):
        op.create_index(
            "ix_payment_orders_paid_effects_reconcile",
            "payment_orders",
            ["type", "status", "paid_effects_status"],
            unique=False,
        )
    op.execute(
        """
        UPDATE payment_orders
        SET paid_effects_status = 'pending'
        WHERE type = 'subscribe'
          AND status = 'paid'
        """
    )


def downgrade() -> None:
    if "ix_payment_orders_paid_effects_reconcile" in _index_names("payment_orders"):
        op.drop_index("ix_payment_orders_paid_effects_reconcile", table_name="payment_orders")
    if "ix_billing_webhook_events_order_id" in _index_names("billing_webhook_events"):
        op.drop_index("ix_billing_webhook_events_order_id", table_name="billing_webhook_events")
    foreign_key = _foreign_key_for_columns("billing_webhook_events", ["order_id"])
    if foreign_key is not None:
        op.drop_constraint(foreign_key, "billing_webhook_events", type_="foreignkey")
    if "order_id" in _column_names("billing_webhook_events"):
        op.drop_column("billing_webhook_events", "order_id")
    payment_columns = _column_names("payment_orders")
    for column_name in (
        "provider_close_next_retry_at",
        "provider_close_last_attempt_at",
        "provider_close_error",
        "provider_close_attempts",
        "provider_close_status",
        "paid_effects_applied_at",
        "paid_effects_started_at",
        "paid_effects_error",
        "paid_effects_attempts",
        "paid_effects_status",
        "refunded_credits",
        "refunded_amount_cents",
    ):
        if column_name in payment_columns:
            op.drop_column("payment_orders", column_name)
