"""Contracts for owner-only onboarding-linked private assistants."""

from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202608011700_reconcile_personal_assistant_access.py"
)


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_follows_the_release_head_and_uses_onboarding_as_identity() -> None:
    source = _migration_source()

    assert 'revision: str = "private_assistant_access"' in source
    assert '"deliverable_execution_shadow"' in source
    assert "onboarding.personal_assistant_agent_id" in source
    assert "agent.creator_id IS DISTINCT FROM onboarding.user_id" in source
    assert "agent.tenant_id IS DISTINCT FROM onboarding.tenant_id" in source
    assert "agent.deleted_at IS NOT NULL" in source
    assert "HAVING count(*) <> 1" in source


def test_upgrade_snapshots_exact_policy_before_enforcing_owner_only_access() -> None:
    source = _migration_source()

    assert "__private_assistant_access_state" in source
    assert "__private_assistant_permission_state" in source
    assert "prior_access_mode" in source
    assert "prior_company_access_level" in source
    assert "permission.scope_type::text" in source
    assert "SELECT count(*)" in source
    assert "OR NOT EXISTS" in source
    assert "SET access_mode = 'private'" in source
    assert "company_access_level = 'use'" in source
    assert "'user'::permission_scope_enum" in source
    assert "state.owner_user_id" in source
    assert "'manage'" in source


def test_downgrade_is_fail_closed_and_restores_the_exact_snapshot() -> None:
    source = _migration_source()

    assert "_assert_downgrade_is_safe()" in source
    assert "refusing to overwrite administrator changes" in source
    assert "scope_type::permission_scope_enum" in source
    assert "permission_id" in source
    assert "state.prior_access_mode" in source
    assert "state.prior_company_access_level" in source
    assert "op.drop_table(_PERMISSION_STATE_TABLE)" in source
    assert "op.drop_table(_AGENT_STATE_TABLE)" in source
