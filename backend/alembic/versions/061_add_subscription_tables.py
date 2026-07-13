"""Add subscription, billing, credit, and tenant_usage tables.

Revision ID: add_subscription_tables
Revises: add_agentbay_session_ledger
Create Date: 2026-07-06

Implements SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §3 (地基) + §7.2 (模型分类):
- plans / subscriptions / credit_balances / credit_transactions
- billing_profiles / payment_orders / tenant_usage
- llm_models += modality / modalities / tier / capabilities
- seed free plan
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_subscription_tables"
down_revision: Union[str, Sequence[str], None] = "add_agentbay_session_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # `initial_schema` bootstraps fresh databases from current metadata. When
    # that path has already materialized this revision's complete schema, only
    # the data seed below remains to be applied.
    required_tables = {
        "plans", "subscriptions", "credit_balances", "credit_transactions",
        "billing_profiles", "payment_orders", "tenant_usage",
    }
    if all(_table_exists(table_name) for table_name in required_tables):
        op.execute(
            """
            INSERT INTO plans (id, code, name, tier, period, price_cents, currency,
                max_agents, max_llm_calls_per_day, message_limit, message_period, max_triggers,
                credits_per_period, allowed_modalities, allowed_tiers, is_active, sort_order)
            VALUES (
                gen_random_uuid(), 'free', 'Free', 0, 'permanent', 0, 'CNY',
                1, 1000, 50, 'permanent', 20,
                0, '["text"]'::jsonb, '["standard"]'::jsonb, true, 0
            )
            ON CONFLICT (code) DO NOTHING
            """
        )
        return

    # ── plans (全局套餐定义) ─────────────────────────────
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("period", sa.String(length=20), nullable=False, server_default="monthly"),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="CNY"),
        sa.Column("max_agents", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_llm_calls_per_day", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("message_limit", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("message_period", sa.String(length=20), nullable=False, server_default="permanent"),
        sa.Column("max_triggers", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("credits_per_period", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_modalities", postgresql.JSONB(), nullable=True),
        sa.Column("allowed_tiers", postgresql.JSONB(), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("stripe_price_id", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )

    # ── subscriptions (租户订阅) ─────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("period_start", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stripe_sub_id", sa.String(length=200), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    # partial unique index: one active/trialing subscription per tenant (3.6)
    op.create_index(
        "uq_subscriptions_tenant_active",
        "subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'trialing')"),
    )

    # ── credit_balances (积分余额, 单行/租户) ────────────
    op.create_table(
        "credit_balances",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── credit_transactions (积分流水) ───────────────────
    op.create_table(
        "credit_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("ref_type", sa.String(length=50), nullable=True),
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_credit_transactions_tenant_id", "credit_transactions", ["tenant_id"])
    op.create_index("ix_credit_transactions_created_at", "credit_transactions", ["created_at"])

    # ── billing_profiles (发票资料, 单行/租户) ───────────
    op.create_table(
        "billing_profiles",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("tax_id", sa.String(length=100), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=50), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── payment_orders (支付订单) ────────────────────────
    op.create_table(
        "payment_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("credits", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="CNY"),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("provider_session_id", sa.String(length=255), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_orders_tenant_id", "payment_orders", ["tenant_id"])

    # ── tenant_usage (tenant 共享配额, 3.7) ──────────────
    op.create_table(
        "tenant_usage",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("period_date", sa.Date(), primary_key=True),
        sa.Column("llm_calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── llm_models 加 modality/tier 字段 (模块四 7.2) ─────
    op.add_column("llm_models", sa.Column("modality", sa.String(length=20), nullable=False, server_default="text"))
    op.add_column("llm_models", sa.Column("modalities", postgresql.JSONB(), nullable=True))
    op.add_column("llm_models", sa.Column("tier", sa.String(length=20), nullable=False, server_default="standard"))
    op.add_column("llm_models", sa.Column("capabilities", postgresql.JSONB(), nullable=True))
    # backfill modalities from supports_vision
    op.execute(
        "UPDATE llm_models SET modalities = '[\"text\",\"vision\"]'::jsonb "
        "WHERE supports_vision = true AND modalities IS NULL"
    )
    op.execute(
        "UPDATE llm_models SET modalities = '[\"text\"]'::jsonb "
        "WHERE supports_vision = false AND modalities IS NULL"
    )

    # ── seed free 套餐 (3.4 迁移策略, 等价现有 tenant 默认值) ──
    op.execute(
        """
        INSERT INTO plans (id, code, name, tier, period, price_cents, currency,
            max_agents, max_llm_calls_per_day, message_limit, message_period, max_triggers,
            credits_per_period, allowed_modalities, allowed_tiers, is_active, sort_order)
        VALUES (
            gen_random_uuid(), 'free', 'Free', 0, 'permanent', 0, 'CNY',
            1, 1000, 50, 'permanent', 20,
            0, '["text"]'::jsonb, '["standard"]'::jsonb, true, 0
        )
        ON CONFLICT (code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_column("llm_models", "capabilities")
    op.drop_column("llm_models", "tier")
    op.drop_column("llm_models", "modalities")
    op.drop_column("llm_models", "modality")
    op.drop_table("tenant_usage")
    op.drop_index("ix_payment_orders_tenant_id", table_name="payment_orders")
    op.drop_table("payment_orders")
    op.drop_table("billing_profiles")
    op.drop_index("ix_credit_transactions_created_at", table_name="credit_transactions")
    op.drop_index("ix_credit_transactions_tenant_id", table_name="credit_transactions")
    op.drop_table("credit_transactions")
    op.drop_index("uq_subscriptions_tenant_active", table_name="subscriptions")
    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
