"""Add MVP phase 1 subscription tables: billing rules, credit packs, model routes.

Revision ID: subscription_mvp_phase1
Revises: add_credential_rate_limits
Create Date: 2026-07-08

Implements SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §11:
- billing_rules: fixed credit cost per action/modality/tier
- credit_packs: Boost credit packs
- model_routes: Lite/Pro/Ultra + modality -> LLMModel routing
- agents.preferred_tier / preferred_modality
- credit_transactions audit fields
- data migration: basic/standard/premium -> lite/pro/ultra
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "subscription_mvp_phase1"
down_revision: Union[str, None] = "add_credential_rate_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(col["name"] == column_name for col in sa.inspect(op.get_bind()).get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _unique_constraint_exists(table_name: str, constraint_name: str, columns: list[str]) -> bool:
    if not _table_exists(table_name):
        return False
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name):
        if constraint["name"] == constraint_name:
            return True
        if constraint.get("column_names") == columns:
            return True
    return False


def upgrade() -> None:
    # ── billing_rules (计费规则) ─────────────────────────
    if not _table_exists("billing_rules"):
        op.create_table(
            "billing_rules",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("modality", sa.String(length=20), nullable=True),
            sa.Column("tier", sa.String(length=20), nullable=True),
            sa.Column("unit", sa.String(length=20), nullable=False, server_default="call"),
            sa.Column("credit_cost", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("action", "modality", "tier", "unit", name="uq_billing_rules_action_modality_tier_unit"),
        )
    elif not _unique_constraint_exists(
        "billing_rules",
        "uq_billing_rules_action_modality_tier_unit",
        ["action", "modality", "tier", "unit"],
    ):
        op.create_unique_constraint(
            "uq_billing_rules_action_modality_tier_unit",
            "billing_rules",
            ["action", "modality", "tier", "unit"],
        )
    if not _index_exists("billing_rules", "ix_billing_rules_lookup"):
        op.create_index("ix_billing_rules_lookup", "billing_rules", ["action", "modality", "tier", "enabled"])

    # ── credit_packs (Boost 额度包) ──────────────────────
    if not _table_exists("credit_packs"):
        op.create_table(
            "credit_packs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("credits", sa.Integer(), nullable=False),
            sa.Column("price_cents", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(length=10), nullable=False, server_default="CNY"),
            sa.Column("applicable_plan_ids", postgresql.JSONB(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("code", name="uq_credit_packs_code"),
        )

    # ── model_routes (档位→模型路由) ─────────────────────
    if not _table_exists("model_routes"):
        op.create_table(
            "model_routes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("saas_tier", sa.String(length=20), nullable=False),
            sa.Column("modality", sa.String(length=20), nullable=False),
            sa.Column("llm_model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("llm_models.id"), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fallback_route_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("model_routes.id"), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not _index_exists("model_routes", "ix_model_routes_lookup"):
        op.create_index("ix_model_routes_lookup", "model_routes", ["saas_tier", "modality", "enabled", "priority"])
    if not _index_exists("model_routes", "ix_model_routes_model_id"):
        op.create_index("ix_model_routes_model_id", "model_routes", ["llm_model_id"])

    # ── agents: preferred_tier / preferred_modality ──────
    if not _column_exists("agents", "preferred_tier"):
        op.add_column("agents", sa.Column("preferred_tier", sa.String(length=20), nullable=True))
    if not _column_exists("agents", "preferred_modality"):
        op.add_column("agents", sa.Column("preferred_modality", sa.String(length=20), nullable=True, server_default="text"))

    # ── credit_transactions: audit fields ────────────────
    if not _column_exists("credit_transactions", "user_id"):
        op.add_column("credit_transactions", sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    if not _column_exists("credit_transactions", "agent_id"):
        op.add_column("credit_transactions", sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True))
    if not _column_exists("credit_transactions", "action"):
        op.add_column("credit_transactions", sa.Column("action", sa.String(length=50), nullable=True))
    if not _column_exists("credit_transactions", "modality"):
        op.add_column("credit_transactions", sa.Column("modality", sa.String(length=20), nullable=True))
    if not _column_exists("credit_transactions", "tier"):
        op.add_column("credit_transactions", sa.Column("tier", sa.String(length=20), nullable=True))
    if not _column_exists("credit_transactions", "provider"):
        op.add_column("credit_transactions", sa.Column("provider", sa.String(length=50), nullable=True))
    if not _column_exists("credit_transactions", "model"):
        op.add_column("credit_transactions", sa.Column("model", sa.String(length=100), nullable=True))
    if not _index_exists("credit_transactions", "ix_credit_transactions_user_id"):
        op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])
    if not _index_exists("credit_transactions", "ix_credit_transactions_agent_id"):
        op.create_index("ix_credit_transactions_agent_id", "credit_transactions", ["agent_id"])

    # ── data migration: tier mapping ─────────────────────
    # llm_models: basic->lite, standard->pro, premium->ultra
    op.execute(
        """
        UPDATE llm_models
        SET tier = CASE tier
            WHEN 'basic' THEN 'lite'
            WHEN 'standard' THEN 'pro'
            WHEN 'premium' THEN 'ultra'
            ELSE tier
        END
        """
    )

    # plans.allowed_tiers JSON mapping
    op.execute(
        """
        UPDATE plans
        SET allowed_tiers = (
            SELECT jsonb_agg(
                CASE value
                    WHEN 'basic' THEN 'lite'
                    WHEN 'standard' THEN 'pro'
                    WHEN 'premium' THEN 'ultra'
                    ELSE value
                END
            )
            FROM jsonb_array_elements_text(allowed_tiers::jsonb) AS value
        )
        WHERE allowed_tiers IS NOT NULL AND jsonb_array_length(allowed_tiers::jsonb) > 0
        """
    )

    # free plan seed: allowed_tiers=['lite']
    op.execute(
        """
        UPDATE plans
        SET allowed_modalities = '["text"]'::json,
            allowed_tiers = '["lite"]'::json,
            max_agents = 1
        WHERE code = 'free'
        """
    )

    # ensure every tenant has a credit balance row
    op.execute(
        """
        INSERT INTO credit_balances (tenant_id, balance, reserved)
        SELECT t.id, 0, 0
        FROM tenants t
        LEFT JOIN credit_balances cb ON cb.tenant_id = t.id
        WHERE cb.tenant_id IS NULL
        """
    )

    # seed default billing_rules (action/tier based costs)
    op.execute(
        """
        INSERT INTO billing_rules (id, action, modality, tier, unit, credit_cost, enabled, priority)
        VALUES
            (gen_random_uuid(), 'chat', 'text', 'lite', 'call', 1, true, 0),
            (gen_random_uuid(), 'chat', 'text', 'pro', 'call', 1, true, 0),
            (gen_random_uuid(), 'chat', 'text', 'ultra', 'call', 5, true, 0),
            (gen_random_uuid(), 'image', 'image', 'lite', 'call', 2, true, 0),
            (gen_random_uuid(), 'image', 'image', 'pro', 'call', 2, true, 0),
            (gen_random_uuid(), 'image', 'image', 'ultra', 'call', 10, true, 0)
        ON CONFLICT (action, modality, tier, unit) DO NOTHING
        """
    )

    # seed sample credit_packs
    op.execute(
        """
        INSERT INTO credit_packs (id, code, name, credits, price_cents, currency, is_active, sort_order)
        VALUES
            (gen_random_uuid(), 'boost_10k', '10,000 Credits', 10000, 10000, 'CNY', true, 0),
            (gen_random_uuid(), 'boost_50k', '50,000 Credits', 50000, 45000, 'CNY', true, 1),
            (gen_random_uuid(), 'boost_200k', '200,000 Credits', 200000, 160000, 'CNY', true, 2)
        ON CONFLICT (code) DO NOTHING
        """
    )

    # seed model_routes from existing platform llm_models (tenant_id IS NULL, enabled)
    # For each (tier, modality) group, pick the highest priority model (lowest id as tie-breaker)
    op.execute(
        """
        INSERT INTO model_routes (id, saas_tier, modality, llm_model_id, priority, enabled)
        SELECT gen_random_uuid(), m.tier, COALESCE(m.modality, 'text'), m.id, 0, true
        FROM (
            SELECT DISTINCT ON (tier, modality) tier, modality, id
            FROM llm_models
            WHERE tenant_id IS NULL AND enabled = true
            ORDER BY tier, modality, created_at ASC
        ) m
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_model_routes_model_id", table_name="model_routes")
    op.drop_index("ix_model_routes_lookup", table_name="model_routes")
    op.drop_table("model_routes")
    op.drop_table("credit_packs")
    op.drop_index("ix_billing_rules_lookup", table_name="billing_rules")
    op.drop_table("billing_rules")

    op.drop_index("ix_credit_transactions_agent_id", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_user_id", table_name="credit_transactions")
    op.drop_column("credit_transactions", "model")
    op.drop_column("credit_transactions", "provider")
    op.drop_column("credit_transactions", "tier")
    op.drop_column("credit_transactions", "modality")
    op.drop_column("credit_transactions", "action")
    op.drop_column("credit_transactions", "agent_id")
    op.drop_column("credit_transactions", "user_id")

    op.drop_column("agents", "preferred_modality")
    op.drop_column("agents", "preferred_tier")
