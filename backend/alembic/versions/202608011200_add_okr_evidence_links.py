"""Link OKR progress updates to verified work evidence.

Revision ID: okr_evidence_links
Revises: task_confirmation_contract
Create Date: 2026-08-01 12:00:00

Progress logs remain immutable audit entries.  The nullable foreign keys keep
legacy and Agent-authored updates valid, while the JSON snapshot preserves the
business evidence that was visible when an administrator confirmed progress.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "okr_evidence_links"
down_revision: str | Sequence[str] | None = "task_confirmation_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _foreign_key_for_column(column_name: str) -> dict | None:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(
        "okr_progress_logs"
    ):
        if foreign_key.get("constrained_columns") == [column_name]:
            return foreign_key
    return None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("okr_progress_logs")
    }
    expected = {
        "source_task_id",
        "source_deliverable_request_id",
        "evidence_snapshot",
    }
    present = expected & columns
    if present == expected:
        # The metadata-backed bootstrap revision creates the current columns,
        # foreign keys, and indexes on a fresh database.  Adopt that complete
        # shape, but fail closed if the columns point at unexpected objects.
        expected_targets = {
            "source_task_id": "tasks",
            "source_deliverable_request_id": "deliverable_requests",
        }
        for column_name, target_table in expected_targets.items():
            foreign_key = _foreign_key_for_column(column_name)
            if foreign_key is None:
                raise RuntimeError(
                    f"OKR evidence column {column_name} is missing its foreign key"
                )
            if (
                foreign_key.get("referred_table") != target_table
                or foreign_key.get("referred_columns") != ["id"]
            ):
                raise RuntimeError(
                    f"OKR evidence column {column_name} has a contradictory foreign key"
                )
        index_names = {
            index.get("name")
            for index in inspector.get_indexes("okr_progress_logs")
        }
        if "ix_okr_progress_logs_source_task_id" not in index_names:
            op.create_index(
                "ix_okr_progress_logs_source_task_id",
                "okr_progress_logs",
                ["source_task_id"],
                unique=False,
            )
        if "ix_okr_progress_logs_source_delivery_id" not in index_names:
            op.create_index(
                "ix_okr_progress_logs_source_delivery_id",
                "okr_progress_logs",
                ["source_deliverable_request_id"],
                unique=False,
            )
        return
    if present:
        raise RuntimeError("Partial OKR evidence schema requires manual repair")

    op.add_column(
        "okr_progress_logs",
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "okr_progress_logs",
        sa.Column(
            "source_deliverable_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "okr_progress_logs",
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_okr_progress_logs_source_task",
        "okr_progress_logs",
        "tasks",
        ["source_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_okr_progress_logs_source_delivery",
        "okr_progress_logs",
        "deliverable_requests",
        ["source_deliverable_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_okr_progress_logs_source_task_id",
        "okr_progress_logs",
        ["source_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_okr_progress_logs_source_delivery_id",
        "okr_progress_logs",
        ["source_deliverable_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_okr_progress_logs_source_delivery_id",
        table_name="okr_progress_logs",
    )
    op.drop_index("ix_okr_progress_logs_source_task_id", table_name="okr_progress_logs")
    delivery_foreign_key = _foreign_key_for_column(
        "source_deliverable_request_id"
    )
    if delivery_foreign_key and delivery_foreign_key.get("name"):
        op.drop_constraint(
            delivery_foreign_key["name"],
            "okr_progress_logs",
            type_="foreignkey",
        )
    task_foreign_key = _foreign_key_for_column("source_task_id")
    if task_foreign_key and task_foreign_key.get("name"):
        op.drop_constraint(
            task_foreign_key["name"],
            "okr_progress_logs",
            type_="foreignkey",
        )
    op.drop_column("okr_progress_logs", "evidence_snapshot")
    op.drop_column("okr_progress_logs", "source_deliverable_request_id")
    op.drop_column("okr_progress_logs", "source_task_id")
