"""Harden subscription billing ledger and reservations.

Revision ID: subscription_billing_hardening
Revises: add_douyin_agent_tables
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "subscription_billing_hardening"
down_revision: Union[str, Sequence[str], None] = "add_douyin_agent_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("billing_webhook_events"):
        op.create_table(
            "billing_webhook_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("event_id", sa.String(length=255), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _index_exists("billing_webhook_events", "ix_billing_webhook_events_provider"):
        op.create_index("ix_billing_webhook_events_provider", "billing_webhook_events", ["provider"])
    if not _index_exists("billing_webhook_events", "uq_billing_webhook_events_provider_event_id"):
        op.create_index(
            "uq_billing_webhook_events_provider_event_id",
            "billing_webhook_events",
            ["provider", "event_id"],
            unique=True,
        )

    if not _table_exists("credit_reservations"):
        op.create_table(
            "credit_reservations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("modality", sa.String(length=20), nullable=True),
            sa.Column("tier", sa.String(length=20), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("model", sa.String(length=100), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="reserved"),
            sa.Column("ref_type", sa.String(length=50), nullable=True),
            sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _index_exists("credit_reservations", "ix_credit_reservations_tenant_id"):
        op.create_index("ix_credit_reservations_tenant_id", "credit_reservations", ["tenant_id"])
    if not _index_exists("credit_reservations", "ix_credit_reservations_agent_id"):
        op.create_index("ix_credit_reservations_agent_id", "credit_reservations", ["agent_id"])
    if not _index_exists("credit_reservations", "ix_credit_reservations_created_at"):
        op.create_index("ix_credit_reservations_created_at", "credit_reservations", ["created_at"])
    if not _index_exists("credit_reservations", "ix_credit_reservations_status_expires_at"):
        op.create_index("ix_credit_reservations_status_expires_at", "credit_reservations", ["status", "expires_at"])
    if not _index_exists("credit_reservations", "ix_credit_reservations_ref"):
        op.create_index("ix_credit_reservations_ref", "credit_reservations", ["ref_type", "ref_id"])

    if not _index_exists("credit_transactions", "uq_credit_transactions_idempotent_grants"):
        op.create_index(
            "uq_credit_transactions_idempotent_grants",
            "credit_transactions",
            ["tenant_id", "reason", "ref_type", "ref_id"],
            unique=True,
            postgresql_where=sa.text(
                "reason IN ('subscribe', 'topup', 'refund_clawback') "
                "AND ref_type IS NOT NULL AND ref_id IS NOT NULL"
            ),
        )
    if not _index_exists("credit_transactions", "uq_credit_transactions_reservation_consume"):
        op.create_index(
            "uq_credit_transactions_reservation_consume",
            "credit_transactions",
            ["tenant_id", "ref_type", "ref_id"],
            unique=True,
            postgresql_where=sa.text(
                "reason = 'consume' AND ref_type = 'reservation' AND ref_id IS NOT NULL"
            ),
        )


def downgrade() -> None:
    if _index_exists("credit_transactions", "uq_credit_transactions_reservation_consume"):
        op.drop_index("uq_credit_transactions_reservation_consume", table_name="credit_transactions")
    if _index_exists("credit_transactions", "uq_credit_transactions_idempotent_grants"):
        op.drop_index("uq_credit_transactions_idempotent_grants", table_name="credit_transactions")
    if _index_exists("credit_reservations", "ix_credit_reservations_ref"):
        op.drop_index("ix_credit_reservations_ref", table_name="credit_reservations")
    if _index_exists("credit_reservations", "ix_credit_reservations_status_expires_at"):
        op.drop_index("ix_credit_reservations_status_expires_at", table_name="credit_reservations")
    if _index_exists("credit_reservations", "ix_credit_reservations_created_at"):
        op.drop_index("ix_credit_reservations_created_at", table_name="credit_reservations")
    if _index_exists("credit_reservations", "ix_credit_reservations_agent_id"):
        op.drop_index("ix_credit_reservations_agent_id", table_name="credit_reservations")
    if _index_exists("credit_reservations", "ix_credit_reservations_tenant_id"):
        op.drop_index("ix_credit_reservations_tenant_id", table_name="credit_reservations")
    if _table_exists("credit_reservations"):
        op.drop_table("credit_reservations")
    if _index_exists("billing_webhook_events", "uq_billing_webhook_events_provider_event_id"):
        op.drop_index("uq_billing_webhook_events_provider_event_id", table_name="billing_webhook_events")
    if _index_exists("billing_webhook_events", "ix_billing_webhook_events_provider"):
        op.drop_index("ix_billing_webhook_events_provider", table_name="billing_webhook_events")
    if _table_exists("billing_webhook_events"):
        op.drop_table("billing_webhook_events")
