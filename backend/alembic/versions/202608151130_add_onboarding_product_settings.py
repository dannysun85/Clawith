"""Add company-initialization and membership-onboarding product settings.

Revision ID: onboarding_product_settings
Revises: harden_tenant_access_control
Create Date: 2026-08-15 11:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "onboarding_product_settings"
down_revision: str | Sequence[str] | None = "harden_tenant_access_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    tenant_columns = _column_names("tenants")
    if "company_size" not in tenant_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "company_size",
                sa.String(length=32),
                server_default="unspecified",
                nullable=False,
            ),
        )
    if "allow_member_private_agents" not in tenant_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "allow_member_private_agents",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )
    if "default_approval_policy" not in tenant_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "default_approval_policy",
                sa.String(length=32),
                server_default="high_risk",
                nullable=False,
            ),
        )

    user_columns = _column_names("users")
    if "timezone" not in user_columns:
        op.add_column("users", sa.Column("timezone", sa.String(length=50), nullable=True))
    if "work_hours_start" not in user_columns:
        op.add_column("users", sa.Column("work_hours_start", sa.String(length=5), nullable=True))
    if "work_hours_end" not in user_columns:
        op.add_column("users", sa.Column("work_hours_end", sa.String(length=5), nullable=True))

    # Existing companies must not be forced through a newly introduced setup
    # wizard. New companies keep NULL until an owner/admin explicitly confirms
    # the safe policy defaults.
    op.execute(
        sa.text(
            """
            UPDATE tenants
               SET initialization_completed_at = COALESCE(created_at, CURRENT_TIMESTAMP)
             WHERE initialization_completed_at IS NULL
            """
        )
    )


def downgrade() -> None:
    user_columns = _column_names("users")
    if "work_hours_end" in user_columns:
        op.drop_column("users", "work_hours_end")
    if "work_hours_start" in user_columns:
        op.drop_column("users", "work_hours_start")
    if "timezone" in user_columns:
        op.drop_column("users", "timezone")

    tenant_columns = _column_names("tenants")
    if "default_approval_policy" in tenant_columns:
        op.drop_column("tenants", "default_approval_policy")
    if "allow_member_private_agents" in tenant_columns:
        op.drop_column("tenants", "allow_member_private_agents")
    if "company_size" in tenant_columns:
        op.drop_column("tenants", "company_size")
