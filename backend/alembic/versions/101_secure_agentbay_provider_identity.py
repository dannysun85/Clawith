"""Enforce one live owner for each AgentBay provider session.

Revision ID: agentbay_provider_identity
Revises: model_route_integrity
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "agentbay_provider_identity"
down_revision: str | Sequence[str] | None = "model_route_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_agentbay_live_provider_session_id"


def upgrade() -> None:
    # Historical rows may bind one provider sandbox to several user/chat
    # lanes. Ownership cannot be guessed safely, so quarantine every live
    # claimant before installing the uniqueness fence. One deterministic
    # poison row retains the provider identity so no new live claim can reuse
    # it; the other evidence rows keep a pointer to that canonical row.
    op.execute(
        sa.text(
            """
            WITH ranked_claims AS (
                SELECT
                    id,
                    provider_session_id,
                    row_number() OVER (
                        PARTITION BY provider_session_id
                        ORDER BY id
                    ) AS claim_rank,
                    first_value(id) OVER (
                        PARTITION BY provider_session_id
                        ORDER BY id
                    ) AS keeper_id,
                    count(*) OVER (
                        PARTITION BY provider_session_id
                    ) AS claim_count
                FROM agentbay_session_ledger
                WHERE provider_session_id IS NOT NULL
                  AND status IN ('active', 'cleanup_required')
            )
            UPDATE agentbay_session_ledger AS ledger
            SET status = 'provider_identity_collision',
                close_reason = 'provider_identity_collision',
                provider_session_id = CASE
                    WHEN ranked.claim_rank = 1
                    THEN ranked.provider_session_id
                    ELSE NULL
                END,
                error_message = CASE
                    WHEN COALESCE(ledger.error_message, '') = ''
                    THEN 'Migration quarantined duplicate live provider session ownership'
                    ELSE ledger.error_message
                END,
                context = (
                    COALESCE(ledger.context::jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        'provider_identity_collision_ledger_id',
                        ranked.keeper_id::text
                    )
                )::json,
                updated_at = now()
            FROM ranked_claims AS ranked
            WHERE ledger.id = ranked.id
              AND ranked.claim_count > 1
            """
        )
    )
    # The repository's fresh bootstrap revision reflects current ORM metadata,
    # so a new database may already contain this index before revision 101 is
    # reached. Rebuild it after quarantine so fresh and historical upgrade
    # paths converge on the exact same predicate.
    op.execute(sa.text(f'DROP INDEX IF EXISTS "{_INDEX_NAME}"'))
    op.create_index(
        _INDEX_NAME,
        "agentbay_session_ledger",
        ["provider_session_id"],
        unique=True,
        postgresql_where=sa.text(
            "provider_session_id IS NOT NULL "
            "AND status IN "
            "('active', 'cleanup_required', 'provider_identity_collision')"
        ),
        sqlite_where=sa.text(
            "provider_session_id IS NOT NULL "
            "AND status IN "
            "('active', 'cleanup_required', 'provider_identity_collision')"
        ),
    )
    # 099 temporarily paused every automatic lane while its durable requester
    # and per-Agent serialization fences were installed. This release enables
    # only durable user automation; correct the retained operator notice
    # without touching each user's is_enabled intent. Legacy schedule/todo
    # runners and system OKR/CEO automation remain independently disabled.
    op.execute(
        sa.text(
            """
            UPDATE notifications
            SET title = 'User automation security upgrade completed',
                body = 'Durable user triggers and approved actions can run again. Legacy schedules and todo tasks are retained but automatic execution remains paused. Platform CEO heartbeat and seeded OKR automation remain disabled.'
            WHERE title = 'Automatic triggers paused for safety review'
              AND body LIKE 'Trigger configuration was retained,%'
            """
        )
    )
    # These two AgentBay tools cannot satisfy the RC5 ownership and bounded-I/O
    # contracts yet. Disable both the catalog rows and every historical grant
    # during migration, before any candidate application process starts. The
    # runtime and startup seeder apply the same fail-closed policy.
    op.execute(
        sa.text(
            """
            UPDATE agent_tools
            SET enabled = false,
                config = '{}'::json
            WHERE tool_id IN (
                SELECT id
                FROM tools
                WHERE name IN (
                    'agentbay_browser_login',
                    'agentbay_file_transfer'
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tools
            SET enabled = false,
                is_default = false,
                parameters_schema = '{"type":"object","properties":{}}'::json,
                config = '{}'::json,
                config_schema = '{}'::json,
                updated_at = now()
            WHERE name IN (
                'agentbay_browser_login',
                'agentbay_file_transfer'
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="agentbay_session_ledger")
    # Collision rows are evidence of ambiguous provider ownership and must not
    # be guessed back into an active lane during rollback.
    # Release-disabled tools and grants also remain disabled deliberately.
