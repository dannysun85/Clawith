"""Add transactional provider daily media allowance claims."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "media_daily_allowance_claims"
down_revision: str | Sequence[str] | None = "backfill_private_assistant_tpl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ALLOWANCE_TABLE = "media_provider_daily_allowance_claims"
_ALLOWANCE_COLUMNS = {
    "id",
    "credential_id",
    "provider",
    "modality",
    "allowance_date",
    "quota_snapshot",
    "status",
    "task_record_id",
    "provider_task_id",
    "accepted_at",
    "released_at",
    "release_reason",
    "created_at",
    "updated_at",
}


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {
        str(index["name"])
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }


def _assert_allowance_table_compatible() -> None:
    """Fail the release instead of accepting a half-created production table."""

    inspector = sa.inspect(op.get_bind())
    columns = _column_names(_ALLOWANCE_TABLE)
    missing_columns = sorted(_ALLOWANCE_COLUMNS.difference(columns))
    primary_key = inspector.get_pk_constraint(_ALLOWANCE_TABLE)
    primary_key_columns = set(primary_key.get("constrained_columns") or [])
    check_names = {
        str(item["name"])
        for item in inspector.get_check_constraints(_ALLOWANCE_TABLE)
        if item.get("name")
    }
    foreign_keys = {
        (
            tuple(item.get("constrained_columns") or []),
            str(item.get("referred_table") or ""),
            tuple(item.get("referred_columns") or []),
        )
        for item in inspector.get_foreign_keys(_ALLOWANCE_TABLE)
    }
    problems: list[str] = []
    if missing_columns:
        problems.append(f"missing columns: {', '.join(missing_columns)}")
    if primary_key_columns != {"id"}:
        problems.append("primary key must be exactly (id)")
    if "ck_media_provider_daily_allowance_claim_status" not in check_names:
        problems.append("missing status check constraint")
    for required_fk in (
        (("credential_id",), "llm_credentials", ("id",)),
        (("task_record_id",), "media_generation_tasks", ("id",)),
    ):
        if required_fk not in foreign_keys:
            problems.append(
                "missing foreign key "
                f"{required_fk[0]} -> {required_fk[1]}{required_fk[2]}"
            )
    if problems:
        raise RuntimeError(
            f"Incompatible pre-existing {_ALLOWANCE_TABLE}: " + "; ".join(problems)
        )


def upgrade() -> None:
    production_issue_columns = _column_names("production_issues")
    if "resolution_reason" not in production_issue_columns:
        op.add_column(
            "production_issues",
            sa.Column("resolution_reason", sa.String(length=100), nullable=True),
        )
    if "auto_resolved" not in production_issue_columns:
        op.add_column(
            "production_issues",
            sa.Column(
                "auto_resolved",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )

    allowance_table_exists = sa.inspect(op.get_bind()).has_table(_ALLOWANCE_TABLE)
    if allowance_table_exists:
        _assert_allowance_table_compatible()
    else:
        op.create_table(
            _ALLOWANCE_TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("credential_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("modality", sa.String(length=20), nullable=False),
            sa.Column("allowance_date", sa.Date(), nullable=False),
            sa.Column("quota_snapshot", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default="claimed",
                nullable=False,
            ),
            sa.Column(
                "task_record_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("provider_task_id", sa.String(length=160), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("release_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('claimed', 'accepted', 'released')",
                name="ck_media_provider_daily_allowance_claim_status",
            ),
            sa.ForeignKeyConstraint(
                ["credential_id"], ["llm_credentials.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["task_record_id"],
                ["media_generation_tasks.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = _index_names(_ALLOWANCE_TABLE)
    for index_name, columns in (
        (
            "ix_media_provider_daily_allowance_claims_credential_id",
            ["credential_id"],
        ),
        (
            "ix_media_provider_daily_allowance_claims_task_record_id",
            ["task_record_id"],
        ),
        (
            "ix_media_provider_daily_allowance_active",
            ["credential_id", "modality", "allowance_date", "status"],
        ),
    ):
        if index_name not in existing_indexes:
            op.create_index(index_name, _ALLOWANCE_TABLE, columns)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(_ALLOWANCE_TABLE):
        op.drop_table(_ALLOWANCE_TABLE)
    production_issue_columns = _column_names("production_issues")
    if "auto_resolved" in production_issue_columns:
        op.drop_column("production_issues", "auto_resolved")
    if "resolution_reason" in production_issue_columns:
        op.drop_column("production_issues", "resolution_reason")
