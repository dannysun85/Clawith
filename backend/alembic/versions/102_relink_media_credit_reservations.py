"""Relink safely-owned legacy media Credits reservations.

Revision ID: relink_media_credit_reservations
Revises: agentbay_provider_identity
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "relink_media_credit_reservations"
down_revision: str | Sequence[str] | None = "agentbay_provider_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older MiniMax video code reserved Credits before the durable media row
    # existed, leaving ref_type=minimax_task (or NULL) and ref_id=NULL. The
    # media table already enforces one task per reservation, so relink only
    # exact tenant/Agent/user matches. Any mismatched/colliding evidence stays
    # untouched and the runtime ownership fence will fail closed for review.
    op.execute(
        sa.text(
            """
            UPDATE credit_reservations AS reservation
            SET ref_type = 'media_task',
                ref_id = task.id
            FROM media_generation_tasks AS task
            WHERE task.reservation_id = reservation.id
              AND reservation.ref_id IS NULL
              AND (
                    reservation.ref_type IS NULL
                    OR reservation.ref_type = 'minimax_task'
                  )
              AND reservation.tenant_id = task.tenant_id
              AND reservation.agent_id = task.agent_id
              AND reservation.user_id IS NOT DISTINCT FROM task.user_id
            """
        )
    )


def downgrade() -> None:
    # The old reference was incomplete and cannot be reconstructed. Keeping
    # the canonical task binding is safer and remains readable by older code.
    pass
