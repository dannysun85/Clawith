"""Reconcile Runtime tool-calling metadata for seeded MiniMax-M3 models.

Revision ID: reconcile_m3_runtime_caps
Revises: agent_template_default_tools
Create Date: 2026-07-22

The M3 catalog rows were created on a migration branch that can run after the
legacy capability backfill on an already-upgraded database. In that ordering,
the rows retain NULL tool-calling metadata even though their revision-owned
capabilities explicitly declare tool support. Durable Runtime refuses those
models before accepting a chat message.

This repair is intentionally limited to the three deterministic M3 seed IDs.
It accepts only either the untouched all-NULL state or an already verified
state and refuses contradictory probe/admin metadata instead of overwriting it.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "reconcile_m3_runtime_caps"
down_revision: str | Sequence[str] | None = "agent_template_default_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SEED_REVISION = "seed_minimax_m3_understanding"
MIGRATION_MARKER = "__reconcile_m3_runtime_caps_applied_at"
MODEL_IDS = {
    "lite": "09300000-0000-4000-8000-000000000001",
    "pro": "09300000-0000-4000-8000-000000000002",
    "ultra": "09300000-0000-4000-8000-000000000003",
}


def _exec(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _exec(
        f"""
        DO $reconcile_m3_runtime_caps$
        DECLARE
            invalid_count integer;
        BEGIN
            SELECT count(*)
            INTO invalid_count
            FROM (
                VALUES
                  ('lite', '{MODEL_IDS["lite"]}'::uuid),
                  ('pro', '{MODEL_IDS["pro"]}'::uuid),
                  ('ultra', '{MODEL_IDS["ultra"]}'::uuid)
            ) AS expected(tier, model_id)
            LEFT JOIN llm_models AS model ON model.id = expected.model_id
            WHERE model.id IS NULL
               OR model.provider IS DISTINCT FROM 'minimax'
               OR model.model IS DISTINCT FROM 'MiniMax-M3'
               OR model.tier IS DISTINCT FROM expected.tier
               OR model.tenant_id IS NOT NULL
               OR COALESCE(model.capabilities::jsonb ->> 'seed_revision', '')
                    <> '{SEED_REVISION}'
               OR COALESCE(model.capabilities::jsonb ->> 'tool_call', '') <> 'true'
               OR (
                    (
                        model.supports_tool_calling IS NULL
                        AND model.tool_calling_capability_source IS NULL
                        AND model.tool_calling_checked_at IS NULL
                        AND model.tool_calling_error IS NULL
                    )
                    OR (
                        model.supports_tool_calling IS TRUE
                        AND model.tool_calling_capability_source
                            IN ('probe', 'builtin_registry')
                        AND model.tool_calling_checked_at IS NOT NULL
                        AND model.tool_calling_error IS NULL
                    )
               ) IS NOT TRUE;

            IF invalid_count <> 0 THEN
                RAISE EXCEPTION
                    'Refusing MiniMax-M3 Runtime capability repair: % seeded rows are missing, foreign-owned, or have contradictory verification metadata',
                    invalid_count;
            END IF;
        END $reconcile_m3_runtime_caps$;
        """
    )
    _exec(
        f"""
        WITH marker AS (
            SELECT clock_timestamp() AS applied_at
        )
        UPDATE llm_models AS model
        SET supports_tool_calling = true,
            tool_calling_capability_source = 'builtin_registry',
            tool_calling_checked_at = marker.applied_at,
            tool_calling_error = NULL,
            capabilities = jsonb_set(
                model.capabilities::jsonb,
                '{{{MIGRATION_MARKER}}}',
                to_jsonb(marker.applied_at),
                true
            )
        FROM marker
        WHERE model.id IN (
            '{MODEL_IDS["lite"]}'::uuid,
            '{MODEL_IDS["pro"]}'::uuid,
            '{MODEL_IDS["ultra"]}'::uuid
        )
          AND model.supports_tool_calling IS NULL;
        """
    )


def downgrade() -> None:
    model_ids = ", ".join(f"'{model_id}'::uuid" for model_id in MODEL_IDS.values())

    # Revert only rows whose capability fields still exactly match the values
    # written by this migration. If a later probe/admin action changed them,
    # preserve that newer truth and remove only our private ownership marker.
    _exec(
        f"""
        UPDATE llm_models
        SET supports_tool_calling = NULL,
            tool_calling_capability_source = NULL,
            tool_calling_checked_at = NULL,
            tool_calling_error = NULL,
            capabilities = capabilities::jsonb - '{MIGRATION_MARKER}'
        WHERE id IN ({model_ids})
          AND capabilities::jsonb ? '{MIGRATION_MARKER}'
          AND supports_tool_calling IS TRUE
          AND tool_calling_capability_source = 'builtin_registry'
          AND tool_calling_checked_at =
                (capabilities::jsonb ->> '{MIGRATION_MARKER}')::timestamptz
          AND tool_calling_error IS NULL;
        """
    )
    _exec(
        f"""
        UPDATE llm_models
        SET capabilities = capabilities::jsonb - '{MIGRATION_MARKER}'
        WHERE id IN ({model_ids})
          AND capabilities::jsonb ? '{MIGRATION_MARKER}';
        """
    )
