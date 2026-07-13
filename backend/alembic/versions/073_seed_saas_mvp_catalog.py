"""Seed SaaS MVP catalog defaults.

Revision ID: seed_saas_mvp_catalog
Revises: repair_agent_openclaw_last_seen
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "seed_saas_mvp_catalog"
down_revision: Union[str, Sequence[str], None] = "repair_agent_openclaw_last_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exec(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _exec(
        """
        INSERT INTO plans (
            id, code, name, tier, period, price_cents, currency,
            max_agents, max_llm_calls_per_day, message_limit, message_period,
            max_triggers, credits_per_period, allowed_modalities, allowed_tiers,
            features, is_active, sort_order
        )
        VALUES
            (
                gen_random_uuid(), 'free', 'Free', 0, 'permanent', 0, 'USD',
                1, 1000, 50, 'permanent',
                20, 1000, '["text"]'::jsonb, '["lite"]'::jsonb,
                '{"display_name":"免费版","monthly_price_cents":0,"yearly_price_cents":0,"yearly_discount_percent":20}'::jsonb,
                true, 0
            ),
            (
                gen_random_uuid(), 'starter', 'Starter', 1, 'monthly', 2000, 'USD',
                10, 5000, 200, 'monthly',
                100, 20000, '["text"]'::jsonb, '["lite","pro"]'::jsonb,
                '{"display_name":"入门版","monthly_price_cents":2000,"monthly_original_price_cents":2500,"yearly_price_cents":19200,"yearly_original_price_cents":24000,"yearly_discount_percent":20}'::jsonb,
                true, 1
            ),
            (
                gen_random_uuid(), 'pro', 'Pro', 2, 'monthly', 16000, 'USD',
                15, 50000, 1000, 'monthly',
                500, 175000, '["text","image","vision"]'::jsonb, '["lite","pro","ultra"]'::jsonb,
                '{"display_name":"专业版","monthly_price_cents":16000,"monthly_original_price_cents":20000,"yearly_price_cents":153600,"yearly_original_price_cents":192000,"yearly_discount_percent":20,"recommended":true,"boost_discount_percent":10}'::jsonb,
                true, 2
            ),
            (
                gen_random_uuid(), 'scale', 'Scale', 3, 'monthly', 160000, 'USD',
                50, 500000, 10000, 'monthly',
                5000, 1800000, '["text","image","vision","audio","voice","tts","video","music"]'::jsonb, '["lite","pro","ultra"]'::jsonb,
                '{"display_name":"规模版","monthly_price_cents":160000,"monthly_original_price_cents":200000,"yearly_price_cents":1536000,"yearly_original_price_cents":1920000,"yearly_discount_percent":20,"boost_discount_percent":20}'::jsonb,
                true, 3
            )
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            tier = EXCLUDED.tier,
            period = EXCLUDED.period,
            price_cents = EXCLUDED.price_cents,
            currency = EXCLUDED.currency,
            max_agents = EXCLUDED.max_agents,
            max_llm_calls_per_day = EXCLUDED.max_llm_calls_per_day,
            message_limit = EXCLUDED.message_limit,
            message_period = EXCLUDED.message_period,
            max_triggers = EXCLUDED.max_triggers,
            credits_per_period = EXCLUDED.credits_per_period,
            allowed_modalities = EXCLUDED.allowed_modalities,
            allowed_tiers = EXCLUDED.allowed_tiers,
            features = EXCLUDED.features,
            is_active = EXCLUDED.is_active,
            sort_order = EXCLUDED.sort_order,
            updated_at = now()
        """
    )

    _exec(
        """
        INSERT INTO credit_packs (
            id, code, name, credits, price_cents, currency, applicable_plan_ids,
            is_active, sort_order
        )
        VALUES
            (
                gen_random_uuid(), 'boost_10k', '10,000 Credits',
                10000, 1500, 'USD', NULL, true, 0
            ),
            (
                gen_random_uuid(), 'boost_50k', '50,000 Credits',
                50000, 7000, 'USD', NULL, true, 1
            ),
            (
                gen_random_uuid(), 'boost_200k', '200,000 Credits',
                200000, 26000, 'USD', NULL, true, 2
            )
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            credits = EXCLUDED.credits,
            price_cents = EXCLUDED.price_cents,
            currency = EXCLUDED.currency,
            applicable_plan_ids = EXCLUDED.applicable_plan_ids,
            is_active = EXCLUDED.is_active,
            sort_order = EXCLUDED.sort_order
        """
    )

    _exec(
        """
        INSERT INTO billing_rules (id, action, modality, tier, unit, credit_cost, enabled, priority)
        VALUES
            (gen_random_uuid(), 'chat', 'text', 'lite', 'call', 1, true, 10),
            (gen_random_uuid(), 'chat', 'text', 'pro', 'call', 2, true, 10),
            (gen_random_uuid(), 'chat', 'text', 'ultra', 'call', 5, true, 10),
            (gen_random_uuid(), 'heartbeat', 'text', 'lite', 'call', 3, true, 10),
            (gen_random_uuid(), 'heartbeat', 'text', 'pro', 'call', 3, true, 10),
            (gen_random_uuid(), 'heartbeat', 'text', 'ultra', 'call', 4, true, 10),
            (gen_random_uuid(), 'image', 'image', 'lite', 'call', 2, true, 10),
            (gen_random_uuid(), 'image', 'image', 'pro', 'call', 5, true, 10),
            (gen_random_uuid(), 'image', 'image', 'ultra', 'call', 10, true, 10),
            (gen_random_uuid(), 'video', 'video', 'pro', 'call', 50, true, 10),
            (gen_random_uuid(), 'video', 'video', 'ultra', 'call', 100, true, 10),
            (gen_random_uuid(), 'tts', 'tts', 'pro', 'call', 5, true, 10),
            (gen_random_uuid(), 'music', 'music', 'ultra', 'call', 50, true, 10)
        ON CONFLICT (action, modality, tier, unit) DO UPDATE SET
            credit_cost = EXCLUDED.credit_cost,
            enabled = EXCLUDED.enabled,
            priority = EXCLUDED.priority
        """
    )


def downgrade() -> None:
    # Data seed migration: keep existing catalog rows intact on downgrade.
    pass
