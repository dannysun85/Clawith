from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_production_compose_splits_api_and_worker_roles_and_shares_durable_volume():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "API_PROCESS_ROLE:-api,bootstrap" in compose
    assert "WORKER_PROCESS_ROLE:-worker,connector" in compose
    assert "AGENT_DATA_VOLUME:-astra-poc_agentdata" in compose
    assert "BACKEND_NETWORK_ALIAS" in compose


def test_production_deploy_health_checks_candidate_before_nginx_cutover():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    candidate_health = script.index('http://127.0.0.1:${CANDIDATE_PORT}/api/health')
    nginx_reload = script.index("sudo systemctl reload nginx", candidate_health)
    assert candidate_health < nginx_reload
    assert "ACTIVE_SLOT_FILE" in script
    assert "DRAIN_TIMEOUT_SECONDS" in script
    assert "astra_no_args" in script
    assert "JWT_ROTATION_MARKER" in script
    assert '"$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT"' in script
    assert "if replacements != 1:" in script
    assert "run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null" in script


def test_production_deploy_quiesces_worker_before_migrations_and_keeps_app_until_public_gate():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    worker_quiesce = script.index('echo "[remote] quiescing old worker')
    migration = script.index("--entrypoint alembic backend upgrade head", worker_quiesce)
    public_gate = script.index('echo "[remote] verifying public cutover identity"')
    old_app_stop = script.index('stop frontend backend', public_gate)
    assert worker_quiesce < migration < public_gate < old_app_stop
    assert "stop --timeout 90 worker" in script[worker_quiesce:migration]
    assert "'Cache-Control: no-cache'" in script[public_gate:old_app_stop]
    assert 'health.get("version") != expected_version' in script[public_gate:old_app_stop]
    assert 'version.get("commit") != expected_commit' in script[public_gate:old_app_stop]
    assert 'if [ "$PUBLIC_READY" != "1" ]' in script[public_gate:old_app_stop]
    assert 'exit 1' in script[public_gate:old_app_stop]
    rollback = script[script.index("rollback()") : worker_quiesce]
    assert 'if [ "$OLD_WORKER_STOPPED" = "1" ]' in rollback
    assert 'up -d --no-deps worker' in rollback
    assert 'if [ "$MIGRATION_APPLIED" = "1" ]' in rollback
    assert 'backend downgrade "$PRE_MIGRATION_REVISION"' in rollback
    candidate_database_stop = rollback.index('stop worker backend')
    database_restore = rollback.index('backend downgrade "$PRE_MIGRATION_REVISION"')
    nginx_restore = rollback.index('sudo cp "$NGINX_BACKUP" "$NGINX_SITE"')
    candidate_cleanup = rollback.index('stop worker frontend backend')
    assert candidate_database_stop < database_restore < nginx_restore < candidate_cleanup
    assert 'candidate API is stopped and workers remain quiesced' in rollback


def test_m3_seed_remains_compatible_with_the_pre_release_api_during_migration_window():
    migration = (
        ROOT / "backend/alembic/versions/093_seed_minimax_m3_understanding_routes.py"
    ).read_text(encoding="utf-8")

    # The old API resolves account-pool capability from llm_models.modality.
    # Keep the primary modality concrete while the new API uses modalities for
    # image/video understanding. This prevents a temporary no-credential outage
    # while the old blue/green slot is still serving after migrations.
    assert "'text', '[\"text\",\"image\",\"video\"]'::jsonb" in migration
    assert "'multimodal', '[\"text\",\"image\",\"video\"]'::jsonb" not in migration


def test_m3_downgrade_detaches_fallbacks_and_preserves_referenced_models():
    migration = (
        ROOT / "backend/alembic/versions/093_seed_minimax_m3_understanding_routes.py"
    ).read_text(encoding="utf-8")

    assert "SET fallback_route_id = NULL" in migration
    assert "primary_model_id = model.id OR fallback_model_id = model.id" in migration
    assert "SELECT 1 FROM tenants WHERE default_model_id = model.id" in migration
    assert "ON CONFLICT (id) DO NOTHING" in migration
    assert "reserved MiniMax-M3 model id" in migration


def test_production_deploy_does_not_rebuild_the_live_project_in_place():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'compose .env "$COMPOSE_FILE" up -d --build' not in script
    assert 'build backend frontend' in script
    assert 'stop frontend backend' in script
