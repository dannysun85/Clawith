"""Add durable approval execution claims and unique Agent tool grants.

Revision ID: durable_approval_execution
Revises: secure_code_execution_defaults
Create Date: 2026-07-15

Approval resolution must commit before an external side effect is claimed.
The execution columns provide that durable hand-off without changing the
existing pending/approved/rejected product status.  Duplicate AgentTool rows
are quarantined fail-closed before the database starts enforcing uniqueness.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "durable_approval_execution"
down_revision: str | Sequence[str] | None = "secure_code_execution_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPROVAL_TABLE = "approval_requests"
AGENT_TOOL_TABLE = "agent_tools"


def _column_names(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _check_constraint_names(table_name: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def _has_unique_constraint(table_name: str, columns: list[str]) -> bool:
    return any(
        constraint.get("column_names") == columns
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
    )


def upgrade() -> None:
    # The historical bootstrap revision creates tables from current ORM
    # metadata. A genuinely empty database can therefore already contain this
    # revision's columns, indexes, and constraints before Alembic reaches 096.
    # Production upgrades still arrive with none of them. Inspect each object
    # so both paths converge on the same schema without duplicate DDL.
    columns = _column_names(APPROVAL_TABLE)
    additions = {
        "request_fingerprint": sa.Column(
            "request_fingerprint",
            sa.String(length=64),
            nullable=True,
        ),
        "execution_status": sa.Column(
            "execution_status",
            sa.String(length=32),
            nullable=True,
        ),
        "execution_claim_token": sa.Column(
            "execution_claim_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        "execution_not_before": sa.Column(
            "execution_not_before",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "execution_claimed_at": sa.Column(
            "execution_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "execution_finished_at": sa.Column(
            "execution_finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "execution_attempts": sa.Column(
            "execution_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        "execution_result_summary": sa.Column(
            "execution_result_summary",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        "execution_error_code": sa.Column(
            "execution_error_code",
            sa.String(length=100),
            nullable=True,
        ),
    }
    for column_name, column in additions.items():
        if column_name not in columns:
            op.add_column(APPROVAL_TABLE, column)

    op.execute(
        "UPDATE approval_requests SET execution_attempts = 0 "
        "WHERE execution_attempts IS NULL"
    )
    op.execute(
        "UPDATE approval_requests SET execution_result_summary = '{}'::json "
        "WHERE execution_result_summary IS NULL"
    )
    op.alter_column(
        APPROVAL_TABLE,
        "execution_attempts",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )
    op.alter_column(
        APPROVAL_TABLE,
        "execution_result_summary",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::json"),
    )

    index_names = _index_names(APPROVAL_TABLE)
    if "ix_approval_requests_execution_status" not in index_names:
        op.create_index(
            "ix_approval_requests_execution_status",
            APPROVAL_TABLE,
            ["execution_status"],
        )
    if "ix_approval_requests_execution_not_before" not in index_names:
        op.create_index(
            "ix_approval_requests_execution_not_before",
            APPROVAL_TABLE,
            ["execution_not_before"],
        )
    if "ix_approval_requests_request_fingerprint" not in index_names:
        op.create_index(
            "ix_approval_requests_request_fingerprint",
            APPROVAL_TABLE,
            ["request_fingerprint"],
        )
    op.execute(
        """
        UPDATE approval_requests
        SET execution_status = 'legacy'
        WHERE status IN ('approved', 'rejected')
        """
    )
    op.execute(
        """
        UPDATE approval_requests
        SET execution_status = 'invalid'
        WHERE status = 'pending'
        """
    )
    check_names = _check_constraint_names(APPROVAL_TABLE)
    if "ck_approval_execution_status" not in check_names:
        op.create_check_constraint(
            "ck_approval_execution_status",
            APPROVAL_TABLE,
            "execution_status IS NULL OR execution_status IN "
            "('legacy', 'invalid', 'pending', 'executing', 'succeeded', "
            "'failed', 'ambiguous', 'not_required')",
        )
    if "ck_approval_execution_single_attempt" not in check_names:
        op.create_check_constraint(
            "ck_approval_execution_single_attempt",
            APPROVAL_TABLE,
            "execution_attempts >= 0 AND execution_attempts <= 1",
        )
    if "ck_approval_execution_state_consistency" not in check_names:
        op.create_check_constraint(
            "ck_approval_execution_state_consistency",
            APPROVAL_TABLE,
            """
        (
            status = 'pending'
            AND (execution_status IS NULL OR execution_status = 'invalid')
            AND execution_attempts = 0
            AND execution_claim_token IS NULL
            AND execution_claimed_at IS NULL
            AND execution_finished_at IS NULL
        ) OR (
            status = 'rejected'
            AND execution_status IN ('legacy', 'not_required')
            AND execution_attempts = 0
            AND execution_claim_token IS NULL
            AND execution_claimed_at IS NULL
            AND execution_finished_at IS NULL
        ) OR (
            status = 'approved'
            AND (
                (
                    execution_status = 'legacy'
                    AND execution_attempts = 0
                    AND execution_claim_token IS NULL
                    AND execution_claimed_at IS NULL
                    AND execution_finished_at IS NULL
                ) OR (
                    execution_status = 'pending'
                    AND execution_attempts = 0
                    AND execution_claim_token IS NULL
                    AND execution_claimed_at IS NULL
                    AND execution_finished_at IS NULL
                ) OR (
                    execution_status = 'executing'
                    AND execution_attempts = 1
                    AND execution_claim_token IS NOT NULL
                    AND execution_claimed_at IS NOT NULL
                    AND execution_finished_at IS NULL
                ) OR (
                    execution_status IN ('succeeded', 'failed', 'ambiguous')
                    AND execution_attempts = 1
                    AND execution_claim_token IS NOT NULL
                    AND execution_claimed_at IS NOT NULL
                    AND execution_finished_at IS NOT NULL
                )
            )
        )
            """,
        )
    index_names = _index_names(APPROVAL_TABLE)
    if "uq_active_approval_request_fingerprint" not in index_names:
        op.create_index(
            "uq_active_approval_request_fingerprint",
            APPROVAL_TABLE,
            ["agent_id", "request_fingerprint"],
            unique=True,
            postgresql_where=sa.text(
                "request_fingerprint IS NOT NULL AND "
                "(status = 'pending' OR "
                "(status = 'approved' AND execution_status IN ('pending', 'executing')))"
            ),
        )
    if "ix_approval_execution_claimable" not in index_names:
        op.create_index(
            "ix_approval_execution_claimable",
            APPROVAL_TABLE,
            ["resolved_at", "id"],
            postgresql_where=sa.text(
                "status = 'approved' AND execution_status = 'pending' "
                "AND execution_attempts = 0"
            ),
        )

    # Historical writers used SELECT-then-INSERT without a unique constraint.
    # Any duplicate set is an ambiguous grant/configuration.  Keep one audit
    # row but revoke and clear it before deleting the extras.
    op.get_bind().exec_driver_sql("SET LOCAL astra.mcp_quarantine_restore = 'on'")
    op.execute(
        """
        WITH duplicate_groups AS (
            SELECT agent_id, tool_id
            FROM agent_tools
            GROUP BY agent_id, tool_id
            HAVING count(*) > 1
        )
        UPDATE agent_tools AS assignment
        SET enabled = false,
            config = '{}'::json,
            source = 'system',
            installed_by_agent_id = NULL
        FROM duplicate_groups AS duplicate
        WHERE assignment.agent_id = duplicate.agent_id
          AND assignment.tool_id = duplicate.tool_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY agent_id, tool_id
                    ORDER BY created_at NULLS FIRST, id
                ) AS row_number
            FROM agent_tools
        )
        DELETE FROM agent_tools AS assignment
        USING ranked
        WHERE assignment.id = ranked.id
          AND ranked.row_number > 1
        """
    )
    op.get_bind().exec_driver_sql("SET LOCAL astra.mcp_quarantine_restore = 'off'")
    if not _has_unique_constraint(AGENT_TOOL_TABLE, ["agent_id", "tool_id"]):
        op.create_unique_constraint(
            "uq_agent_tools_agent_tool",
            AGENT_TOOL_TABLE,
            ["agent_id", "tool_id"],
        )


def downgrade() -> None:
    op.drop_constraint(
        "uq_agent_tools_agent_tool",
        "agent_tools",
        type_="unique",
    )
    op.drop_index(
        "ix_approval_execution_claimable",
        table_name="approval_requests",
    )
    op.drop_index(
        "uq_active_approval_request_fingerprint",
        table_name="approval_requests",
    )
    op.drop_index(
        "ix_approval_requests_execution_status",
        table_name="approval_requests",
    )
    op.drop_index(
        "ix_approval_requests_execution_not_before",
        table_name="approval_requests",
    )
    op.drop_index(
        "ix_approval_requests_request_fingerprint",
        table_name="approval_requests",
    )
    op.drop_constraint(
        "ck_approval_execution_single_attempt",
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_approval_execution_state_consistency",
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_approval_execution_status",
        "approval_requests",
        type_="check",
    )
    op.drop_column("approval_requests", "execution_error_code")
    op.drop_column("approval_requests", "execution_result_summary")
    op.drop_column("approval_requests", "execution_attempts")
    op.drop_column("approval_requests", "execution_finished_at")
    op.drop_column("approval_requests", "execution_claimed_at")
    op.drop_column("approval_requests", "execution_not_before")
    op.drop_column("approval_requests", "execution_claim_token")
    op.drop_column("approval_requests", "execution_status")
    op.drop_column("approval_requests", "request_fingerprint")
