from pathlib import Path

from app.models.task import Task


def test_task_model_accepts_historical_failed_status():
    assert Task.__table__.c.status.type.enums == [
        "pending",
        "doing",
        "done",
        "failed",
    ]


def test_task_failed_status_migration_is_additive_and_idempotent():
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "202607191430_align_task_failed_status.py"
    ).read_text(encoding="utf-8")

    assert "ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'failed'" in migration
    assert "def downgrade()" in migration
    assert "pass" in migration
