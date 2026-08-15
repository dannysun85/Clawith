"""Add Identity-level TOTP MFA and recovery ledgers.

Revision ID: identity_mfa
Revises: outbound_email_delivery
Create Date: 2026-08-16 11:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "identity_mfa"
down_revision: str | Sequence[str] | None = "outbound_email_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    bind = op.get_bind()
    identity_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("identities")
    }
    identity_additions = (
        ("mfa_secret_envelope", sa.Column("mfa_secret_envelope", sa.Text(), nullable=True)),
        (
            "mfa_enabled",
            sa.Column(
                "mfa_enabled",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        ),
        (
            "mfa_confirmed_at",
            sa.Column("mfa_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "mfa_last_totp_step",
            sa.Column("mfa_last_totp_step", sa.BigInteger(), nullable=True),
        ),
    )
    for name, column in identity_additions:
        if name not in identity_columns:
            op.add_column("identities", column)

    tables = _table_names()
    if "identity_mfa_recovery_codes" not in tables:
        op.create_table(
            "identity_mfa_recovery_codes",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["identity_id"], ["identities.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    recovery_indexes = _index_names("identity_mfa_recovery_codes")
    if "ix_identity_mfa_recovery_codes_identity_id" not in recovery_indexes:
        op.create_index(
            "ix_identity_mfa_recovery_codes_identity_id",
            "identity_mfa_recovery_codes",
            ["identity_id"],
        )
    if "uq_identity_mfa_recovery_codes_active_hash" not in recovery_indexes:
        op.create_index(
            "uq_identity_mfa_recovery_codes_active_hash",
            "identity_mfa_recovery_codes",
            ["identity_id", "code_hash"],
            unique=True,
            postgresql_where=sa.text("used_at IS NULL"),
        )

    tables = _table_names()
    if "identity_mfa_challenges" not in tables:
        op.create_table(
            "identity_mfa_challenges",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("purpose", sa.String(length=24), nullable=False),
            sa.Column("auth_version", sa.Integer(), nullable=False),
            sa.Column("secret_envelope", sa.Text(), nullable=True),
            sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "purpose IN ('login', 'bootstrap', 'setup')",
                name="ck_identity_mfa_challenges_purpose",
            ),
            sa.CheckConstraint(
                "failed_attempts >= 0 AND failed_attempts <= 8",
                name="ck_identity_mfa_challenges_failed_attempts",
            ),
            sa.ForeignKeyConstraint(
                ["identity_id"], ["identities.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    challenge_indexes = _index_names("identity_mfa_challenges")
    for name, columns in (
        ("ix_identity_mfa_challenges_identity_id", ["identity_id"]),
        ("ix_identity_mfa_challenges_user_id", ["user_id"]),
        ("ix_identity_mfa_challenges_expires_at", ["expires_at"]),
    ):
        if name not in challenge_indexes:
            op.create_index(name, "identity_mfa_challenges", columns)
    if "ix_identity_mfa_challenges_active" not in challenge_indexes:
        op.create_index(
            "ix_identity_mfa_challenges_active",
            "identity_mfa_challenges",
            ["identity_id", "expires_at"],
            postgresql_where=sa.text("consumed_at IS NULL"),
        )


def downgrade() -> None:
    tables = _table_names()
    if "identity_mfa_challenges" in tables:
        op.drop_table("identity_mfa_challenges")
    if "identity_mfa_recovery_codes" in tables:
        op.drop_table("identity_mfa_recovery_codes")

    identity_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("identities")
    }
    for name in (
        "mfa_last_totp_step",
        "mfa_confirmed_at",
        "mfa_enabled",
        "mfa_secret_envelope",
    ):
        if name in identity_columns:
            op.drop_column("identities", name)
