"""Attribute every production alert delivery to the worker that completed it.

Revision ID: alert_worker_attribution
Revises: channel_secret_envelopes
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "alert_worker_attribution"
down_revision: str | Sequence[str] | None = "channel_secret_envelopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DELIVERY_TABLE = "production_issue_alert_deliveries"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(DELIVERY_TABLE)
    }


def _check_constraint_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(DELIVERY_TABLE)
        if constraint.get("name")
    }


def upgrade() -> None:
    additions = {
        "attribution_version": sa.Column(
            "attribution_version",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        "claim_worker_actor_id": sa.Column(
            "claim_worker_actor_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        "claim_worker_release_id": sa.Column(
            "claim_worker_release_id",
            sa.String(length=160),
            nullable=True,
        ),
        "claim_worker_release_commit": sa.Column(
            "claim_worker_release_commit",
            sa.String(length=64),
            nullable=True,
        ),
        "delivered_by_worker_actor_id": sa.Column(
            "delivered_by_worker_actor_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        "delivered_by_release_id": sa.Column(
            "delivered_by_release_id",
            sa.String(length=160),
            nullable=True,
        ),
        "delivered_by_release_commit": sa.Column(
            "delivered_by_release_commit",
            sa.String(length=64),
            nullable=True,
        ),
    }
    columns = _column_names()
    for column_name, column in additions.items():
        if column_name not in columns:
            op.add_column(DELIVERY_TABLE, column)

    op.execute(
        "UPDATE production_issue_alert_deliveries "
        "SET attribution_version = 0 WHERE attribution_version IS NULL"
    )
    op.alter_column(
        DELIVERY_TABLE,
        "attribution_version",
        existing_type=sa.SmallInteger(),
        nullable=False,
        server_default=sa.text("0"),
    )

    existing_checks = _check_constraint_names()
    for constraint_name in (
        "ck_production_issue_alert_delivery_attribution_version",
        "ck_production_issue_alert_delivery_attribution",
        "ck_production_issue_alert_delivery_state",
        "ck_production_issue_alert_delivery_status",
    ):
        if constraint_name in existing_checks:
            op.drop_constraint(
                constraint_name,
                DELIVERY_TABLE,
                type_="check",
            )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_status",
        DELIVERY_TABLE,
        "status IN ('pending', 'delivering', 'delivered', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_state",
        DELIVERY_TABLE,
        "(status = 'pending' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND delivered_at IS NULL) OR "
        "(status = 'delivering' AND claim_token IS NOT NULL "
        "AND claimed_at IS NOT NULL AND delivered_at IS NULL) OR "
        "(status = 'delivered' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND delivered_at IS NOT NULL) OR "
        "(status = 'cancelled' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND delivered_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_attribution",
        DELIVERY_TABLE,
        "attribution_version = 0 OR (attribution_version = 1 AND ("
        "(status IN ('pending', 'cancelled') "
        "AND claim_worker_actor_id IS NULL "
        "AND claim_worker_release_id IS NULL "
        "AND claim_worker_release_commit IS NULL "
        "AND delivered_by_worker_actor_id IS NULL "
        "AND delivered_by_release_id IS NULL "
        "AND delivered_by_release_commit IS NULL) OR "
        "(status = 'delivering' "
        "AND claim_worker_actor_id IS NOT NULL "
        "AND claim_worker_release_id IS NOT NULL "
        "AND claim_worker_release_commit IS NOT NULL "
        "AND delivered_by_worker_actor_id IS NULL "
        "AND delivered_by_release_id IS NULL "
        "AND delivered_by_release_commit IS NULL) OR "
        "(status = 'delivered' "
        "AND claim_worker_actor_id IS NULL "
        "AND claim_worker_release_id IS NULL "
        "AND claim_worker_release_commit IS NULL "
        "AND delivered_by_worker_actor_id IS NOT NULL "
        "AND delivered_by_release_id IS NOT NULL "
        "AND delivered_by_release_commit IS NOT NULL)))",
    )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_attribution_version",
        DELIVERY_TABLE,
        "attribution_version IN (0, 1)",
    )


def downgrade() -> None:
    # A cancelled row represents an obsolete epoch, not a successful delivery.
    # Returning it to pending preserves that truth under the legacy state model.
    op.execute(
        "UPDATE production_issue_alert_deliveries "
        "SET status = 'pending' WHERE status = 'cancelled'"
    )
    op.drop_constraint(
        "ck_production_issue_alert_delivery_attribution_version",
        DELIVERY_TABLE,
        type_="check",
    )
    op.drop_constraint(
        "ck_production_issue_alert_delivery_attribution",
        DELIVERY_TABLE,
        type_="check",
    )
    op.drop_constraint(
        "ck_production_issue_alert_delivery_state",
        DELIVERY_TABLE,
        type_="check",
    )
    op.drop_constraint(
        "ck_production_issue_alert_delivery_status",
        DELIVERY_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_status",
        DELIVERY_TABLE,
        "status IN ('pending', 'delivering', 'delivered')",
    )
    op.create_check_constraint(
        "ck_production_issue_alert_delivery_state",
        DELIVERY_TABLE,
        "(status = 'pending' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND delivered_at IS NULL) OR "
        "(status = 'delivering' AND claim_token IS NOT NULL "
        "AND claimed_at IS NOT NULL AND delivered_at IS NULL) OR "
        "(status = 'delivered' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND delivered_at IS NOT NULL)",
    )
    op.drop_column(DELIVERY_TABLE, "delivered_by_release_commit")
    op.drop_column(DELIVERY_TABLE, "delivered_by_release_id")
    op.drop_column(DELIVERY_TABLE, "delivered_by_worker_actor_id")
    op.drop_column(DELIVERY_TABLE, "claim_worker_release_commit")
    op.drop_column(DELIVERY_TABLE, "claim_worker_release_id")
    op.drop_column(DELIVERY_TABLE, "claim_worker_actor_id")
    op.drop_column(DELIVERY_TABLE, "attribution_version")
