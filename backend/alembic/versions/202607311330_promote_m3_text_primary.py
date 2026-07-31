"""Promote MiniMax-M3 text routes and retain Agent Plan as fallback.

Revision ID: promote_m3_text_primary
Revises: merge_v1113_astra_heads
Create Date: 2026-07-31 13:30:00

The migration changes only deterministic revision-owned routes.  It records
the exact previous route state on the corresponding revision-owned M3 model,
rewires fallback edges without creating a cycle, and fails closed if an
administrator has changed the expected baseline topology.
"""

from __future__ import annotations

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa


revision: str = "promote_m3_text_primary"
down_revision: str | Sequence[str] | None = "merge_v1113_astra_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BACKUP_KEY = "__promote_m3_text_primary_route_state"
M3_SEED_REVISION = "seed_minimax_m3_understanding"
AGENT_PLAN_SEED_REVISION = "seed_agent_plan_text_routes"
M3_MODEL_IDS = {
    "lite": "09300000-0000-4000-8000-000000000001",
    "pro": "09300000-0000-4000-8000-000000000002",
    "ultra": "09300000-0000-4000-8000-000000000003",
}
M3_ROUTE_IDS = {
    "lite": "09300000-0000-4000-8000-000000000101",
    "pro": "09300000-0000-4000-8000-000000000104",
    "ultra": "09300000-0000-4000-8000-000000000107",
}
AGENT_PLAN_MODEL_IDS = {
    "lite": "10700000-0000-4000-8000-000000000001",
    "pro": "10700000-0000-4000-8000-000000000002",
    "ultra": "10700000-0000-4000-8000-000000000003",
}
AGENT_PLAN_ROUTE_IDS = {
    "lite": "10700000-0000-4000-8000-000000000101",
    "pro": "10700000-0000-4000-8000-000000000102",
    "ultra": "10700000-0000-4000-8000-000000000103",
}
INT32_MAX = 2_147_483_647


def _route_state(tier: str) -> dict[str, object]:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            """
            SELECT
                m3_model.id AS m3_model_id,
                COALESCE(m3_model.capabilities::jsonb ->> 'seed_revision', '')
                    AS m3_seed_revision,
                m3_model.capabilities::jsonb ? :backup_key AS has_backup,
                m3_route.priority AS m3_priority,
                m3_route.fallback_route_id AS m3_fallback_route_id,
                agent_plan_model.id AS agent_plan_model_id,
                COALESCE(
                    agent_plan_model.capabilities::jsonb ->> 'seed_revision', ''
                ) AS agent_plan_seed_revision,
                agent_plan_route.priority AS agent_plan_priority,
                agent_plan_route.fallback_route_id AS agent_plan_fallback_route_id
            FROM llm_models AS m3_model
            JOIN model_routes AS m3_route
              ON m3_route.id = cast(:m3_route_id AS uuid)
             AND m3_route.llm_model_id = m3_model.id
            JOIN llm_models AS agent_plan_model
              ON agent_plan_model.id = cast(:agent_plan_model_id AS uuid)
            JOIN model_routes AS agent_plan_route
              ON agent_plan_route.id = cast(:agent_plan_route_id AS uuid)
             AND agent_plan_route.llm_model_id = agent_plan_model.id
            WHERE m3_model.id = cast(:m3_model_id AS uuid)
              AND m3_model.tenant_id IS NULL
              AND agent_plan_model.tenant_id IS NULL
              AND m3_route.saas_tier = :tier
              AND agent_plan_route.saas_tier = :tier
              AND m3_route.modality = 'text'
              AND agent_plan_route.modality = 'text'
              AND m3_route.enabled IS TRUE
              AND agent_plan_route.enabled IS TRUE
            """
        ),
        {
            "tier": tier,
            "backup_key": BACKUP_KEY,
            "m3_model_id": M3_MODEL_IDS[tier],
            "m3_route_id": M3_ROUTE_IDS[tier],
            "agent_plan_model_id": AGENT_PLAN_MODEL_IDS[tier],
            "agent_plan_route_id": AGENT_PLAN_ROUTE_IDS[tier],
        },
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"Revision-owned text route pair is missing for tier {tier}")
    state = dict(row)
    if state["m3_seed_revision"] != M3_SEED_REVISION:
        raise RuntimeError(f"MiniMax-M3 model ownership mismatch for tier {tier}")
    if state["agent_plan_seed_revision"] != AGENT_PLAN_SEED_REVISION:
        raise RuntimeError(f"Agent Plan model ownership mismatch for tier {tier}")
    return state


