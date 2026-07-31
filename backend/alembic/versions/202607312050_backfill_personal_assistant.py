"""Adopt one unambiguous legacy private assistant per onboarding record.

Revision ID: backfill_private_assistant
Revises: add_experience_provenance
Create Date: 2026-07-31 20:50:00

Older companies could create the builtin Private Assistant template before the
onboarding relation became the product identity boundary. Only an exact,
single candidate created by the same user in the same tenant is adopted. Any
ambiguous history remains untouched for manual review.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "backfill_private_assistant"
down_revision: str | Sequence[str] | None = "add_experience_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATE_TABLE = "__backfill_private_assistant_state"


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {_STATE_TABLE} (
                onboarding_id UUID PRIMARY KEY,
                adopted_agent_id UUID NOT NULL,
                migrated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            WITH candidates AS (
                SELECT
                    onboarding.id AS onboarding_id,
                    (array_agg(agent.id ORDER BY agent.created_at, agent.id))[1]
                        AS agent_id
                FROM user_tenant_onboardings AS onboarding
                JOIN agents AS agent
                  ON agent.tenant_id = onboarding.tenant_id
                 AND agent.creator_id = onboarding.user_id
                 AND agent.deleted_at IS NULL
                JOIN agent_templates AS template
                  ON template.id = agent.template_id
                 AND template.name = 'Private Assistant'
                 AND template.is_builtin IS TRUE
                WHERE onboarding.personal_assistant_agent_id IS NULL
                GROUP BY onboarding.id
                HAVING count(*) = 1
            ), recorded AS (
                INSERT INTO {_STATE_TABLE} (onboarding_id, adopted_agent_id)
                SELECT onboarding_id, agent_id
                FROM candidates
                ON CONFLICT (onboarding_id) DO NOTHING
                RETURNING onboarding_id, adopted_agent_id
            )
            UPDATE user_tenant_onboardings AS onboarding
               SET personal_assistant_agent_id = recorded.adopted_agent_id,
                   updated_at = now()
              FROM recorded
             WHERE onboarding.id = recorded.onboarding_id
               AND onboarding.personal_assistant_agent_id IS NULL
            """
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _STATE_TABLE not in inspector.get_table_names():
        return
    op.execute(
        sa.text(
            f"""
            UPDATE user_tenant_onboardings AS onboarding
               SET personal_assistant_agent_id = NULL,
                   updated_at = now()
              FROM {_STATE_TABLE} AS state
             WHERE onboarding.id = state.onboarding_id
               AND onboarding.personal_assistant_agent_id = state.adopted_agent_id
            """
        )
    )
    op.execute(sa.text(f"DROP TABLE {_STATE_TABLE}"))
