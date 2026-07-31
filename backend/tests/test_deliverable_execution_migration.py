"""Compatibility contract for the Deliverable execution shadow migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608011430_add_deliverable_execution_shadow.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "deliverable_execution_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_schema(migration):
    schema = {}
    for table_name, columns in (
        migration._NEW_TABLE_COLUMNS | migration._AUGMENTED_TABLE_COLUMNS
    ).items():
        schema[table_name] = {
            "columns": set(columns),
            "indexes": set(
                migration._REQUIRED_INDEX_NAMES.get(table_name, set())
            ),
            "index_columns": set(),
            "unique_columns": set(
                migration._REQUIRED_UNIQUE_COLUMNS.get(table_name, set())
            ),
            "foreign_keys": set(
                migration._REQUIRED_FOREIGN_KEYS.get(table_name, set())
            ),
            "checks": set(
                migration._REQUIRED_CHECKS.get(table_name, set())
            ),
            "primary_key": (
                {"id"} if table_name in migration._NEW_TABLE_COLUMNS else set()
            ),
        }
    schema["media_generation_tasks"]["index_columns"].add(
        ("deliverable_execution_id",)
    )
    return schema


def test_absent_execution_shape_requires_normal_create() -> None:
    migration = _load_migration()

    assert migration._precreated_execution_state({}) is False


def test_complete_current_orm_shape_is_adopted_before_artifact_index_repair() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)

    assert migration._precreated_execution_state(schema) is True


def test_augmented_columns_without_execution_tables_fail_closed() -> None:
    migration = _load_migration()
    schema = {
        "deliverable_requests": {
            "columns": {"current_execution_id"},
        }
    }

    with pytest.raises(RuntimeError, match="without execution tables"):
        migration._precreated_execution_state(schema)


def test_partial_execution_tables_fail_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema.pop("deliverable_execution_units")

    with pytest.raises(RuntimeError, match="Partial"):
        migration._precreated_execution_state(schema)


def test_missing_media_execution_index_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["media_generation_tasks"]["index_columns"].clear()

    with pytest.raises(RuntimeError, match="index columns"):
        migration._precreated_execution_state(schema)


def test_missing_execution_constraint_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_executions"]["foreign_keys"].remove(
        "fk_deliverable_executions_tenant_request"
    )

    with pytest.raises(RuntimeError, match="foreign_keys"):
        migration._precreated_execution_state(schema)