def _assert_no_enabled_fallback_cycles() -> None:
    cycle = op.get_bind().execute(
        sa.text(
            """
            WITH RECURSIVE fallback_walk(start_id, id, fallback_route_id, path, cycle) AS (
                SELECT route.id,
                       route.id,
                       route.fallback_route_id,
                       ARRAY[route.id]::uuid[],
                       false
                FROM model_routes AS route
                WHERE route.enabled IS TRUE
                UNION ALL
                SELECT fallback_walk.start_id,
                       next_route.id,
                       next_route.fallback_route_id,
                       fallback_walk.path || next_route.id,
                       next_route.id = ANY(fallback_walk.path)
                FROM fallback_walk
                JOIN model_routes AS next_route
                  ON next_route.id = fallback_walk.fallback_route_id
                 AND next_route.enabled IS TRUE
                WHERE fallback_walk.cycle IS FALSE
            )
            SELECT start_id, path
            FROM fallback_walk
            WHERE cycle IS TRUE
            LIMIT 1
            """
        )
    ).mappings().first()
    if cycle:
        raise RuntimeError(
            "Enabled model-route fallback cycle detected after text routing change: "
            f"start={cycle['start_id']} path={cycle['path']}"
        )


def _promote_tier(tier: str) -> None:
    bind = op.get_bind()
    state = _route_state(tier)
    m3_route_id = M3_ROUTE_IDS[tier]
    agent_plan_route_id = AGENT_PLAN_ROUTE_IDS[tier]

    if state["has_backup"]:
        raise RuntimeError(f"MiniMax-M3 text route backup already exists for tier {tier}")
    if str(state["agent_plan_fallback_route_id"]) != m3_route_id:
        raise RuntimeError(
            f"Unexpected Agent Plan fallback topology for tier {tier}; refusing to overwrite it"
        )
    if (
        state["m3_fallback_route_id"] is not None
        and str(state["m3_fallback_route_id"]) == agent_plan_route_id
    ):
        raise RuntimeError(f"MiniMax-M3 text route already points to Agent Plan for tier {tier}")

    highest_other = bind.execute(
        sa.text(
            """
            SELECT MAX(priority)
            FROM model_routes
            WHERE saas_tier = :tier
              AND modality = 'text'
              AND enabled IS TRUE
              AND id <> cast(:m3_route_id AS uuid)
            """
        ),
        {"tier": tier, "m3_route_id": m3_route_id},
    ).scalar_one_or_none()
    if highest_other is not None and int(highest_other) >= INT32_MAX:
        raise RuntimeError(f"Cannot promote MiniMax-M3 above int32 priority for tier {tier}")
    applied_priority = max(int(state["m3_priority"]), int(highest_other or 0) + 1)

    backup = json.dumps(
        {
            "m3_priority": int(state["m3_priority"]),
            "m3_fallback_route_id": (
                str(state["m3_fallback_route_id"])
                if state["m3_fallback_route_id"] is not None
                else None
            ),
            "agent_plan_priority": int(state["agent_plan_priority"]),
            "agent_plan_fallback_route_id": str(state["agent_plan_fallback_route_id"]),
            "applied_m3_priority": applied_priority,
        },
        separators=(",", ":"),
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE llm_models
            SET capabilities = jsonb_set(
                    COALESCE(capabilities::jsonb, '{{}}'::jsonb),
                    '{{{BACKUP_KEY}}}',
                    cast(:backup AS jsonb),
                    true
                ),
                updated_at = now()
            WHERE id = cast(:m3_model_id AS uuid)
            """
        ),
        {"backup": backup, "m3_model_id": M3_MODEL_IDS[tier]},
    )

    # Move the Agent Plan edge first so the subsequent M3 -> Agent Plan edge
    # cannot create the old Agent Plan -> M3 -> Agent Plan cycle.
    bind.execute(
        sa.text(
            """
            UPDATE model_routes
            SET fallback_route_id = cast(:fallback_route_id AS uuid),
                updated_at = now()
            WHERE id = cast(:agent_plan_route_id AS uuid)
            """
        ),
        {
            "fallback_route_id": (
                str(state["m3_fallback_route_id"])
                if state["m3_fallback_route_id"] is not None
                else None
            ),
            "agent_plan_route_id": agent_plan_route_id,
        },
    )
    bind.execute(
        sa.text(
            """
            UPDATE model_routes
            SET priority = :priority,
                fallback_route_id = cast(:agent_plan_route_id AS uuid),
                updated_at = now()
            WHERE id = cast(:m3_route_id AS uuid)
            """
        ),
        {
            "priority": applied_priority,
            "agent_plan_route_id": agent_plan_route_id,
            "m3_route_id": m3_route_id,
        },
    )


def upgrade() -> None:
    for tier in ("lite", "pro", "ultra"):
        _promote_tier(tier)
    _assert_no_enabled_fallback_cycles()


def _restore_tier(tier: str) -> None:
    bind = op.get_bind()
    state = _route_state(tier)
    backup = bind.execute(
        sa.text(
            """
            SELECT capabilities::jsonb -> :backup_key
            FROM llm_models
            WHERE id = cast(:m3_model_id AS uuid)
            """
        ),
        {"backup_key": BACKUP_KEY, "m3_model_id": M3_MODEL_IDS[tier]},
    ).scalar_one_or_none()
    if not isinstance(backup, dict):
        raise RuntimeError(f"MiniMax-M3 text route backup is missing for tier {tier}")

    m3_route_id = M3_ROUTE_IDS[tier]
    agent_plan_route_id = AGENT_PLAN_ROUTE_IDS[tier]
    expected_agent_plan_fallback = backup.get("m3_fallback_route_id")
    if (
        str(state["m3_fallback_route_id"]) != agent_plan_route_id
        or int(state["m3_priority"]) != int(backup["applied_m3_priority"])
        or (
            str(state["agent_plan_fallback_route_id"])
            if state["agent_plan_fallback_route_id"] is not None
            else None
        )
        != expected_agent_plan_fallback
    ):
        raise RuntimeError(
            f"Text route topology changed after promotion for tier {tier}; "
            "refusing a destructive downgrade"
        )

    # Restore M3 first so re-establishing Agent Plan -> M3 cannot form a cycle.
    bind.execute(
        sa.text(
            """
            UPDATE model_routes
            SET priority = :priority,
                fallback_route_id = cast(:fallback_route_id AS uuid),
                updated_at = now()
            WHERE id = cast(:m3_route_id AS uuid)
            """
        ),
        {
            "priority": int(backup["m3_priority"]),
            "fallback_route_id": backup.get("m3_fallback_route_id"),
            "m3_route_id": m3_route_id,
        },
    )
    bind.execute(
        sa.text(
            """
            UPDATE model_routes
            SET priority = :priority,
                fallback_route_id = cast(:fallback_route_id AS uuid),
                updated_at = now()
            WHERE id = cast(:agent_plan_route_id AS uuid)
            """
        ),
        {
            "priority": int(backup["agent_plan_priority"]),
            "fallback_route_id": backup.get("agent_plan_fallback_route_id"),
            "agent_plan_route_id": agent_plan_route_id,
        },
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE llm_models
            SET capabilities = COALESCE(capabilities::jsonb, '{{}}'::jsonb)
                    - '{BACKUP_KEY}',
                updated_at = now()
            WHERE id = cast(:m3_model_id AS uuid)
            """
        ),
        {"m3_model_id": M3_MODEL_IDS[tier]},
    )


def downgrade() -> None:
    for tier in ("lite", "pro", "ultra"):
        _restore_tier(tier)
    _assert_no_enabled_fallback_cycles()
