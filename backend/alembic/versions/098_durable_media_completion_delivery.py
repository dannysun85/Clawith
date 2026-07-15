"""Persist asynchronous media completion delivery and quarantine stale video tools.

Revision ID: durable_media_completion
Revises: durable_issue_alert_delivery
Create Date: 2026-07-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "durable_media_completion"
down_revision: str | Sequence[str] | None = "durable_issue_alert_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "media_generation_tasks"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    }


def _index_names() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)
        if index.get("name")
    }


def _constraint_names(kind: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    loader = {
        "check": inspector.get_check_constraints,
        "unique": inspector.get_unique_constraints,
    }[kind]
    return {
        str(constraint["name"])
        for constraint in loader(TABLE_NAME)
        if constraint.get("name")
    }


def _has_foreign_key(columns: list[str], referred_table: str) -> bool:
    return any(
        foreign_key.get("constrained_columns") == columns
        and foreign_key.get("referred_table") == referred_table
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(TABLE_NAME)
    )


def _has_unique_constraint(columns: list[str]) -> bool:
    return any(
        constraint.get("column_names") == columns
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(TABLE_NAME)
    )


def upgrade() -> None:
    columns = _column_names()
    additions = {
        "origin_session_id": sa.Column(
            "origin_session_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        "completion_message_id": sa.Column(
            "completion_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        "output_size": sa.Column("output_size", sa.BigInteger(), nullable=True),
        "completion_delivery_status": sa.Column(
            "completion_delivery_status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        "realtime_attempt_count": sa.Column(
            "realtime_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        "realtime_next_attempt_at": sa.Column(
            "realtime_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "realtime_published_at": sa.Column(
            "realtime_published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        "realtime_last_error": sa.Column(
            "realtime_last_error",
            sa.Text(),
            nullable=True,
        ),
    }
    for column_name, column in additions.items():
        if column_name not in columns:
            op.add_column(TABLE_NAME, column)

    # The repository's historical bootstrap migration creates tables from the
    # current ORM metadata. Normalize defaults as well as tolerating columns
    # that therefore already exist on a fresh database.
    op.execute(
        "UPDATE media_generation_tasks "
        "SET completion_delivery_status = 'pending' "
        "WHERE completion_delivery_status IS NULL"
    )
    op.execute(
        "UPDATE media_generation_tasks "
        "SET realtime_attempt_count = 0 "
        "WHERE realtime_attempt_count IS NULL"
    )
    op.alter_column(
        TABLE_NAME,
        "completion_delivery_status",
        existing_type=sa.String(length=24),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    op.alter_column(
        TABLE_NAME,
        "realtime_attempt_count",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )

    if not _has_foreign_key(["origin_session_id"], "chat_sessions"):
        op.create_foreign_key(
            "fk_media_generation_origin_session",
            TABLE_NAME,
            "chat_sessions",
            ["origin_session_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_foreign_key(["completion_message_id"], "chat_messages"):
        op.create_foreign_key(
            "fk_media_generation_completion_message",
            TABLE_NAME,
            "chat_messages",
            ["completion_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_unique_constraint(["completion_message_id"]):
        op.create_unique_constraint(
            "uq_media_generation_completion_message",
            TABLE_NAME,
            ["completion_message_id"],
        )
    if "ck_media_generation_completion_delivery_status" not in _constraint_names("check"):
        op.create_check_constraint(
            "ck_media_generation_completion_delivery_status",
            TABLE_NAME,
            "completion_delivery_status IN ('pending', 'inline', 'persisted', 'not_applicable')",
        )

    index_names = _index_names()
    if "ix_media_generation_tasks_origin_session_id" not in index_names:
        op.create_index(
            "ix_media_generation_tasks_origin_session_id",
            TABLE_NAME,
            ["origin_session_id"],
        )
    if "ix_media_generation_completion_outbox" not in index_names:
        op.create_index(
            "ix_media_generation_completion_outbox",
            TABLE_NAME,
            ["realtime_next_attempt_at", "completed_at"],
            postgresql_where=sa.text(
                "status = 'succeeded' AND completion_message_id IS NOT NULL "
                "AND realtime_published_at IS NULL"
            ),
        )

    # Historical terminal rows predate the completion-delivery contract and
    # cannot be attached to a conversation without guessing an authorization
    # boundary. Keep them available through the workspace and notification UI.
    op.execute(
        """
        UPDATE media_generation_tasks
        SET completion_delivery_status = 'not_applicable'
        WHERE status IN ('succeeded', 'failed')
        """
    )

    # These names were persisted as builtin tools in production, but no
    # executor exists in this release. Disable them and their assignments
    # instead of routing an unknown builtin through MCP.
    op.execute(
        """
        UPDATE agent_tools
        SET enabled = false
        WHERE tool_id IN (
            SELECT id FROM tools
            WHERE name IN ('media_video_generate', 'media_video_edit')
              AND (source = 'builtin' OR type = 'builtin')
        )
        """
    )
    op.execute(
        """
        UPDATE tools
        SET enabled = false, is_default = false
        WHERE name IN ('media_video_generate', 'media_video_edit')
          AND (source = 'builtin' OR type = 'builtin')
        """
    )


def downgrade() -> None:
    # Stale tools stay disabled on rollback: re-enabling names with no executor
    # would restore a known production failure mode.
    op.drop_index("ix_media_generation_completion_outbox", table_name="media_generation_tasks")
    op.drop_index("ix_media_generation_tasks_origin_session_id", table_name="media_generation_tasks")
    op.drop_constraint(
        "ck_media_generation_completion_delivery_status",
        "media_generation_tasks",
        type_="check",
    )
    op.drop_constraint(
        "uq_media_generation_completion_message",
        "media_generation_tasks",
        type_="unique",
    )
    op.drop_constraint(
        "fk_media_generation_completion_message",
        "media_generation_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_media_generation_origin_session",
        "media_generation_tasks",
        type_="foreignkey",
    )
    op.drop_column("media_generation_tasks", "realtime_last_error")
    op.drop_column("media_generation_tasks", "realtime_published_at")
    op.drop_column("media_generation_tasks", "realtime_next_attempt_at")
    op.drop_column("media_generation_tasks", "realtime_attempt_count")
    op.drop_column("media_generation_tasks", "completion_delivery_status")
    op.drop_column("media_generation_tasks", "output_size")
    op.drop_column("media_generation_tasks", "completion_message_id")
    op.drop_column("media_generation_tasks", "origin_session_id")
