"""Add Planning cost budgets and evidence-backed reconciliation receipts.

Revision ID: planning_cost_controls
Revises: planning_system_costs
Create Date: 2026-08-22 16:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "planning_cost_controls"
down_revision: str | Sequence[str] | None = "planning_system_costs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RECEIPT_TABLE = "llm_system_cost_receipts"
RESOLUTION_TABLE = "llm_system_cost_resolutions"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_names() -> set[str]:
    return set(_inspector().get_table_names())


def _named_contracts(method: str, table_name: str) -> set[str]:
    inspector_method = getattr(_inspector(), method)
    return {
        str(item["name"])
        for item in inspector_method(table_name)
        if item.get("name")
    }


def _assert_existing_controls_compatible() -> None:
    """Accept the current metadata bootstrap only when every contract exists.

    ``initial_schema`` uses ``Base.metadata.create_all(checkfirst=True)`` for a
    fresh database, so a new checkout can already contain this revision's
    tables before Alembic reaches this revision. Existing production databases
    still take the incremental ALTER/CREATE path below.
    """

    receipt_columns = {
        str(item["name"]) for item in _inspector().get_columns(RECEIPT_TABLE)
    }
    resolution_columns = {
        str(item["name"]) for item in _inspector().get_columns(RESOLUTION_TABLE)
    }
    missing = sorted(
        {
            "budget_reservation_credits",
            "request_input_token_upper_bound",
            "request_max_output_tokens",
        }
        - receipt_columns
    )
    missing.extend(
        sorted(
            {
                "id",
                "receipt_id",
                "tenant_id",
                "actor_user_id",
                "idempotency_key_hash",
                "request_fingerprint",
                "action",
                "source",
                "evidence_ref",
                "reason",
                "previous_status",
                "resulting_status",
                "previous_provider_outcome",
                "resulting_provider_outcome",
                "reported_system_cost_credits",
                "created_at",
            }
            - resolution_columns
        )
    )
    required_receipt_checks = {
        "ck_llm_system_cost_receipts_budget_positive",
        "ck_llm_system_cost_receipts_reconciled_shape",
        "ck_llm_system_cost_receipts_voided_shape",
    }
    required_resolution_checks = {
        "ck_llm_system_cost_resolutions_action",
        "ck_llm_system_cost_resolutions_source",
        "ck_llm_system_cost_resolutions_statuses",
        "ck_llm_system_cost_resolutions_outcomes",
        "ck_llm_system_cost_resolutions_cost_nonnegative",
    }
    required_resolution_foreign_keys = {
        "fk_llm_system_cost_resolutions_tenant_receipt",
        "fk_llm_system_cost_resolutions_actor_user_id_users",
    }
    required_resolution_indexes = {
        "ix_llm_system_cost_resolutions_tenant_created",
        "ix_llm_system_cost_resolutions_receipt_created",
    }
    missing.extend(
        sorted(
            required_receipt_checks
            - _named_contracts("get_check_constraints", RECEIPT_TABLE)
        )
    )
    missing.extend(
        sorted(
            required_resolution_checks
            - _named_contracts("get_check_constraints", RESOLUTION_TABLE)
        )
    )
    missing.extend(
        sorted(
            required_resolution_foreign_keys
            - _named_contracts("get_foreign_keys", RESOLUTION_TABLE)
        )
    )
    missing.extend(
        sorted(
            required_resolution_indexes
            - _named_contracts("get_indexes", RESOLUTION_TABLE)
        )
    )
    if "uq_llm_system_cost_receipts_tenant_id_id" not in _named_contracts(
        "get_unique_constraints", RECEIPT_TABLE
    ):
        missing.append("uq_llm_system_cost_receipts_tenant_id_id")
    if "uq_llm_system_cost_resolutions_idempotency" not in _named_contracts(
        "get_unique_constraints", RESOLUTION_TABLE
    ):
        missing.append("uq_llm_system_cost_resolutions_idempotency")
    if _inspector().get_pk_constraint(RESOLUTION_TABLE).get("name") != (
        "pk_llm_system_cost_resolutions"
    ):
        missing.append("pk_llm_system_cost_resolutions")
    if missing:
        raise RuntimeError(
            "Incompatible pre-existing Planning cost controls; missing contracts: "
            + ", ".join(sorted(set(missing)))
        )


def upgrade() -> None:
    if RESOLUTION_TABLE in _table_names():
        _assert_existing_controls_compatible()
        return

    receipt_columns = {
        str(item["name"]) for item in _inspector().get_columns(RECEIPT_TABLE)
    }
    new_columns = (
        sa.Column(
            "budget_reservation_credits",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "request_input_token_upper_bound",
            sa.BigInteger(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "request_max_output_tokens",
            sa.BigInteger(),
            server_default="1",
            nullable=False,
        ),
    )
    for column in new_columns:
        if column.name not in receipt_columns:
            op.add_column(RECEIPT_TABLE, column)

    for constraint_name in (
        "ck_llm_system_cost_receipts_status",
        "ck_llm_system_cost_receipts_provider_outcome",
        "ck_llm_system_cost_receipts_usage_source",
        "ck_llm_system_cost_receipts_cost_status",
        "ck_llm_system_cost_receipts_finalized_shape",
    ):
        op.drop_constraint(constraint_name, RECEIPT_TABLE, type_="check")

    op.create_check_constraint(
        "ck_llm_system_cost_receipts_status",
        RECEIPT_TABLE,
        "status IN ('provider_inflight', 'reconciling', 'finalized', "
        "'reconciled', 'voided')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_provider_outcome",
        RECEIPT_TABLE,
        "provider_outcome IN ('pending', 'acceptance_unknown', 'accepted', "
        "'not_accepted')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_usage_source",
        RECEIPT_TABLE,
        "usage_source IN ('pending', 'provider_reported', 'estimated', "
        "'operator_reported', 'unknown')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_cost_status",
        RECEIPT_TABLE,
        "cost_status IN ('pending', 'priced', 'unpriced', 'not_applicable')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_finalized_shape",
        RECEIPT_TABLE,
        "status <> 'finalized' OR (provider_outcome = 'accepted' "
        "AND usage_source <> 'pending' AND cost_status <> 'pending' "
        "AND finalized_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_budget_positive",
        RECEIPT_TABLE,
        "budget_reservation_credits > 0 AND request_input_token_upper_bound > 0 "
        "AND request_max_output_tokens > 0",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_reconciled_shape",
        RECEIPT_TABLE,
        "status <> 'reconciled' OR (provider_outcome = 'accepted' "
        "AND usage_source = 'operator_reported' AND cost_status = 'priced' "
        "AND system_cost_credits IS NOT NULL AND finalized_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_voided_shape",
        RECEIPT_TABLE,
        "status <> 'voided' OR (provider_outcome = 'not_accepted' "
        "AND usage_source = 'unknown' AND cost_status = 'not_applicable' "
        "AND system_cost_credits = 0 AND finalized_at IS NOT NULL)",
    )
    if "uq_llm_system_cost_receipts_tenant_id_id" not in _named_contracts(
        "get_unique_constraints", RECEIPT_TABLE
    ):
        op.create_unique_constraint(
            "uq_llm_system_cost_receipts_tenant_id_id",
            RECEIPT_TABLE,
            ["tenant_id", "id"],
        )

    op.create_table(
        RESOLUTION_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_ref", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=False),
        sa.Column("resulting_status", sa.String(length=24), nullable=False),
        sa.Column("previous_provider_outcome", sa.String(length=24), nullable=False),
        sa.Column("resulting_provider_outcome", sa.String(length=24), nullable=False),
        sa.Column("reported_system_cost_credits", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('mark_stale_unknown', 'confirm_not_accepted', "
            "'settle_accepted')",
            name="ck_llm_system_cost_resolutions_action",
        ),
        sa.CheckConstraint(
            "source IN ('operator', 'daemon')",
            name="ck_llm_system_cost_resolutions_source",
        ),
        sa.CheckConstraint(
            "previous_status IN ('provider_inflight', 'reconciling') AND "
            "resulting_status IN ('reconciling', 'reconciled', 'voided')",
            name="ck_llm_system_cost_resolutions_statuses",
        ),
        sa.CheckConstraint(
            "previous_provider_outcome IN ('pending', 'acceptance_unknown') AND "
            "resulting_provider_outcome IN ('acceptance_unknown', 'accepted', "
            "'not_accepted')",
            name="ck_llm_system_cost_resolutions_outcomes",
        ),
        sa.CheckConstraint(
            "reported_system_cost_credits IS NULL OR "
            "reported_system_cost_credits >= 0",
            name="ck_llm_system_cost_resolutions_cost_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "receipt_id"],
            [f"{RECEIPT_TABLE}.tenant_id", f"{RECEIPT_TABLE}.id"],
            name="fk_llm_system_cost_resolutions_tenant_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_llm_system_cost_resolutions_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_system_cost_resolutions"),
        sa.UniqueConstraint(
            "receipt_id",
            "idempotency_key_hash",
            name="uq_llm_system_cost_resolutions_idempotency",
        ),
    )
    op.create_index(
        "ix_llm_system_cost_resolutions_tenant_created",
        RESOLUTION_TABLE,
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_system_cost_resolutions_receipt_created",
        RESOLUTION_TABLE,
        ["receipt_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table(RESOLUTION_TABLE)
    op.drop_constraint(
        "uq_llm_system_cost_receipts_tenant_id_id",
        RECEIPT_TABLE,
        type_="unique",
    )
    for constraint_name in (
        "ck_llm_system_cost_receipts_voided_shape",
        "ck_llm_system_cost_receipts_reconciled_shape",
        "ck_llm_system_cost_receipts_budget_positive",
        "ck_llm_system_cost_receipts_finalized_shape",
        "ck_llm_system_cost_receipts_cost_status",
        "ck_llm_system_cost_receipts_usage_source",
        "ck_llm_system_cost_receipts_provider_outcome",
        "ck_llm_system_cost_receipts_status",
    ):
        op.drop_constraint(constraint_name, RECEIPT_TABLE, type_="check")

    op.create_check_constraint(
        "ck_llm_system_cost_receipts_status",
        RECEIPT_TABLE,
        "status IN ('provider_inflight', 'reconciling', 'finalized')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_provider_outcome",
        RECEIPT_TABLE,
        "provider_outcome IN ('pending', 'acceptance_unknown', 'accepted')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_usage_source",
        RECEIPT_TABLE,
        "usage_source IN ('pending', 'provider_reported', 'estimated', 'unknown')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_cost_status",
        RECEIPT_TABLE,
        "cost_status IN ('pending', 'priced', 'unpriced')",
    )
    op.create_check_constraint(
        "ck_llm_system_cost_receipts_finalized_shape",
        RECEIPT_TABLE,
        "status <> 'finalized' OR (provider_outcome = 'accepted' "
        "AND usage_source <> 'pending' AND cost_status <> 'pending' "
        "AND finalized_at IS NOT NULL)",
    )
    op.drop_column(RECEIPT_TABLE, "request_max_output_tokens")
    op.drop_column(RECEIPT_TABLE, "request_input_token_upper_bound")
    op.drop_column(RECEIPT_TABLE, "budget_reservation_credits")
