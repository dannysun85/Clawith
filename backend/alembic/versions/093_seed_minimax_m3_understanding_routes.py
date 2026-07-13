"""Route SaaS understanding inputs through isolated MiniMax-M3 models.

Revision ID: seed_minimax_m3_understanding
Revises: add_user_chat_tier_preference
Create Date: 2026-07-13

The M3 rows and routes are additive. Existing M2.x model rows, administrator
routes and billing choices remain in place so a downgrade can restore the
previous routing state without reconstructing overwritten data.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "seed_minimax_m3_understanding"
down_revision: str | Sequence[str] | None = "add_user_chat_tier_preference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MODALITIES = ("text", "image", "video")
ROUTE_PRIORITY = 930
BILLING_PRIORITY = 93
SEED_REVISION = "seed_minimax_m3_understanding"
TIER_SETTINGS = {
    "lite": {
        "max_output_tokens": 2048,
        "thinking": "disabled",
        "service_tier": "standard",
        "costs": {"text": 1, "image": 1, "video": 2},
    },
    "pro": {
        "max_output_tokens": 4096,
        "thinking": "adaptive",
        "service_tier": "standard",
        "costs": {"text": 2, "image": 2, "video": 4},
    },
    "ultra": {
        "max_output_tokens": 8192,
        "thinking": "adaptive",
        "service_tier": "priority",
        "costs": {"text": 5, "image": 5, "video": 10},
    },
}


def _exec(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _model_label(tier: str) -> str:
    return f"MiniMax-M3 {tier.capitalize()} (Platform)"


def _ensure_m3_model(tier: str, *, max_output_tokens: int, thinking: str, service_tier: str) -> None:
    label = _model_label(tier)
    capabilities = (
        '{"stream":true,"tool_call":true,"image":true,"video":true,'
        f'"thinking":"{thinking}","service_tier":"{service_tier}",'
        f'"seed_revision":"{SEED_REVISION}"}}'
    )
    _exec(
        f"""
        UPDATE llm_models
        SET modality = 'multimodal',
            modalities = '["text","image","video"]'::jsonb,
            tier = '{tier}',
            supports_vision = true,
            capabilities = COALESCE(capabilities::jsonb, '{{}}'::jsonb) || '{capabilities}'::jsonb,
            max_output_tokens = {max_output_tokens},
            enabled = true,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND model = 'MiniMax-M3'
          AND label = '{label}'
        """
    )
    _exec(
        f"""
        INSERT INTO llm_models (
            id, provider, model, api_key_encrypted, label, enabled, supports_vision,
            modality, modalities, tier, capabilities, max_output_tokens
        )
        SELECT
            gen_random_uuid(), 'minimax', 'MiniMax-M3', 'platform-credential-pool',
            '{label}', true, true,
            'multimodal', '["text","image","video"]'::jsonb, '{tier}',
            '{capabilities}'::jsonb, {max_output_tokens}
        WHERE NOT EXISTS (
            SELECT 1
            FROM llm_models
            WHERE tenant_id IS NULL
              AND provider = 'minimax'
              AND model = 'MiniMax-M3'
              AND label = '{label}'
        )
        """
    )


def _ensure_route(tier: str, modality: str) -> None:
    label = _model_label(tier)
    fallback_expression = "previous.id" if modality == "text" else "NULL"
    _exec(
        f"""
        WITH target AS (
            SELECT id
            FROM llm_models
            WHERE tenant_id IS NULL
              AND provider = 'minimax'
              AND model = 'MiniMax-M3'
              AND label = '{label}'
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 1
        ), previous AS (
            SELECT id
            FROM model_routes
            WHERE saas_tier = '{tier}'
              AND modality = '{modality}'
              AND enabled = true
              AND llm_model_id <> (SELECT id FROM target)
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
        )
        INSERT INTO model_routes (
            id, saas_tier, modality, llm_model_id, priority, fallback_route_id, enabled
        )
        SELECT
            gen_random_uuid(), '{tier}', '{modality}', target.id,
            {ROUTE_PRIORITY}, {fallback_expression}, true
        FROM target
        LEFT JOIN previous ON true
        WHERE NOT EXISTS (
            SELECT 1
            FROM model_routes
            WHERE saas_tier = '{tier}'
              AND modality = '{modality}'
              AND llm_model_id = target.id
        )
        """
    )


def _ensure_chat_billing_rule(tier: str, modality: str, credit_cost: int) -> None:
    _exec(
        f"""
        INSERT INTO billing_rules (id, action, modality, tier, unit, credit_cost, enabled, priority)
        VALUES (
            gen_random_uuid(), 'chat', '{modality}', '{tier}', 'call',
            {credit_cost}, true, {BILLING_PRIORITY}
        )
        ON CONFLICT (action, modality, tier, unit) DO NOTHING
        """
    )


def upgrade() -> None:
    for tier, tier_settings in TIER_SETTINGS.items():
        _ensure_m3_model(
            tier,
            max_output_tokens=tier_settings["max_output_tokens"],
            thinking=tier_settings["thinking"],
            service_tier=tier_settings["service_tier"],
        )
        for modality in MODALITIES:
            _ensure_route(tier, modality)

    # Preserve administrator-added modalities and only widen the understanding
    # set. The deterministic ordering keeps schema smoke checks stable.
    _exec(
        """
        UPDATE plans AS plan
        SET allowed_modalities = (
                SELECT jsonb_agg(value ORDER BY
                    CASE value WHEN 'text' THEN 0 WHEN 'image' THEN 1 WHEN 'video' THEN 2 ELSE 3 END,
                    value
                )
                FROM (
                    SELECT DISTINCT value
                    FROM jsonb_array_elements_text(
                        COALESCE(plan.allowed_modalities::jsonb, '[]'::jsonb) ||
                        '["text","image","video"]'::jsonb
                    ) AS value
                ) AS merged
            ),
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
        """
    )

    for tier, tier_settings in TIER_SETTINGS.items():
        for modality, credit_cost in tier_settings["costs"].items():
            _ensure_chat_billing_rule(tier, modality, credit_cost)


def downgrade() -> None:
    # Remove only the routes seeded by this revision. Existing lower-priority
    # M2.x and administrator-defined routes become active again automatically.
    _exec(
        f"""
        DELETE FROM model_routes AS route
        USING llm_models AS model
        WHERE route.llm_model_id = model.id
          AND route.priority = {ROUTE_PRIORITY}
          AND route.saas_tier IN ('lite', 'pro', 'ultra')
          AND route.modality IN ('text', 'image', 'video')
          AND model.tenant_id IS NULL
          AND model.provider = 'minimax'
          AND model.model = 'MiniMax-M3'
          AND model.capabilities::jsonb @> '{{"seed_revision":"{SEED_REVISION}"}}'::jsonb
        """
    )
    _exec(
        f"""
        DELETE FROM billing_rules
        WHERE action = 'chat'
          AND modality IN ('text', 'image', 'video')
          AND tier IN ('lite', 'pro', 'ultra')
          AND unit = 'call'
          AND priority = {BILLING_PRIORITY}
        """
    )
    # Restore the known pre-migration catalog only when it was not customized
    # after this migration widened it.
    _exec(
        """
        UPDATE plans
        SET allowed_modalities = '["text"]'::jsonb,
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
          AND allowed_modalities::jsonb = '["text","image","video"]'::jsonb
        """
    )
    _exec(
        f"""
        DELETE FROM llm_models AS model
        WHERE model.tenant_id IS NULL
          AND model.provider = 'minimax'
          AND model.model = 'MiniMax-M3'
          AND model.capabilities::jsonb @> '{{"seed_revision":"{SEED_REVISION}"}}'::jsonb
          AND NOT EXISTS (
              SELECT 1 FROM model_routes WHERE llm_model_id = model.id
          )
        """
    )
