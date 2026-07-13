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


def test_production_deploy_verifies_public_release_identity_before_stopping_old_slot():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    public_gate = script.index('echo "[remote] verifying public cutover identity"')
    old_worker_stop = script.index('compose_project "$OLD_PROJECT"', public_gate)
    assert public_gate < old_worker_stop
    assert "'Cache-Control: no-cache'" in script[public_gate:old_worker_stop]
    assert 'health.get("version") != expected_version' in script[public_gate:old_worker_stop]
    assert 'version.get("commit") != expected_commit' in script[public_gate:old_worker_stop]
    assert 'if [ "$PUBLIC_READY" != "1" ]' in script[public_gate:old_worker_stop]
    assert 'exit 1' in script[public_gate:old_worker_stop]


def test_production_deploy_does_not_rebuild_the_live_project_in_place():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'compose .env "$COMPOSE_FILE" up -d --build' not in script
    assert 'build backend frontend' in script
    assert 'stop frontend backend' in script
