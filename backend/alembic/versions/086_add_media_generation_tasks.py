"""Add durable asynchronous media generation tasks.

Revision ID: add_media_generation_tasks
Revises: disable_heartbeat_default
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "add_media_generation_tasks"
down_revision: Union[str, Sequence[str], None] = "disable_heartbeat_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Successful media tasks emit a durable in-product notification. Older
    # databases normally have this table, but the historical fresh-install
    # migration chain only altered it when it already existed. Repair that
    # dependency before the worker can settle any paid task.
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("type", sa.String(length=50), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("link", sa.String(length=500), nullable=True),
            sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("sender_name", sa.String(length=100), nullable=True),
            sa.Column("is_read", sa.Boolean(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
        op.create_index("ix_notifications_agent_id", "notifications", ["agent_id"])
        op.create_index("ix_notifications_created_at", "notifications", ["created_at"])

    table_name = "media_generation_tasks"
    if not sa.inspect(op.get_bind()).has_table(table_name):
        op.create_table(
            table_name,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("modality", sa.String(length=20), nullable=False),
            sa.Column("model", sa.String(length=100), nullable=True),
            sa.Column("provider_task_id", sa.String(length=160), nullable=True),
            sa.Column("status", sa.String(length=24), server_default="submitting", nullable=False),
            sa.Column("metadata_path", sa.String(length=1000), nullable=False),
            sa.Column("output_path", sa.String(length=1000), nullable=False),
            sa.Column("request_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("last_response", postgresql.JSON(astext_type=sa.Text()), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["credential_id"], ["llm_credentials.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["reservation_id"], ["credit_reservations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "provider_task_id", name="uq_media_generation_provider_task"),
            sa.UniqueConstraint("reservation_id", name="uq_media_generation_reservation"),
        )

    expected_indexes = {
        "ix_media_generation_tasks_tenant_id": ["tenant_id"],
        "ix_media_generation_tasks_agent_id": ["agent_id"],
        "ix_media_generation_tasks_status": ["status"],
        "ix_media_generation_tasks_next_poll_at": ["next_poll_at"],
        "ix_media_generation_tasks_created_at": ["created_at"],
        "ix_media_generation_due": ["status", "next_poll_at"],
    }
    existing_indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    for index_name, columns in expected_indexes.items():
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    # Preserve provider identities and settlement audit state during a code
    # rollback. A later upgrade is idempotent when this table already exists.
    pass
