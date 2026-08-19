"""Add per-tenant CEO orchestrator settings (FR-CEO-1/3/4/5).

Revision ID: ceo_orchestrator_settings
Revises: subscription_change_kind
Create Date: 2026-08-19 21:00:00

Expand-only: one new tenant-scoped table. Existing Agent/AgentTrigger/Group/OKR
tables are not touched, and no 094-retained OKR rows are read or written here.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "ceo_orchestrator_settings"
down_revision: str | Sequence[str] | None = "subscription_change_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_TABLE = "ceo_orchestrator_settings"


def _jsonb() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _json_default(literal: str) -> sa.TextClause:
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{literal}'::jsonb")
    return sa.text(f"'{literal}'")


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _NEW_TABLE in _existing_tables():
        return

    op.create_table(
        "ceo_orchestrator_settings",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("ceo_agent_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("enabled_by_user_id", sa.UUID(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("briefing_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("morning_meeting_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("meeting_group_id", sa.UUID(), nullable=True),
        sa.Column("daily_credit_cap", sa.Integer(), server_default="20", nullable=False),
        sa.Column("monthly_credit_cap", sa.Integer(), server_default="300", nullable=False),
        sa.Column(
            "meeting_member_agent_ids",
            _jsonb(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_ceo_orchestrator_settings_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ceo_agent_id"],
            ["agents.id"],
            name="fk_ceo_orchestrator_settings_ceo_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enabled_by_user_id"],
            ["users.id"],
            name="fk_ceo_orchestrator_settings_enabled_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["meeting_group_id"],
            ["groups.id"],
            name="fk_ceo_orchestrator_settings_meeting_group",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_ceo_orchestrator_settings"),
        sa.UniqueConstraint("ceo_agent_id", name="uq_ceo_orchestrator_settings_ceo_agent"),
    )


def downgrade() -> None:
    if _NEW_TABLE in _existing_tables():
        op.drop_table("ceo_orchestrator_settings")
