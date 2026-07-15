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
            (COALESCE(config::jsonb, '{}'::jsonb) - 'api_url')
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


def downgrade() -> None:
    # A rollback must never silently restore historical code-execution grants.
    # Operators can explicitly re-authorize a tenant and Agent after rollback.
    pass
