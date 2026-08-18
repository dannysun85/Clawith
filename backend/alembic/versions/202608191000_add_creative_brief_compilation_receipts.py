"""Add creative-brief and prompt-compilation receipts for v2 deliverables.

Revision ID: creative_brief_compilation_receipts
Revises: legacy_assistant_lifecycle
Create Date: 2026-08-19 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "creative_brief_receipts"
down_revision: str | Sequence[str] | None = "legacy_assistant_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_TABLES = ("deliverable_creative_briefs", "deliverable_prompt_compilations")


def _jsonb() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _json_default(literal: str) -> sa.TextClause:
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{literal}'::jsonb")
    return sa.text(f"'{literal}'")


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    present = _existing_tables()
    found = {table for table in _NEW_TABLES if table in present}
    if found and found != set(_NEW_TABLES):
        raise RuntimeError(
            "Partial creative brief/compilation bootstrap; missing "
            + ", ".join(sorted(set(_NEW_TABLES) - found))
        )
    if found:
        return

    op.create_table(
        "deliverable_creative_briefs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=True),
        sa.Column("modality", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("brief", _jsonb(), server_default=_json_default("{}"), nullable=False),
        sa.Column("source_inventory", _jsonb(), server_default=_json_default("[]"), nullable=False),
        sa.Column("missing_fields", _jsonb(), server_default=_json_default("[]"), nullable=False),
        sa.Column("brief_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "modality IN ('image', 'video', 'presentation')",
            name="ck_deliverable_creative_briefs_modality",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'clarifying', 'confirmed')",
            name="ck_deliverable_creative_briefs_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_creative_briefs_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_creative_briefs_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["deliverable_executions.id"],
            name="fk_deliverable_creative_briefs_execution",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id"],
            ["agent_runs.id"],
            name="fk_deliverable_creative_briefs_run",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_creative_briefs"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "execution_id",
            "schema_version",
            name="uq_deliverable_creative_briefs_execution_schema",
        ),
    )
    op.create_index(
        "ix_deliverable_creative_briefs_tenant_created",
        "deliverable_creative_briefs",
        ["tenant_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "deliverable_prompt_compilations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=True),
        sa.Column("unit_id", sa.UUID(), nullable=True),
        sa.Column("compiler_version", sa.String(length=32), nullable=False),
        sa.Column("brief_sha256", sa.String(length=64), nullable=False),
        sa.Column("compiled_prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("compiled_prompt_path", sa.String(length=1000), nullable=False),
        sa.Column("provider_target", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_prompt_compilations_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_prompt_compilations_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["deliverable_executions.id"],
            name="fk_deliverable_prompt_compilations_execution",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["deliverable_execution_units.id"],
            name="fk_deliverable_prompt_compilations_unit",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_prompt_compilations"),
        sa.UniqueConstraint(
            "tenant_id",
            "execution_id",
            "unit_id",
            "compiler_version",
            name="uq_deliverable_prompt_compilations_unit_version",
        ),
    )
    op.create_index(
        "ix_deliverable_prompt_compilations_tenant_created",
        "deliverable_prompt_compilations",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    present = _existing_tables()
    if "deliverable_prompt_compilations" in present:
        op.drop_index(
            "ix_deliverable_prompt_compilations_tenant_created",
            table_name="deliverable_prompt_compilations",
        )
        op.drop_table("deliverable_prompt_compilations")
    if "deliverable_creative_briefs" in present:
        op.drop_index(
            "ix_deliverable_creative_briefs_tenant_created",
            table_name="deliverable_creative_briefs",
        )
        op.drop_table("deliverable_creative_briefs")
