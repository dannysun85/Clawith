"""Release contracts for the Alembic revision graph."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).parents[1]


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_revision_ids_fit_the_default_alembic_version_column() -> None:
    revisions = list(_script_directory().walk_revisions())
    oversized = sorted(revision.revision for revision in revisions if len(revision.revision) > 32)

    assert oversized == []


def test_release_migration_graph_has_one_expected_head() -> None:
    assert _script_directory().get_heads() == ["model_route_integrity"]
