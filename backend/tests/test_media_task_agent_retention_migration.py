"""Contracts for durable media-task retention after Agent removal."""

from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608011730_repair_media_task_agent_retention.py"
)


def _source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_repair_follows_private_assistant_access_and_targets_the_orm_contract() -> None:
    source = _source()

    assert 'revision: str = "media_task_agent_retention"' in source
    assert '"private_assistant_access"' in source
    assert '__media_task_agent_retention_state' in source
    assert 'nullable=True' in source
    assert 'ondelete="SET NULL"' in source


def test_repair_snapshots_prior_policy_and_downgrade_is_fail_closed() -> None:
    source = _source()

    assert "prior_nullable" in source
    assert "prior_fk_name" in source
    assert "prior_fk_on_delete" in source
    assert "prior_fk_deferrable" in source
    assert "prior_fk_initially" in source
    assert "_assert_managed_state()" in source
    assert "would destroy preserved audit rows" in source
    assert "op.drop_table(_STATE_TABLE)" in source
