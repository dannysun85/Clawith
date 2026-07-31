"""Fresh-install adoption contract for deliverable quality-review tables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607271200_add_deliverable_quality_reviews.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "deliverable_quality_review_migration",
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


def test_absent_quality_review_tables_require_normal_create() -> None:
    migration = _load_migration()

    assert migration._precreated_quality_review_state({}) is False


def test_complete_quality_review_bootstrap_is_adopted() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_quality_reviews"]["columns"].add("future_column")

    assert migration._precreated_quality_review_state(schema) is True


def test_partial_quality_review_bootstrap_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema.pop("deliverable_quality_review_evidence")

    with pytest.raises(RuntimeError, match="Partial"):
        migration._precreated_quality_review_state(schema)


def test_incomplete_quality_review_bootstrap_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_quality_reviews"]["indexes"].remove(
        "uq_deliverable_quality_reviews_open_request"
    )

    with pytest.raises(RuntimeError, match="Incomplete"):
        migration._precreated_quality_review_state(schema)


def test_contradictory_quality_review_primary_key_fails_closed() -> None:
    migration = _load_migration()
    schema = _complete_schema(migration)
    schema["deliverable_quality_review_assignments"]["primary_key"] = {
        "tenant_id",
        "id",
    }

    with pytest.raises(RuntimeError, match="primary key"):
        migration._precreated_quality_review_state(schema)
