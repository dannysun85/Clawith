"""Reconcile Agent template lifecycle schema after a partially recorded upgrade.

The original lifecycle migration may already be recorded as the current
Alembic revision on a database where only the ``agent_templates`` changes were
applied.  Keep this repair migration idempotent so fresh databases (where the
original migration applied completely) and those older local databases both
reach the same ORM schema without a destructive downgrade.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "recon_agent_tpl_lifecycle"
down_revision: str | Sequence[str] | None = "agent_template_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGENT_TEMPLATE_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("role_key", sa.Column("role_key", sa.String(100), nullable=True)),
    ("role_revision", sa.Column("role_revision", sa.Integer(), nullable=False, server_default="1")),
    ("responsibilities", sa.Column("responsibilities", sa.JSON(), nullable=False, server_default="[]")),
    ("non_responsibilities", sa.Column("non_responsibilities", sa.JSON(), nullable=False, server_default="[]")),
    ("limitations", sa.Column("limitations", sa.JSON(), nullable=False, server_default="[]")),
    ("workflows", sa.Column("workflows", sa.JSON(), nullable=False, server_default="[]")),
    ("deliverables", sa.Column("deliverables", sa.JSON(), nullable=False, server_default="[]")),
    ("evaluation_criteria", sa.Column("evaluation_criteria", sa.JSON(), nullable=False, server_default="[]")),
    ("source_provenance", sa.Column("source_provenance", sa.JSON(), nullable=False, server_default="{}")),
    (
        "lifecycle_status",
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="enabled"),
    ),
    ("activation_gate", sa.Column("activation_gate", sa.Text(), nullable=True)),
    ("workforce_source_role_id", sa.Column("workforce_source_role_id", sa.String(100), nullable=True)),
    ("workforce_decision", sa.Column("workforce_decision", sa.String(32), nullable=True)),
    ("workforce_pack", sa.Column("workforce_pack", sa.String(100), nullable=True)),
)

_AGENT_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("template_revision_applied", sa.Column("template_revision_applied", sa.Integer(), nullable=True)),
    (
        "template_sync_status",
        sa.Column("template_sync_status", sa.String(20), nullable=False, server_default="current"),
    ),
    (
        "template_sync_details",
        sa.Column("template_sync_details", sa.JSON(), nullable=False, server_default="{}"),
    ),
    ("template_synced_at", sa.Column("template_synced_at", sa.DateTime(timezone=True), nullable=True)),
)


def _add_missing_columns(table_name: str, columns: tuple[tuple[str, sa.Column], ...]) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    for name, column in columns:
        if name not in existing:
            op.add_column(table_name, column)


def _create_missing_indexes(table_name: str, names_to_columns: dict[str, list[str]]) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes(table_name)}
    for name, columns in names_to_columns.items():
        if name not in existing:
            op.create_index(name, table_name, columns)


def _create_evaluation_table_if_missing() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_template_evaluations" in inspector.get_table_names():
        return
    op.create_table(
        "agent_template_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_revision", sa.Integer(), nullable=False),
        sa.Column("evaluator_version", sa.String(50), nullable=False),
        sa.Column("fixture_set_version", sa.String(50), nullable=False),
        sa.Column("role_family", sa.String(40), nullable=False),
        sa.Column("baseline_metrics", sa.JSON(), nullable=False),
        sa.Column("candidate_metrics", sa.JSON(), nullable=False),
        sa.Column("fixture_results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("safety_pass", sa.Boolean(), nullable=False),
        sa.Column("capability_pass", sa.Boolean(), nullable=False),
        sa.Column("gate_status", sa.String(20), nullable=False),
        sa.Column("gate_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["promoted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["rolled_back_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["agent_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    _add_missing_columns("agent_templates", _AGENT_TEMPLATE_COLUMNS)
    _add_missing_columns("agents", _AGENT_COLUMNS)
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
              AND template_revision_applied IS NULL
            """
        )
    )
    _create_missing_indexes(
        "agent_templates",
        {
            "ix_agent_templates_role_key": ["role_key"],
            "ix_agent_templates_lifecycle_status": ["lifecycle_status"],
            "ix_agent_templates_workforce_source_role_id": ["workforce_source_role_id"],
            "ix_agent_templates_workforce_decision": ["workforce_decision"],
            "ix_agent_templates_workforce_pack": ["workforce_pack"],
        },
    )
    _create_evaluation_table_if_missing()
    inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes("agent_template_evaluations")}
    if "ix_agent_template_evaluations_template_revision_created" not in existing:
        op.create_index(
            "ix_agent_template_evaluations_template_revision_created",
            "agent_template_evaluations",
            ["template_id", "role_revision", "created_at"],
        )


def downgrade() -> None:
    # This repair is intentionally non-destructive.  The parent migration owns
    # the lifecycle schema and can be downgraded explicitly if ever required.
    pass
