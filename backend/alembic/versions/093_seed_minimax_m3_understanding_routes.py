"""Route SaaS understanding inputs through isolated MiniMax-M3 models.

Revision ID: seed_minimax_m3_understanding
Revises: add_user_chat_tier_preference
Create Date: 2026-07-13

The M3 rows and routes use deterministic revision-owned IDs and distinct
``Understanding`` labels. Existing M2.x/M3 catalog rows, administrator routes,
billing choices and plan modalities remain recoverable on downgrade without
guessing which data belonged to the migration.
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
PLAN_BACKUP_KEY = "__seed_minimax_m3_understanding_original_modalities"
MODEL_IDS = {
    "lite": "09300000-0000-4000-8000-000000000001",
    "pro": "09300000-0000-4000-8000-000000000002",
    "ultra": "09300000-0000-4000-8000-000000000003",
}
ROUTE_IDS = {
    ("lite", "text"): "09300000-0000-4000-8000-000000000101",
    ("lite", "image"): "09300000-0000-4000-8000-000000000102",
    ("lite", "video"): "09300000-0000-4000-8000-000000000103",
    ("pro", "text"): "09300000-0000-4000-8000-000000000104",
    ("pro", "image"): "09300000-0000-4000-8000-000000000105",
    ("pro", "video"): "09300000-0000-4000-8000-000000000106",
    ("ultra", "text"): "09300000-0000-4000-8000-000000000107",
    ("ultra", "image"): "09300000-0000-4000-8000-000000000108",
    ("ultra", "video"): "09300000-0000-4000-8000-000000000109",
}
BILLING_RULE_IDS = {
    ("lite", "text"): "09300000-0000-4000-8000-000000000201",
    ("lite", "image"): "09300000-0000-4000-8000-000000000202",
    ("lite", "video"): "09300000-0000-4000-8000-000000000203",
    ("pro", "text"): "09300000-0000-4000-8000-000000000204",
    ("pro", "image"): "09300000-0000-4000-8000-000000000205",
    ("pro", "video"): "09300000-0000-4000-8000-000000000206",
    ("ultra", "text"): "09300000-0000-4000-8000-000000000207",
    ("ultra", "image"): "09300000-0000-4000-8000-000000000208",
    ("ultra", "video"): "09300000-0000-4000-8000-000000000209",
}
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
    return f"MiniMax-M3 {tier.capitalize()} Understanding (Platform)"


def _ensure_m3_model(tier: str, *, max_output_tokens: int, thinking: str, service_tier: str) -> None:
    model_id = MODEL_IDS[tier]
    label = _model_label(tier)
    capabilities = (
        '{"stream":true,"tool_call":true,"image":true,"video":true,'
        f'"thinking":"{thinking}","service_tier":"{service_tier}",'
        f'"seed_revision":"{SEED_REVISION}"}}'
    )
    _exec(
        f"""
        DO $seed_model$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM llm_models
                WHERE id = '{model_id}'::uuid
                  AND COALESCE(capabilities::jsonb ->> 'seed_revision', '')
                      <> '{SEED_REVISION}'
            ) THEN
                RAISE EXCEPTION
                    'reserved MiniMax-M3 model id {model_id} is administrator-owned';
            END IF;

            INSERT INTO llm_models (
                id, provider, model, api_key_encrypted, label, enabled, supports_vision,
                modality, modalities, tier, capabilities, max_output_tokens
            )
            VALUES (
                '{model_id}'::uuid, 'minimax', 'MiniMax-M3', 'platform-credential-pool',
                '{label}', true, true,
                'text', '["text","image","video"]'::jsonb, '{tier}',
                '{capabilities}'::jsonb, {max_output_tokens}
            )
            ON CONFLICT (id) DO NOTHING;
        END $seed_model$
        """
    )


def _ensure_route(tier: str, modality: str) -> None:
    model_id = MODEL_IDS[tier]
    route_id = ROUTE_IDS[(tier, modality)]
    fallback_expression = "previous.id" if modality == "text" else "NULL"
    _exec(
        f"""
        WITH target AS (
            SELECT id
            FROM llm_models
            WHERE id = '{model_id}'::uuid
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
            '{route_id}'::uuid, '{tier}', '{modality}', target.id,
            {ROUTE_PRIORITY}, {fallback_expression}, true
        FROM target
        LEFT JOIN previous ON true
        """
    )


def _ensure_chat_billing_rule(tier: str, modality: str, credit_cost: int) -> None:
    rule_id = BILLING_RULE_IDS[(tier, modality)]
    _exec(
        f"""
        INSERT INTO billing_rules (id, action, modality, tier, unit, credit_cost, enabled, priority)
        VALUES (
            '{rule_id}'::uuid, 'chat', '{modality}', '{tier}', 'call',
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

    # Capture the exact pre-migration values before widening the understanding
    # set. The private marker lets downgrade restore only unchanged seeded
    # values and preserve any administrator edit made after deployment.
    _exec(
        f"""
        UPDATE plans
        SET features = jsonb_set(
                COALESCE(features::jsonb, '{{}}'::jsonb),
                '{{{PLAN_BACKUP_KEY}}}',
                COALESCE(allowed_modalities::jsonb, '[]'::jsonb),
                true
            ),
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
          AND NOT (COALESCE(features::jsonb, '{{}}'::jsonb) ? '{PLAN_BACKUP_KEY}')
        """
    )
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
    route_ids = ", ".join(f"'{value}'::uuid" for value in ROUTE_IDS.values())
    billing_rule_ids = ", ".join(
        f"'{value}'::uuid" for value in BILLING_RULE_IDS.values()
    )
    model_ids = ", ".join(f"'{value}'::uuid" for value in MODEL_IDS.values())

    # Deterministic IDs are the ownership boundary: never infer ownership from
    # a label, priority, provider, or capability that an administrator can use.
    # Administrator routes may legitimately point at a seeded fallback. Detach
    # only that edge before removing revision-owned routes so rollback cannot
    # fail on the self-referential foreign key.
    _exec(
        f"""
        UPDATE model_routes
        SET fallback_route_id = NULL,
            updated_at = now()
        WHERE fallback_route_id IN ({route_ids})
          AND id NOT IN ({route_ids})
        """
    )
    _exec(
        f"""
        DELETE FROM model_routes
        WHERE id IN ({route_ids})
        """
    )
    _exec(
        f"""
        DELETE FROM billing_rules
        WHERE id IN ({billing_rule_ids})
        """
    )
    # Restore the captured value only when the current value is still exactly
    # the widened value produced from that capture. Otherwise retain the
    # administrator's post-upgrade edit and remove only our backup marker.
    _exec(
        f"""
        UPDATE plans AS plan
        SET allowed_modalities = CASE
                WHEN plan.allowed_modalities::jsonb = (
                    SELECT jsonb_agg(value ORDER BY
                        CASE value WHEN 'text' THEN 0 WHEN 'image' THEN 1 WHEN 'video' THEN 2 ELSE 3 END,
                        value
                    )
                    FROM (
                        SELECT DISTINCT value
                        FROM jsonb_array_elements_text(
                            COALESCE(
                                plan.features::jsonb -> '{PLAN_BACKUP_KEY}',
                                '[]'::jsonb
                            ) || '["text","image","video"]'::jsonb
                        ) AS value
                    ) AS expected_values
                )
                THEN plan.features::jsonb -> '{PLAN_BACKUP_KEY}'
                ELSE plan.allowed_modalities::jsonb
            END,
            features = COALESCE(plan.features::jsonb, '{{}}'::jsonb) - '{PLAN_BACKUP_KEY}',
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
          AND COALESCE(plan.features::jsonb, '{{}}'::jsonb) ? '{PLAN_BACKUP_KEY}'
        """
    )
    _exec(
        f"""
        DELETE FROM llm_models AS model
        WHERE model.id IN ({model_ids})
          AND NOT EXISTS (
              SELECT 1 FROM model_routes WHERE llm_model_id = model.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM agents
              WHERE primary_model_id = model.id OR fallback_model_id = model.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM tenants WHERE default_model_id = model.id
          )
        """
    )
