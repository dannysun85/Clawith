"""Stop excess active agents for Free tenants.

Revision ID: stop_excess_free_agents
Revises: free_plan_max_agents_one
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "stop_excess_free_agents"
down_revision: Union[str, Sequence[str], None] = "free_plan_max_agents_one"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH active_free_subscriptions AS (
            SELECT s.tenant_id, p.max_agents
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE p.code = 'free'
              AND s.status IN ('active', 'trialing')
        ),
        ranked_agents AS (
            SELECT
                a.id,
                ROW_NUMBER() OVER (
                    PARTITION BY a.tenant_id
                    ORDER BY a.created_at ASC NULLS LAST, a.id ASC
                ) AS rn,
                active_free_subscriptions.max_agents
            FROM agents a
            JOIN active_free_subscriptions ON active_free_subscriptions.tenant_id = a.tenant_id
            WHERE a.status NOT IN ('stopped', 'error')
              AND COALESCE(a.is_expired, false) = false
        )
        UPDATE agents
        SET status = 'stopped'
        WHERE id IN (
            SELECT id
            FROM ranked_agents
            WHERE rn > max_agents
        )
        """
    )


def downgrade() -> None:
    # Deliberately do not restore stopped agents automatically; the lifecycle
    # restore path runs after a real plan upgrade/renewal and preserves limits.
    pass
