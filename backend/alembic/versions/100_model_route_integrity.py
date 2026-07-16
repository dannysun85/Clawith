"""Make enabled SaaS model-route selection deterministic.

Revision ID: model_route_integrity
Revises: trigger_privacy_serial
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "model_route_integrity"
down_revision: str | Sequence[str] | None = "trigger_privacy_serial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_model_routes_enabled_slot"
_SEED_REVISION = "seed_minimax_m3_understanding"
_MODEL_IDS = {
    "lite": "09300000-0000-4000-8000-000000000001",
    "pro": "09300000-0000-4000-8000-000000000002",
    "ultra": "09300000-0000-4000-8000-000000000003",
}
_ROUTE_IDS = {
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


def _promote_revision_owned_m3_routes() -> None:
    """Make each 3x3 M3 route the unique top route without deleting history."""

    bind = op.get_bind()
    for tier, model_id in _MODEL_IDS.items():
        owned = bind.execute(
            sa.text(
                """
                SELECT COALESCE(capabilities::jsonb ->> 'seed_revision', '')
                FROM llm_models
                WHERE id = cast(:model_id AS uuid)
                """
            ),
            {"model_id": model_id},
        ).scalar_one_or_none()
        if owned != _SEED_REVISION:
            raise RuntimeError(
                f"Revision-owned MiniMax-M3 model is missing or was replaced: {model_id}"
            )
        bind.execute(
            sa.text(
                """
                UPDATE llm_models
                SET provider = 'minimax',
                    model = 'MiniMax-M3',
                    enabled = true,
                    supports_vision = true,
                    modality = 'text',
                    modalities = '["text","image","video"]'::jsonb,
                    updated_at = now()
                WHERE id = cast(:model_id AS uuid)
                """
            ),
            {"model_id": model_id},
        )

        for modality in ("text", "image", "video"):
            route_id = _ROUTE_IDS[(tier, modality)]
            highest_other = bind.execute(
                sa.text(
                    """
                    SELECT MAX(priority)
                    FROM model_routes
                    WHERE saas_tier = :tier
                      AND modality = :modality
                      AND enabled IS TRUE
                      AND id <> cast(:route_id AS uuid)
                    """
                ),
                {"tier": tier, "modality": modality, "route_id": route_id},
            ).scalar_one_or_none()
            if highest_other is not None and int(highest_other) >= 2_147_483_647:
                raise RuntimeError(
                    f"Cannot promote MiniMax-M3 route above int32 priority for {tier}/{modality}"
                )
            target_priority = max(930, int(highest_other or 929) + 1)
            fallback_id = bind.execute(
                sa.text(
                    """
                    SELECT route.id
                    FROM model_routes AS route
                    JOIN llm_models AS model ON model.id = route.llm_model_id
                    WHERE route.saas_tier = :tier
                      AND route.modality = :modality
                      AND route.enabled IS TRUE
                      AND route.id <> cast(:route_id AS uuid)
                      AND model.enabled IS TRUE
                    ORDER BY route.priority DESC, route.created_at ASC, route.id ASC
                    LIMIT 1
                    """
                ),
                {"tier": tier, "modality": modality, "route_id": route_id},
            ).scalar_one_or_none()
            updated = bind.execute(
                sa.text(
                    """
                    UPDATE model_routes
                    SET saas_tier = :tier,
                        modality = :modality,
                        llm_model_id = cast(:model_id AS uuid),
                        priority = :priority,
                        fallback_route_id = cast(:fallback_id AS uuid),
                        enabled = true,
                        updated_at = now()
                    WHERE id = cast(:route_id AS uuid)
                    """
                ),
                {
                    "tier": tier,
                    "modality": modality,
                    "model_id": model_id,
                    "priority": target_priority,
                    "fallback_id": str(fallback_id) if fallback_id else None,
                    "route_id": route_id,
                },
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"Revision-owned MiniMax-M3 route is missing: {tier}/{modality}"
                )


def upgrade() -> None:
    _promote_revision_owned_m3_routes()
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT saas_tier, modality, priority, COUNT(*) AS route_count
            FROM model_routes
            WHERE enabled IS TRUE
            GROUP BY saas_tier, modality, priority
            HAVING COUNT(*) > 1
            ORDER BY saas_tier, modality, priority
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate:
        raise RuntimeError(
            "Ambiguous enabled model routes must be resolved before migration: "
            f"{duplicate['saas_tier']}/{duplicate['modality']} priority="
            f"{duplicate['priority']} count={duplicate['route_count']}"
        )
    op.create_index(
        _INDEX_NAME,
        "model_routes",
        ["saas_tier", "modality", "priority"],
        unique=True,
        postgresql_where=sa.text("enabled IS TRUE"),
        sqlite_where=sa.text("enabled IS 1"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="model_routes")
    bind = op.get_bind()
    for (tier, modality), route_id in _ROUTE_IDS.items():
        fallback_id = None
        if modality == "text":
            fallback_id = bind.execute(
                sa.text(
                    """
                    SELECT id
                    FROM model_routes
                    WHERE saas_tier = :tier
                      AND modality = :modality
                      AND enabled IS TRUE
                      AND id <> cast(:route_id AS uuid)
                    ORDER BY priority DESC, created_at ASC, id ASC
                    LIMIT 1
                    """
                ),
                {"tier": tier, "modality": modality, "route_id": route_id},
            ).scalar_one_or_none()
        bind.execute(
            sa.text(
                """
                UPDATE model_routes
                SET priority = 930,
                    fallback_route_id = cast(:fallback_id AS uuid),
                    updated_at = now()
                WHERE id = cast(:route_id AS uuid)
                """
            ),
            {
                "fallback_id": str(fallback_id) if fallback_id else None,
                "route_id": route_id,
            },
        )
