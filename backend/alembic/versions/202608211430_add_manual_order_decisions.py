"""Add auditable, reversible operator decisions for manual payment orders.

Revision ID: manual_order_decisions
Revises: billing_effect_receipts
Create Date: 2026-08-21 14:30:00

This is an expand-only release migration.  It creates an independent receipt
table and does not rewrite or classify any existing order.  Historical manual
pending orders therefore remain untouched until an operator supplies exact
tenant, evidence, reason, expected state, and an idempotency key.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "manual_order_decisions"
down_revision: str | Sequence[str] | None = "billing_effect_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "payment_order_operator_decisions"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE_NAME in _table_names():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("evidence_ref", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.String(length=20), nullable=False),
        sa.Column("resulting_status", sa.String(length=20), nullable=False),
        sa.Column(
            "rollback_of_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "disposition IN ('keep_pending', 'mark_paid', 'cancel_expired', "
            "'cancel_test', 'cancel_invalid', 'restore_pending')",
            name="ck_payment_order_operator_decision_disposition",
        ),
        sa.CheckConstraint(
            "previous_status IN ('pending', 'canceled') AND "
            "resulting_status IN ('pending', 'paid', 'canceled')",
            name="ck_payment_order_operator_decision_statuses",
        ),
        sa.CheckConstraint(
            "(disposition = 'restore_pending' AND rollback_of_decision_id IS NOT NULL) OR "
            "(disposition != 'restore_pending' AND rollback_of_decision_id IS NULL)",
            name="ck_payment_order_operator_decision_rollback_shape",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_payment_order_decision_actor_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["payment_orders.id"],
            name="fk_payment_order_decision_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_of_decision_id"],
            [f"{TABLE_NAME}.id"],
            name="fk_payment_order_decision_rollback",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_payment_order_decision_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_order_operator_decisions"),
        sa.UniqueConstraint(
            "order_id",
            "idempotency_key_hash",
            name="uq_payment_order_operator_decision_idempotency",
        ),
        sa.UniqueConstraint(
            "rollback_of_decision_id",
            name="uq_payment_order_operator_decision_rollback",
        ),
    )
    op.create_index(
        "ix_payment_order_operator_decisions_order_created",
        TABLE_NAME,
        ["order_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_order_operator_decisions_tenant_id",
        TABLE_NAME,
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    if TABLE_NAME in _table_names():
        op.drop_table(TABLE_NAME)
