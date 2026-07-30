"""Seed Volcengine Agent Plan text routes with MiniMax fallbacks.

Revision ID: seed_agent_plan_text_routes
Revises: add_provider_plan_tier
Create Date: 2026-07-26 15:00:00

The provider models use deterministic revision-owned IDs.  Each SaaS tier gets
one higher-priority Agent Plan text route whose fallback points to the
pre-existing highest-priority text route for the same tier.  Credential
capabilities remain administrator-owned and are never widened by this
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "seed_agent_plan_text_routes"
down_revision: str | Sequence[str] | None = "add_provider_plan_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SEED_REVISION = "seed_agent_plan_text_routes"
ROUTE_PRIORITY = 950
MODEL_IDS = {
    "lite": "10700000-0000-4000-8000-000000000001",
    "pro": "10700000-0000-4000-8000-000000000002",
    "ultra": "10700000-0000-4000-8000-000000000003",
}
ROUTE_IDS = {
    "lite": "10700000-0000-4000-8000-000000000101",
    "pro": "10700000-0000-4000-8000-000000000102",
    "ultra": "10700000-0000-4000-8000-000000000103",
}
TIER_SETTINGS = {
    "lite": {
        "model": "doubao-seed-2.0-mini",
        "label": "Doubao Seed 2.0 Mini Lite (Agent Plan)",
        "max_output_tokens": 2048,
    },
    "pro": {
        "model": "doubao-seed-2.1-turbo",
        "label": "Doubao Seed 2.1 Turbo Pro (Agent Plan)",
        "max_output_tokens": 4096,
    },
    "ultra": {
        "model": "doubao-seed-evolving",
        "label": "Doubao Seed Evolving Ultra (Agent Plan)",
        "max_output_tokens": 8192,
    },
}


def _exec(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _ensure_model(tier: str) -> None:
    model_id = MODEL_IDS[tier]
    settings = TIER_SETTINGS[tier]
    _exec(
        f"""
        DO $seed_agent_plan_model$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM llm_models
                WHERE id = '{model_id}'::uuid
                  AND COALESCE(capabilities::jsonb ->> 'seed_revision', '')
                      <> '{SEED_REVISION}'
            ) THEN
                RAISE EXCEPTION
                    'reserved Agent Plan model id {model_id} is administrator-owned';
            END IF;

            INSERT INTO llm_models (
                id, provider, model, api_key_encrypted, base_url, label,
                enabled, supports_vision, max_output_tokens,
                supports_tool_calling, tool_calling_capability_source,
                tool_calling_checked_at, modality, modalities, tier,
                capabilities, capability_source, capability_checked_at
            )
            VALUES (
                '{model_id}'::uuid,
                'volcengine_agent_plan',
                '{settings["model"]}',
                'platform-credential-pool',
                'https://ark.cn-beijing.volces.com/api/plan',
                '{settings["label"]}',
                true,
                false,
                {settings["max_output_tokens"]},
                true,
                'builtin_registry',
                now(),
                'text',
                '["text"]'::jsonb,
                '{tier}',
                '{{"stream":true,"tool_call":true,"seed_revision":"{SEED_REVISION}"}}'::jsonb,
                'builtin_registry',
                now()
            )
            ON CONFLICT (id) DO NOTHING;
        END $seed_agent_plan_model$;
        """
    )


def _ensure_route(tier: str) -> None:
    model_id = MODEL_IDS[tier]
    route_id = ROUTE_IDS[tier]
    _exec(
        f"""
        DO $seed_agent_plan_route$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM model_routes
                WHERE id = '{route_id}'::uuid
                  AND (
                    llm_model_id <> '{model_id}'::uuid
                    OR saas_tier <> '{tier}'
                    OR modality <> 'text'
                  )
            ) THEN
                RAISE EXCEPTION
                    'reserved Agent Plan route id {route_id} is administrator-owned';
            END IF;
        END $seed_agent_plan_route$;
        """
    )

    _exec(
        f"""
        WITH fallback AS (
            SELECT route.id
            FROM model_routes AS route
            JOIN llm_models AS model ON model.id = route.llm_model_id
            WHERE route.saas_tier = '{tier}'
              AND route.modality = 'text'
              AND route.enabled = true
              AND route.id <> '{route_id}'::uuid
              AND model.provider <> 'volcengine_agent_plan'
            ORDER BY route.priority DESC, route.created_at ASC
            LIMIT 1
        )
        INSERT INTO model_routes (
            id, saas_tier, modality, llm_model_id, priority,
            fallback_route_id, enabled
        )
        SELECT
            '{route_id}'::uuid,
            '{tier}',
            'text',
            '{model_id}'::uuid,
            {ROUTE_PRIORITY},
            fallback.id,
            true
        FROM fallback
        ON CONFLICT (id) DO NOTHING;
        """
    )


def upgrade() -> None:
    for tier in ("lite", "pro", "ultra"):
        _ensure_model(tier)
        _ensure_route(tier)


def downgrade() -> None:
    route_ids = ", ".join(f"'{value}'::uuid" for value in ROUTE_IDS.values())
    model_ids = ", ".join(f"'{value}'::uuid" for value in MODEL_IDS.values())

    _exec(
        f"""
        UPDATE model_routes
        SET fallback_route_id = NULL,
            updated_at = now()
        WHERE fallback_route_id IN ({route_ids})
          AND id NOT IN ({route_ids})
        """
    )
    _exec(f"DELETE FROM model_routes WHERE id IN ({route_ids})")
    _exec(
        f"""
        DELETE FROM llm_models
        WHERE id IN ({model_ids})
          AND COALESCE(capabilities::jsonb ->> 'seed_revision', '')
              = '{SEED_REVISION}'
        """
    )
