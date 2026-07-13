"""Add product-level media generation entitlements.

Revision ID: add_media_generation_caps
Revises: add_chat_session_model_selection
"""

from collections.abc import Sequence

from alembic import op


revision: str = "add_media_generation_caps"
down_revision: str | None = "add_chat_session_model_selection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        UPDATE plans
        SET allowed_modalities = '["text"]'::jsonb,
            features = COALESCE(features::jsonb, '{}'::jsonb) ||
            jsonb_build_object(
                'generation_modalities', '["image","audio","music","video"]'::jsonb,
                'generation_tiers', CASE code
                    WHEN 'free' THEN '["lite"]'::jsonb
                    WHEN 'starter' THEN '["lite","pro"]'::jsonb
                    ELSE '["lite","pro","ultra"]'::jsonb
                END
            ),
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE agents
        SET preferred_modality = 'text', updated_at = now()
        WHERE agent_type = 'native'
          AND tenant_id IN (
              SELECT DISTINCT subscriptions.tenant_id
              FROM subscriptions
              JOIN plans ON plans.id = subscriptions.plan_id
              WHERE plans.code IN ('free', 'starter', 'pro', 'scale')
                AND subscriptions.status IN ('active', 'trialing', 'canceled', 'past_due')
          )
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE chat_sessions
        SET model_modality = 'text'
        WHERE agent_id IN (
            SELECT agents.id
            FROM agents
            WHERE agents.agent_type = 'native'
              AND agents.tenant_id IN (
                  SELECT DISTINCT subscriptions.tenant_id
                  FROM subscriptions
                  JOIN plans ON plans.id = subscriptions.plan_id
                  WHERE plans.code IN ('free', 'starter', 'pro', 'scale')
                    AND subscriptions.status IN ('active', 'trialing', 'canceled', 'past_due')
              )
        )
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tools
        SET is_default = true, updated_at = now()
        WHERE source = 'builtin'
          AND name IN (
              'generate_image_minimax',
              'generate_speech_minimax',
              'generate_music_minimax',
              'generate_video_minimax'
          )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        UPDATE plans
        SET allowed_modalities = CASE code
                WHEN 'pro' THEN '["text","image","vision"]'::jsonb
                WHEN 'scale' THEN '["text","image","vision","audio","voice","tts","video","music"]'::jsonb
                ELSE '["text"]'::jsonb
            END,
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE plans
        SET features = COALESCE(features::jsonb, '{}'::jsonb)
            - 'generation_modalities' - 'generation_tiers',
            updated_at = now()
        WHERE code IN ('free', 'starter', 'pro', 'scale')
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tools
        SET is_default = false, updated_at = now()
        WHERE source = 'builtin'
          AND name IN (
              'generate_image_minimax',
              'generate_speech_minimax',
              'generate_music_minimax',
              'generate_video_minimax'
          )
        """
    )
