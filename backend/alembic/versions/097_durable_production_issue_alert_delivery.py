"""Add durable retry state for production issue alert delivery.

Revision ID: durable_issue_alert_delivery
Revises: durable_approval_execution
Create Date: 2026-07-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "durable_issue_alert_delivery"
down_revision: str | Sequence[str] | None = "durable_approval_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ISSUE_TABLE = "production_issues"
DELIVERY_TABLE = "production_issue_alert_deliveries"


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_names(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _check_constraint_names(table_name: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def _has_unique_constraint(table_name: str, columns: list[str]) -> bool:
    return any(
        constraint.get("column_names") == columns
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
    )


def _has_foreign_key(table_name: str, columns: list[str], referred_table: str) -> bool:
    return any(
        foreign_key.get("constrained_columns") == columns
        and foreign_key.get("referred_table") == referred_table
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    )


def upgrade() -> None:
    # The initial bootstrap revision reflects current ORM metadata. On a fresh
    # database these objects already exist; on an in-place production upgrade
    # they do not. Inspect before DDL so both paths are first-class and tested.
    columns = _column_names(ISSUE_TABLE)
    additions = {
        "alert_epoch": sa.Column(
            "alert_epoch",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        "alert_attempts": sa.Column(
            "alert_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        "alert_next_attempt_at": sa.Column(
            "alert_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "alert_last_error_code": sa.Column(
            "alert_last_error_code",
            sa.String(length=100),
            nullable=True,
        ),
        "alert_notification_sent_at": sa.Column(
            "alert_notification_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    }
    for column_name, column in additions.items():
        if column_name not in columns:
            op.add_column(ISSUE_TABLE, column)

    op.execute(
        "UPDATE production_issues SET alert_epoch = 1 WHERE alert_epoch IS NULL"
    )
    op.execute(
        "UPDATE production_issues SET alert_attempts = 0 "
        "WHERE alert_attempts IS NULL"
    )
    op.alter_column(
        ISSUE_TABLE,
        "alert_epoch",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
    )
    op.alter_column(
        ISSUE_TABLE,
        "alert_attempts",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )

    if "ix_production_issues_alert_retry" not in _index_names(ISSUE_TABLE):
        op.create_index(
            "ix_production_issues_alert_retry",
            ISSUE_TABLE,
            ["alert_next_attempt_at", "last_seen_at"],
            postgresql_where=sa.text("status = 'open' AND alerted_at IS NULL"),
        )
    issue_checks = _check_constraint_names(ISSUE_TABLE)
    if "ck_production_issue_alert_attempts_nonnegative" not in issue_checks:
        op.create_check_constraint(
            "ck_production_issue_alert_attempts_nonnegative",
            ISSUE_TABLE,
            "alert_attempts >= 0",
        )
    if "ck_production_issue_alert_epoch_positive" not in issue_checks:
        op.create_check_constraint(
            "ck_production_issue_alert_epoch_positive",
            ISSUE_TABLE,
            "alert_epoch > 0",
        )

    if not _table_exists(DELIVERY_TABLE):
        op.create_table(
            DELIVERY_TABLE,
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "issue_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column("alert_epoch", sa.Integer(), nullable=False),
            sa.Column("sink", sa.String(length=32), nullable=False),
            sa.Column("idempotency_key", sa.String(length=160), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column(
                "payload_snapshot",
                postgresql.JSON(astext_type=sa.Text()),
                server_default=sa.text("'{}'::json"),
                nullable=False,
            ),
            sa.Column(
                "attempts",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "claim_token",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(length=100), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["issue_id"],
                ["production_issues.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "idempotency_key",
                name="uq_production_issue_alert_delivery_idempotency",
            ),
            sa.UniqueConstraint(
                "issue_id",
                "alert_epoch",
                "sink",
                name="uq_production_issue_alert_delivery_epoch_sink",
            ),
            sa.CheckConstraint(
                "attempts >= 0",
                name="ck_production_issue_alert_delivery_attempts_nonnegative",
            ),
            sa.CheckConstraint(
                "alert_epoch > 0",
                name="ck_production_issue_alert_delivery_epoch_positive",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'delivering', 'delivered')",
                name="ck_production_issue_alert_delivery_status",
            ),
            sa.CheckConstraint(
                "(status = 'pending' AND claim_token IS NULL "
                "AND claimed_at IS NULL AND delivered_at IS NULL) OR "
                "(status = 'delivering' AND claim_token IS NOT NULL "
                "AND claimed_at IS NOT NULL AND delivered_at IS NULL) OR "
                "(status = 'delivered' AND claim_token IS NULL "
                "AND claimed_at IS NULL AND delivered_at IS NOT NULL)",
                name="ck_production_issue_alert_delivery_state",
            ),
        )
    else:
        # ORM create_all supplies client-side defaults only. Persist equivalent
        # server defaults so inserts from SQL, migrations, and workers behave
        # identically on fresh and upgraded databases.
        op.execute(
            "UPDATE production_issue_alert_deliveries "
            "SET status = 'pending' WHERE status IS NULL"
        )
        op.execute(
            "UPDATE production_issue_alert_deliveries "
            "SET payload_snapshot = '{}'::json WHERE payload_snapshot IS NULL"
        )
        op.execute(
            "UPDATE production_issue_alert_deliveries "
            "SET attempts = 0 WHERE attempts IS NULL"
        )
        op.alter_column(
            DELIVERY_TABLE,
            "status",
            existing_type=sa.String(length=24),
            nullable=False,
            server_default=sa.text("'pending'"),
        )
        op.alter_column(
            DELIVERY_TABLE,
            "payload_snapshot",
            existing_type=postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        )
        op.alter_column(
            DELIVERY_TABLE,
            "attempts",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        if not _has_foreign_key(DELIVERY_TABLE, ["issue_id"], ISSUE_TABLE):
            op.create_foreign_key(
                "fk_production_issue_alert_delivery_issue",
                DELIVERY_TABLE,
                ISSUE_TABLE,
                ["issue_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if not _has_unique_constraint(DELIVERY_TABLE, ["idempotency_key"]):
            op.create_unique_constraint(
                "uq_production_issue_alert_delivery_idempotency",
                DELIVERY_TABLE,
                ["idempotency_key"],
            )
        if not _has_unique_constraint(
            DELIVERY_TABLE,
            ["issue_id", "alert_epoch", "sink"],
        ):
            op.create_unique_constraint(
                "uq_production_issue_alert_delivery_epoch_sink",
                DELIVERY_TABLE,
                ["issue_id", "alert_epoch", "sink"],
            )
        delivery_checks = _check_constraint_names(DELIVERY_TABLE)
        checks = {
            "ck_production_issue_alert_delivery_attempts_nonnegative": "attempts >= 0",
            "ck_production_issue_alert_delivery_epoch_positive": "alert_epoch > 0",
            "ck_production_issue_alert_delivery_status": (
                "status IN ('pending', 'delivering', 'delivered')"
            ),
            "ck_production_issue_alert_delivery_state": (
                "(status = 'pending' AND claim_token IS NULL "
                "AND claimed_at IS NULL AND delivered_at IS NULL) OR "
                "(status = 'delivering' AND claim_token IS NOT NULL "
                "AND claimed_at IS NOT NULL AND delivered_at IS NULL) OR "
                "(status = 'delivered' AND claim_token IS NULL "
                "AND claimed_at IS NULL AND delivered_at IS NOT NULL)"
            ),
        }
        for constraint_name, condition in checks.items():
            if constraint_name not in delivery_checks:
                op.create_check_constraint(
                    constraint_name,
                    DELIVERY_TABLE,
                    condition,
                )

    delivery_indexes = _index_names(DELIVERY_TABLE)
    if "ix_production_issue_alert_delivery_due" not in delivery_indexes:
        op.create_index(
            "ix_production_issue_alert_delivery_due",
            DELIVERY_TABLE,
            ["status", "next_attempt_at"],
        )
    if "ix_production_issue_alert_deliveries_issue_id" not in delivery_indexes:
        op.create_index(
            "ix_production_issue_alert_deliveries_issue_id",
            DELIVERY_TABLE,
            ["issue_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_production_issue_alert_deliveries_issue_id",
        table_name="production_issue_alert_deliveries",
    )
    op.drop_index(
        "ix_production_issue_alert_delivery_due",
        table_name="production_issue_alert_deliveries",
    )
    op.drop_table("production_issue_alert_deliveries")
    op.drop_constraint(
        "ck_production_issue_alert_epoch_positive",
        "production_issues",
        type_="check",
    )
    op.drop_constraint(
        "ck_production_issue_alert_attempts_nonnegative",
        "production_issues",
        type_="check",
    )
    op.drop_index(
        "ix_production_issues_alert_retry",
        table_name="production_issues",
    )
    op.drop_column("production_issues", "alert_notification_sent_at")
    op.drop_column("production_issues", "alert_last_error_code")
    op.drop_column("production_issues", "alert_next_attempt_at")
    op.drop_column("production_issues", "alert_attempts")
    op.drop_column("production_issues", "alert_epoch")
