"""Contracts for the seeded MiniMax-M3 Runtime capability repair."""

from importlib import util
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607220100_reconcile_m3_runtime_capabilities.py"
)


def _load_migration():
    spec = util.spec_from_file_location("reconcile_m3_runtime_caps", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_follows_the_release_head() -> None:
    migration = _load_migration()

    assert migration.revision == "reconcile_m3_runtime_caps"
    assert migration.down_revision == "agent_template_default_tools"


def test_upgrade_repairs_only_revision_owned_seed_rows(monkeypatch) -> None:
    migration = _load_migration()
    statements: list[str] = []
    monkeypatch.setattr(migration, "_exec", statements.append)

    migration.upgrade()

    assert len(statements) == 2
    sql = "\n".join(statements)
    assert set(migration.MODEL_IDS.values()) <= {
        model_id for model_id in migration.MODEL_IDS.values() if model_id in sql
    }
    assert "seed_minimax_m3_understanding" in sql
    assert "model.tenant_id IS NOT NULL" in sql
    assert "model.supports_tool_calling IS NULL" in sql
    assert "model.supports_tool_calling IS TRUE" in sql
    assert "tool_calling_capability_source = 'builtin_registry'" in sql
    assert "supports_tool_calling = true" in sql
    assert migration.MIGRATION_MARKER in sql


def test_downgrade_preserves_newer_capability_evidence(monkeypatch) -> None:
    migration = _load_migration()
    statements: list[str] = []
    monkeypatch.setattr(migration, "_exec", statements.append)

    migration.downgrade()

    assert len(statements) == 2
    exact_revert, marker_cleanup = statements
    assert "tool_calling_checked_at =" in exact_revert
    assert "::timestamptz" in exact_revert
    assert "supports_tool_calling = NULL" in exact_revert
    assert "supports_tool_calling = NULL" not in marker_cleanup
    assert migration.MIGRATION_MARKER in marker_cleanup
