"""Add durable system email delivery ledger.

Revision ID: outbound_email_delivery
Revises: onboarding_product_settings
Create Date: 2026-08-16 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "outbound_email_delivery"
down_revision: str | Sequence[str] | None = "onboarding_product_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    invitation_columns = {
        column["name"] for column in inspector.get_columns("organization_invitations")
    }
    if "delivery_mode" not in invitation_columns:
        op.add_column(
            "organization_invitations",
            sa.Column(
                "delivery_mode",
                sa.String(length=24),
                server_default="email",
                nullable=False,
            ),
        )
    invitation_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("organization_invitations")
    }
    if "ck_organization_invitations_delivery_mode" not in invitation_checks:
        op.create_check_constraint(
            "ck_organization_invitations_delivery_mode",
            "organization_invitations",
            "delivery_mode IN ('email', 'manual_link')",
        )

    table_exists = "outbound_email_deliveries" in sa.inspect(bind).get_table_names()
    if not table_exists:
        op.create_table(
            "outbound_email_deliveries",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("purpose", sa.String(length=40), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("invitation_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("recipient_hash", sa.String(length=64), nullable=False),
            sa.Column("recipient_mask", sa.String(length=255), nullable=False),
            sa.Column("payload_envelope", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(length=80), nullable=True),
            sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("smtp_accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("transport_receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("idempotency_key", sa.String(length=160), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "purpose IN ('email_verification', 'password_reset', 'company_invitation')",
                name="ck_outbound_email_deliveries_purpose",
            ),
            sa.CheckConstraint(
                "status IN ('queued', 'sending', 'retry_wait', 'smtp_accepted', "
                "'blocked_configuration', 'permanent_failed', 'cancelled')",
                name="ck_outbound_email_deliveries_status",
            ),
            sa.CheckConstraint(
                "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
                name="ck_outbound_email_deliveries_attempts",
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["invitation_id"],
                ["organization_invitations.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["requested_by_user_id"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("outbound_email_deliveries")
    }
    indexes = (
        ("ix_outbound_email_deliveries_due", ["status", "next_attempt_at"], False, None),
        ("ix_outbound_email_deliveries_purpose", ["purpose"], False, None),
        ("ix_outbound_email_deliveries_tenant_id", ["tenant_id"], False, None),
        ("ix_outbound_email_deliveries_identity_id", ["identity_id"], False, None),
        ("ix_outbound_email_deliveries_invitation_id", ["invitation_id"], False, None),
        ("ix_outbound_email_deliveries_recipient_hash", ["recipient_hash"], False, None),
        ("ix_outbound_email_deliveries_next_attempt_at", ["next_attempt_at"], False, None),
        ("ix_outbound_email_deliveries_status", ["status"], False, None),
        (
            "uq_outbound_email_deliveries_idempotency_key",
            ["idempotency_key"],
            True,
            sa.text("idempotency_key IS NOT NULL"),
        ),
    )
    for name, columns, unique, predicate in indexes:
        if name in existing_indexes:
            continue
        op.create_index(
            name,
            "outbound_email_deliveries",
            columns,
            unique=unique,
            postgresql_where=predicate,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "outbound_email_deliveries" in inspector.get_table_names():
        op.drop_table("outbound_email_deliveries")
    invitation_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("organization_invitations")
    }
    if "ck_organization_invitations_delivery_mode" in invitation_checks:
        op.drop_constraint(
            "ck_organization_invitations_delivery_mode",
            "organization_invitations",
            type_="check",
        )
    invitation_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("organization_invitations")
    }
    if "delivery_mode" in invitation_columns:
        op.drop_column("organization_invitations", "delivery_mode")
