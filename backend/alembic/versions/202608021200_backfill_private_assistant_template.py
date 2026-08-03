"""Bind legacy private assistants to the reviewed Private Assistant template.

Older onboarding rows were created before the template identity became part of
the private-assistant contract.  Those rows still work as chat companions, but
they cannot receive the provider-neutral multimodal Skills that are now part of
the default entry point.  This migration is deliberately narrow and
idempotent: it only touches an active private Agent with the exact legacy role
description and no existing template.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "backfill_private_assistant_tpl"
down_revision: str | Sequence[str] | None = "recon_agent_tpl_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agents
            SET template_id = (
                    SELECT id
                    FROM agent_templates
                    WHERE name = 'Private Assistant'
                      AND is_builtin = TRUE
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                ),
                template_revision_applied = NULL,
                template_sync_status = 'pending',
                template_sync_details = '{}'::json,
                template_synced_at = NULL
            WHERE deleted_at IS NULL
              AND template_id IS NULL
              AND access_mode = 'private'
              AND is_system = FALSE
              AND role_description = 'Private Assistant'
              AND EXISTS (
                    SELECT 1
                    FROM agent_templates
                    WHERE name = 'Private Assistant'
                      AND is_builtin = TRUE
                )
            """
        )
    )


def downgrade() -> None:
    # Deliberately non-destructive.  Removing the template binding would
    # silently take Skills away from a live private assistant.
    pass
