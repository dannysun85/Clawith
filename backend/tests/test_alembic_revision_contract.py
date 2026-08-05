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
    assert _script_directory().get_heads() == ["media_daily_allowance_claims"]


def test_release_head_preserves_both_upgrade_lineages() -> None:
    script = _script_directory()
    release_head = script.get_revision("media_daily_allowance_claims")
    assistant_template_revision = script.get_revision(
        "backfill_private_assistant_tpl"
    )
    reconciliation_revision = script.get_revision("recon_agent_tpl_lifecycle")
    lifecycle_revision = script.get_revision("agent_template_lifecycle")
    media_retention_revision = script.get_revision("media_task_agent_retention")
    assistant_access_revision = script.get_revision("private_assistant_access")
    provider_verification_revision = script.get_revision(
        "provider_verification_receipts"
    )
    execution_revision = script.get_revision("deliverable_execution_shadow")
    evidence_revision = script.get_revision("okr_evidence_links")
    confirmation_revision = script.get_revision("task_confirmation_contract")
    private_assistant_revision = script.get_revision("backfill_private_assistant")
    experience_revision = script.get_revision("add_experience_provenance")
    work_context_revision = script.get_revision("add_task_work_context")
    m3_primary_revision = script.get_revision("promote_m3_text_primary")
    v1113_merge_revision = script.get_revision("merge_v1113_astra_heads")
    deliverable_quality_revision = script.get_revision("add_deliverable_quality_reviews")
    checkpoint_delivery_revision = script.get_revision("allow_checkpoint_deliveries")
    logical_delete_revision = script.get_revision("add_agent_model_deleted_at")
    agent_plan_text_revision = script.get_revision("seed_agent_plan_text_routes")
    provider_plan_revision = script.get_revision("add_provider_plan_tier")
    media_plan_revision = script.get_revision("reconcile_m3_runtime_caps")
    template_tools_revision = script.get_revision("agent_template_default_tools")
    deliverable_revision = script.get_revision("add_deliverable_workbench")
    task_status_revision = script.get_revision("align_task_failed_status")
    merge_revision = script.get_revision("merge_v111_astra_heads")

    assert release_head._normalized_down_revisions == (
        "backfill_private_assistant_tpl",
    )
    assert assistant_template_revision._normalized_down_revisions == (
        "recon_agent_tpl_lifecycle",
    )
    assert reconciliation_revision._normalized_down_revisions == (
        "agent_template_lifecycle",
    )
    assert lifecycle_revision._normalized_down_revisions == ("media_task_agent_retention",)
    assert media_retention_revision._normalized_down_revisions == (
        "private_assistant_access",
    )
    assert assistant_access_revision._normalized_down_revisions == (
        "deliverable_execution_shadow",
    )
    assert execution_revision._normalized_down_revisions == (
        "provider_verification_receipts",
    )
    assert provider_verification_revision._normalized_down_revisions == (
        "okr_evidence_links",
    )
    assert evidence_revision._normalized_down_revisions == ("task_confirmation_contract",)
    assert confirmation_revision._normalized_down_revisions == ("backfill_private_assistant",)
    assert private_assistant_revision._normalized_down_revisions == ("add_experience_provenance",)
    assert experience_revision._normalized_down_revisions == ("add_task_work_context",)
    assert work_context_revision._normalized_down_revisions == ("promote_m3_text_primary",)
    assert m3_primary_revision._normalized_down_revisions == ("merge_v1113_astra_heads",)
    assert set(v1113_merge_revision._normalized_down_revisions) == {
        "add_deliverable_quality_reviews",
        "allow_checkpoint_deliveries",
    }
    assert deliverable_quality_revision._normalized_down_revisions == (
        "seed_agent_plan_text_routes",
    )
    assert checkpoint_delivery_revision._normalized_down_revisions == (
        "add_agent_model_deleted_at",
    )
    assert logical_delete_revision._normalized_down_revisions == (
        "add_experience_revision_drafts",
    )
    assert agent_plan_text_revision._normalized_down_revisions == ("add_provider_plan_tier",)
    assert provider_plan_revision._normalized_down_revisions == ("reconcile_m3_runtime_caps",)
    assert media_plan_revision._normalized_down_revisions == ("agent_template_default_tools",)
    assert template_tools_revision._normalized_down_revisions == ("add_deliverable_workbench",)
    assert deliverable_revision._normalized_down_revisions == ("align_task_failed_status",)
    assert task_status_revision._normalized_down_revisions == ("merge_v111_astra_heads",)

    assert set(merge_revision._normalized_down_revisions) == {
        "sso_password_login",
        "add_experience_revision_drafts",
    }


def test_postgres_migration_smoke_targets_the_release_head() -> None:
    smoke = (BACKEND_ROOT.parent / "scripts/postgres-migration-smoke.sh").read_text(
        encoding="utf-8"
    )

    assert 'MIGRATION_SMOKE_EXPECTED_HEAD:-media_daily_allowance_claims' in smoke
    assert "restore_runtime_chat_foreign_key" in smoke
    assert "DROP CONSTRAINT IF EXISTS fk_agent_runs_tenant_session_chat_sessions" in smoke
    assert "partial allowance table unexpectedly passed migration" in smoke
    assert "Incompatible pre-existing media_provider_daily_allowance_claims" in smoke


def test_daily_allowance_migration_fails_closed_on_partial_existing_table() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202608061015_add_media_daily_allowance_claims.py"
    ).read_text(encoding="utf-8")

    assert "_assert_allowance_table_compatible" in migration
    assert "missing columns:" in migration
    assert "missing status check constraint" in migration
    assert "missing foreign key" in migration


