"""Add tenant-scoped work context and Task to Deliverable provenance.

Revision ID: add_task_work_context
Revises: promote_m3_text_primary
Create Date: 2026-07-31 15:30:00

The change is additive: existing Agent Task APIs and identifiers remain valid.
Legacy Tasks are backfilled from their owning Agent and keep their current
runtime source identity.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_task_work_context"
down_revision: str | Sequence[str] | None = "promote_m3_text_primary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    delivery_columns = {
        column["name"] for column in inspector.get_columns("deliverable_requests")
    }
    expected_task_columns = {
        "tenant_id",
        "intent",
        "origin_type",
        "executor_kind",
        "executor_snapshot",
        "group_id",
        "client_request_id",
        "request_fingerprint",
    }
    task_present = expected_task_columns & task_columns
    delivery_present = "task_id" in delivery_columns
    if task_present == expected_task_columns:
        # Fresh installs create Task from current ORM metadata, while the
        # Deliverable table is introduced by a later explicit migration using
        # its historical shape. Complete only that cross-object link.
        if not delivery_present:
            op.add_column(
                "deliverable_requests",
                sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
            op.create_foreign_key(
                "fk_deliverable_requests_tenant_task",
                "deliverable_requests",
                "tasks",
                ["tenant_id", "task_id"],
                ["tenant_id", "id"],
                ondelete="RESTRICT",
            )
            op.create_index(
                "ix_deliverable_requests_task_created",
                "deliverable_requests",
                ["task_id", "created_at"],
                unique=False,
            )
        return
    if task_present or delivery_present:
        raise RuntimeError("Partial Task work-context schema requires manual repair")

    op.add_column("tasks", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tasks", sa.Column("intent", sa.Text(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "origin_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'legacy_agent_task'"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "executor_kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'agent_employee'"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "executor_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("tasks", sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "tasks", sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("tasks", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))

    op.execute(
        """
        UPDATE tasks AS task
        SET tenant_id = agent.tenant_id,
            intent = COALESCE(NULLIF(BTRIM(task.description), ''), task.title),
            executor_snapshot = jsonb_build_object(
                'agent_id', agent.id::text,
                'agent_name', agent.name,
                'role_description', COALESCE(agent.role_description, '')
            )
        FROM agents AS agent
        WHERE agent.id = task.agent_id
          AND task.tenant_id IS NULL
        """
    )
    missing = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM tasks WHERE tenant_id IS NULL OR intent IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"Cannot tenant-scope {missing} legacy Tasks because their Agent is missing"
        )

    op.alter_column("tasks", "tenant_id", nullable=False)
    op.alter_column("tasks", "intent", nullable=False)
    op.create_foreign_key(
        "fk_tasks_tenant_id_tenants",
        "tasks",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tasks_group_id_groups",
        "tasks",
        "groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_tasks_origin_type",
        "tasks",
        "origin_type IN ('workbench', 'agent_page', 'agent_chat', 'group', "
        "'trigger', 'api', 'legacy_agent_task')",
    )
    op.create_check_constraint(
        "ck_tasks_executor_kind",
        "tasks",
        "executor_kind IN ('personal_assistant', 'agent_employee', "
        "'temporary_expert', 'group')",
    )
    op.create_check_constraint(
        "ck_tasks_client_fingerprint",
        "tasks",
        "(client_request_id IS NULL AND request_fingerprint IS NULL) OR "
        "(client_request_id IS NOT NULL AND request_fingerprint IS NOT NULL)",
    )
    op.create_unique_constraint("uq_tasks_tenant_id_id", "tasks", ["tenant_id", "id"])
    op.create_index(
        "uq_tasks_workbench_client_identity",
        "tasks",
        ["tenant_id", "created_by", "client_request_id"],
        unique=True,
        postgresql_where=sa.text("client_request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tasks_tenant_creator_updated",
        "tasks",
        ["tenant_id", "created_by", "updated_at"],
        unique=False,
    )

    op.add_column(
        "deliverable_requests",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_deliverable_requests_tenant_task",
        "deliverable_requests",
        "tasks",
        ["tenant_id", "task_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_deliverable_requests_task_created",
        "deliverable_requests",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_deliverable_requests_task_created", table_name="deliverable_requests")
    op.drop_constraint(
        "fk_deliverable_requests_tenant_task",
        "deliverable_requests",
        type_="foreignkey",
    )
    op.drop_column("deliverable_requests", "task_id")

    op.drop_index("ix_tasks_tenant_creator_updated", table_name="tasks")
    op.drop_index("uq_tasks_workbench_client_identity", table_name="tasks")
    op.drop_constraint("uq_tasks_tenant_id_id", "tasks", type_="unique")
    op.drop_constraint("ck_tasks_client_fingerprint", "tasks", type_="check")
    op.drop_constraint("ck_tasks_executor_kind", "tasks", type_="check")
    op.drop_constraint("ck_tasks_origin_type", "tasks", type_="check")
    op.drop_constraint("fk_tasks_group_id_groups", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_tenant_id_tenants", "tasks", type_="foreignkey")
    op.drop_column("tasks", "request_fingerprint")
    op.drop_column("tasks", "client_request_id")
    op.drop_column("tasks", "group_id")
    op.drop_column("tasks", "executor_snapshot")
    op.drop_column("tasks", "executor_kind")
    op.drop_column("tasks", "origin_type")
    op.drop_column("tasks", "intent")
    op.drop_column("tasks", "tenant_id")
