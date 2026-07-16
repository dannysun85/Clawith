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
_ROUTE_TRIGGER = "trg_model_routes_require_platform_model"
_MODEL_TRIGGER = "trg_routed_models_remain_route_compatible"
_ROUTE_FUNCTION = "require_platform_model_route"
_MODEL_FUNCTION = "prevent_routed_model_invalidation"
_FALLBACK_UPDATE_TRIGGER = "trg_model_routes_preserve_fallback_target_update"
_FALLBACK_DELETE_TRIGGER = "trg_model_routes_preserve_fallback_target_delete"
_FALLBACK_FUNCTION = "prevent_fallback_target_invalidation"
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
                  AND tenant_id IS NULL
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
                      AND model.tenant_id IS NULL
                      AND (
                          (
                              jsonb_array_length(
                                  COALESCE(model.modalities::jsonb, '[]'::jsonb)
                              ) > 0
                              AND (
                                  jsonb_exists(model.modalities::jsonb, :modality)
                                  OR jsonb_exists(model.modalities::jsonb, 'multimodal')
                                  OR (
                                      :modality = 'image'
                                      AND jsonb_exists(model.modalities::jsonb, 'vision')
                                  )
                              )
                          )
                          OR (
                              jsonb_array_length(
                                  COALESCE(model.modalities::jsonb, '[]'::jsonb)
                              ) = 0
                              AND (
                                  lower(COALESCE(model.modality, '')) IN (
                                      :modality, 'multimodal'
                                  )
                                  OR (
                                      :modality = 'image'
                                      AND lower(COALESCE(model.modality, '')) = 'vision'
                                  )
                              )
                          )
                          OR (
                              :modality = 'image'
                              AND model.supports_vision IS TRUE
                          )
                      )
                      AND NOT EXISTS (
                          WITH RECURSIVE fallback_chain(id, fallback_route_id, path) AS (
                              SELECT route.id,
                                     route.fallback_route_id,
                                     ARRAY[route.id]::uuid[]
                              UNION ALL
                              SELECT next_route.id,
                                     next_route.fallback_route_id,
                                     fallback_chain.path || next_route.id
                              FROM fallback_chain
                              JOIN model_routes AS next_route
                                ON next_route.id = fallback_chain.fallback_route_id
                              WHERE NOT next_route.id = ANY(fallback_chain.path)
                          )
                          SELECT 1
                          FROM fallback_chain
                          WHERE id = cast(:route_id AS uuid)
                      )
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


def _assert_no_enabled_fallback_cycles() -> None:
    bind = op.get_bind()
    cycle = bind.execute(
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
            "Enabled model-route fallback cycle detected after M3 promotion: "
            f"start={cycle['start_id']} path={cycle['path']}"
        )