def test_agent_plan_text_route_migration_preserves_credential_ownership_and_fallback() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202607261500_seed_agent_plan_text_routes.py"
    ).read_text(encoding="utf-8")

    assert "capabilities remain administrator-owned" in migration
    assert "UPDATE llm_credentials" not in migration
    assert "model.provider <> 'volcengine_agent_plan'" in migration
    assert "fallback_route_id" in migration
    assert "'builtin_registry'" in migration


def test_m3_primary_migration_is_owned_reversible_and_cycle_safe() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202607311330_promote_m3_text_primary.py"
    ).read_text(encoding="utf-8")

    assert "merge_v1113_astra_heads" in migration
    assert "__promote_m3_text_primary_route_state" in migration
    assert "seed_minimax_m3_understanding" in migration
    assert "seed_agent_plan_text_routes" in migration
    assert "administrator has changed" in migration
    assert "fallback cycle" in migration
    assert "UPDATE llm_credentials" not in migration


def test_task_work_context_migration_is_additive_tenant_scoped_and_idempotent() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202607311530_add_task_work_context.py"
    ).read_text(encoding="utf-8")

    assert "promote_m3_text_primary" in migration
    assert "fk_tasks_tenant_id_tenants" in migration
    assert "uq_tasks_workbench_client_identity" in migration
    assert "ck_tasks_client_fingerprint" in migration
    assert "temporary_expert" in migration
    assert "fk_deliverable_requests_tenant_task" in migration
    assert "DROP TABLE tasks" not in migration


def test_experience_provenance_migration_is_additive_and_reversible() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202607311700_add_experience_provenance.py"
    ).read_text(encoding="utf-8")

    assert "add_task_work_context" in migration
    assert "source_task_id" in migration
    assert "source_deliverable_request_id" in migration
    assert "ondelete=\"SET NULL\"" in migration
    assert "drop_column" in migration
    assert "DROP TABLE experience_entries" not in migration


def test_private_assistant_backfill_is_unambiguous_owned_and_reversible() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202607312050_backfill_personal_assistant.py"
    ).read_text(encoding="utf-8")

    assert "add_experience_provenance" in migration
    assert "agent.creator_id = onboarding.user_id" in migration
    assert "agent.tenant_id = onboarding.tenant_id" in migration
    assert "template.name = 'Private Assistant'" in migration
    assert "HAVING count(*) = 1" in migration
    assert "personal_assistant_agent_id = NULL" in migration


def test_task_confirmation_contract_is_additive_and_reversible() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202608011100_add_task_confirmation_contract.py"
    ).read_text(encoding="utf-8")

    assert "backfill_private_assistant" in migration
    assert "work_statement" in migration
    assert "confirmation_fingerprint" in migration
    assert "confirmed_at" in migration
    assert "Partial Task confirmation schema requires manual repair" in migration
    assert "get_columns(\"tasks\")" in migration
    assert "DROP TABLE tasks" not in migration


def test_okr_evidence_links_are_additive_and_reversible() -> None:
    migration = (
        BACKEND_ROOT
        / "alembic/versions/202608011200_add_okr_evidence_links.py"
    ).read_text(encoding="utf-8")

    assert "task_confirmation_contract" in migration
    assert "source_task_id" in migration
    assert "source_deliverable_request_id" in migration
    assert "evidence_snapshot" in migration
    assert "ondelete=\"SET NULL\"" in migration
    assert "Partial OKR evidence schema requires manual repair" in migration
    assert "get_foreign_keys" in migration
    assert "DROP TABLE okr_progress_logs" not in migration


def test_sso_password_migration_is_fail_closed_and_non_destructive() -> None:
    migration = (
        BACKEND_ROOT / "alembic/versions/106_secure_sso_password_login.py"
    ).read_text(encoding="utf-8")

    assert "password_login_enabled" in migration
    assert "auth_version" in migration
    assert "activation_pending_email_verification" in migration
    assert "bcrypt.checkpw" not in migration
    assert "registration_source" in migration
    assert "SET password_hash = NULL" not in migration
    assert "No password hashes are destroyed by upgrade" in migration
    assert "om.provider_id IS NOT NULL" in migration
    assert "ck_identities_email_canonical" in migration
    assert "uq_users_identity_tenant" in migration
    assert "uq_users_identity_tenantless" in migration
    assert "Duplicate tenantless users(identity_id) rows must be audited" in migration


def test_model_route_migration_guards_live_model_ownership_and_capability() -> None:
    migration = (
        BACKEND_ROOT / "alembic/versions/100_model_route_integrity.py"
    ).read_text(encoding="utf-8")

    assert "trg_model_routes_require_platform_model" in migration
    assert "trg_routed_models_remain_route_compatible" in migration
    assert "trg_model_routes_preserve_fallback_target_update" in migration
    assert "trg_model_routes_preserve_fallback_target_delete" in migration
    assert "NEW.tenant_id IS NOT NULL" in migration
    assert "NEW.enabled IS NOT TRUE" in migration
    assert "NEW.provider IS DISTINCT FROM OLD.provider" in migration
    assert "NEW.model IS DISTINCT FROM OLD.model" in migration
    assert "NEW.base_url IS DISTINCT FROM OLD.base_url" in migration
    assert "jsonb_exists" in migration
    assert "jsonb_array_length" in migration
    assert "BEFORE UPDATE OF tenant_id, enabled, modality, modalities, supports_vision," in migration
    assert "provider, model, base_url" in migration
