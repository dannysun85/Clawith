"""Add reviewed lifecycle and provenance fields to Agent templates.

Revision ID: agent_template_lifecycle
Revises: media_task_agent_retention
Create Date: 2026-08-02 10:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "agent_template_lifecycle"
down_revision: str | Sequence[str] | None = "media_task_agent_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        "agent_templates", sa.Column("role_key", sa.String(100), nullable=True)
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("role_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("responsibilities", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("non_responsibilities", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("limitations", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("workflows", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("deliverables", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("evaluation_criteria", sa.JSON(), nullable=False, server_default="[]"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("source_provenance", sa.JSON(), nullable=False, server_default="{}"),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column(
            "lifecycle_status",
            sa.String(32),
            nullable=False,
            server_default="enabled",
        ),
    )
    _add_column_if_missing(
        "agent_templates", sa.Column("activation_gate", sa.Text(), nullable=True)
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("workforce_source_role_id", sa.String(100), nullable=True),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("workforce_decision", sa.String(32), nullable=True),
    )
    _add_column_if_missing(
        "agent_templates",
        sa.Column("workforce_pack", sa.String(100), nullable=True),
    )
    for index_name, columns in (
        ("ix_agent_templates_role_key", ["role_key"]),
        ("ix_agent_templates_lifecycle_status", ["lifecycle_status"]),
        (
            "ix_agent_templates_workforce_source_role_id",
            ["workforce_source_role_id"],
        ),
        ("ix_agent_templates_workforce_decision", ["workforce_decision"]),
        ("ix_agent_templates_workforce_pack", ["workforce_pack"]),
    ):
        if index_name not in _index_names("agent_templates"):
            op.create_index(index_name, "agent_templates", columns)

    _add_column_if_missing(
        "agents",
        sa.Column("template_revision_applied", sa.Integer(), nullable=True),
    )
    _add_column_if_missing(
        "agents",
        sa.Column(
            "template_sync_status",
            sa.String(20),
            nullable=False,
            server_default="current",
        ),
    )
    _add_column_if_missing(
        "agents",
        sa.Column("template_sync_details", sa.JSON(), nullable=False, server_default="{}"),
    )
    _add_column_if_missing(
        "agents",
        sa.Column("template_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    # ``001_initial_schema`` creates tables from current ORM metadata. Ensure
    # fresh installs receive the same server defaults as upgraded databases.
    op.alter_column("agent_templates", "role_revision", server_default="1")
    for column_name in (
        "responsibilities",
        "non_responsibilities",
        "limitations",
        "workflows",
        "deliverables",
        "evaluation_criteria",
    ):
        op.alter_column(
            "agent_templates",
            column_name,
            server_default=sa.text("'[]'::json"),
        )
    op.alter_column(
        "agent_templates",
        "source_provenance",
        server_default=sa.text("'{}'::json"),
    )
    op.alter_column(
        "agent_templates", "lifecycle_status", server_default="enabled"
    )
    op.alter_column("agents", "template_sync_status", server_default="current")
    op.alter_column(
        "agents",
        "template_sync_details",
        server_default=sa.text("'{}'::json"),
    )
    op.execute(
        sa.text(
            """
            UPDATE agents
            SET template_revision_applied = (
                    SELECT agent_templates.role_revision
                    FROM agent_templates
                    WHERE agent_templates.id = agents.template_id
                ),
                template_synced_at = CURRENT_TIMESTAMP
            WHERE template_id IS NOT NULL
            """
        )
    )
    if not sa.inspect(op.get_bind()).has_table("agent_template_evaluations"):
        op.create_table(
            "agent_template_evaluations",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("template_id", sa.UUID(), nullable=False),
            sa.Column("role_revision", sa.Integer(), nullable=False),
            sa.Column("evaluator_version", sa.String(50), nullable=False),
            sa.Column("fixture_set_version", sa.String(50), nullable=False),
            sa.Column("role_family", sa.String(40), nullable=False),
            sa.Column("baseline_metrics", sa.JSON(), nullable=False),
            sa.Column("candidate_metrics", sa.JSON(), nullable=False),
            sa.Column("fixture_results", sa.JSON(), nullable=False),
            sa.Column("safety_pass", sa.Boolean(), nullable=False),
            sa.Column("capability_pass", sa.Boolean(), nullable=False),
            sa.Column("gate_status", sa.String(20), nullable=False),
            sa.Column("gate_reasons", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.UUID(), nullable=False),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("promoted_by", sa.UUID(), nullable=True),
            sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rolled_back_by", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["promoted_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["rolled_back_by"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["template_id"], ["agent_templates.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    if (
        "ix_agent_template_evaluations_template_revision_created"
        not in _index_names("agent_template_evaluations")
    ):
        op.create_index(
            "ix_agent_template_evaluations_template_revision_created",
            "agent_template_evaluations",
            ["template_id", "role_revision", "created_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_template_evaluations_template_revision_created",
        table_name="agent_template_evaluations",
    )
    op.drop_table("agent_template_evaluations")
    op.drop_column("agents", "template_synced_at")
    op.drop_column("agents", "template_sync_details")
    op.drop_column("agents", "template_sync_status")
    op.drop_column("agents", "template_revision_applied")
    op.drop_index("ix_agent_templates_workforce_pack", table_name="agent_templates")
    op.drop_index("ix_agent_templates_workforce_decision", table_name="agent_templates")
    op.drop_index(
        "ix_agent_templates_workforce_source_role_id",
        table_name="agent_templates",
    )
    op.drop_index("ix_agent_templates_lifecycle_status", table_name="agent_templates")
    op.drop_index("ix_agent_templates_role_key", table_name="agent_templates")
    op.drop_column("agent_templates", "workforce_pack")
    op.drop_column("agent_templates", "workforce_decision")
    op.drop_column("agent_templates", "workforce_source_role_id")
    op.drop_column("agent_templates", "activation_gate")
    op.drop_column("agent_templates", "lifecycle_status")
    op.drop_column("agent_templates", "source_provenance")
    op.drop_column("agent_templates", "evaluation_criteria")
    op.drop_column("agent_templates", "deliverables")
    op.drop_column("agent_templates", "workflows")
    op.drop_column("agent_templates", "limitations")
    op.drop_column("agent_templates", "non_responsibilities")
    op.drop_column("agent_templates", "responsibilities")
    op.drop_column("agent_templates", "role_revision")
    op.drop_column("agent_templates", "role_key")