def _install_platform_model_route_guards() -> None:
    """Enforce route ownership and live capability invariants in PostgreSQL."""

    bind = op.get_bind()
    invalid = bind.execute(
        sa.text(
            """
            SELECT route.id, model.id AS model_id, model.tenant_id,
                   route.modality
            FROM model_routes AS route
            LEFT JOIN llm_models AS model ON model.id = route.llm_model_id
            WHERE model.id IS NULL
               OR model.tenant_id IS NOT NULL
               OR (
                   route.enabled IS TRUE
                   AND (
                       model.enabled IS NOT TRUE
                       OR NOT (
                           (
                               jsonb_array_length(
                                   COALESCE(model.modalities::jsonb, '[]'::jsonb)
                               ) > 0
                               AND (
                                   jsonb_exists(model.modalities::jsonb, lower(route.modality))
                                   OR jsonb_exists(model.modalities::jsonb, 'multimodal')
                                   OR (
                                       lower(route.modality) = 'image'
                                       AND jsonb_exists(model.modalities::jsonb, 'vision')
                                   )
                               )
                           )
                           OR (
                               jsonb_array_length(
                                   COALESCE(model.modalities::jsonb, '[]'::jsonb)
                               ) = 0
                               AND (
                                   lower(COALESCE(model.modality, '')) IN (
                                       lower(route.modality), 'multimodal'
                                   )
                                   OR (
                                       lower(route.modality) = 'image'
                                       AND lower(COALESCE(model.modality, '')) = 'vision'
                                   )
                               )
                           )
                           OR (
                               lower(route.modality) = 'image'
                               AND model.supports_vision IS TRUE
                           )
                       )
                   )
               )
            ORDER BY route.id
            LIMIT 1
            """
        )
    ).mappings().first()
    if invalid:
        raise RuntimeError(
            "Global model route references a missing, tenant-owned, disabled, "
            "or modality-incompatible model: "
            f"route={invalid['id']} model={invalid['model_id']} "
            f"tenant={invalid['tenant_id']} modality={invalid['modality']}"
        )

    invalid_fallback = bind.execute(
        sa.text(
            """
            SELECT route.id, fallback.id AS fallback_id
            FROM model_routes AS route
            LEFT JOIN model_routes AS fallback
              ON fallback.id = route.fallback_route_id
            LEFT JOIN llm_models AS fallback_model
              ON fallback_model.id = fallback.llm_model_id
            WHERE route.enabled IS TRUE
              AND route.fallback_route_id IS NOT NULL
              AND (
                  fallback.id IS NULL
                  OR fallback.enabled IS NOT TRUE
                  OR fallback.saas_tier <> route.saas_tier
                  OR fallback.modality <> route.modality
                  OR fallback.id = route.id
                  OR fallback_model.id IS NULL
                  OR fallback_model.tenant_id IS NOT NULL
                  OR fallback_model.enabled IS NOT TRUE
                  OR NOT (
                      (
                          jsonb_array_length(
                              COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                          ) > 0
                          AND (
                              jsonb_exists(
                                  fallback_model.modalities::jsonb,
                                  lower(fallback.modality)
                              )
                              OR jsonb_exists(
                                  fallback_model.modalities::jsonb,
                                  'multimodal'
                              )
                              OR (
                                  lower(fallback.modality) = 'image'
                                  AND jsonb_exists(
                                      fallback_model.modalities::jsonb,
                                      'vision'
                                  )
                              )
                          )
                      )
                      OR (
                          jsonb_array_length(
                              COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                          ) = 0
                          AND (
                              lower(COALESCE(fallback_model.modality, '')) IN (
                                  lower(fallback.modality), 'multimodal'
                              )
                              OR (
                                  lower(fallback.modality) = 'image'
                                  AND lower(COALESCE(fallback_model.modality, '')) = 'vision'
                              )
                          )
                      )
                      OR (
                          lower(fallback.modality) = 'image'
                          AND fallback_model.supports_vision IS TRUE
                      )
                  )
              )
            ORDER BY route.id
            LIMIT 1
            """
        )
    ).mappings().first()
    if invalid_fallback:
        raise RuntimeError(
            "Enabled model route has an invalid fallback target: "
            f"route={invalid_fallback['id']} "
            f"fallback={invalid_fallback['fallback_id']}"
        )

    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_ROUTE_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                route_model llm_models%ROWTYPE;
                fallback_route model_routes%ROWTYPE;
                fallback_model llm_models%ROWTYPE;
            BEGIN
                SELECT *
                INTO route_model
                FROM llm_models
                WHERE id = NEW.llm_model_id;
                IF NOT FOUND OR route_model.tenant_id IS NOT NULL THEN
                    RAISE EXCEPTION
                        'global model route requires a platform-owned model';
                END IF;
                IF NEW.enabled IS TRUE AND (
                    route_model.enabled IS NOT TRUE
                    OR NOT (
                        (
                            jsonb_array_length(
                                COALESCE(route_model.modalities::jsonb, '[]'::jsonb)
                            ) > 0
                            AND (
                                jsonb_exists(
                                    route_model.modalities::jsonb,
                                    lower(NEW.modality)
                                )
                                OR jsonb_exists(
                                    route_model.modalities::jsonb,
                                    'multimodal'
                                )
                                OR (
                                    lower(NEW.modality) = 'image'
                                    AND jsonb_exists(
                                        route_model.modalities::jsonb,
                                        'vision'
                                    )
                                )
                            )
                        )
                        OR (
                            jsonb_array_length(
                                COALESCE(route_model.modalities::jsonb, '[]'::jsonb)
                            ) = 0
                            AND (
                                lower(COALESCE(route_model.modality, '')) IN (
                                    lower(NEW.modality), 'multimodal'
                                )
                                OR (
                                    lower(NEW.modality) = 'image'
                                    AND lower(COALESCE(route_model.modality, '')) = 'vision'
                                )
                            )
                        )
                        OR (
                            lower(NEW.modality) = 'image'
                            AND route_model.supports_vision IS TRUE
                        )
                    )
                ) THEN
                    RAISE EXCEPTION
                        'enabled model route requires an enabled modality-compatible model';
                END IF;
                IF NEW.enabled IS TRUE AND NEW.fallback_route_id IS NOT NULL THEN
                    IF NEW.fallback_route_id = NEW.id THEN
                        RAISE EXCEPTION 'a model route cannot fall back to itself';
                    END IF;
                    SELECT *
                    INTO fallback_route
                    FROM model_routes
                    WHERE id = NEW.fallback_route_id
                    FOR UPDATE;
                    IF NOT FOUND
                       OR fallback_route.enabled IS NOT TRUE
                       OR fallback_route.saas_tier <> NEW.saas_tier
                       OR fallback_route.modality <> NEW.modality THEN
                        RAISE EXCEPTION
                            'enabled model route requires an enabled same-slot fallback';
                    END IF;
                    SELECT *
                    INTO fallback_model
                    FROM llm_models
                    WHERE id = fallback_route.llm_model_id;
                    IF NOT FOUND
                       OR fallback_model.tenant_id IS NOT NULL
                       OR fallback_model.enabled IS NOT TRUE
                       OR NOT (
                           (
                               jsonb_array_length(
                                   COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                               ) > 0
                               AND (
                                   jsonb_exists(
                                       fallback_model.modalities::jsonb,
                                       lower(fallback_route.modality)
                                   )
                                   OR jsonb_exists(
                                       fallback_model.modalities::jsonb,
                                       'multimodal'
                                   )
                                   OR (
                                       lower(fallback_route.modality) = 'image'
                                       AND jsonb_exists(
                                           fallback_model.modalities::jsonb,
                                           'vision'
                                       )
                                   )
                               )
                           )
                           OR (
                               jsonb_array_length(
                                   COALESCE(fallback_model.modalities::jsonb, '[]'::jsonb)
                               ) = 0
                               AND (
                                   lower(COALESCE(fallback_model.modality, '')) IN (
                                       lower(fallback_route.modality), 'multimodal'
                                   )
                                   OR (
                                       lower(fallback_route.modality) = 'image'
                                       AND lower(COALESCE(fallback_model.modality, '')) = 'vision'
                                   )
                               )
                           )
                           OR (
                               lower(fallback_route.modality) = 'image'
                               AND fallback_model.supports_vision IS TRUE
                           )
                       ) THEN
                        RAISE EXCEPTION
                            'enabled model route requires a valid platform fallback model';
                    END IF;
                    IF EXISTS (
                        WITH RECURSIVE fallback_chain(id, fallback_route_id, path) AS (
                            SELECT fallback_route.id,
                                   fallback_route.fallback_route_id,
                                   ARRAY[fallback_route.id]::uuid[]
                            UNION ALL
                            SELECT next_route.id,
                                   next_route.fallback_route_id,
                                   fallback_chain.path || next_route.id
                            FROM fallback_chain
                            JOIN model_routes AS next_route
                              ON next_route.id = fallback_chain.fallback_route_id
                            WHERE NOT next_route.id = ANY(fallback_chain.path)
                        )
                        SELECT 1
                        FROM fallback_chain
                        WHERE id = NEW.id
                    ) THEN
                        RAISE EXCEPTION 'model route fallback cycle detected';
                    END IF;
                END IF;
                RETURN NEW;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_ROUTE_TRIGGER}
            BEFORE INSERT OR UPDATE ON model_routes
            FOR EACH ROW EXECUTE FUNCTION {_ROUTE_FUNCTION}()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FALLBACK_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                dependant_id uuid;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    SELECT route.id
                    INTO dependant_id
                    FROM model_routes AS route
                    WHERE route.fallback_route_id = OLD.id
                    ORDER BY route.id
                    LIMIT 1
                    FOR UPDATE;
                    IF FOUND THEN
                        RAISE EXCEPTION
                            'model route remains referenced as a fallback target';
                    END IF;
                    RETURN OLD;
                END IF;
                SELECT route.id
                INTO dependant_id
                FROM model_routes AS route
                WHERE route.fallback_route_id = OLD.id
                  AND route.enabled IS TRUE
                  AND (
                      NEW.enabled IS NOT TRUE
                      OR NEW.saas_tier <> route.saas_tier
                      OR NEW.modality <> route.modality
                  )
                ORDER BY route.id
                LIMIT 1
                FOR UPDATE;
                IF FOUND THEN
                    RAISE EXCEPTION
                        'active fallback target cannot be disabled or moved to another slot';
                END IF;
                RETURN NEW;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_FALLBACK_UPDATE_TRIGGER}
            BEFORE UPDATE OF enabled, saas_tier, modality ON model_routes
            FOR EACH ROW EXECUTE FUNCTION {_FALLBACK_FUNCTION}()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_FALLBACK_DELETE_TRIGGER}
            BEFORE DELETE ON model_routes
            FOR EACH ROW EXECUTE FUNCTION {_FALLBACK_FUNCTION}()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_MODEL_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM model_routes AS route
                    WHERE route.llm_model_id = NEW.id
                      AND (
                          NEW.tenant_id IS NOT NULL
                          OR (
                              route.enabled IS TRUE
                              AND (
                                  NEW.enabled IS NOT TRUE
                                  OR NEW.provider IS DISTINCT FROM OLD.provider
                                  OR NEW.model IS DISTINCT FROM OLD.model
                                  OR NEW.base_url IS DISTINCT FROM OLD.base_url
                                  OR NOT (
                                      (
                                          jsonb_array_length(
                                              COALESCE(NEW.modalities::jsonb, '[]'::jsonb)
                                          ) > 0
                                          AND (
                                              jsonb_exists(
                                                  NEW.modalities::jsonb,
                                                  lower(route.modality)
                                              )
                                              OR jsonb_exists(
                                                  NEW.modalities::jsonb,
                                                  'multimodal'
                                              )
                                              OR (
                                                  lower(route.modality) = 'image'
                                                  AND jsonb_exists(
                                                      NEW.modalities::jsonb,
                                                      'vision'
                                                  )
                                              )
                                          )
                                      )
                                      OR (
                                          jsonb_array_length(
                                              COALESCE(NEW.modalities::jsonb, '[]'::jsonb)
                                          ) = 0
                                          AND (
                                              lower(COALESCE(NEW.modality, '')) IN (
                                                  lower(route.modality), 'multimodal'
                                              )
                                              OR (
                                                  lower(route.modality) = 'image'
                                                  AND lower(COALESCE(NEW.modality, '')) = 'vision'
                                              )
                                          )
                                      )
                                      OR (
                                          lower(route.modality) = 'image'
                                          AND NEW.supports_vision IS TRUE
                                      )
                                  )
                              )
                          )
                      )
                ) THEN
                    RAISE EXCEPTION
                        'a globally routed model must remain platform-owned, enabled, modality-compatible, and connection-stable';
                END IF;
                RETURN NEW;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_MODEL_TRIGGER}
            BEFORE UPDATE OF tenant_id, enabled, modality, modalities, supports_vision,
                provider, model, base_url
            ON llm_models
            FOR EACH ROW EXECUTE FUNCTION {_MODEL_FUNCTION}()
            """
        )
    )


def upgrade() -> None:
    _promote_revision_owned_m3_routes()
    _install_platform_model_route_guards()
    _assert_no_enabled_fallback_cycles()
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
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_MODEL_TRIGGER} ON llm_models"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_MODEL_FUNCTION}()"))
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS {_FALLBACK_DELETE_TRIGGER} ON model_routes"
        )
    )
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS {_FALLBACK_UPDATE_TRIGGER} ON model_routes"
        )
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FALLBACK_FUNCTION}()"))
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_ROUTE_TRIGGER} ON model_routes"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_ROUTE_FUNCTION}()"))
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
