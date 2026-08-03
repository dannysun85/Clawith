from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "202608021200_backfill_private_assistant_template.py"
)


def test_private_assistant_backfill_is_narrow_and_idempotent() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "revision: str = \"backfill_private_assistant_tpl\"" in source
    assert "down_revision: str | Sequence[str] | None = \"recon_agent_tpl_lifecycle\"" in source
    assert "deleted_at IS NULL" in source
    assert "template_id IS NULL" in source
    assert "access_mode = 'private'" in source
    assert "is_system = FALSE" in source
    assert "role_description = 'Private Assistant'" in source
    assert "name = 'Private Assistant'" in source
    assert "is_builtin = TRUE" in source
    assert "def downgrade()" in source
    assert "non-destructive" in source
