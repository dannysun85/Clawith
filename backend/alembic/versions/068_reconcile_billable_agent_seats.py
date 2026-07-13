"""Reconcile billable Agent seats after subscription quota semantics.

Revision ID: reconcile_billable_agent_seats
Revises: reconcile_free_agent_limit
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "reconcile_billable_agent_seats"
down_revision: Union[str, Sequence[str], None] = "reconcile_free_agent_limit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE plans SET max_agents = 1 WHERE code = 'free'")
    op.execute(
        """
        WITH active_subscriptions AS (
            SELECT DISTINCT ON (s.tenant_id)
                s.tenant_id,
                GREATEST(COALESCE(p.max_agents, 1), 0) AS max_agents
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.status IN ('active', 'trialing')
            ORDER BY s.tenant_id, s.created_at DESC
        ),
        ranked_agents AS (
            SELECT
                a.id,
                a.tenant_id,
                active_subscriptions.max_agents,
                ROW_NUMBER() OVER (
                    PARTITION BY a.tenant_id
                    ORDER BY
                        CASE
                            WHEN COALESCE(a.access_mode, 'company') = 'private'
                              OR a.role_description = 'Private Assistant'
                              OR t.name = 'Private Assistant'
                            THEN 0
                            ELSE 1
                        END ASC,
                        a.created_at ASC NULLS LAST,
                        a.id ASC
                ) AS rn
            FROM agents a
            JOIN active_subscriptions ON active_subscriptions.tenant_id = a.tenant_id
            LEFT JOIN agent_templates t ON t.id = a.template_id
            WHERE COALESCE(a.is_system, false) = false
              AND COALESCE(a.is_expired, false) = false
              AND a.status != 'error'
        )
        UPDATE agents AS a
        SET status = CASE
            WHEN ranked_agents.rn <= ranked_agents.max_agents THEN 'idle'::agent_status_enum
            ELSE 'stopped'::agent_status_enum
        END
        FROM ranked_agents
        WHERE a.id = ranked_agents.id
          AND (
              (ranked_agents.rn <= ranked_agents.max_agents AND a.status = 'stopped')
              OR (ranked_agents.rn > ranked_agents.max_agents AND a.status NOT IN ('stopped', 'error'))
          )
        """
    )


def downgrade() -> None:
    pass
