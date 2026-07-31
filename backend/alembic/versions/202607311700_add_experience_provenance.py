"""Add task and formal-delivery provenance to Experience.

Revision ID: add_experience_provenance
Revises: add_task_work_context
Create Date: 2026-07-31 17:00:00

The links are optional and additive. Existing chat-sourced Experience remains
valid, while workbench-created entries can be traced to authoritative objects.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_experience_provenance"
down_revision: str | Sequence[str] | None = "add_task_work_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("experience_entries")
    }
    present = {
        "source_task_id",
        "source_deliverable_request_id",
    } & columns
    if present == {"source_task_id", "source_deliverable_request_id"}:
        # The bootstrap revision creates current Experience columns from ORM
        # metadata before the later Deliverable migration runs. Add only any
        # FK that metadata could not safely precreate.
        foreign_keys = {
            foreign_key.get("name")
            for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(
                "experience_entries"
            )
        }
        if "fk_experience_entries_source_task" not in foreign_keys:
            op.create_foreign_key(
                "fk_experience_entries_source_task",
                "experience_entries",
                "tasks",
                ["source_task_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "fk_experience_entries_source_delivery" not in foreign_keys:
            op.create_foreign_key(
                "fk_experience_entries_source_delivery",
                "experience_entries",
                "deliverable_requests",
                ["source_deliverable_request_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return
    if present:
        raise RuntimeError("Partial Experience provenance schema requires manual repair")

    op.add_column(
        "experience_entries",
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "experience_entries",
        sa.Column(
            "source_deliverable_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_experience_entries_source_task",
        "experience_entries",
        "tasks",
        ["source_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_experience_entries_source_delivery",
        "experience_entries",
        "deliverable_requests",
        ["source_deliverable_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_experience_entries_source_task_id",
        "experience_entries",
        ["source_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_experience_entries_source_delivery_id",
        "experience_entries",
        ["source_deliverable_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experience_entries_source_delivery_id",
        table_name="experience_entries",
    )
    op.drop_index("ix_experience_entries_source_task_id", table_name="experience_entries")
    op.drop_constraint(
        "fk_experience_entries_source_delivery",
        "experience_entries",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_experience_entries_source_task",
        "experience_entries",
        type_="foreignkey",
    )
    op.drop_column("experience_entries", "source_deliverable_request_id")
    op.drop_column("experience_entries", "source_task_id")
