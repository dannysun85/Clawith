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
    assert _script_directory().get_heads() == ["align_task_failed_status"]


def test_release_head_preserves_both_upgrade_lineages() -> None:
    script = _script_directory()
    release_head = script.get_revision("align_task_failed_status")
    merge_revision = script.get_revision("merge_v111_astra_heads")

    assert release_head._normalized_down_revisions == ("merge_v111_astra_heads",)

    assert set(merge_revision._normalized_down_revisions) == {
        "sso_password_login",
        "add_experience_revision_drafts",
    }


def test_postgres_migration_smoke_targets_the_release_head() -> None:
    smoke = (BACKEND_ROOT.parent / "scripts/postgres-migration-smoke.sh").read_text(
        encoding="utf-8"
    )

    assert 'MIGRATION_SMOKE_EXPECTED_HEAD:-align_task_failed_status' in smoke


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
