"""Fresh/upgrade/downgrade contract for the creative-brief receipts migration."""

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
    / "202608191000_add_creative_brief_compilation_receipts.py"
)
VERSIONS_DIR = MIGRATION_PATH.parent

_EXPECTED_COLUMNS = {
    "deliverable_creative_briefs": {
        "id",
        "tenant_id",
        "request_id",
        "execution_id",
        "modality",
        "schema_version",
        "status",
        "brief",
        "source_inventory",
        "missing_fields",
        "brief_sha256",
        "created_by_run_id",
        "created_at",
        "updated_at",
    },
    "deliverable_prompt_compilations": {
        "id",
        "tenant_id",
        "request_id",
        "execution_id",
        "unit_id",
        "compiler_version",
        "brief_sha256",
        "compiled_prompt_sha256",
        "compiled_prompt_path",
        "provider_target",
        "created_at",
    },
}

_PARENT_TABLE_DDL = (
    "CREATE TABLE tenants (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE agent_runs (id CHAR(36) PRIMARY KEY)",
    (
        "CREATE TABLE deliverable_requests ("
        "id CHAR(36) PRIMARY KEY, tenant_id CHAR(36) NOT NULL, "
        "CONSTRAINT uq_deliverable_requests_tenant_id_id UNIQUE (tenant_id, id))"
    ),
    "CREATE TABLE deliverable_executions (id CHAR(36) PRIMARY KEY)",
    "CREATE TABLE deliverable_execution_units (id CHAR(36) PRIMARY KEY)",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "creative_brief_compilation_receipts_migration",
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


def test_migration_upgrade_downgrade_is_idempotent_on_sqlite() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for ddl in _PARENT_TABLE_DDL:
            connection.exec_driver_sql(ddl)

        _run(migration, connection, "upgrade")
        created = _table_names(connection)
        assert set(_EXPECTED_COLUMNS) <= created
        for table_name, columns in _EXPECTED_COLUMNS.items():
            present = {
                column["name"]
                for column in sa.inspect(connection).get_columns(table_name)
            }
            assert columns <= present

        # Re-running upgrade must adopt the existing tables without error.
        _run(migration, connection, "upgrade")
        assert set(_EXPECTED_COLUMNS) <= _table_names(connection)

        _run(migration, connection, "downgrade")
        remaining = _table_names(connection)
        assert not (set(_EXPECTED_COLUMNS) & remaining)
        assert "deliverable_requests" in remaining

        # Downgrade must tolerate already-absent tables, and a later upgrade
        # must recreate the full shape again.
        _run(migration, connection, "downgrade")
        _run(migration, connection, "upgrade")
        assert set(_EXPECTED_COLUMNS) <= _table_names(connection)


def test_migration_keeps_a_single_alembic_head() -> None:
    revisions: dict[str, str | tuple[str, ...] | None] = {}
    for path in VERSIONS_DIR.glob("*.py"):
        spec = importlib.util.spec_from_file_location(f"alembic_scan_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        down = module.down_revision
        if isinstance(down, (list, tuple)):
            revisions[module.revision] = tuple(down)
        else:
            revisions[module.revision] = down
    referenced = {
        parent
        for down in revisions.values()
        for parent in ((down,) if isinstance(down, str) else (down or ()))
    }
    heads = sorted(set(revisions) - referenced)
    assert heads == ["creative_brief_receipts"], (
        f"expected a single head, found {heads}"
    )
