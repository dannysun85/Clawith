"""Add the opt-in CEO P2 coordination authority layer.

Revision ID: ceo_coordination_mode
Revises: ceo_orchestrator_settings
Create Date: 2026-08-20 09:00:00

Expand-only and fail-closed: every existing company remains observer-only.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "ceo_coordination_mode"
down_revision: str | Sequence[str] | None = "ceo_orchestrator_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ceo_orchestrator_settings"
_FK = "fk_ceo_orchestrator_settings_coordination_enabled_by"
_CHECK = "ck_ceo_orchestrator_max_parallel_delegations"


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    existing = _columns()
    with op.batch_alter_table(_TABLE) as batch_op:
        if "coordination_enabled" not in existing:
            batch_op.add_column(
                sa.Column(
                    "coordination_enabled",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )
        if "auto_dispatch_enabled" not in existing:
            batch_op.add_column(
                sa.Column(
                    "auto_dispatch_enabled",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )
        if "coordination_enabled_by_user_id" not in existing:
            batch_op.add_column(
                sa.Column("coordination_enabled_by_user_id", sa.UUID(), nullable=True)
            )
        if "coordination_enabled_at" not in existing:
            batch_op.add_column(
                sa.Column("coordination_enabled_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "max_parallel_delegations" not in existing:
            batch_op.add_column(
                sa.Column(
                    "max_parallel_delegations",
                    sa.Integer(),
                    server_default="3",
                    nullable=False,
                )
            )

    inspector = sa.inspect(op.get_bind())
    foreign_keys = {constraint.get("name") for constraint in inspector.get_foreign_keys(_TABLE)}
    checks = {constraint.get("name") for constraint in inspector.get_check_constraints(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch_op:
        if _FK not in foreign_keys:
            batch_op.create_foreign_key(
                _FK,
                "users",
                ["coordination_enabled_by_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if _CHECK not in checks:
            batch_op.create_check_constraint(
                _CHECK,
                "max_parallel_delegations BETWEEN 1 AND 12",
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    foreign_keys = {constraint.get("name") for constraint in inspector.get_foreign_keys(_TABLE)}
    checks = {constraint.get("name") for constraint in inspector.get_check_constraints(_TABLE)}
    existing = _columns()
    with op.batch_alter_table(_TABLE) as batch_op:
        if _CHECK in checks:
            batch_op.drop_constraint(_CHECK, type_="check")
        if _FK in foreign_keys:
            batch_op.drop_constraint(_FK, type_="foreignkey")
        for column in (
            "max_parallel_delegations",
            "coordination_enabled_at",
            "coordination_enabled_by_user_id",
            "auto_dispatch_enabled",
            "coordination_enabled",
        ):
            if column in existing:
                batch_op.drop_column(column)
