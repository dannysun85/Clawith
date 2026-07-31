"""Add durable deliverable request and artifact revision contracts.

Revision ID: add_deliverable_workbench
Revises: align_task_failed_status
Create Date: 2026-07-20 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_deliverable_workbench"
down_revision: str | Sequence[str] | None = "align_task_failed_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BOOTSTRAP_COLUMNS = {
    "deliverable_requests": {
        "id",
        "tenant_id",
        "created_by_user_id",
        "agent_id",
        "session_id",
        "agent_run_id",
        "launch_message_id",
        "client_request_id",
        "request_fingerprint",
        "work_type",
        "workflow_id",
        "workflow_version",
        "goal",
        "inputs",
        "spec",
        "tier",
        "approval_policy",
        "output_contract",
        "status",
        "current_stage",
        "version",
        "last_error_code",
        "launched_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "deliverable_artifact_revisions": {
        "id",
        "tenant_id",
        "request_id",
        "parent_revision_id",
        "approved_by_user_id",
        "artifact_key",
        "artifact_type",
        "workspace_path",
        "mime_type",
        "content_hash",
        "size_bytes",
        "revision_number",
        "status",
        "evaluation",
        "approved_at",
        "created_at",
    },
}

_BOOTSTRAP_INDEXES = {
    "deliverable_requests": {
        "ix_deliverable_requests_session_created",
        "ix_deliverable_requests_tenant_agent_created",
    },
    "deliverable_artifact_revisions": {
        "ix_deliverable_artifacts_request_created",
    },
}

_BOOTSTRAP_UNIQUES = {
    "deliverable_requests": {
        "uq_deliverable_requests_agent_run",
        "uq_deliverable_requests_launch_message",
        "uq_deliverable_requests_client_identity",
        "uq_deliverable_requests_tenant_id_id",
    },
    "deliverable_artifact_revisions": {
        "uq_deliverable_artifacts_request_key_revision",
    },
}

_BOOTSTRAP_FOREIGN_KEYS = {
    "deliverable_requests": {
        "fk_deliverable_requests_agent",
        "fk_deliverable_requests_agent_run",
        "fk_deliverable_requests_creator",
        "fk_deliverable_requests_launch_message",
        "fk_deliverable_requests_session",
        "fk_deliverable_requests_tenant",
    },
    "deliverable_artifact_revisions": {
        "fk_deliverable_artifacts_approver",
        "fk_deliverable_artifacts_parent",
        "fk_deliverable_artifacts_tenant_request",
        "fk_deliverable_artifacts_tenant",
    },
}

_BOOTSTRAP_CHECKS = {
    "deliverable_requests": {
        "ck_deliverable_requests_work_type",
        "ck_deliverable_requests_tier",
        "ck_deliverable_requests_status",
        "ck_deliverable_requests_version_positive",
    },
    "deliverable_artifact_revisions": {
        "ck_deliverable_artifacts_revision_positive",
        "ck_deliverable_artifacts_status",
    },
}


def _precreated_deliverable_state(
    schema: dict[str, dict[str, set[str]]],
) -> bool:
    """Adopt only the complete shape pre-created by ``001_initial_schema``.

    The historical bootstrap calls the current ORM metadata, so a fresh
    database can already contain these tables when this revision is reached.
    Extra columns and objects from later revisions are allowed, but a partial
    or contradictory shape is never silently accepted.
    """

    expected_tables = set(_BOOTSTRAP_COLUMNS)
    present_tables = set(schema) & expected_tables
    if not present_tables:
        return False
    if present_tables != expected_tables:
        missing = sorted(expected_tables - present_tables)
        raise RuntimeError(
            "Partial deliverable workbench bootstrap schema; missing tables: "
            + ", ".join(missing)
        )

    requirements = {
        "columns": _BOOTSTRAP_COLUMNS,
        "indexes": _BOOTSTRAP_INDEXES,
        "uniques": _BOOTSTRAP_UNIQUES,
        "foreign_keys": _BOOTSTRAP_FOREIGN_KEYS,
        "checks": _BOOTSTRAP_CHECKS,
    }
    for object_type, expected_by_table in requirements.items():
        for table_name, required in expected_by_table.items():
            present = schema[table_name].get(object_type, set())
            missing = required - present
            if missing:
                raise RuntimeError(
                    f"Incomplete {table_name} bootstrap {object_type}: "
                    + ", ".join(sorted(missing))
                )

    for table_name in expected_tables:
        if schema[table_name].get("primary_key") != {"id"}:
            raise RuntimeError(
                f"Contradictory {table_name} bootstrap primary key"
            )
    return True


def _inspected_bootstrap_schema() -> dict[str, dict[str, set[str]]]:
    inspector = sa.inspect(op.get_bind())
    expected_tables = set(_BOOTSTRAP_COLUMNS)
    present_tables = set(inspector.get_table_names()) & expected_tables
    return {
        table_name: {
            "columns": {
                column["name"] for column in inspector.get_columns(table_name)
            },
            "indexes": {
                index["name"]
                for index in inspector.get_indexes(table_name)
                if index.get("name")
            },
            "uniques": {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint.get("name")
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
        for table_name in present_tables
    }


def upgrade() -> None:
    if _precreated_deliverable_state(_inspected_bootstrap_schema()):
        return

    op.create_table(
        "deliverable_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("launch_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("work_type", sa.String(length=32), nullable=False),
        sa.Column("workflow_id", sa.String(length=120), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("approval_policy", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("output_contract", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'ready'"), nullable=False),
        sa.Column("current_stage", sa.String(length=64), server_default=sa.text("'brief_confirmed'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("work_type IN ('presentation', 'poster', 'video', 'report', 'spreadsheet')", name="ck_deliverable_requests_work_type"),
        sa.CheckConstraint("tier IN ('lite', 'pro', 'ultra')", name="ck_deliverable_requests_tier"),
        sa.CheckConstraint("status IN ('draft', 'ready', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled')", name="ck_deliverable_requests_status"),
        sa.CheckConstraint("version > 0", name="ck_deliverable_requests_version_positive"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name="fk_deliverable_requests_agent", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], name="fk_deliverable_requests_agent_run", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_deliverable_requests_creator", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["launch_message_id"], ["chat_messages.id"], name="fk_deliverable_requests_launch_message", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], name="fk_deliverable_requests_session", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_deliverable_requests_tenant", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", name="uq_deliverable_requests_agent_run"),
        sa.UniqueConstraint("launch_message_id", name="uq_deliverable_requests_launch_message"),
        sa.UniqueConstraint("tenant_id", "created_by_user_id", "client_request_id", name="uq_deliverable_requests_client_identity"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_deliverable_requests_tenant_id_id"),
    )
    op.create_index("ix_deliverable_requests_session_created", "deliverable_requests", ["session_id", "created_at"], unique=False)
    op.create_index("ix_deliverable_requests_tenant_agent_created", "deliverable_requests", ["tenant_id", "agent_id", "created_at"], unique=False)

    op.create_table(
        "deliverable_artifact_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_key", sa.String(length=100), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("workspace_path", sa.String(length=1000), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'candidate'"), nullable=False),
        sa.Column("evaluation", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revision_number > 0", name="ck_deliverable_artifacts_revision_positive"),
        sa.CheckConstraint("status IN ('candidate', 'approved', 'rejected', 'superseded')", name="ck_deliverable_artifacts_status"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], name="fk_deliverable_artifacts_approver", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_revision_id"], ["deliverable_artifact_revisions.id"], name="fk_deliverable_artifacts_parent", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["deliverable_requests.tenant_id", "deliverable_requests.id"],
            name="fk_deliverable_artifacts_tenant_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_deliverable_artifacts_tenant", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "artifact_key", "revision_number", name="uq_deliverable_artifacts_request_key_revision"),
    )
    op.create_index("ix_deliverable_artifacts_request_created", "deliverable_artifact_revisions", ["request_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_deliverable_artifacts_request_created", table_name="deliverable_artifact_revisions")
    op.drop_table("deliverable_artifact_revisions")
    op.drop_index("ix_deliverable_requests_tenant_agent_created", table_name="deliverable_requests")
    op.drop_index("ix_deliverable_requests_session_created", table_name="deliverable_requests")
    op.drop_table("deliverable_requests")
