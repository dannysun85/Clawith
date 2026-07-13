"""Align the known platform MiniMax text routes with product tiers.

Revision ID: align_minimax_text_tiers
Revises: credential_unverified_default
Create Date: 2026-07-12

This migration only changes the three platform rows created for the Astra SaaS
catalog.  Tenant-owned models and administrator-defined route rows are left
untouched.  The rows remain routing metadata: provider credentials continue to
come from the shared ``llm_credentials`` pool.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "align_minimax_text_tiers"
down_revision: Union[str, Sequence[str], None] = "credential_unverified_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exec(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M2.5',
            label = 'MiniMax-M2.5 Lite (Platform)',
            tier = 'lite',
            modality = 'text',
            modalities = '["text"]'::jsonb,
            supports_vision = false,
            max_output_tokens = 2048,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label IN (
              'MiniMax-M3 Lite (Platform)',
              'MiniMax-M2.5 Lite (Platform)'
          )
        """
    )
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M2.7',
            label = 'MiniMax-M2.7 Pro (Platform)',
            tier = 'pro',
            modality = 'text',
            modalities = '["text"]'::jsonb,
            supports_vision = false,
            max_output_tokens = 4096,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label IN (
              'MiniMax-M3 Pro (Platform)',
              'MiniMax-M2.7 Pro (Platform)'
          )
        """
    )
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M2.7-highspeed',
            label = 'MiniMax-M2.7 Highspeed Ultra (Platform)',
            tier = 'ultra',
            modality = 'text',
            modalities = '["text"]'::jsonb,
            supports_vision = false,
            max_output_tokens = 8192,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label IN (
              'MiniMax-M3 Ultra (Platform)',
              'MiniMax-M2.7 Highspeed Ultra (Platform)'
          )
        """
    )


def downgrade() -> None:
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M3',
            label = 'MiniMax-M3 Lite (Platform)',
            max_output_tokens = 2048,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label = 'MiniMax-M2.5 Lite (Platform)'
        """
    )
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M3',
            label = 'MiniMax-M3 Pro (Platform)',
            max_output_tokens = 2048,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label = 'MiniMax-M2.7 Pro (Platform)'
        """
    )
    _exec(
        """
        UPDATE llm_models
        SET model = 'MiniMax-M3',
            label = 'MiniMax-M3 Ultra (Platform)',
            max_output_tokens = 2048,
            updated_at = now()
        WHERE tenant_id IS NULL
          AND provider = 'minimax'
          AND label = 'MiniMax-M2.7 Highspeed Ultra (Platform)'
        """
    )
