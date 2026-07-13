"""Reconcile Free tenants to their Agent seat limit.

Revision ID: reconcile_free_agent_limit
Revises: stop_excess_free_agents
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "reconcile_free_agent_limit"
down_revision: Union[str, Sequence[str], None] = "stop_excess_free_agents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                a.id,
                ROW_NUMBER() OVER (
                    PARTITION BY a.tenant_id
                    ORDER BY a.created_at ASC NULLS LAST, a.id ASC
                ) AS rn,
                p.max_agents
            FROM agents a
            JOIN subscriptions s ON s.tenant_id = a.tenant_id
            JOIN plans p ON p.id = s.plan_id
            WHERE p.code = 'free'
              AND s.status IN ('active', 'trialing')
              AND a.status NOT IN ('stopped', 'error')
              AND COALESCE(a.is_expired, false) = false
        )
        UPDATE agents AS a
        SET status = 'stopped'
        FROM ranked
        WHERE a.id = ranked.id
          AND ranked.rn > ranked.max_agents
        """
    )


def downgrade() -> None:
    pass
