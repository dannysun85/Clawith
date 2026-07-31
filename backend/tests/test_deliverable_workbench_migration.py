"""Fresh-install adoption contract for the deliverable workbench migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607201200_add_deliverable_workbench.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "deliverable_workbench_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_schema(migration) -> dict[str, dict[str, set[str]]]:
    return {
        table_name: {
            "columns": set(migration._BOOTSTRAP_COLUMNS[table_name]),
            "indexes": set(migration._BOOTSTRAP_INDEXES[table_name]),
            "uniques": set(migration._BOOTSTRAP_UNIQUES[table_name]),
            "foreign_keys": set(
                migration._BOOTSTRAP_FOREIGN_KEYS[table_name]
            ),
            "checks": set(migration._BOOTSTRAP_CHECKS[table_name]),
            "primary_key": {"id"},
        }
        for table_name in migration._BOOTSTRAP_COLUMNS
    }


def test_absent_bootstrap_tables_require_normal_create() -> None:
    migration = _load_migration()

    assert migration._precreated_deliverable_state({}) is False


def test_complete_bootstrap_shape_with_later_objects_is_adopted() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_requests"]["columns"].add("current_execution_id")
    schema["deliverable_artifact_revisions"]["indexes"].add(
        "ix_deliverable_artifacts_execution"
    )

    assert migration._precreated_deliverable_state(schema) is True


def test_partial_bootstrap_tables_fail_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema.pop("deliverable_artifact_revisions")

    with pytest.raises(RuntimeError, match="missing tables"):
        migration._precreated_deliverable_state(schema)


@pytest.mark.parametrize(
    ("object_type", "missing_name"),
    (
        ("columns", "workflow_id"),
        ("indexes", "ix_deliverable_requests_session_created"),
        ("uniques", "uq_deliverable_requests_client_identity"),
        ("foreign_keys", "fk_deliverable_requests_tenant"),
        ("checks", "ck_deliverable_requests_status"),
    ),
)
def test_incomplete_bootstrap_objects_fail_closed(
    object_type: str,
    missing_name: str,
) -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_requests"][object_type].remove(missing_name)

    with pytest.raises(RuntimeError, match="Incomplete deliverable_requests"):
        migration._precreated_deliverable_state(schema)


def test_contradictory_primary_key_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_requests"]["primary_key"] = {"tenant_id", "id"}

    with pytest.raises(RuntimeError, match="primary key"):
        migration._precreated_deliverable_state(schema)
