"""Scope skill uniqueness by tenant.

Revision ID: scope_skills_by_tenant
Revises: add_agent_error_diagnostics
"""

from collections.abc import Sequence

from alembic import op


revision: str = "scope_skills_by_tenant"
down_revision: str | None = "add_agent_error_diagnostics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_name_key")
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_folder_name_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_skills_global_name "
        "ON skills (name) WHERE tenant_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_skills_global_folder_name "
        "ON skills (folder_name) WHERE tenant_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_skills_tenant_name "
        "ON skills (tenant_id, name) WHERE tenant_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_skills_tenant_folder_name "
        "ON skills (tenant_id, folder_name) WHERE tenant_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_skills_tenant_folder_name")
    op.execute("DROP INDEX IF EXISTS ux_skills_tenant_name")
    op.execute("DROP INDEX IF EXISTS ux_skills_global_folder_name")
    op.execute("DROP INDEX IF EXISTS ux_skills_global_name")
    op.create_unique_constraint("skills_name_key", "skills", ["name"])
    op.create_unique_constraint(
        "skills_folder_name_key",
        "skills",
        ["folder_name"],
    )
