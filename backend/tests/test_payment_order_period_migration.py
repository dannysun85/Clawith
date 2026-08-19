"""Fresh/upgrade/downgrade contract for the payment_order_period migration."""

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
    / "202608191400_add_payment_order_period.py"
)

_PARENT_TABLE_DDL = (
    "CREATE TABLE payment_orders ("
    "id CHAR(36) PRIMARY KEY, tenant_id CHAR(36) NOT NULL, "
    "type VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL)",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("payment_order_period_migration", MIGRATION_PATH)
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


def _columns(connection) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns("payment_orders")}


def test_payment_order_period_upgrade_adds_column_with_monthly_default() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(_PARENT_TABLE_DDL[0])

        _run(migration, connection, "upgrade")
        assert "period" in _columns(connection)
        connection.exec_driver_sql(
            "INSERT INTO payment_orders (id, tenant_id, type, status) "
            "VALUES ('x', 't', 'topup', 'pending')"
        )
        default_rows = connection.exec_driver_sql("SELECT period FROM payment_orders").fetchall()
        assert default_rows == [("monthly",)]

        _run(migration, connection, "downgrade")
        assert "period" not in _columns(connection)
        assert {"id", "tenant_id", "type", "status"} <= _columns(connection)


def test_payment_order_period_model_matches_migration_shape() -> None:
    from app.models.subscription import PaymentOrder

    table = PaymentOrder.__table__
    assert "period" in set(table.c.keys())
    assert table.c["period"].server_default.arg == "monthly"
