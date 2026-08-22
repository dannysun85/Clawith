"""Add durable system-cost receipts for Group Planning LLM calls.

Revision ID: planning_system_costs
Revises: task_result_reviews
Create Date: 2026-08-22 14:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "planning_system_costs"
down_revision: str | Sequence[str] | None = "task_result_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "llm_system_cost_receipts"
GROUP_TENANT_CONSTRAINT = "uq_groups_tenant_id_id"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_names() -> set[str]:
    return set(_inspector().get_table_names())


def _unique_constraint_names(table_name: str) -> set[str]:
    return {
        str(item["name"])
        for item in _inspector().get_unique_constraints(table_name)
        if item.get("name")
    }


def _named_contracts(method: str, table_name: str) -> set[str]:
    inspector_method = getattr(_inspector(), method)
    return {
        str(item["name"])
        for item in inspector_method(table_name)
        if item.get("name")
    }


def _assert_existing_receipt_table_compatible() -> None:
    required_columns = {
        "id",
        "tenant_id",
        "group_id",
        "session_id",
        "run_id",
        "call_index",
        "operation",
        "model_id",
        "credential_id",
        "provider",
        "model",
        "provider_service_tier",
        "request_fingerprint",
        "status",
        "provider_outcome",
        "usage_source",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "estimated_tokens",
        "system_cost_credits",
        "cost_status",
        "response_snapshot",
        "response_fingerprint",
        "reconciliation_error_code",
        "provider_accepted_at",
        "finalized_at",
        "created_at",
        "updated_at",
    }
    actual_columns = {
        str(item["name"]) for item in _inspector().get_columns(TABLE_NAME)
    }
    missing = sorted(required_columns - actual_columns)
    if missing:
        raise RuntimeError(
            "Incompatible pre-existing llm_system_cost_receipts; missing columns: "
            + ", ".join(missing)
        )
    if "uq_llm_system_cost_receipts_run_call" not in _unique_constraint_names(
        TABLE_NAME
    ):
        raise RuntimeError(
            "Incompatible pre-existing llm_system_cost_receipts; missing run/call uniqueness"
        )
    expected_checks = {
        "ck_llm_system_cost_receipts_operation",
        "ck_llm_system_cost_receipts_status",
        "ck_llm_system_cost_receipts_provider_outcome",
        "ck_llm_system_cost_receipts_usage_source",
        "ck_llm_system_cost_receipts_cost_status",
        "ck_llm_system_cost_receipts_call_index_positive",
        "ck_llm_system_cost_receipts_tokens_nonnegative",
        "ck_llm_system_cost_receipts_cost_nonnegative",
        "ck_llm_system_cost_receipts_finalized_shape",
    }
    expected_foreign_keys = {
        "fk_llm_system_cost_receipts_tenant_run_agent_runs",
        "fk_llm_system_cost_receipts_tenant_session_chat_sessions",
        "fk_llm_system_cost_receipts_tenant_group_groups",
        "fk_llm_system_cost_receipts_model_id_llm_models",
    }
    expected_indexes = {
        "ix_llm_system_cost_receipts_tenant_created",
        "ix_llm_system_cost_receipts_group_created",
        "ix_llm_system_cost_receipts_status_updated",
    }
    missing_contracts = sorted(
        (expected_checks - _named_contracts("get_check_constraints", TABLE_NAME))
        | (expected_foreign_keys - _named_contracts("get_foreign_keys", TABLE_NAME))
        | (expected_indexes - _named_contracts("get_indexes", TABLE_NAME))
    )
    primary_key = _inspector().get_pk_constraint(TABLE_NAME).get("name")
    if primary_key != "pk_llm_system_cost_receipts":
        missing_contracts.append("pk_llm_system_cost_receipts")
    if missing_contracts:
        raise RuntimeError(
            "Incompatible pre-existing llm_system_cost_receipts; missing constraints: "
            + ", ".join(sorted(set(missing_contracts)))
        )


def upgrade() -> None:
    if GROUP_TENANT_CONSTRAINT not in _unique_constraint_names("groups"):
        op.create_unique_constraint(
            GROUP_TENANT_CONSTRAINT,
            "groups",
            ["tenant_id", "id"],
        )

    if TABLE_NAME in _table_names():
        _assert_existing_receipt_table_compatible()
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column(
            "operation",
            sa.String(length=32),
            server_default="group_planning",
            nullable=False,
        ),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column(
            "provider_service_tier",
            sa.String(length=24),
            server_default="standard",
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="provider_inflight",
            nullable=False,
        ),
        sa.Column(
            "provider_outcome",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "usage_source",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "cache_creation_tokens",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("estimated_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("system_cost_credits", sa.Integer(), nullable=True),
        sa.Column(
            "cost_status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("response_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("reconciliation_error_code", sa.String(length=100), nullable=True),
        sa.Column("provider_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "operation IN ('group_planning')",
            name="ck_llm_system_cost_receipts_operation",
        ),
        sa.CheckConstraint(
            "status IN ('provider_inflight', 'reconciling', 'finalized')",
            name="ck_llm_system_cost_receipts_status",
        ),
        sa.CheckConstraint(
            "provider_outcome IN ('pending', 'acceptance_unknown', 'accepted')",
            name="ck_llm_system_cost_receipts_provider_outcome",
        ),
        sa.CheckConstraint(
            "usage_source IN ('pending', 'provider_reported', 'estimated', 'unknown')",
            name="ck_llm_system_cost_receipts_usage_source",
        ),
        sa.CheckConstraint(
            "cost_status IN ('pending', 'priced', 'unpriced')",
            name="ck_llm_system_cost_receipts_cost_status",
        ),
        sa.CheckConstraint(
            "call_index > 0",
            name="ck_llm_system_cost_receipts_call_index_positive",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 "
            "AND cache_read_tokens >= 0 AND cache_creation_tokens >= 0 "
            "AND estimated_tokens >= 0",
            name="ck_llm_system_cost_receipts_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "system_cost_credits IS NULL OR system_cost_credits >= 0",
            name="ck_llm_system_cost_receipts_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "status <> 'finalized' OR (provider_outcome = 'accepted' "
            "AND usage_source <> 'pending' AND cost_status <> 'pending' "
            "AND finalized_at IS NOT NULL)",
            name="ck_llm_system_cost_receipts_finalized_shape",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name="fk_llm_system_cost_receipts_tenant_run_agent_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.id"],
            name="fk_llm_system_cost_receipts_tenant_session_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_llm_system_cost_receipts_tenant_group_groups",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["llm_models.id"],
            name="fk_llm_system_cost_receipts_model_id_llm_models",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_system_cost_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "call_index",
            name="uq_llm_system_cost_receipts_run_call",
        ),
    )
    op.create_index(
        "ix_llm_system_cost_receipts_tenant_created",
        TABLE_NAME,
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_system_cost_receipts_group_created",
        TABLE_NAME,
        ["group_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_system_cost_receipts_status_updated",
        TABLE_NAME,
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    if TABLE_NAME in _table_names():
        op.drop_table(TABLE_NAME)
    # ``groups(tenant_id, id)`` is now part of the canonical Group-domain
    # schema as well as the ORM contract. Keep it when this receipt table is
    # downgraded; older databases that first received the constraint here are
    # thereby normalized to the same safe historical baseline as fresh ones.
