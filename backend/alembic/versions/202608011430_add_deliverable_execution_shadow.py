"""Add revision-safe Deliverable execution and unit shadow facts.

Revision ID: deliverable_execution_shadow
Revises: provider_verification_receipts
Create Date: 2026-08-01 14:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "deliverable_execution_shadow"
down_revision: str | Sequence[str] | None = "provider_verification_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


_NEW_TABLE_COLUMNS = {
    "deliverable_executions": {
        "id",
        "tenant_id",
        "request_id",
        "execution_number",
        "kind",
        "status",
        "current_stage",
        "intake_run_id",
        "coordinator_run_id",
        "launch_message_id",
        "workflow_id",
        "workflow_version",
        "contract_snapshot",
        "preflight_snapshot",
        "revision_instruction",
        "idempotency_key",
        "request_fingerprint",
        "blocked_reason",
        "last_error_code",
        "launched_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "deliverable_execution_units": {
        "id",
        "tenant_id",
        "request_id",
        "execution_id",
        "stage_key",
        "unit_key",
        "status",
        "dependency_hash",
        "attempt_count",
        "agent_run_id",
        "agent_tool_execution_id",
        "media_generation_task_id",
        "input_snapshot",
        "result_snapshot",
        "quality_evaluation",
        "last_error_code",
        "next_retry_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "deliverable_approval_receipts": {
        "id",
        "tenant_id",
        "request_id",
        "execution_id",
        "actor_user_id",
        "client_action_id",
        "request_fingerprint",
        "request_version",
        "stage",
        "action",
        "instruction",
        "target_units",
        "receipt",
        "created_at",
    },
}

_AUGMENTED_TABLE_COLUMNS = {
    "deliverable_requests": {
        "current_execution_id",
        "contract_revision",
        "latest_preflight",
    },
    "deliverable_artifact_revisions": {
        "execution_id",
        "unit_id",
        "stage_key",
        "unit_key",
    },
    "media_generation_tasks": {
        "deliverable_execution_id",
        "deliverable_unit_id",
    },
}

_REQUIRED_INDEX_NAMES = {
    "deliverable_executions": {
        "uq_deliverable_executions_active_request",
        "ix_deliverable_executions_request_created",
    },
    "deliverable_execution_units": {
        "ix_deliverable_units_execution_status",
    },
    "deliverable_approval_receipts": {
        "ix_deliverable_approval_receipts_request_created",
    },
}

_REQUIRED_UNIQUE_COLUMNS = {
    "deliverable_executions": {
        ("request_id", "execution_number"),
        ("request_id", "idempotency_key"),
        ("tenant_id", "id"),
        ("tenant_id", "request_id", "id"),
        ("intake_run_id",),
        ("coordinator_run_id",),
        ("launch_message_id",),
    },
    "deliverable_execution_units": {
        ("execution_id", "stage_key", "unit_key"),
        ("tenant_id", "id"),
        ("media_generation_task_id",),
    },
    "deliverable_approval_receipts": {
        ("tenant_id", "request_id", "client_action_id"),
    },
    "media_generation_tasks": {("deliverable_unit_id",)},
}

_REQUIRED_FOREIGN_KEYS = {
    "deliverable_executions": {
        "fk_deliverable_executions_tenant",
        "fk_deliverable_executions_tenant_request",
        "fk_deliverable_executions_intake_run",
        "fk_deliverable_executions_coordinator_run",
        "fk_deliverable_executions_launch_message",
    },
    "deliverable_execution_units": {
        "fk_deliverable_units_tenant",
        "fk_deliverable_units_tenant_request_execution",
        "fk_deliverable_units_agent_run",
        "fk_deliverable_units_tool_execution",
        "fk_deliverable_units_media_task",
    },
    "deliverable_approval_receipts": {
        "fk_deliverable_approval_receipts_tenant",
        "fk_deliverable_approval_receipts_tenant_request",
        "fk_deliverable_approval_receipts_execution",
        "fk_deliverable_approval_receipts_actor",
    },
    "deliverable_requests": {
        "fk_deliverable_requests_current_execution",
    },
    "deliverable_artifact_revisions": {
        "fk_deliverable_artifacts_execution",
        "fk_deliverable_artifacts_unit",
    },
    "media_generation_tasks": {
        "fk_media_generation_deliverable_execution",
        "fk_media_generation_deliverable_unit",
    },
}

_REQUIRED_CHECKS = {
    "deliverable_executions": {
        "ck_deliverable_executions_kind",
        "ck_deliverable_executions_status",
        "ck_deliverable_executions_number_positive",
    },
    "deliverable_execution_units": {
        "ck_deliverable_execution_units_status",
        "ck_deliverable_execution_units_attempt_count",
    },
    "deliverable_approval_receipts": {
        "ck_deliverable_approval_receipts_stage",
        "ck_deliverable_approval_receipts_action",
    },
    "deliverable_requests": {
        "ck_deliverable_requests_contract_revision_positive",
    },
}


def _precreated_execution_state(
    schema: dict[str, dict[str, set[str] | set[tuple[str, ...]]]],
) -> bool:
    """Validate a current-ORM bootstrap before adopting its R5 objects."""

    new_tables = set(_NEW_TABLE_COLUMNS)
    present_new_tables = set(schema) & new_tables
    augmented_columns_present = any(
        required & set(schema.get(table_name, {}).get("columns", set()))
        for table_name, required in _AUGMENTED_TABLE_COLUMNS.items()
    )
    if not present_new_tables:
        if augmented_columns_present:
            raise RuntimeError(
                "Partial deliverable execution schema has augmented columns "
                "without execution tables"
            )
        return False
    if present_new_tables != new_tables:
        raise RuntimeError(
            "Partial deliverable execution bootstrap schema; missing "
            + ", ".join(sorted(new_tables - present_new_tables))
        )

    required_columns = _NEW_TABLE_COLUMNS | _AUGMENTED_TABLE_COLUMNS
    for table_name, required in required_columns.items():
        if table_name not in schema:
            raise RuntimeError(
                f"Missing augmented deliverable execution table {table_name}"
            )
        missing = required - set(schema[table_name].get("columns", set()))
        if missing:
            raise RuntimeError(
                f"Incomplete {table_name} execution columns: "
                + ", ".join(sorted(missing))
            )

    for table_name, required in _REQUIRED_INDEX_NAMES.items():
        missing = required - set(schema[table_name].get("indexes", set()))
        if missing:
            raise RuntimeError(
                f"Incomplete {table_name} execution indexes: "
                + ", ".join(sorted(missing))
            )

    for table_name, required in _REQUIRED_UNIQUE_COLUMNS.items():
        present = set(schema[table_name].get("unique_columns", set()))
        missing = required - present
        if missing:
            raise RuntimeError(
                f"Incomplete {table_name} execution unique constraints: "
                + ", ".join("/".join(columns) for columns in sorted(missing))
            )

    for object_type, expected_by_table in (
        ("foreign_keys", _REQUIRED_FOREIGN_KEYS),
        ("checks", _REQUIRED_CHECKS),
    ):
        for table_name, required in expected_by_table.items():
            missing = required - set(
                schema[table_name].get(object_type, set())
            )
            if missing:
                raise RuntimeError(
                    f"Incomplete {table_name} execution {object_type}: "
                    + ", ".join(sorted(missing))
                )

    for table_name in new_tables:
        if schema[table_name].get("primary_key") != {"id"}:
            raise RuntimeError(
                f"Contradictory {table_name} execution primary key"
            )

    required_index_columns = {
        "deliverable_artifact_revisions": {("execution_id",)},
        "media_generation_tasks": {("deliverable_execution_id",)},
    }
    for table_name, required in required_index_columns.items():
        present = set(schema[table_name].get("index_columns", set()))
        # The current ORM bootstrap predates the explicit artifact index.  It
        # is the one safe missing object repaired by ``upgrade`` below.
        if table_name == "deliverable_artifact_revisions":
            continue
        if not required <= present:
            raise RuntimeError(
                f"Incomplete {table_name} execution index columns"
            )
    return True


def _inspected_execution_schema() -> dict[
    str, dict[str, set[str] | set[tuple[str, ...]]]
]:
    inspector = sa.inspect(op.get_bind())
    relevant_tables = (
        set(_NEW_TABLE_COLUMNS) | set(_AUGMENTED_TABLE_COLUMNS)
    ) & set(inspector.get_table_names())
    schema: dict[str, dict[str, set[str] | set[tuple[str, ...]]]] = {}
    for table_name in relevant_tables:
        indexes = inspector.get_indexes(table_name)
        schema[table_name] = {
            "columns": {
                column["name"] for column in inspector.get_columns(table_name)
            },
            "indexes": {
                index["name"] for index in indexes if index.get("name")
            },
            "index_columns": {
                tuple(index.get("column_names") or ()) for index in indexes
            },
            "unique_columns": {
                tuple(constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(table_name)
            },
            "foreign_keys": {
                constraint["name"]
                for constraint in inspector.get_foreign_keys(table_name)
                if constraint.get("name")
            },
            "checks": {
                constraint["name"]
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name")
            },
            "primary_key": set(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns", []
                )
            ),
        }
    return schema


def _named_shape_object(
    table_name: str,
    object_type: str,
    columns: tuple[str, ...],
) -> str:
    inspector = sa.inspect(op.get_bind())
    getter = (
        inspector.get_unique_constraints
        if object_type == "unique"
        else inspector.get_indexes
    )
    matches = [
        item.get("name")
        for item in getter(table_name)
        if tuple(item.get("column_names") or ()) == columns and item.get("name")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {object_type} on {table_name}{columns}, "
            f"found {len(matches)}"
        )
    return matches[0]


def upgrade() -> None:
    inspected_schema = _inspected_execution_schema()
    if _precreated_execution_state(inspected_schema):
        artifact_indexes = set(
            inspected_schema["deliverable_artifact_revisions"].get(
                "index_columns", set()
            )
        )
        if ("execution_id",) not in artifact_indexes:
            op.create_index(
                "ix_deliverable_artifacts_execution",
                "deliverable_artifact_revisions",
                ["execution_id"],
                unique=False,
            )
        return

    op.create_table(
        "deliverable_executions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ready", nullable=False),
        sa.Column("current_stage", sa.String(length=64), nullable=False),
        sa.Column("intake_run_id", sa.UUID(), nullable=True),
        sa.Column("coordinator_run_id", sa.UUID(), nullable=True),
        sa.Column("launch_message_id", sa.UUID(), nullable=True),
        sa.Column("workflow_id", sa.String(length=120), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("contract_snapshot", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("preflight_snapshot", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("revision_instruction", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('initial', 'revision', 'recovery')",
            name="ck_deliverable_executions_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'running', 'blocked', 'reconciling', "
            "'waiting_approval', 'succeeded', 'failed', 'cancelled')",
            name="ck_deliverable_executions_status",
        ),
        sa.CheckConstraint(
            "execution_number > 0",
            name="ck_deliverable_executions_number_positive",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_executions_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_executions_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["intake_run_id"],
            ["agent_runs.id"],
            name="fk_deliverable_executions_intake_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["coordinator_run_id"],
            ["agent_runs.id"],
            name="fk_deliverable_executions_coordinator_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["launch_message_id"],
            ["chat_messages.id"],
            name="fk_deliverable_executions_launch_message",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_executions"),
        sa.UniqueConstraint(
            "request_id",
            "execution_number",
            name="uq_deliverable_executions_request_number",
        ),
        sa.UniqueConstraint(
            "request_id",
            "idempotency_key",
            name="uq_deliverable_executions_request_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_deliverable_executions_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "id",
            name="uq_deliverable_executions_tenant_request_id",
        ),
        sa.UniqueConstraint("intake_run_id", name="uq_deliverable_executions_intake_run"),
        sa.UniqueConstraint(
            "coordinator_run_id",
            name="uq_deliverable_executions_coordinator_run",
        ),
        sa.UniqueConstraint(
            "launch_message_id",
            name="uq_deliverable_executions_launch_message",
        ),
    )
    op.create_index(
        "uq_deliverable_executions_active_request",
        "deliverable_executions",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('ready', 'running', 'blocked', 'reconciling', 'waiting_approval')"
        ),
    )
    op.create_index(
        "ix_deliverable_executions_request_created",
        "deliverable_executions",
        ["request_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "deliverable_execution_units",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("stage_key", sa.String(length=64), nullable=False),
        sa.Column("unit_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("dependency_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=True),
        sa.Column("agent_tool_execution_id", sa.UUID(), nullable=True),
        sa.Column("media_generation_task_id", sa.UUID(), nullable=True),
        sa.Column("input_snapshot", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_snapshot", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("quality_evaluation", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'reconciling', "
            "'succeeded', 'failed', 'cancelled', 'superseded')",
            name="ck_deliverable_execution_units_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_deliverable_execution_units_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_units_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id", "execution_id"],
            [
                "deliverable_executions.tenant_id",
                "deliverable_executions.request_id",
                "deliverable_executions.id",
            ],
            name="fk_deliverable_units_tenant_request_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name="fk_deliverable_units_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_tool_execution_id"],
            ["agent_tool_executions.id"],
            name="fk_deliverable_units_tool_execution",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["media_generation_task_id"],
            ["media_generation_tasks.id"],
            name="fk_deliverable_units_media_task",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_execution_units"),
        sa.UniqueConstraint(
            "execution_id",
            "stage_key",
            "unit_key",
            name="uq_deliverable_units_execution_stage_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_deliverable_units_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "media_generation_task_id",
            name="uq_deliverable_units_media_task",
        ),
    )
    op.create_index(
        "ix_deliverable_units_execution_status",
        "deliverable_execution_units",
        ["execution_id", "status"],
        unique=False,
    )

    op.create_table(
        "deliverable_approval_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("client_action_id", sa.UUID(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("target_units", _jsonb(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("receipt", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "stage IN ('brief', 'outline', 'composition', 'storyboard', 'final')",
            name="ck_deliverable_approval_receipts_stage",
        ),
        sa.CheckConstraint(
            "action IN ('approve', 'request_changes', 'cancel')",
            name="ck_deliverable_approval_receipts_action",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_deliverable_approval_receipts_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_approval_receipts_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["deliverable_executions.id"],
            name="fk_deliverable_approval_receipts_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_deliverable_approval_receipts_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable_approval_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "client_action_id",
            name="uq_deliverable_approval_receipts_client_action",
        ),
    )
    op.create_index(
        "ix_deliverable_approval_receipts_request_created",
        "deliverable_approval_receipts",
        ["request_id", "created_at"],
        unique=False,
    )

    op.add_column(
        "deliverable_requests",
        sa.Column("current_execution_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "deliverable_requests",
        sa.Column("contract_revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "deliverable_requests",
        sa.Column("latest_preflight", _jsonb(), nullable=True),
    )
    op.create_check_constraint(
        "ck_deliverable_requests_contract_revision_positive",
        "deliverable_requests",
        "contract_revision > 0",
    )
    op.create_foreign_key(
        "fk_deliverable_requests_current_execution",
        "deliverable_requests",
        "deliverable_executions",
        ["current_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for name, column_type in (
        ("execution_id", sa.UUID()),
        ("unit_id", sa.UUID()),
        ("stage_key", sa.String(length=64)),
        ("unit_key", sa.String(length=120)),
    ):
        op.add_column(
            "deliverable_artifact_revisions",
            sa.Column(name, column_type, nullable=True),
        )
    op.create_foreign_key(
        "fk_deliverable_artifacts_execution",
        "deliverable_artifact_revisions",
        "deliverable_executions",
        ["execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_deliverable_artifacts_unit",
        "deliverable_artifact_revisions",
        "deliverable_execution_units",
        ["unit_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_deliverable_artifacts_execution",
        "deliverable_artifact_revisions",
        ["execution_id"],
        unique=False,
    )

    op.add_column(
        "media_generation_tasks",
        sa.Column("deliverable_execution_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "media_generation_tasks",
        sa.Column("deliverable_unit_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_media_generation_deliverable_execution",
        "media_generation_tasks",
        "deliverable_executions",
        ["deliverable_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_media_generation_deliverable_unit",
        "media_generation_tasks",
        "deliverable_execution_units",
        ["deliverable_unit_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_media_generation_deliverable_execution",
        "media_generation_tasks",
        ["deliverable_execution_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_media_generation_deliverable_unit",
        "media_generation_tasks",
        ["deliverable_unit_id"],
    )


def downgrade() -> None:
    media_unit_unique_name = _named_shape_object(
        "media_generation_tasks",
        "unique",
        ("deliverable_unit_id",),
    )
    media_execution_index_name = _named_shape_object(
        "media_generation_tasks",
        "index",
        ("deliverable_execution_id",),
    )
    op.drop_constraint(
        media_unit_unique_name,
        "media_generation_tasks",
        type_="unique",
    )
    op.drop_index(
        media_execution_index_name,
        table_name="media_generation_tasks",
    )
    op.drop_constraint(
        "fk_media_generation_deliverable_unit",
        "media_generation_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_media_generation_deliverable_execution",
        "media_generation_tasks",
        type_="foreignkey",
    )
    op.drop_column("media_generation_tasks", "deliverable_unit_id")
    op.drop_column("media_generation_tasks", "deliverable_execution_id")

    op.drop_index(
        "ix_deliverable_artifacts_execution",
        table_name="deliverable_artifact_revisions",
    )
    op.drop_constraint(
        "fk_deliverable_artifacts_unit",
        "deliverable_artifact_revisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_deliverable_artifacts_execution",
        "deliverable_artifact_revisions",
        type_="foreignkey",
    )
    for name in ("unit_key", "stage_key", "unit_id", "execution_id"):
        op.drop_column("deliverable_artifact_revisions", name)

    op.drop_constraint(
        "fk_deliverable_requests_current_execution",
        "deliverable_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_deliverable_requests_contract_revision_positive",
        "deliverable_requests",
        type_="check",
    )
    op.drop_column("deliverable_requests", "latest_preflight")
    op.drop_column("deliverable_requests", "contract_revision")
    op.drop_column("deliverable_requests", "current_execution_id")

    op.drop_index(
        "ix_deliverable_approval_receipts_request_created",
        table_name="deliverable_approval_receipts",
    )
    op.drop_table("deliverable_approval_receipts")
    op.drop_index(
        "ix_deliverable_units_execution_status",
        table_name="deliverable_execution_units",
    )
    op.drop_table("deliverable_execution_units")
    op.drop_index(
        "ix_deliverable_executions_request_created",
        table_name="deliverable_executions",
    )
    op.drop_index(
        "uq_deliverable_executions_active_request",
        table_name="deliverable_executions",
    )
    op.drop_table("deliverable_executions")
