"""Fresh/upgrade/downgrade contract for the selection-receipts migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608191200_add_deliverable_selection_receipts.py"
)

_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "request_id",
    "execution_id",
    "actor_user_id",
    "selected_unit_key",
    "candidate_scores",
    "selection_reason",
    "cost_breakdown",
    "actor",
    "client_selection_id",
    "request_fingerprint",
    "created_at",
}

_PARENT_TABLE_DDL = (
    "CREATE TABLE tenants (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE users (id CHAR(36) PRIMARY KEY)",
    (
        "CREATE TABLE deliverable_requests ("
        "id CHAR(36) PRIMARY KEY, tenant_id CHAR(36) NOT NULL, "
        "CONSTRAINT uq_deliverable_requests_tenant_id_id UNIQUE (tenant_id, id))"
    ),
    "CREATE TABLE deliverable_executions (id CHAR(36) PRIMARY KEY)",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "deliverable_selection_receipts_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(migration, connection, operation: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        if operation == "upgrade":
            migration.upgrade()
        else:
            migration.downgrade()


def _table_names(connection) -> set[str]:
    return set(sa.inspect(connection).get_table_names())


def test_selection_receipts_upgrade_downgrade_is_idempotent_on_sqlite() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for ddl in _PARENT_TABLE_DDL:
            connection.exec_driver_sql(ddl)

        _run(migration, connection, "upgrade")
        assert "deliverable_selection_receipts" in _table_names(connection)
        present = {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "deliverable_selection_receipts"
            )
        }
        assert _EXPECTED_COLUMNS <= present
        uniques = {
            constraint["name"]
            for constraint in sa.inspect(connection).get_unique_constraints(
                "deliverable_selection_receipts"
            )
        }
        assert "uq_deliverable_selection_receipts_client" in uniques

        # Re-running upgrade adopts the existing table without error.
        _run(migration, connection, "upgrade")
        assert "deliverable_selection_receipts" in _table_names(connection)

        _run(migration, connection, "downgrade")
        remaining = _table_names(connection)
        assert "deliverable_selection_receipts" not in remaining
        assert "deliverable_requests" in remaining

        # Downgrade tolerates an absent table and a later upgrade recreates it.
        _run(migration, connection, "downgrade")
        _run(migration, connection, "upgrade")
        assert "deliverable_selection_receipts" in _table_names(connection)


def test_selection_receipts_model_matches_migration_shape() -> None:
    from app.models.deliverable import DeliverableSelectionReceipt

    table = DeliverableSelectionReceipt.__table__
    assert _EXPECTED_COLUMNS <= set(table.c.keys())
