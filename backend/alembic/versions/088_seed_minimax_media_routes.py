"""Seed the explicit MiniMax media routing matrix.

Revision ID: seed_minimax_media_routes
Revises: make_refunds_idempotent
Create Date: 2026-07-13

The media routes are platform routing policy stored on global Tool rows. They
do not grant model-object access and never contain provider credentials.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "seed_minimax_media_routes"
down_revision: Union[str, Sequence[str], None] = "make_refunds_idempotent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ROUTES = {
    "generate_image_minimax": {
        "lite_model": "image-01", "lite_enabled": True,
        "pro_model": "image-01", "pro_enabled": True,
        "ultra_model": "image-01", "ultra_enabled": True,
    },
    "generate_speech_minimax": {
        "lite_model": "speech-2.8-turbo", "lite_sample_rate": 24000, "lite_bitrate": 64000, "lite_enabled": True,
        "pro_model": "speech-2.8-turbo", "pro_sample_rate": 32000, "pro_bitrate": 128000, "pro_enabled": True,
        "ultra_model": "speech-2.8-hd", "ultra_sample_rate": 44100, "ultra_bitrate": 256000, "ultra_enabled": True,
    },
    "generate_music_minimax": {
        "lite_model": "music-2.6", "lite_sample_rate": 44100, "lite_bitrate": 128000, "lite_enabled": True,
        "pro_model": "music-2.6", "pro_sample_rate": 44100, "pro_bitrate": 256000, "pro_enabled": True,
        "ultra_model": "music-2.6", "ultra_sample_rate": 44100, "ultra_bitrate": 256000, "ultra_enabled": True,
    },
    "generate_video_minimax": {
        "lite_model": "MiniMax-Hailuo-02", "lite_duration": 6, "lite_resolution": "768P", "lite_enabled": True,
        "pro_model": "MiniMax-Hailuo-2.3", "pro_duration": 6, "pro_resolution": "768P", "pro_enabled": True,
        "ultra_model": "MiniMax-Hailuo-2.3", "ultra_duration": 6, "ultra_resolution": "1080P", "ultra_enabled": True,
    },
}


def upgrade() -> None:
    import json

    bind = op.get_bind()
    for tool_name, defaults in _ROUTES.items():
        # Existing tier-specific administrator choices win over the seeded
        # defaults. Legacy generic keys remain for non-routing preferences.
        payload = json.dumps(defaults, ensure_ascii=True).replace("'", "''")
        escaped_name = tool_name.replace("'", "''")
        bind.exec_driver_sql(
            f"""
            UPDATE tools
            SET config = ('{payload}'::jsonb || COALESCE(config::jsonb, '{{}}'::jsonb))::json,
                updated_at = now()
            WHERE name = '{escaped_name}' AND tenant_id IS NULL
            """
        )
    bind.exec_driver_sql(
        """
        UPDATE billing_rules
        SET enabled = false, updated_at = now()
        WHERE (action = 'tts' OR modality = 'tts') AND enabled = true
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    for tool_name, defaults in _ROUTES.items():
        keys = ", ".join("'" + key.replace("'", "''") + "'" for key in defaults)
        escaped_name = tool_name.replace("'", "''")
        bind.exec_driver_sql(
            f"""
            UPDATE tools
            SET config = (COALESCE(config::jsonb, '{{}}'::jsonb) - ARRAY[{keys}]::text[])::json,
                updated_at = now()
            WHERE name = '{escaped_name}' AND tenant_id IS NULL
            """
        )
    # The legacy TTS row intentionally stays disabled on rollback. Its
    # action/modality values are invalid in the current billing contract, and
    # blindly re-enabling pre-existing disabled rows would be unsafe.
