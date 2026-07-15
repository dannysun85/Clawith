"""Require fresh explicit grants for every Code execution tool.

Revision ID: secure_code_execution_defaults
Revises: disable_system_okr_automation
Create Date: 2026-07-14

The application-level platform and tenant kill switches are fail-closed. This
migration also removes historical implicit Agent grants so a later operator
enablement cannot accidentally reactivate old assignments.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "secure_code_execution_defaults"
down_revision: str | Sequence[str] | None = "disable_system_okr_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CODE_TOOL_NAMES = (
    "execute_code",
    "execute_code_e2b",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
)


def _quoted_names() -> str:
    return ", ".join(f"'{name}'" for name in CODE_TOOL_NAMES)


def _ensure_tenant_settings_table() -> None:
    """Repair older databases where runtime create_all made this table ad hoc."""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("tenant_settings"):
        return
    op.create_table(
        "tenant_settings",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "key"),
    )


def upgrade() -> None:
    _ensure_tenant_settings_table()
    names = _quoted_names()
    bind = op.get_bind()
    # Production may install a persistent MCP quarantine trigger immediately
    # before this migration. This exact transaction is the trusted safe writer:
    # it backfills ownership and revokes ambiguous grants before restoration.
    bind.exec_driver_sql("SET LOCAL astra.mcp_quarantine_restore = 'on'")
    # Older seeders auto-enabled a Code tool when its Tool row was missing.
    # Materialize the complete catalog first so an application rollback cannot
    # reopen that create-and-auto-assign path on an incomplete database.
    bind.exec_driver_sql(
        f"""
        INSERT INTO tools (
            id, name, display_name, description, type, category, icon,
            parameters_schema, config, config_schema, enabled, is_default,
            source
        )
        SELECT
            gen_random_uuid(), seed.name, seed.name,
            'Code execution disabled pending explicit authorization',
            'builtin', 'code', 'C', '{{}}'::json, '{{}}'::json, '{{}}'::json,
            true, false, 'builtin'
        FROM (VALUES
            {", ".join(f"('{name}')" for name in CODE_TOOL_NAMES)}
        ) AS seed(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM tools AS existing WHERE existing.name = seed.name
        )
        """
    )
    bind.exec_driver_sql(
        f"""
        UPDATE tools
        SET is_default = false
        WHERE name IN ({names})
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tools
        SET config = (
            (
                COALESCE(config::jsonb, '{}'::jsonb)
                - 'api_url'
                - 'api_key'
            )
            || jsonb_build_object(
                'sandbox_type', CASE
                    WHEN name = 'execute_code_e2b' THEN 'e2b'
                    ELSE 'subprocess'
                END,
                'allow_network', false,
                'allow_unsafe_fallback_when_bwrap_missing', false
            )
        )::json
        WHERE name IN ('execute_code', 'execute_code_e2b')
        """
    )
    # Pre-create every missing Agent/Code assignment as disabled. This is
    # intentionally stronger than only updating existing rows: if application
    # code is rolled back to the historical seeder, its "create if missing"
    # behavior still cannot resurrect an enabled Code helper grant.
    bind.exec_driver_sql(
        f"""
        INSERT INTO agent_tools (
            id,
            agent_id,
            tool_id,
            enabled,
            config,
            source
        )
        SELECT
            gen_random_uuid(),
            agent.id,
            tool.id,
            false,
            '{{
                "allow_network": false,
                "allow_unsafe_fallback_when_bwrap_missing": false
            }}'::json,
            'system'
        FROM agents AS agent
        CROSS JOIN tools AS tool
        WHERE tool.name IN ({names})
          AND NOT EXISTS (
              SELECT 1
              FROM agent_tools AS existing
              WHERE existing.agent_id = agent.id
                AND existing.tool_id = tool.id
          )
        """
    )
    bind.exec_driver_sql(
        f"""
        UPDATE agent_tools AS assignment
        SET enabled = false,
            config = (
                COALESCE(assignment.config::jsonb, '{{}}'::jsonb)
                - 'sandbox_type'
                - 'api_url'
                - 'api_key'
                - 'cpu_limit'
                - 'memory_limit'
                - 'default_timeout'
                - 'max_timeout'
                - 'language_mapping'
                - 'allow_network'
                - 'allow_unsafe_fallback_when_bwrap_missing'
                || '{{
                    "allow_network": false,
                    "allow_unsafe_fallback_when_bwrap_missing": false
                }}'::jsonb
            )::json
        FROM tools AS tool
        WHERE assignment.tool_id = tool.id
          AND tool.name IN ({names})
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tenant_settings
        SET value = jsonb_set(
            COALESCE(value::jsonb, '{}'::jsonb),
            '{config}',
            (
                COALESCE(value::jsonb -> 'config', '{}'::jsonb)
                - 'sandbox_type'
                - 'api_url'
                - 'api_key'
                - 'cpu_limit'
                - 'memory_limit'
                - 'default_timeout'
                - 'max_timeout'
                - 'language_mapping'
                - 'allow_network'
                - 'allow_unsafe_fallback_when_bwrap_missing'
            )
                || '{
                    "allow_network": false,
                    "allow_unsafe_fallback_when_bwrap_missing": false
                }'::jsonb,
            true
        )
        WHERE key IN (
            'tool_config:execute_code',
            'tool_config:execute_code_e2b'
        )
        """
    )
    # Agent-installed MCP tools created before tenant scoping are safe to
    # preserve only when every assignment resolves to one exact company.
    bind.exec_driver_sql(
        """
        WITH ownership AS (
            SELECT
                tool.id AS tool_id,
                (array_agg(DISTINCT agent.tenant_id)
                    FILTER (WHERE agent.tenant_id IS NOT NULL))[1] AS tenant_id,
                count(DISTINCT agent.tenant_id) AS tenant_count
            FROM tools AS tool
            LEFT JOIN agent_tools AS assignment ON assignment.tool_id = tool.id
            LEFT JOIN agents AS agent ON agent.id = assignment.agent_id
            WHERE tool.type = 'mcp'
              AND tool.source = 'agent'
              AND tool.tenant_id IS NULL
            GROUP BY tool.id
        )
        UPDATE tools AS tool
        SET tenant_id = ownership.tenant_id
        FROM ownership
        WHERE tool.id = ownership.tool_id
          AND ownership.tenant_count = 1
        """
    )
    # A stale assignment must never make a tenant-scoped MCP tool visible to
    # another company. Run this only after the unique-owner legacy backfill so
    # valid historical credentials are preserved for their exact company.
    bind.exec_driver_sql(
        """
        UPDATE agent_tools AS assignment
        SET enabled = false,
            config = '{}'::json
        FROM tools AS tool,
             agents AS agent
        WHERE assignment.tool_id = tool.id
          AND assignment.agent_id = agent.id
          AND tool.type = 'mcp'
          AND (
              (
                  tool.source = 'agent'
                  AND (
                      tool.tenant_id IS NULL
                      OR agent.tenant_id IS NULL
                      OR tool.tenant_id <> agent.tenant_id
                  )
              )
              OR
              (
                  tool.source = 'admin'
                  AND tool.tenant_id IS NOT NULL
                  AND (
                      agent.tenant_id IS NULL
                      OR tool.tenant_id <> agent.tenant_id
                  )
              )
          )
        """
    )
    # Orphaned and cross-tenant shared legacy rows cannot be attributed safely.
    # Remove execution grants and credential-bearing fields for manual repair.
    bind.exec_driver_sql(
        """
        UPDATE agent_tools AS assignment
        SET enabled = false,
            config = '{}'::json
        FROM tools AS tool
        WHERE assignment.tool_id = tool.id
          AND tool.type = 'mcp'
          AND tool.source = 'agent'
          AND tool.tenant_id IS NULL
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tools
        SET enabled = false,
            config = '{}'::json,
            mcp_server_url = NULL
        WHERE type = 'mcp'
          AND source = 'agent'
          AND tenant_id IS NULL
        """
    )
    # Global Atlassian tool definitions may remain visible, but a platform
    # credential must never be inherited by every tenant through Tool.config.
    bind.exec_driver_sql(
        """
        UPDATE agent_tools AS assignment
        SET config = '{}'::json
        FROM tools AS tool
        WHERE assignment.tool_id = tool.id
          AND tool.mcp_server_name = 'Atlassian Rovo'
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE tools
        SET config = '{}'::json
        WHERE (
            name = 'atlassian_rovo'
            OR mcp_server_name = 'Atlassian Rovo'
        )
          AND tenant_id IS NULL
        """
    )
    # Do not leak the trusted-writer bypass into a later revision that Alembic
    # may execute in the same outer transaction.
    bind.exec_driver_sql("SET LOCAL astra.mcp_quarantine_restore = 'off'")


def downgrade() -> None:
    # A rollback must never silently restore historical code-execution grants.
    # Operators can explicitly re-authorize a tenant and Agent after rollback.
    pass
