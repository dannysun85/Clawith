"""Keep media audit rows after an Agent is removed.

Revision ID: media_task_agent_retention
Revises: private_assistant_access
Create Date: 2026-08-01 17:30:00

Revision 099 established the product contract that a durable media task may
outlive its Agent.  Some databases were later stamped past that revision while
retaining the older NOT NULL / ON DELETE CASCADE policy.  This repair converges
those databases to the current ORM contract and snapshots the exact prior
column and foreign-key policy for a fail-closed downgrade.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "media_task_agent_retention"
down_revision: str | Sequence[str] | None = "private_assistant_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATE_TABLE = "__media_task_agent_retention_state"
_TABLE = "media_generation_tasks"
_COLUMN = "agent_id"
_FINAL_FK = "fk_media_generation_tasks_agent_id_set_null"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _column_nullable() -> bool:
    inspector = _inspector()
    if not inspector.has_table(_TABLE) or not inspector.has_table("agents"):
        raise RuntimeError("Media task Agent retention repair is missing source tables")
    for column in inspector.get_columns(_TABLE):
        if column.get("name") == _COLUMN:
            return bool(column.get("nullable"))
    raise RuntimeError(f"Media task Agent retention repair is missing {_TABLE}.{_COLUMN}")


def _agent_foreign_key() -> dict[str, object] | None:
    candidates = [
        foreign_key
        for foreign_key in _inspector().get_foreign_keys(_TABLE)
        if foreign_key.get("constrained_columns") == [_COLUMN]
    ]
    if len(candidates) > 1:
        raise RuntimeError("Media task Agent retention repair found duplicate agent_id foreign keys")
    if not candidates:
        return None
    foreign_key = candidates[0]
    if (
        foreign_key.get("referred_table") != "agents"
        or foreign_key.get("referred_columns") != ["id"]
    ):
        raise RuntimeError("Media task Agent retention repair found an unexpected agent_id target")
    if not foreign_key.get("name"):
        raise RuntimeError("Media task Agent retention repair cannot replace an unnamed foreign key")
    return foreign_key


def _on_delete(foreign_key: dict[str, object] | None) -> str | None:
    if foreign_key is None:
        return None
    options = foreign_key.get("options") or {}
    if not isinstance(options, dict):
        raise RuntimeError("Media task Agent retention repair found invalid foreign-key options")
    value = options.get("ondelete")
    return str(value).upper() if value else None


def _assert_managed_state() -> dict[str, object]:
    if not _column_nullable():
        raise RuntimeError(
            "Media task Agent retention policy changed after migration; refusing downgrade"
        )
    foreign_key = _agent_foreign_key()
    if foreign_key is None or _on_delete(foreign_key) != "SET NULL":
        raise RuntimeError(
            "Media task Agent retention foreign key changed after migration; refusing downgrade"
        )
    return foreign_key


def upgrade() -> None:
    prior_nullable = _column_nullable()
    inspector = _inspector()
    if inspector.has_table(_STATE_TABLE):
        raise RuntimeError("Media task Agent retention repair found a partial state table")

    prior_foreign_key = _agent_foreign_key()
    prior_options = (
        prior_foreign_key.get("options")
        if prior_foreign_key is not None
        else {}
    ) or {}
    if not isinstance(prior_options, dict):
        raise RuntimeError("Media task Agent retention repair found invalid foreign-key options")

    op.create_table(
        _STATE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prior_nullable", sa.Boolean(), nullable=False),
        sa.Column("prior_fk_name", sa.String(length=255), nullable=True),
        sa.Column("prior_fk_on_delete", sa.String(length=32), nullable=True),
        sa.Column("prior_fk_deferrable", sa.Boolean(), nullable=True),
        sa.Column("prior_fk_initially", sa.String(length=32), nullable=True),
    )
    op.get_bind().execute(
        sa.text(
            f"""
            INSERT INTO {_STATE_TABLE} (
                id,
                prior_nullable,
                prior_fk_name,
                prior_fk_on_delete,
                prior_fk_deferrable,
                prior_fk_initially
            ) VALUES (
                1,
                :prior_nullable,
                :prior_fk_name,
                :prior_fk_on_delete,
                :prior_fk_deferrable,
                :prior_fk_initially
            )
            """
        ),
        {
            "prior_nullable": prior_nullable,
            "prior_fk_name": (
                str(prior_foreign_key["name"])
                if prior_foreign_key is not None
                else None
            ),
            "prior_fk_on_delete": _on_delete(prior_foreign_key),
            "prior_fk_deferrable": prior_options.get("deferrable"),
            "prior_fk_initially": prior_options.get("initially"),
        },
    )

    if not prior_nullable:
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.Uuid(),
            nullable=True,
        )
    if prior_foreign_key is None or _on_delete(prior_foreign_key) != "SET NULL":
        if prior_foreign_key is not None:
            op.drop_constraint(
                str(prior_foreign_key["name"]),
                _TABLE,
                type_="foreignkey",
            )
        op.create_foreign_key(
            _FINAL_FK,
            _TABLE,
            "agents",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    inspector = _inspector()
    if not inspector.has_table(_STATE_TABLE):
        return

    row = op.get_bind().execute(
        sa.text(
            f"""
            SELECT
                prior_nullable,
                prior_fk_name,
                prior_fk_on_delete,
                prior_fk_deferrable,
                prior_fk_initially
              FROM {_STATE_TABLE}
             WHERE id = 1
            """
        )
    ).mappings().one_or_none()
    count = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {_STATE_TABLE}")
    ).scalar_one()
    if row is None or count != 1:
        raise RuntimeError("Media task Agent retention downgrade found invalid state")

    current_foreign_key = _assert_managed_state()
    if not bool(row["prior_nullable"]):
        orphan_count = op.get_bind().execute(
            sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NULL")
        ).scalar_one()
        if orphan_count:
            raise RuntimeError(
                "Media task Agent retention downgrade would destroy preserved audit rows"
            )

    op.drop_constraint(
        str(current_foreign_key["name"]),
        _TABLE,
        type_="foreignkey",
    )
    if row["prior_fk_name"]:
        op.create_foreign_key(
            str(row["prior_fk_name"]),
            _TABLE,
            "agents",
            [_COLUMN],
            ["id"],
            ondelete=(str(row["prior_fk_on_delete"]) if row["prior_fk_on_delete"] else None),
            deferrable=row["prior_fk_deferrable"],
            initially=(str(row["prior_fk_initially"]) if row["prior_fk_initially"] else None),
        )
    if not bool(row["prior_nullable"]):
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.Uuid(),
            nullable=False,
        )
    op.drop_table(_STATE_TABLE)
