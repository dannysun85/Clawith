import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
NGINX_CONFIGURATOR = ROOT / "scripts/configure_production_nginx.py"
PRODUCTION_CLOSED_FEATURE_FLAGS = {
    "CEO_ORCHESTRATOR_ENABLED": "false",
    "CEO_ORCHESTRATOR_TENANT_IDS": "",
    "CEO_ORCHESTRATOR_AGENT_IDS": "",
    "CEO_COORDINATION_ENABLED": "false",
    "CEO_COORDINATION_TENANT_IDS": "",
    "CEO_COORDINATION_AGENT_IDS": "",
    "DELIVERABLE_POSTER_V2_ENABLED": "false",
    "DELIVERABLE_POSTER_V2_TENANT_IDS": "",
    "DELIVERABLE_POSTER_V2_AGENT_IDS": "",
    "DELIVERABLE_VIDEO_V2_ENABLED": "false",
    "DELIVERABLE_VIDEO_V2_TENANT_IDS": "",
    "DELIVERABLE_VIDEO_V2_AGENT_IDS": "",
    "DELIVERABLE_PRESENTATION_V2_ENABLED": "false",
    "DELIVERABLE_PRESENTATION_V2_TENANT_IDS": "",
    "DELIVERABLE_PRESENTATION_V2_AGENT_IDS": "",
    "DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED": "false",
    "DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS": "",
    "DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS": "",
}


def _load_nginx_configurator():
    spec = importlib.util.spec_from_file_location(
        "configure_production_nginx",
        NGINX_CONFIGURATOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_test_release(
    app_root: Path,
    release_id: str,
    *,
    version: str = "1.10.12",
    commit: str = "abc1234",
) -> Path:
    release = app_root / "releases" / release_id
    release.mkdir(parents=True)
    (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (release / "COMMIT").write_text(f"{commit}\n", encoding="utf-8")
    (release / ".env").write_text(
        f'ASTRA_RELEASE_ID="{release_id}"\n',
        encoding="utf-8",
    )
    (release / "docker-compose.prod.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    return release


def _shell_function_source(script: str, name: str, next_name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index(f"{next_name}() {{", start)
    return script[start:end]


def _heredoc_source(script: str, marker: str) -> str:
    start_marker = f"<<'{marker}'\n"
    start = script.index(start_marker) + len(start_marker)
    end = script.index(f"\n{marker}", start)
    return script[start:end]


def _run_embedded_python(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-", *args],
        input=source,
        check=False,
        capture_output=True,
        text=True,
    )


def _recovery_shell_source(script: str) -> tuple[str, str, str]:
    port_function = _shell_function_source(
        script,
        "port_for_slot",
        "release_payloads_match",
    )
    recovery_helpers = "\n".join(
        [
            # MCP quarantine/restore has its own database contract tests. The
            # cutover state-machine fixtures deliberately isolate networking
            # and persistence from those deployment-security side effects.
            "quarantine_mcp_for_unsafe_release() { :; }",
            "restore_mcp_quarantine_for_safe_release() { :; }",
            "reconcile_agentbay_for_cutover() { :; }",
            # Recovery state-machine fixtures model a candidate whose durable
            # authenticated-smoke marker was already verified. The evidence
            # parser has its own contract test below.
            "candidate_business_evidence_valid() { :; }",
            "approval_schema_forward_state() { printf '%s' \"${SCHEMA_FORWARD_STATE:-1}\"; }",
            _shell_function_source(
                script,
                "project_for_slot",
                "count_established_connections",
            ),
            _shell_function_source(
                script,
                "canonical_managed_release",
                "wait_for_worker_release",
            ),
        ]
    )
    recovery_start = script.index("recover_indeterminate_cutover() {")
    recovery_end = script.index("\nif ! CURRENT_TARGET=", recovery_start)
    return port_function, recovery_helpers, script[recovery_start:recovery_end]


def test_production_compose_splits_api_and_worker_roles_and_shares_durable_volume():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "API_PROCESS_ROLE:-api,bootstrap" in compose
    assert "WORKER_PROCESS_ROLE:-worker,connector" in compose
    assert "AGENT_DATA_VOLUME:-astra-poc_agentdata" in compose
    assert "BACKEND_NETWORK_ALIAS" in compose


def test_runtime_rollout_policy_reaches_every_supported_deployment_path():
    compose_contracts = {
        "docker-compose.yml": 1,
        "deploy/docker-compose.yml": 1,
        "deploy/docker-compose-multi.yml": 2,
        "docker-compose.ci.yml": 1,
        # The worker inherits the anchored backend environment.
        "deploy/astra-poc/docker-compose.prod.yml": 1,
    }
    variables = (
        "AGENT_RUNTIME_COMMAND_CONCURRENCY",
        "AGENT_RUNTIME_V2_ENABLED",
        "AGENT_RUNTIME_V2_AGENT_IDS",
        "AGENT_RUNTIME_V2_SOURCE_TYPES",
    )

    for relative_path, expected_count in compose_contracts.items():
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        for variable in variables:
            actual_count = len(
                re.findall(rf"^\s+{re.escape(variable)}:", compose, re.MULTILINE)
            )
            assert actual_count == expected_count, (
                relative_path,
                variable,
            )

    values = (ROOT / "helm/clawith/values.yaml").read_text(encoding="utf-8")
    deployment = (ROOT / "helm/clawith/templates/backend.yaml").read_text(
        encoding="utf-8"
    )
    assert "runtimeV2Enabled: true" in values
    assert "runtimeV2AgentIds:" in values
    assert 'runtimeV2SourceTypes: ""' in values
    assert "runtimeCommandConcurrency: 10" in values
    for variable in variables:
        assert f"name: {variable}" in deployment

    # Supported v1.11.1 deployments pin the only remaining Runtime path and do
    # not inherit stale rollout variables from an older release environment.
    for relative_path in compose_contracts:
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        assert 'AGENT_RUNTIME_V2_ENABLED: "true"' in compose
        assert 'AGENT_RUNTIME_V2_AGENT_IDS: ""' in compose
        assert 'AGENT_RUNTIME_V2_SOURCE_TYPES: ""' in compose
        assert "AGENT_RUNTIME_V2_ENABLED: ${" not in compose
        assert "AGENT_RUNTIME_V2_AGENT_IDS: ${" not in compose
        assert "AGENT_RUNTIME_V2_SOURCE_TYPES: ${" not in compose

    deploy_script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )
    assert '"AGENT_RUNTIME_V2_ENABLED": "true"' in deploy_script
    assert '"AGENT_RUNTIME_V2_AGENT_IDS": ""' in deploy_script
    assert '"AGENT_RUNTIME_V2_SOURCE_TYPES": ""' in deploy_script


def test_provider_egress_proxy_is_explicitly_propagated_to_every_backend_path():
    compose_files = (
        ROOT / "docker-compose.yml",
        ROOT / "deploy/docker-compose.yml",
        ROOT / "deploy/docker-compose-multi.yml",
        ROOT / "docker-compose.ci.yml",
        ROOT / "deploy/astra-poc/docker-compose.prod.yml",
    )

    for compose_file in compose_files:
        compose = compose_file.read_text(encoding="utf-8")
        assert "HTTP_PROXY: ${HTTP_PROXY:-}" in compose, compose_file

    # The production worker inherits the anchored backend environment, so one
    # declaration must serve both API and worker without a second divergent
    # proxy setting.
    production = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    assert production.count("HTTP_PROXY: ${HTTP_PROXY:-}") == 1
    assert "<<: *backend-environment" in production

    for env_example in (ROOT / ".env.example", ROOT / "deploy/.env.example"):
        example = env_example.read_text(encoding="utf-8")
        assert "HTTP_PROXY=" in example
        assert "127.0.0.1:7890" not in example

    helm_values = (ROOT / "helm/clawith/values.yaml").read_text(encoding="utf-8")
    helm_backend = (ROOT / "helm/clawith/templates/backend.yaml").read_text(
        encoding="utf-8"
    )
    assert 'httpProxy: ""' in helm_values
    assert "name: HTTP_PROXY" in helm_backend
    assert ".Values.backend.env.httpProxy" in helm_backend


def test_wechat_pay_env_is_propagated_on_backend_compose_paths():
    production = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    assert "PAYMENT_BASE_URL: ${PAYMENT_BASE_URL:-https://opc.rama-server.com}" in production
    assert "BILLING_PROVIDER: ${BILLING_PROVIDER:-manual}" in production
    assert "WECHAT_PAY_APPID: ${WECHAT_PAY_APPID:-}" in production
    assert "WECHAT_PAY_NOTIFY_URL: ${WECHAT_PAY_NOTIFY_URL:-}" in production
    assert "WECHAT_PAY_PLATFORM_PUBLIC_KEY: ${WECHAT_PAY_PLATFORM_PUBLIC_KEY:-}" in production
    assert "WECHAT_PAY_PLATFORM_SERIAL_NO: ${WECHAT_PAY_PLATFORM_SERIAL_NO:-}" in production
    assert production.count("PAYMENT_BASE_URL: ${") == 1
    assert "<<: *backend-environment" in production

    for compose_file in (
        ROOT / "docker-compose.yml",
        ROOT / "deploy/docker-compose.yml",
        ROOT / "deploy/docker-compose-multi.yml",
    ):
        compose = compose_file.read_text(encoding="utf-8")
        assert "PAYMENT_BASE_URL: ${PAYMENT_BASE_URL:-}" in compose, compose_file
        assert "BILLING_PROVIDER: ${BILLING_PROVIDER:-manual}" in compose, compose_file
        assert "WECHAT_PAY_NOTIFY_URL: ${WECHAT_PAY_NOTIFY_URL:-}" in compose, compose_file
        assert "WECHAT_PAY_PLATFORM_PUBLIC_KEY: ${WECHAT_PAY_PLATFORM_PUBLIC_KEY:-}" in compose, compose_file
        assert "WECHAT_PAY_PLATFORM_SERIAL_NO: ${WECHAT_PAY_PLATFORM_SERIAL_NO:-}" in compose, compose_file

    for env_example in (ROOT / ".env.example", ROOT / "deploy/.env.example"):
        example = env_example.read_text(encoding="utf-8")
        assert "PAYMENT_BASE_URL=" in example
        assert "BILLING_PROVIDER=" in example
        assert "WECHAT_PAY_PLATFORM_PUBLIC_KEY=" in example
        assert "WECHAT_PAY_PLATFORM_SERIAL_NO=" in example


def test_planning_system_cost_caps_are_release_pinned_and_candidate_verified():
    expected = {
        "PLANNING_SYSTEM_COST_MAX_CREDITS_PER_RUN": "3000",
        "PLANNING_SYSTEM_COST_MAX_CREDITS_PER_TENANT_DAY": "20000",
        "PLANNING_SYSTEM_COST_MAX_CALLS_PER_RUN": "5",
        "PLANNING_SYSTEM_COST_MAX_CALLS_PER_TENANT_DAY": "100",
        "PLANNING_SYSTEM_COST_UNPRICED_RESERVATION_CREDITS": "1000",
        "PLANNING_SYSTEM_COST_INFLIGHT_STALE_SECONDS": "600",
        "PLANNING_SYSTEM_COST_RECONCILIATION_SCAN_SECONDS": "60",
        "PLANNING_SYSTEM_COST_RECONCILIATION_BATCH_SIZE": "100",
    }
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for key, value in expected.items():
        assert compose.count(f"{key}: ${{{key}:-{value}}}") == 1, key
        assert f'"{key}": "{value}"' in script
        assert f"{key}={value}" in env_example

    verifier = script.index("inspect_candidate_planning_cost_contract() {")
    candidate_healthy = script.index(
        'test "$(docker inspect -f \'{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}\' "$CANDIDATE_BACKEND_ID")" = "healthy"'
    )
    candidate_verify = script.index(
        'inspect_candidate_planning_cost_contract "$CANDIDATE_BACKEND_ID"',
        candidate_healthy,
    )
    frontend_start = script.index(
        'compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps frontend',
        candidate_verify,
    )
    assert verifier < candidate_healthy < candidate_verify < frontend_start


def test_production_code_execution_defaults_fail_closed():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert 'CODE_EXECUTION_ENABLED: "false"' in compose
    assert 'ALLOW_UNVERIFIED_LOCAL_SIGNUP: "false"' in compose
    assert 'CODE_EXECUTION_ALLOWED_TENANT_IDS: ""' in compose
    assert 'CODE_EXECUTION_ALLOWED_TOOL_NAMES: ""' in compose
    assert 'CODE_EXECUTION_ALLOWED_SANDBOX_TYPES: ""' in compose
    assert 'CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS: ""' in compose
    assert 'CODE_EXECUTION_REQUIRE_APPROVAL: "true"' in compose
    assert 'SANDBOX_TYPE: "e2b"' in compose
    assert 'SANDBOX_API_KEY: ""' in compose
    assert 'SANDBOX_API_URL: ""' in compose
    assert 'SANDBOX_ALLOW_NETWORK: "false"' in compose
    assert 'SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING: "false"' in compose
    assert "privileged: true" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "SYS_ADMIN" not in compose
    assert "seccomp=unconfined" not in compose
    assert "apparmor=unconfined" not in compose


def test_polluted_parent_environment_cannot_activate_code_in_effective_compose():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not installed")
    try:
        version = subprocess.run(
            ["docker", "compose", "version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("docker compose plugin probe timed out")
    if version.returncode != 0:
        pytest.skip("docker compose plugin is not installed")

    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "contract-test",
        "SECRET_KEY": "contract-test-secret",
        "JWT_SECRET_KEY": "contract-test-jwt",
        "CORS_ORIGINS": "https://example.test",
        "PUBLIC_BASE_URL": "https://example.test",
        "ASTRA_ALERT_WORKER_ACTOR_ID": "00000000-0000-4000-8000-000000000001",
        "CODE_EXECUTION_ENABLED": "true",
        "CODE_EXECUTION_ALLOWED_TENANT_IDS": "tenant-from-parent",
        "CODE_EXECUTION_ALLOWED_TOOL_NAMES": "execute_code",
        "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES": "subprocess",
        "CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS": "https://unsafe.example",
        "CODE_EXECUTION_REQUIRE_APPROVAL": "false",
        "SANDBOX_TYPE": "subprocess",
        "SANDBOX_API_KEY": "parent-secret",
        "SANDBOX_API_URL": "https://unsafe.example",
        "SANDBOX_ALLOW_NETWORK": "true",
        "SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING": "true",
    }
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/astra-poc/docker-compose.prod.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    config = json.loads(rendered.stdout)
    for service_name in ("backend", "worker"):
        effective = config["services"][service_name]["environment"]
        assert effective["CODE_EXECUTION_ENABLED"] == "false"
        assert effective["CODE_EXECUTION_ALLOWED_TENANT_IDS"] == ""
        assert effective["CODE_EXECUTION_ALLOWED_TOOL_NAMES"] == ""
        assert effective["CODE_EXECUTION_ALLOWED_SANDBOX_TYPES"] == ""
        assert effective["CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS"] == ""
        assert effective["CODE_EXECUTION_REQUIRE_APPROVAL"] == "true"
        assert effective["SANDBOX_TYPE"] == "e2b"
        assert effective["SANDBOX_API_KEY"] == ""
        assert effective["SANDBOX_API_URL"] == ""
        assert effective["SANDBOX_ALLOW_NETWORK"] == "false"
        assert effective["SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING"] == "false"


def test_production_closed_feature_contract_is_literal_and_release_bound():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )

    for key, value in PRODUCTION_CLOSED_FEATURE_FLAGS.items():
        assert compose.count(f'{key}: "{value}"') == 1, key
        assert f"{key}: ${{" not in compose
        assert f'"{key}": "{value}"' in script

    verifier = script.index("inspect_candidate_closed_feature_contract() {")
    candidate_healthy = script.index(
        'test "$(docker inspect -f \'{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}\' "$CANDIDATE_BACKEND_ID")" = "healthy"'
    )
    candidate_verify = script.index(
        'inspect_candidate_closed_feature_contract "$CANDIDATE_BACKEND_ID"',
        candidate_healthy,
    )
    frontend_start = script.index(
        'compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps frontend',
        candidate_verify,
    )
    assert verifier < candidate_healthy < candidate_verify < frontend_start

    governance_inventory = script.index(
        "python -m app.scripts.inventory_production_governance",
        candidate_verify,
    )
    assert candidate_verify < governance_inventory < frontend_start
    assert "--fail-on-ledger-drift" in script[governance_inventory:frontend_start]
    assert "production-governance-inventory.candidate.json" in script[
        governance_inventory:frontend_start
    ]
    assert 'chmod 0600 "$BACKUP/production-governance-inventory.candidate.json"' in script

    if shutil.which("docker") is None:
        return
    version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if version.returncode != 0:
        return

    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "contract-test",
        "SECRET_KEY": "contract-test-secret",
        "JWT_SECRET_KEY": "contract-test-jwt",
        "CORS_ORIGINS": "https://example.test",
        "PUBLIC_BASE_URL": "https://example.test",
        "ASTRA_ALERT_WORKER_ACTOR_ID": "00000000-0000-4000-8000-000000000001",
        **{
            key: ("true" if value == "false" else "polluted-parent-value")
            for key, value in PRODUCTION_CLOSED_FEATURE_FLAGS.items()
        },
    }
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/astra-poc/docker-compose.prod.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    config = json.loads(rendered.stdout)
    for service_name in ("backend", "worker"):
        effective = config["services"][service_name]["environment"]
        assert {
            key: effective[key] for key in PRODUCTION_CLOSED_FEATURE_FLAGS
        } == PRODUCTION_CLOSED_FEATURE_FLAGS


def test_candidate_closed_feature_verifier_rejects_runtime_drift():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )
    function = _shell_function_source(
        script,
        "inspect_candidate_closed_feature_contract",
        "inspect_rollback_worker_runtime_identity",
    )
    harness = f"""
set -euo pipefail
docker() {{
    [ "$1" = inspect ]
    printf '%s\\n' "$DOCKER_ENV_OUTPUT"
}}
{function}
inspect_candidate_closed_feature_contract candidate-backend
"""
    closed_output = "\n".join(
        f"{key}={value}" for key, value in PRODUCTION_CLOSED_FEATURE_FLAGS.items()
    )
    passed = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={**os.environ, "DOCKER_ENV_OUTPUT": closed_output},
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stderr
    assert "candidate_closed_feature_contract=ok" in passed.stdout

    drifted_output = closed_output.replace(
        "CEO_COORDINATION_ENABLED=false",
        "CEO_COORDINATION_ENABLED=true",
    )
    rejected = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={**os.environ, "DOCKER_ENV_OUTPUT": drifted_output},
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "candidate closed-feature contract drifted" in rejected.stderr


def test_release_env_preserves_live_data_plane_identity_and_rejects_drift(
    tmp_path: Path,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )
    marker = 'python3 - "$PREVIOUS/.env" "$RELEASE/.env" <<\'PY\'\n'
    source_start = script.index(marker) + len(marker)
    source_end = script.index("\nPY\n", source_start)
    continuity_source = script[source_start:source_end]

    assert '"POSTGRES_VOLUME": f' not in script
    assert '"REDIS_VOLUME": f' not in script
    assert '"AGENT_DATA_VOLUME": f' not in script
    assert "data-plane identity continuity verified" in continuity_source
    assert "separate authorized data-plane change" in continuity_source

    previous = tmp_path / "previous.env"
    candidate = tmp_path / "candidate.env"
    live_env = "\n".join(
        (
            'SECRET_KEY="envelope-secret"',
            'DOCKER_NETWORK="astra_network"',
            'POSTGRES_VOLUME="astra-poc_pgdata_restored"',
            'REDIS_VOLUME="astra-poc_redisdata"',
            'AGENT_DATA_VOLUME="astra-poc_agentdata"',
            'POSTGRES_USER="astra"',
            'POSTGRES_DB="astra"',
            'POSTGRES_PASSWORD="database-secret"',
            "",
        )
    )
    previous.write_text(live_env, encoding="utf-8")
    candidate.write_text(live_env, encoding="utf-8")

    preserved = subprocess.run(
        [sys.executable, "-", str(previous), str(candidate)],
        input=continuity_source,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert preserved.returncode == 0, preserved.stderr
    assert "SECRET_KEY continuity verified" in preserved.stdout
    assert "data-plane identity continuity verified" in preserved.stdout

    candidate.write_text(
        live_env.replace("astra-poc_pgdata_restored", "astra-poc_pgdata"),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, "-", str(previous), str(candidate)],
        input=continuity_source,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "data-plane identity continuity check failed: POSTGRES_VOLUME" in rejected.stderr


def test_production_release_gate_covers_code_execution_security():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert "(cd backend && uv run --frozen --extra dev pytest -q)" in script
    assert "uv run --project backend --frozen --extra dev python scripts/ruff_diff_gate.py" in script
    assert 'ALEMBIC_HEADS="$(cd backend && uv run --frozen alembic heads)"' in script
    assert script.index("(cd frontend && npm ci)") < script.index("(cd frontend && npm test)")
    assert script.index("(cd frontend && npm test)") < script.index("(cd frontend && npm run build)")
    assert "production releases cannot disable local release gates" in script
    assert "agentbay_unresolved_count" in script
    assert ("status IN ('active', 'cleanup_required', 'provider_identity_collision')") in script
    assert "AGENTBAY_RECONCILE_DEADLINE_SECONDS=120" in script
    assert script.index('AGENTBAY_UNRESOLVED_BEFORE_MAINTENANCE="$(agentbay_unresolved_count') < script.index(
        'echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"'
    )
    stop_old_writers = script.index('echo "[remote] stopping every old application writer before quarantine/migration"')
    migration = script.index('echo "[remote] applying migrations before candidate startup"')
    post_migration_cleanup = script.index('if ! reconcile_agentbay_for_cutover "$CANDIDATE_PROJECT" "$RELEASE";')
    candidate_start = script.index(
        'compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps backend'
    )
    assert stop_old_writers < migration < post_migration_cleanup < candidate_start
    assert "reconcile_agentbay_for_cutover" not in script[stop_old_writers:migration]
    assert "scripts/ruff_diff_gate.py" in script
    assert "git describe --tags --abbrev=0 HEAD^" in script
    assert '--base "$RELEASE_BASE_COMMIT" --target HEAD' in script
    assert '"$RELEASE/BASE_COMMIT"' in script
    assert "bash scripts/postgres-migration-smoke.sh" in script
    assert '"ALLOW_UNVERIFIED_LOCAL_SIGNUP": "false"' in script


def test_production_identity_preflight_runs_before_maintenance_and_redacts_values():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".agents/workflows/deploy-production.md").read_text(encoding="utf-8")
    migration = script.index('echo "[remote] applying migrations before candidate startup"')

    function = _shell_function_source(
        script,
        "identity_integrity_preflight",
        "verify_public_maintenance",
    )
    assert "duplicate_email_groups" in function
    assert "cross_namespace_username_email_conflicts" in function
    assert "cross_namespace_username_phone_conflicts" in function
    assert "duplicate_provider_open_id_groups" in function
    assert "duplicate_tenantless_membership_groups" in function
    assert "WHERE identity_id IS NOT NULL AND tenant_id IS NULL" in function
    assert "invalid_identity_provider_config_shapes" in function
    assert "pg_typeof(config)::text IN ('json', 'jsonb')" in function
    assert "jsonb_typeof(to_jsonb(config)) IS DISTINCT FROM 'object'" in function
    assert "unencrypted_identity_provider_text_configs" in function
    assert "CAST(config AS text) NOT LIKE 'enc:idp:v1:%'" in function
    assert "NOT IN ('web', 'platform')" in function
    assert "provider_member_tenant_mismatches" in function
    assert "tenant_provider_member_user_mismatches" in function
    assert "agent_member_provider_tenant_mismatches" in function
    assert "password_hashes_disabled_for_audit" in function
    assert "os.chmod(temp_path, 0o600)" in function
    assert "SELECT email" not in function
    assert "SELECT open_id" not in function
    assert function.count('"duplicate_tenantless_membership_groups"') >= 1

    preflight_call = script.index(
        "identity_integrity_preflight \\" +
        '\n    "$PREVIOUS" "$BACKUP/identity-integrity-preflight.json"'
    )
    maintenance = script.index(
        'echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"'
    )
    assert preflight_call < maintenance
    candidate_build = script.index(
        'compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" build backend frontend'
    )
    preexisting_secret_gate = script.index(
        "if ! verify_preexisting_identity_provider_secrets \\",
        candidate_build,
    )
    assert candidate_build < preexisting_secret_gate < maintenance
    storage_type_function = _shell_function_source(
        script,
        "identity_provider_config_storage_type",
        "verify_preexisting_identity_provider_secrets",
    )
    assert "information_schema.columns" in storage_type_function
    assert "json|jsonb|text|'character varying'" in storage_type_function
    preexisting_secret_function = _shell_function_source(
        script,
        "verify_preexisting_identity_provider_secrets",
        "identity_integrity_preflight",
    )
    assert "json|jsonb)" in preexisting_secret_function
    assert "-m app.scripts.verify_identity_provider_secrets" in preexisting_secret_function
    assert "identity_provider_secret_envelopes_verified=[0-9]+" in preexisting_secret_function
    assert "-m app.scripts.verify_channel_secrets" in script
    assert "-m app.scripts.verify_identity_provider_secrets" in script
    assert "SECRET_KEY continuity verified" in script
    assert "explicit envelope-key" in script
    assert "docker compose -f deploy/astra-poc/docker-compose.prod.yml config --quiet" in script
    assert 'git archive --format=tar --output="$PACKAGE_TAR" "$COMMIT"' in script
    assert 'git get-tar-commit-id < "$PACKAGE_TAR"' in script
    assert 'gzip -n -c "$PACKAGE_TAR" > "$PACKAGE"' in script
    assert '"$PACKAGE_SHA256" "$REMOTE_SMOKE_CREDENTIAL_DIGEST" \\' in script
    assert (
        '"$SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID" \\\n'
        '    "$RECOVERY_QA_TOOLING_BASE_COMMIT" <<\'REMOTE_LOADER\''
        in script
    )
    assert 'cat > "$REMOTE_SCRIPT_FILE" <<\'REMOTE_SCRIPT\'' in script
    assert 'bash "$REMOTE_SCRIPT_FILE" "$@" < /dev/null' in script
    assert '" > "$raw_path" < /dev/null; then' in function
    assert "release package digest mismatch" in script
    assert 'write_atomic_line "$RELEASE/PACKAGE_SHA256" "$PACKAGE_SHA256"' in script
    assert (
        script.index('echo "[local] running full backend suite"')
        < script.index("git archive --format=tar")
        < script.index('echo "[remote] uploading package"')
    )
    assert script.index("release package digest mismatch") < script.index('tar -xzf "$PACKAGE" -C "$RELEASE"')
    channel_secret_gate = script.index(
        "-m app.scripts.verify_channel_secrets",
        migration,
    )
    identity_provider_secret_gate = script.index(
        "-m app.scripts.verify_identity_provider_secrets",
        channel_secret_gate,
    )
    schema_forward = script.index(
        'write_cutover_state schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID"',
        identity_provider_secret_gate,
    )
    assert (
        migration
        < channel_secret_gate
        < identity_provider_secret_gate
        < schema_forward
    )
    assert "Code 激活状态为\n`BLOCKED`" in workflow
    assert "精确 tenant UUID" in workflow
    assert "生产禁止 `subprocess`、`docker`" in workflow


def test_production_model_route_gate_rejects_invalid_platform_models():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    preflight = _shell_function_source(
        script,
        "model_route_credential_preflight",
        "m3_route_post_migration_preflight",
    )

    assert "model.tenant_id IS NOT NULL" in preflight
    assert "fallback_model.tenant_id IS NOT NULL" in preflight
    assert "model.enabled IS NOT TRUE" in preflight
    assert "fallback_model.enabled IS NOT TRUE" in preflight
    assert preflight.count("jsonb_exists") >= 6
    assert preflight.count("jsonb_array_length") >= 4
    assert "expected.modality = 'image'" in preflight
    assert "'[\"vision\"]'::jsonb" in preflight


def test_m3_post_migration_gate_requires_verified_runtime_tool_calling():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    postflight = _shell_function_source(
        script,
        "m3_route_post_migration_preflight",
        "quarantine_mcp_for_unsafe_release",
    )

    assert "model.supports_tool_calling IS NOT TRUE" in postflight
    assert "model.tool_calling_capability_source IS NULL" in postflight
    assert "model.tool_calling_capability_source NOT IN ('probe', 'builtin_registry')" in postflight
    assert "model.tool_calling_checked_at IS NULL" in postflight
    assert "model.tool_calling_error IS NOT NULL" in postflight
    assert "top_route.id <> expected.top_route_id" in postflight
    assert "top_route.fallback_route_id <> expected.fallback_route_id" in postflight
    assert "top_model.provider <> 'minimax'" in postflight
    assert "fallback_model.provider <> 'volcengine_agent_plan'" in postflight
    assert (
        "COALESCE(top_model.capabilities::jsonb ->> 'seed_revision', '')"
        in postflight
    )
    assert "seed_minimax_m3_understanding" in postflight
    assert "seed_agent_plan_text_routes" in postflight


def test_remote_product_smoke_is_required_unless_break_glass_is_audited():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    consumer_path = ROOT / "scripts/consume_break_glass_approval.py"
    consumer = consumer_path.read_text(encoding="utf-8")

    assert 'RUN_REMOTE_SMOKE="${RUN_REMOTE_SMOKE:-1}"' in script
    assert "REMOTE_SMOKE_BREAK_GLASS_ARTIFACT" in script
    for field in (
        '"approval_id"',
        '"approval_nonce"',
        '"approved_by"',
        '"bypassed_gates"',
        '"reason"',
        '"issued_at_utc"',
        '"expires_at_utc"',
        '"release_version"',
        '"release_commit"',
    ):
        assert field in script
    assert 'fields["release_commit"] != commit' in script
    assert "break-glass artifact contains duplicate field" in script
    assert "break-glass approval_nonce has an invalid format" in script
    assert ("break-glass artifact must explicitly bypass subscription_api and subscription_browser") in script
    assert "timedelta(hours=4)" in script
    assert 'BREAK_GLASS_NONCE_ROOT="$APP_ROOT/break-glass-nonces"' in script
    assert "break-glass approval nonce has already been used" in consumer
    assert consumer_path.is_file()
    assert 'sudo python3 "$RELEASE/scripts/consume_break_glass_approval.py"' in script
    assert '"approval_artifact_base64"' in consumer
    assert "os.fsync(temporary_fd)" in consumer
    assert "os.link(" in consumer
    assert "os.fsync(ledger_fd)" in consumer
    assert "remote-smoke-break-glass.approval" in script
    assert "remote-smoke-break-glass.sha256" in script
    assert "remote-smoke-break-glass.nonce-sha256" in script
    assert 'python3 "$RELEASE/scripts/subscription_production_smoke.py"' in script
    assert 'python3 "$RELEASE/scripts/merge_subscription_smoke_evidence.py"' in script
    assert '"$image" \\\n        --frontend-url "http://candidate-frontend:3000"' in script
    assert 'source "$SMOKE_ENV_FILE"' not in script
    assert '--credentials-file "$SMOKE_ENV_FILE"' in script
    candidate_worker = script.index('write_cutover_state candidate_services_ready "$CANDIDATE_SLOT" "$RELEASE_ID"')
    candidate_smoke = script.index(
        "if ! run_candidate_business_smoke \\\n",
        candidate_worker,
    )
    verified = script.index('write_cutover_state candidate_business_verified "$CANDIDATE_SLOT" "$RELEASE_ID"')
    cutover = script.index('echo "[remote] switching the verified maintenance fence to candidate traffic"')
    assert candidate_worker < candidate_smoke < verified < cutover
    assert '--api-base "$PUBLIC_URL/api"' not in script
    assert 'candidate_business_evidence_valid "$RELEASE_ID" "$CANDIDATE_PORT"' in script
    recovery = script.index("recover_indeterminate_cutover() {")
    recovery_evidence = script.index(
        'candidate_business_evidence_valid \\\n        "$target_release_id" "$target_port"',
        recovery,
    )
    recovery_cutover = script.index(
        'install "$NGINX_SITE" "$source_port" "$target_port"',
        recovery,
    )
    assert recovery_evidence < recovery_cutover
    consume = script.index("consume_break_glass_approval.py")
    recovery_call = script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', consume)
    assert consume < recovery_call


def test_dedicated_smoke_principal_lifecycle_is_explicit_bounded_and_pre_cutover():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'PREPARE_REMOTE_SMOKE_PRINCIPALS="${PREPARE_REMOTE_SMOKE_PRINCIPALS:-0}"' in script
    assert 'SMOKE_PRINCIPAL_CONFIRM_TENANT_ID="${SMOKE_PRINCIPAL_CONFIRM_TENANT_ID:-}"' in script
    assert "production smoke principals can only be prepared when RUN_REMOTE_SMOKE=1" in script
    assert "SMOKE_PRINCIPAL_CONFIRM_TENANT_ID must match SMOKE_TENANT_ID" in script
    assert "provision and deactivate operation ids must be distinct" in script
    assert "export -n PREPARE_REMOTE_SMOKE_PRINCIPALS" in script
    assert "export -n SMOKE_PRINCIPAL_CONFIRM_TENANT_ID" in script
    assert "export -n SMOKE_PRINCIPAL_PROVISION_OPERATION_ID" in script
    assert "export -n SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID" in script
    assert "manage_production_smoke_principals.py" in script
    assert 'env PYTHONPATH=/app python "$manager_path"' in script
    assert '--confirm-environment production' in script
    assert '--confirm-tenant-id "$SMOKE_PRINCIPAL_CONFIRM_TENANT_ID"' in script
    assert 'manager_args+=(--operation-id "$operation_id" --apply)' in script
    assert 'source "$SMOKE_ENV_FILE"' not in script

    candidate_ready = script.index(
        'write_cutover_state candidate_alert_canary_verified \\\n'
        '    "$CANDIDATE_SLOT" "$RELEASE_ID"'
    )
    prepare = script.index("if ! prepare_smoke_principals; then", candidate_ready)
    smoke = script.index("if ! run_candidate_business_smoke \\\n", prepare)
    deactivate = script.index(
        "if ! deactivate_smoke_platform_principal after-smoke; then",
        smoke,
    )
    remove_credentials = script.index('rm -f "$SMOKE_ENV_FILE"', deactivate)
    cutover = script.index(
        'echo "[remote] switching the verified maintenance fence to candidate traffic"',
        remove_credentials,
    )
    assert candidate_ready < prepare < smoke < deactivate < remove_credentials < cutover

    rollback = _shell_function_source(script, "rollback", "abort_release")
    rollback_deactivate = rollback.index("deactivate_smoke_platform_principal rollback")
    rollback_fail_closed = rollback.index(
        "refusing to restore public traffic until temporary platform authority is removed"
    )
    rollback_return = rollback.index("return 1", rollback_fail_closed)
    rollback_remove_credentials = rollback.index('rm -f "$SMOKE_ENV_FILE"')
    rollback_restore = rollback.index("schema_state=", rollback_remove_credentials)
    assert (
        rollback_deactivate
        < rollback_fail_closed
        < rollback_return
        < rollback_remove_credentials
        < rollback_restore
    )
    assert "smoke_principal_cleanup_incomplete" in rollback
    assert "verify_public_maintenance" in rollback
    assert "temporary release-smoke platform authority requires operator attention" in rollback


def test_recovery_prepares_and_revokes_dedicated_smoke_principals_before_public_restore():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper = _shell_function_source(
        script,
        "recover_candidate_business_evidence_with_smoke_principals",
        "write_atomic_symlink",
    )
    recovery_start = script.index("recover_indeterminate_cutover() {")
    recovery_end = script.index("\nif ! CURRENT_TARGET=", recovery_start)
    recovery = script[recovery_start:recovery_end]

    prepare = helper.index("if ! prepare_smoke_principals")
    smoke = helper.index("elif ! regenerate_candidate_business_evidence", prepare)
    deactivate = helper.index("! deactivate_smoke_platform_principal", smoke)
    consumed = helper.index("RECOVERY_SMOKE_LIFECYCLE_CONSUMED=1", deactivate)
    public_restore = recovery.index('install "$NGINX_SITE"')

    assert prepare < smoke < deactivate < consumed
    assert "recover_candidate_business_evidence_with_smoke_principals" in recovery
    assert recovery.index("recover_candidate_business_evidence_with_smoke_principals") < public_restore
    assert "temporary release-smoke platform authority could not be removed" in helper


@pytest.mark.parametrize(
    (
        "prepare_status",
        "smoke_status",
        "deactivate_status",
        "expected_status",
        "expected_active",
        "expected_consumed",
    ),
    [
        (0, 0, 0, 0, 0, 1),
        (1, 0, 0, 1, 0, 0),
        (0, 1, 0, 1, 0, 0),
        (0, 0, 1, 1, 1, 0),
    ],
)
def test_recovery_smoke_helper_always_attempts_post_provision_cleanup(
    tmp_path,
    prepare_status,
    smoke_status,
    deactivate_status,
    expected_status,
    expected_active,
    expected_consumed,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper = _shell_function_source(
        script,
        "recover_candidate_business_evidence_with_smoke_principals",
        "write_atomic_symlink",
    )
    release = _write_test_release(tmp_path, "candidate-release")
    harness = f"""set -u
APP_ROOT={shlex.quote(str(tmp_path))}
COMPOSE_FILE=docker-compose.prod.yml
PREPARE_REMOTE_SMOKE_PRINCIPALS=1
SMOKE_PLATFORM_PRINCIPAL_ACTIVE=0
RECOVERY_SMOKE_LIFECYCLE_CONSUMED=0
compose_project() {{ echo backend-container; }}
prepare_smoke_principals() {{
    SMOKE_PLATFORM_PRINCIPAL_ACTIVE=1
    return {prepare_status}
}}
regenerate_candidate_business_evidence() {{ return {smoke_status}; }}
deactivate_smoke_platform_principal() {{
    echo cleanup_attempted
    if [ {deactivate_status} -eq 0 ]; then
        SMOKE_PLATFORM_PRINCIPAL_ACTIVE=0
    fi
    return {deactivate_status}
}}
{helper}
recover_candidate_business_evidence_with_smoke_principals \
    {shlex.quote(str(release))} candidate-release 3008 candidate-project
status=$?
echo "status=$status"
echo "active=$SMOKE_PLATFORM_PRINCIPAL_ACTIVE"
echo "consumed=$RECOVERY_SMOKE_LIFECYCLE_CONSUMED"
exit 0
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "cleanup_attempted" in result.stdout
    assert f"status={expected_status}" in result.stdout
    assert f"active={expected_active}" in result.stdout
    assert f"consumed={expected_consumed}" in result.stdout


def test_recovered_release_result_is_identity_bound_and_checks_runtime_equivalence():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'write_atomic_line "$BACKUP/recovered-release-result"' in script
    assert 'chmod 0600 "$BACKUP/recovered-release-result"' in script
    assert 'version.get("release_id") != expected_release_id' in script
    assert 'git merge-base --is-ancestor "$EXPECTED_COMMIT" "$COMMIT"' in script
    assert 'stat -c \'%a\' "$result_file"' in script
    assert 'stat -c \'%s\' "$result_file"' in script
    assert '"RELEASE_NOTES.md"' in script
    assert '"backend/tests/test_production_deploy_contract.py"' in script
    assert '"scripts/deploy-astra-production.sh"' in script
    assert '"scripts/subscription_production_smoke.py"' in script
    assert '"scripts/merge_subscription_smoke_evidence.py"' in script
    assert '"backend/tests/test_subscription_smoke_evidence.py"' in script
    assert '"deploy/browser-smoke/subscription_browser_smoke.mjs"' in script


@pytest.mark.parametrize(
    "payload",
    [
        "recovered_existing|release-id|1.12.0|" + "a" * 40,
        "recovered_existing|release-id|1.12.1|" + "a" * 40 + "|a",
        "recovered_existing|release-id|1.12.0|not-a-commit|a",
        "recovered_existing|release-id|1.12.0|" + "a" * 40 + "|legacy",
        "recovered_existing|release-id|1.12.0|" + "a" * 40 + "|a\ntampered",
        "recovered_existing|release-id|1.12.0|" + "a" * 40 + "|a|extra",
    ],
)
def test_recovered_release_result_parser_rejects_tampering(payload):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    parser = _heredoc_source(script, "PY_RECOVERED_RELEASE_RESULT")

    result = _run_embedded_python(parser, payload, "1.12.0")

    assert result.returncode != 0


def test_recovered_release_result_parser_accepts_identity_bound_payload():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    parser = _heredoc_source(script, "PY_RECOVERED_RELEASE_RESULT")
    payload = "recovered_existing|release-id|1.12.0|" + "a" * 40 + "|a"

    result = _run_embedded_python(parser, payload, "1.12.0")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("changed_files", "expected_status"),
    [
        ("", 0),
        ("scripts/deploy-astra-production.sh", 0),
        (
            "RELEASE_NOTES.md\n"
            "scripts/deploy-astra-production.sh\n"
            "backend/tests/test_production_deploy_contract.py",
            0,
        ),
        (
            "RELEASE_NOTES.md\n"
            "scripts/deploy-astra-production.sh\n"
            "deploy/browser-smoke/subscription_browser_smoke.mjs\n"
            "scripts/subscription_production_smoke.py\n"
            "scripts/merge_subscription_smoke_evidence.py\n"
            "backend/tests/test_subscription_smoke_evidence.py",
            0,
        ),
        ("backend/app/main.py", 1),
        ("scripts/deploy-astra-production.sh\nfrontend/src/App.tsx", 1),
    ],
)
def test_recovered_candidate_diff_guard_is_fail_closed(changed_files, expected_status):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    guard = _heredoc_source(script, "PY_RECOVERED_RELEASE_DIFF")

    result = _run_embedded_python(guard, changed_files)

    assert (result.returncode == 0) is (expected_status == 0), result.stderr


@pytest.mark.parametrize(
    ("changed_files", "expected_status"),
    [
        ("", 0),
        ("RELEASE_NOTES.md", 0),
        ("scripts/subscription_production_smoke.py", 0),
        (
            "RELEASE_NOTES.md\n"
            "scripts/deploy-astra-production.sh\n"
            "deploy/browser-smoke/subscription_browser_smoke.mjs\n"
            "scripts/subscription_production_smoke.py\n"
            "scripts/merge_subscription_smoke_evidence.py\n"
            "backend/tests/test_production_deploy_contract.py\n"
            "backend/tests/test_subscription_smoke_evidence.py",
            0,
        ),
        ("backend/app/main.py", 1),
        ("scripts/subscription_production_smoke.py\nfrontend/src/App.tsx", 1),
    ],
)
def test_nonterminal_recovery_qa_tooling_diff_guard_is_fail_closed(
    changed_files,
    expected_status,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    guard = _heredoc_source(script, "PY_RECOVERY_QA_TOOLING_DIFF")

    result = _run_embedded_python(guard, changed_files)

    assert (result.returncode == 0) is (expected_status == 0), result.stderr


def test_public_release_verifier_rejects_release_id_mismatch():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    verifier = _heredoc_source(script, "PY_PUBLIC_RELEASE_VERIFY")
    health = json.dumps({"status": "ok", "version": "1.12.0"})
    matching = json.dumps(
        {"version": "1.12.0", "commit": "a" * 40, "release_id": "release-id"}
    )
    mismatched = json.dumps(
        {"version": "1.12.0", "commit": "a" * 40, "release_id": "other-release"}
    )

    accepted = _run_embedded_python(
        verifier, health, matching, "1.12.0", "a" * 40, "release-id"
    )
    rejected = _run_embedded_python(
        verifier, health, mismatched, "1.12.0", "a" * 40, "release-id"
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


def test_smoke_principal_controls_stay_local_to_the_release_orchestrator():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    start = script.index("isolate_smoke_principal_controls() {")
    end = script.index("\nisolate_smoke_principal_controls\n", start)
    isolate = script[start:end]
    harness = f"""set -eu
export PREPARE_REMOTE_SMOKE_PRINCIPALS=1
export SMOKE_PRINCIPAL_CONFIRM_TENANT_ID=11111111-1111-4111-8111-111111111111
export SMOKE_PRINCIPAL_PROVISION_OPERATION_ID=22222222-2222-4222-8222-222222222222
export SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID=33333333-3333-4333-8333-333333333333
{isolate}
isolate_smoke_principal_controls
[ "$PREPARE_REMOTE_SMOKE_PRINCIPALS" = 1 ]
[ "$SMOKE_PRINCIPAL_CONFIRM_TENANT_ID" = 11111111-1111-4111-8111-111111111111 ]
[ "$SMOKE_PRINCIPAL_PROVISION_OPERATION_ID" = 22222222-2222-4222-8222-222222222222 ]
[ "$SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID" = 33333333-3333-4333-8333-333333333333 ]
bash -c '
    test -z "${{PREPARE_REMOTE_SMOKE_PRINCIPALS-}}" &&
    test -z "${{SMOKE_PRINCIPAL_CONFIRM_TENANT_ID-}}" &&
    test -z "${{SMOKE_PRINCIPAL_PROVISION_OPERATION_ID-}}" &&
    test -z "${{SMOKE_PRINCIPAL_DEACTIVATE_OPERATION_ID-}}"
'
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    isolate_call = script.index("\nisolate_smoke_principal_controls\n", start)
    local_gates = script.index('echo "[local] verifying unique production data-plane DNS"')
    assert isolate_call < local_gates


def test_candidate_business_evidence_rejects_tampering_and_wrong_slot(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    verifier = _shell_function_source(
        script,
        "candidate_business_evidence_valid",
        "write_atomic_symlink",
    )
    release_id = "candidate-release"
    _write_test_release(tmp_path, release_id, commit="a" * 40)
    backup = tmp_path / "backups" / release_id
    backup.mkdir(parents=True)
    evidence = backup / "subscription-smoke.candidate.json"
    payload = {
        "ok": True,
        "api_base": "http://127.0.0.1:3009/api",
        "checks": [
            "tenant_login_ok",
            "tenant_me_ok",
            "client_subscription_summary_ok",
            "client_credit_transactions_ok",
            "client_orders_ok",
            "platform_admin_login_ok",
            "saas_ledger_reconciliation_ok",
            "saas_payment_reconciliation_ok",
            "orders_csv_export_ok",
            "credit_transactions_csv_export_ok",
        ],
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    marker = backup / "candidate-business-verification"
    marker.write_text(f"smoke:{digest}\n", encoding="utf-8")
    # Exercise the shell verifier with the same interpreter that runs the
    # suite.  A developer machine may expose an unrelated preview/system
    # ``python3`` first on PATH; that must not make this hermetic contract test
    # hang or validate a different Python runtime than the project supports.
    child_env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    harness = f"""set -e
APP_ROOT={shlex.quote(str(tmp_path))}
{verifier}
candidate_business_evidence_valid {release_id} 3009
"""

    valid = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
        timeout=10,
    )
    assert valid.returncode == 0, valid.stderr

    wrong_slot = subprocess.run(
        ["bash", "-c", harness.replace(" 3009\n", " 3008\n")],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
        timeout=10,
    )
    assert wrong_slot.returncode != 0

    evidence.write_text(json.dumps({**payload, "ok": False}), encoding="utf-8")
    tampered = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
        timeout=10,
    )
    assert tampered.returncode != 0


def test_v3_candidate_evidence_binds_full_business_flow_to_candidate_slot(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    browser_helpers = _shell_function_source(
        script,
        "browser_smoke_bundle_digest",
        "cleanup_browser_smoke_runtime",
    )
    verifier = _shell_function_source(
        script,
        "candidate_business_evidence_valid",
        "write_atomic_symlink",
    )
    release_id = "candidate-release-v3"
    commit = "b" * 40
    package_sha256 = "c" * 64
    release = _write_test_release(tmp_path, release_id, commit=commit)
    (release / "PACKAGE_SHA256").write_text(f"{package_sha256}\n", encoding="utf-8")
    prior_qa_release = _write_test_release(
        tmp_path,
        "prior-qa-tooling-release",
        commit=commit,
    )
    (prior_qa_release / "PACKAGE_SHA256").write_text(
        f"{package_sha256}\n",
        encoding="utf-8",
    )
    browser_dir = release / "deploy/browser-smoke"
    browser_dir.mkdir(parents=True)
    bundle_names = (
        "Dockerfile",
        "EVIDENCE_SCHEMA",
        "browser_launch_selftest.mjs",
        "browser_assertions.mjs",
        "package-lock.json",
        "package.json",
        "seccomp_profile.json",
        "subscription_browser_smoke.mjs",
    )
    for name in bundle_names:
        (browser_dir / name).write_text(
            "3\n" if name == "EVIDENCE_SCHEMA" else f"fixture:{name}\n",
            encoding="utf-8",
        )
    bundle_digest = hashlib.sha256()
    for name in bundle_names:
        bundle_digest.update(name.encode())
        bundle_digest.update(b"\0")
        bundle_digest.update((browser_dir / name).read_bytes())
        bundle_digest.update(b"\0")
    runner_digest = f"sha256:{bundle_digest.hexdigest()}"

    required_checks = [
        "candidate_release_identity_ok",
        "tenant_login_ok",
        "tenant_me_ok",
        "tenant_scope_ok",
        "tenant_billing_manage_capability_ok",
        "billing_manual_semantics_ok",
        "client_plans_ok",
        "client_subscription_summary_ok",
        "client_credit_transactions_ok",
        "client_orders_ok",
        "client_credit_packs_ok",
        "personal_assistant_preflight_ok",
        "agent_employee_ready_ok",
        "agent_employee_preflight_ok",
        "work_executor_preflight_ok",
        "work_task_executed_ok",
        "work_task_output_marker_ok",
        "work_task_create_idempotency_ok",
        "work_task_result_review_ok",
        "group_persistence_ok",
        "group_member_visibility_ok",
        "group_message_idempotency_ok",
        "workforce_topology_refresh_ok",
        "credits_exactly_once_ok",
        "platform_admin_login_ok",
        "saas_ledger_reconciliation_ok",
        "saas_payment_reconciliation_ok",
        "orders_csv_export_ok",
        "credit_transactions_csv_export_ok",
        "ui_release_identity_ok",
        "ui_tenant_login_ok",
        "ui_tenant_scope_ok",
        "ui_subscription_summary_api_ok",
        "ui_subscription_balance_rendered_ok",
        "ui_subscription_page_ok",
        "ui_work_task_visible_ok",
        "ui_group_persistence_ok",
        "ui_workforce_topology_ok",
        "ui_direct_chat_round_trip_ok",
        "ui_direct_chat_recovery_ok",
        "ui_post_chat_credits_settled_ok",
        "ui_no_console_error_ok",
        "ui_no_server_error_ok",
    ]
    payload = {
        "evidence_schema_version": 3,
        "evidence_kind": "release_business_composite",
        "ok": True,
        "api_base": "http://127.0.0.1:3009/api",
        "frontend_url": "http://127.0.0.1:3009",
        "release_identity": {
            "version": "1.10.12",
            "commit": commit,
            "release_id": release_id,
        },
        "qa_tooling_identity": {
            "release_id": release_id,
            "commit": commit,
            "package_sha256": package_sha256,
        },
        "evidence_nonce": "1" * 32,
        "browser_gate": {
            "runner_bundle_sha256": runner_digest,
            "image_id": f"sha256:{'2' * 64}",
        },
        "checks": required_checks,
        "subscription_summary": {
            "plan_code": "pro",
            "balance": 100,
            "available_balance": 90,
            "reserved": 10,
        },
        "work_executor_preflight": {
            "personal_assistant": {
                "capability_status": "available",
                "reason_count": 0,
            },
            "agent_employee": {
                "capability_status": "available",
                "reason_count": 0,
            },
        },
        "agent_employee": {
            "created_for_release_qa": False,
            "ready": True,
        },
        "billing_mode": {
            "provider": "manual",
            "status": "manual",
            "checkout_enabled": True,
            "native_payment_enabled": False,
            "webhook_ready": False,
        },
        "business_flow": {
            "api": {
                "work": {
                    "executor_kind": "agent_employee",
                    "execution_status": "completed",
                    "output_marker_verified": True,
                    "create_replayed": True,
                    "result_review_status": "approved",
                    "review_replayed": True,
                },
                "group": {
                    "member_count": 2,
                    "owner_message_persisted": True,
                    "member_visibility": True,
                    "message_replayed": True,
                },
                "topology": {
                    "node_count": 1,
                    "employee_visible": True,
                    "completed_work_visible": True,
                },
                "credits": {
                    "consumed_delta": 4,
                    "transaction_delta": 1,
                    "reserved_before": 0,
                    "reserved_after": 0,
                    "replay_balance_delta": 0,
                    "replay_transaction_delta": 0,
                },
            },
            "ui": {
                "work": {"task_visible": True},
                "group": {"group_visible": True, "message_restored": True},
                "topology": {"completed_work_visible": True},
                "direct_chat": {
                    "round_trip": True,
                    "durable_after_reload": True,
                    "message_count": 2,
                    "assistant_count": 1,
                },
                "credits": {
                    "settled_after_chat": True,
                    "reserved_after": 0,
                    "consumed_delta_positive": True,
                },
            },
        },
        "saas_ledger_reconciliation": {
            "checked_tenants": 2,
            "issue_count": 0,
        },
        "saas_payment_reconciliation": {
            "checked_orders": 1,
            "issue_count": 0,
        },
        "ui": {
            "final_path": "/account/subscription",
            "browser_target": "isolated_candidate_frontend_network",
        },
    }
    backup = tmp_path / "backups" / release_id
    backup.mkdir(parents=True)
    evidence = backup / "subscription-smoke.candidate.json"
    marker = backup / "candidate-business-verification"

    def write_evidence(value: dict) -> None:
        evidence.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
        marker.write_text(f"smoke-v3:{digest}\n", encoding="utf-8")

    write_evidence(payload)
    child_env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    harness = f"""set -e
APP_ROOT={shlex.quote(str(tmp_path))}
RELEASE_ID={release_id}
COMMIT={commit}
PACKAGE_SHA256={package_sha256}
{browser_helpers}
{verifier}
candidate_business_evidence_valid {release_id} 3009
"""

    def verify() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            check=False,
            env=child_env,
            timeout=10,
        )

    assert verify().returncode == 0
    write_evidence(
        {
            **payload,
            "qa_tooling_identity": {
                **payload["qa_tooling_identity"],
                "release_id": "prior-qa-tooling-release",
            },
        }
    )
    assert verify().returncode == 0
    write_evidence(payload)
    invalid_payloads = [
        {**payload, "frontend_url": "http://127.0.0.1:3008"},
        {**payload, "checks": required_checks[:-1]},
        {
            **payload,
            "work_executor_preflight": {
                **payload["work_executor_preflight"],
                "agent_employee": {
                    "capability_status": "unavailable",
                    "reason_count": 1,
                },
            },
        },
        {
            **payload,
            "release_identity": {**payload["release_identity"], "release_id": "other"},
        },
        {
            **payload,
            "qa_tooling_identity": {
                **payload["qa_tooling_identity"],
                "commit": "d" * 40,
            },
        },
        {
            **payload,
            "browser_gate": {
                **payload["browser_gate"],
                "runner_bundle_sha256": f"sha256:{'3' * 64}",
            },
        },
        {
            **payload,
            "business_flow": {
                **payload["business_flow"],
                "api": {
                    **payload["business_flow"]["api"],
                    "credits": {
                        **payload["business_flow"]["api"]["credits"],
                        "replay_transaction_delta": 1,
                    },
                },
            },
        },
    ]
    for invalid in invalid_payloads:
        write_evidence(invalid)
        assert verify().returncode != 0


def test_browser_smoke_runner_is_isolated_pinned_and_pre_mutation():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    api_runner = (ROOT / "scripts/subscription_production_smoke.py").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy/browser-smoke/Dockerfile").read_text(encoding="utf-8")
    evidence_schema = (ROOT / "deploy/browser-smoke/EVIDENCE_SCHEMA").read_text(
        encoding="utf-8"
    ).strip()
    browser_runner = (ROOT / "deploy/browser-smoke/subscription_browser_smoke.mjs").read_text(encoding="utf-8")

    assert "@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48" in dockerfile
    assert "USER pwuser" in dockerfile
    assert evidence_schema == "3"
    assert f'browser-smoke-schema="{evidence_schema}"' in dockerfile
    assert 'tr -d \'[:space:]\' < "$target_release/deploy/browser-smoke/EVIDENCE_SCHEMA"' in script
    assert '[ "$label" = "$expected_schema" ]' in script
    for contract in (
        "--internal",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        '--security-opt "seccomp=$target_release/deploy/browser-smoke/seccomp_profile.json"',
        "--pids-limit 256",
        "--memory 1g",
        "--cpus 1.0",
        "--mount",
    ):
        assert contract in script
    assert "host-gateway" not in script
    assert "--network host" not in script
    assert "SMOKE_PLATFORM_ADMIN" not in browser_runner
    assert "credentials.SMOKE_TENANT_PASSWORD" in browser_runner
    assert "credentials.SMOKE_TENANT_ID" in browser_runner
    assert "frontendOrigin === 'http://candidate-frontend:3000'" in browser_runner
    assert '--unsafely-treat-insecure-origin-as-secure=${frontendOrigin}' in browser_runner
    assert "secureContextParity.is_secure_context === true" in browser_runner
    assert "secureContextParity.random_uuid_available === true" in browser_runner
    assert "ui_secure_context_parity_ok" in browser_runner
    assert "requires_tenant_selection" in browser_runner
    assert "target_tenant_not_available" in browser_runner
    assert "isolated_candidate_frontend_network" in browser_runner
    assert "subscription-credits-usage-value" in browser_runner
    assert "subscription-available-credits-value" in browser_runner
    assert "subscription-available-credits-reserved" in browser_runner
    assert "waitForExactText" in browser_runner
    assert "ui_direct_chat_round_trip_ok" in browser_runner
    assert "ui_direct_chat_recovery_ok" in browser_runner
    assert "ui_group_persistence_ok" in browser_runner
    assert "ui_workforce_topology_ok" in browser_runner
    assert '"executor_kind": "agent_employee"' in api_runner
    assert '"executor_kind": "personal_assistant"' in api_runner
    assert '"employee_visible": True' in api_runner
    assert '"assistant_visible": True' not in api_runner
    assert '"$RECOVERY_QA_TOOLING_BASE_COMMIT"' in script
    recovery_guard = script.index(
        'recovery candidate changed after the local QA tooling diff gate'
    )
    recovery_browser = script.index(
        'recovery QA-tooling browser smoke image failed its launch preflight'
    )
    assert recovery_guard < recovery_browser
    completed_tab = browser_runner.index("name: /最近完成|Recently completed/i")
    completed_task = browser_runner.index("getByText(taskTitle, { exact: true })")
    assert completed_tab < completed_task
    assert 'local qa_browser_release="${8:-$target_release}"' in script
    assert 'local image="astra-browser-smoke:${qa_browser_release_id}"' in script
    assert 'browser_smoke_bundle_digest "$qa_browser_release"' in script
    assert 'browser_smoke_bundle_digest "$qa_tooling_release"' in script
    assert "await Promise.all" in browser_runner
    assert '"body": body' not in api_runner
    assert "body[:200]" not in api_runner
    assert '(cd deploy/browser-smoke && npm test)' in script
    assert 'BROWSER_SMOKE_RUNTIME_ROOT="/dev/shm/astra-deploy-smoke"' in script
    assert 'mktemp -d "$BROWSER_SMOKE_RUNTIME_ROOT/.candidate-smoke.XXXXXX"' in script
    assert 'mktemp -d "$target_backup/.candidate-smoke.XXXXXX"' not in script
    current_preflight = script.index('ensure_browser_smoke_image "$RELEASE" "$RELEASE_ID"')
    recovery = script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', current_preflight)
    maintenance = script.index('echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"')
    assert current_preflight < recovery < maintenance


def test_browser_smoke_runtime_cleans_stale_credentials_and_rejects_symlinks(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    cleanup = _shell_function_source(
        script,
        "prepare_browser_smoke_runtime_root",
        "cleanup_browser_smoke_runtime",
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    stale = runtime_root / ".candidate-smoke.interrupted"
    stale.mkdir()
    (stale / "browser-credentials.json").write_text(
        "must-be-removed",
        encoding="utf-8",
    )
    stale.chmod(0o700)
    current = runtime_root / "current.smoke-credentials.json"
    current.write_text("preserve-current-upload", encoding="utf-8")
    current.chmod(0o600)
    old = runtime_root / "old.smoke-credentials.json"
    old.write_text("remove-interrupted-upload", encoding="utf-8")
    old.chmod(0o600)
    harness = f"""set -e
BROWSER_SMOKE_RUNTIME_ROOT={shlex.quote(str(runtime_root))}
SMOKE_ENV_FILE={shlex.quote(str(current))}
RUN_REMOTE_SMOKE=1
{cleanup}
prepare_browser_smoke_runtime_root
"""

    cleaned = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert not stale.exists()
    assert not old.exists()
    assert current.read_text(encoding="utf-8") == "preserve-current-upload"

    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe = runtime_root / ".candidate-smoke.symlink"
    unsafe.symlink_to(outside, target_is_directory=True)
    rejected = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert rejected.returncode != 0
    assert outside.is_dir()
    unsafe.unlink()

    unsafe_credential_target = tmp_path / "unsafe-credential-target"
    unsafe_credential_target.write_text("do-not-touch", encoding="utf-8")
    unsafe_credential = runtime_root / "unsafe.smoke-credentials.json"
    unsafe_credential.symlink_to(unsafe_credential_target)
    rejected_credential = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert rejected_credential.returncode != 0
    assert unsafe_credential_target.read_text(encoding="utf-8") == "do-not-touch"
    unsafe_credential.unlink()

    wrong_mode = runtime_root / "wrong-mode.smoke-credentials.json"
    wrong_mode.write_text("must-fail-closed", encoding="utf-8")
    wrong_mode.chmod(0o644)
    rejected_mode = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert rejected_mode.returncode != 0
    assert wrong_mode.read_text(encoding="utf-8") == "must-fail-closed"


def test_deploy_streams_smoke_credentials_without_local_disk_file():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'SMOKE_ENV_FILE="$PACKAGE_DIR/' not in script
    assert 'scp "${SSH_OPTS[@]}" "$SMOKE_ENV_FILE"' not in script
    assert "emit_smoke_credential_payload |" in script
    assert "REMOTE_CREDENTIAL_WRITER_B64" in script
    assert "os.O_EXCL" in script
    assert "os.O_NOFOLLOW" in script


def test_smoke_credentials_are_hidden_from_local_gate_children_and_emit_exact_json():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    capture = _shell_function_source(
        script,
        "capture_smoke_credentials",
        "assert_smoke_credentials_not_exported",
    )
    isolation = _shell_function_source(
        script,
        "assert_smoke_credentials_not_exported",
        "emit_smoke_credential_payload",
    )
    emitter = _shell_function_source(
        script,
        "emit_smoke_credential_payload",
        "require_cmd",
    )
    expected = {
        "SMOKE_TENANT_EMAIL": "tenant@example.com",
        "SMOKE_TENANT_PASSWORD": "tenant password with spaces",
        "SMOKE_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "SMOKE_PLATFORM_ADMIN_EMAIL": "admin@example.com",
        "SMOKE_PLATFORM_ADMIN_PASSWORD": "管理员-password",
        "SMOKE_MEMBER_EMAIL": "member@example.com",
        "SMOKE_MEMBER_PASSWORD": "member password sentinel",
    }
    harness = f"""set -euo pipefail
SMOKE_ENV_KEYS=(SMOKE_TENANT_EMAIL SMOKE_TENANT_PASSWORD SMOKE_TENANT_ID SMOKE_PLATFORM_ADMIN_EMAIL SMOKE_PLATFORM_ADMIN_PASSWORD SMOKE_MEMBER_EMAIL SMOKE_MEMBER_PASSWORD)
SMOKE_ENV_VALUES=()
export SMOKE_TENANT_EMAIL={shlex.quote(expected["SMOKE_TENANT_EMAIL"])}
export SMOKE_TENANT_PASSWORD={shlex.quote(expected["SMOKE_TENANT_PASSWORD"])}
export SMOKE_TENANT_ID={shlex.quote(expected["SMOKE_TENANT_ID"])}
export SMOKE_PLATFORM_ADMIN_EMAIL={shlex.quote(expected["SMOKE_PLATFORM_ADMIN_EMAIL"])}
export SMOKE_PLATFORM_ADMIN_PASSWORD={shlex.quote(expected["SMOKE_PLATFORM_ADMIN_PASSWORD"])}
export SMOKE_MEMBER_EMAIL={shlex.quote(expected["SMOKE_MEMBER_EMAIL"])}
export SMOKE_MEMBER_PASSWORD={shlex.quote(expected["SMOKE_MEMBER_PASSWORD"])}
{capture}
{isolation}
{emitter}
capture_smoke_credentials
assert_smoke_credentials_not_exported
emit_smoke_credential_payload
"""

    emitted = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert emitted.returncode == 0, emitted.stderr.decode()
    assert json.loads(emitted.stdout) == expected


def test_smoke_credentials_never_enter_bash_xtrace_output():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    credential_prefix = script[: script.index("ROOT_DIR=")]
    expected = {
        "SMOKE_TENANT_EMAIL": "xtrace-tenant@example.test",
        "SMOKE_TENANT_PASSWORD": "xtrace-tenant-password-sentinel",
        "SMOKE_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "SMOKE_PLATFORM_ADMIN_EMAIL": "xtrace-admin@example.test",
        "SMOKE_PLATFORM_ADMIN_PASSWORD": "xtrace-admin-password-sentinel",
        "SMOKE_MEMBER_EMAIL": "xtrace-member@example.test",
        "SMOKE_MEMBER_PASSWORD": "xtrace-member-password-sentinel",
    }
    canonical = (json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    expected_digest = hashlib.sha256(canonical).hexdigest()
    harness = credential_prefix + r'''
emit_smoke_credential_payload | python3 -c '
import hashlib
import sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())
'
'''
    env = {
        "PATH": os.environ.get("PATH", ""),
        **expected,
    }

    traced = subprocess.run(
        ["bash", "-x", "-c", harness],
        env=env,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert traced.returncode == 0, traced.stderr.decode()
    assert traced.stdout.decode().strip() == expected_digest
    combined_output = traced.stdout + traced.stderr
    for sentinel in expected.values():
        assert sentinel.encode() not in combined_output


def test_remote_worker_identity_is_allowlisted_and_xtrace_safe(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper = _shell_function_source(
        script,
        "inspect_worker_runtime_identity",
        "run_candidate_alert_canary",
    )
    remote_body = script.split("<<'REMOTE_SCRIPT'\n", 1)[1].split("\nREMOTE_SCRIPT", 1)[0]
    runtime_root_body = script.split("<<'REMOTE_SMOKE_RUNTIME_ROOT'\n", 1)[1].split(
        "\nREMOTE_SMOKE_RUNTIME_ROOT",
        1,
    )[0]

    assert remote_body.startswith("{ set +x; } 2>/dev/null\nset -Eeuo pipefail\n")
    assert runtime_root_body.startswith("{ set +x; } 2>/dev/null\nset -euo pipefail\n")
    assert script.count("env -u BASH_ENV -u BASHOPTS -u SHELLOPTS bash -s --") == 2
    assert "worker_environment" not in script
    assert "{{range .Config.Env}}{{println .}}{{end}}" not in script
    assert '"ASTRA_RELEASE_ID"}}{{println .}}' in helper
    assert '"ASTRA_RELEASE_COMMIT"}}{{println .}}' in helper
    assert '"ASTRA_ALERT_WORKER_ACTOR_ID"}}{{println .}}' in helper
    assert '"PROCESS_ROLE"}}{{println .}}' in helper

    sentinel = "remote-worker-secret-must-not-be-traced"
    docker_stub = tmp_path / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env python3\n"
        "print('ASTRA_RELEASE_ID=release-canary')\n"
        f"print('ASTRA_RELEASE_COMMIT={'a' * 40}')\n"
        "print('ASTRA_ALERT_WORKER_ACTOR_ID=00000000-0000-4000-8000-000000000001')\n"
        "print('PROCESS_ROLE=worker,connector')\n"
        f"print('DATABASE_URL={sentinel}')\n",
        encoding="utf-8",
    )
    docker_stub.chmod(0o700)
    env = {
        "PATH": (
            f"{tmp_path}{os.pathsep}{Path(sys.executable).parent}"
            f"{os.pathsep}{os.environ.get('PATH', '')}"
        )
    }
    docker_fixture = (
        f"docker() {{ {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(docker_stub))} \"$@\"; }}"
    )

    traced = subprocess.run(
        [
            "bash",
            "-x",
            "-c",
            (
                f"set -euo pipefail\n{docker_fixture}\n{helper}\n"
                "inspect_worker_runtime_identity worker-id"
            ),
        ],
        env=env,
        capture_output=True,
        check=False,
        # Full release suites can temporarily saturate local process startup;
        # retain a hard bound without turning scheduler latency into a false
        # production-contract failure.
        timeout=30,
    )

    assert traced.returncode == 0, traced.stderr.decode()
    assert traced.stdout.decode().splitlines() == [
        "release-canary",
        "a" * 40,
        "00000000-0000-4000-8000-000000000001",
        "worker,connector",
    ]
    assert sentinel.encode() not in traced.stdout + traced.stderr

    duplicate_dir = tmp_path / "duplicate"
    duplicate_dir.mkdir()
    duplicate_stub = duplicate_dir / "docker"
    duplicate_stub.write_text(
        "#!/usr/bin/env python3\n"
        "print('ASTRA_RELEASE_ID=release-canary')\n"
        "print('ASTRA_RELEASE_ID=duplicate-release')\n"
        f"print('ASTRA_RELEASE_COMMIT={'a' * 40}')\n"
        "print('ASTRA_ALERT_WORKER_ACTOR_ID=00000000-0000-4000-8000-000000000001')\n"
        "print('PROCESS_ROLE=worker,connector')\n",
        encoding="utf-8",
    )
    duplicate_stub.chmod(0o700)
    duplicate_env = {
        "PATH": (
            f"{duplicate_dir}{os.pathsep}{Path(sys.executable).parent}"
            f"{os.pathsep}{os.environ.get('PATH', '')}"
        ),
    }
    duplicate_docker_fixture = (
        f"docker() {{ {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(duplicate_stub))} \"$@\"; }}"
    )
    duplicated = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -euo pipefail\n{duplicate_docker_fixture}\n{helper}\n"
                "inspect_worker_runtime_identity worker-id"
            ),
        ],
        env=duplicate_env,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert duplicated.returncode != 0


def test_rollback_worker_identity_accepts_the_legacy_release_contract(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper = _shell_function_source(
        script,
        "inspect_rollback_worker_runtime_identity",
        "run_candidate_alert_canary",
    )
    sentinel = "legacy-worker-secret-must-not-be-traced"
    docker_stub = tmp_path / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env python3\n"
        "print('ASTRA_RELEASE_ID=release-legacy')\n"
        "print('ASTRA_RELEASE_COMMIT=53b7cbd')\n"
        "print('PROCESS_ROLE=worker,connector')\n"
        f"print('DATABASE_URL={sentinel}')\n",
        encoding="utf-8",
    )
    docker_stub.chmod(0o700)
    env = {
        "PATH": (
            f"{tmp_path}{os.pathsep}{Path(sys.executable).parent}"
            f"{os.pathsep}{os.environ.get('PATH', '')}"
        )
    }
    docker_fixture = (
        f"docker() {{ {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(docker_stub))} \"$@\"; }}"
    )

    traced = subprocess.run(
        [
            "bash",
            "-x",
            "-c",
            (
                f"set -euo pipefail\n{docker_fixture}\n{helper}\n"
                "inspect_rollback_worker_runtime_identity worker-id"
            ),
        ],
        env=env,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert traced.returncode == 0, traced.stderr.decode()
    assert traced.stdout.decode().splitlines() == [
        "release-legacy",
        "53b7cbd",
        "worker,connector",
    ]
    assert sentinel.encode() not in traced.stdout + traced.stderr


def test_failed_legacy_rollback_check_never_stops_the_previous_worker():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    activate = _shell_function_source(
        script,
        "activate_worker_release",
        "remaining_old_nginx_workers",
    )
    harness = f"""set +e
stop_managed_workers_except() {{ :; }}
compose_project() {{
    case " $* " in
        *" stop worker "*) echo unexpected_worker_stop ;;
        *) echo compose_ok ;;
    esac
}}
wait_for_worker_release() {{ echo unexpected_strict_wait; return 1; }}
assert_single_active_worker() {{ echo unexpected_strict_assert; return 1; }}
wait_for_rollback_worker_release() {{ return 1; }}
assert_single_managed_worker() {{ echo unexpected_single_assert; return 1; }}
{activate}
activate_worker_release project env compose release-old 1 rollback_legacy 53b7cbd
status=$?
echo status=$status
exit 0
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "status=1" in result.stdout
    assert "compose_ok" in result.stdout
    assert "unexpected_" not in result.stdout


def test_remote_deploy_buffers_script_and_rechecks_public_release_identity():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    loader_start = script.index("<<'REMOTE_LOADER'\n")
    buffered_script_start = script.index(
        'cat > "$REMOTE_SCRIPT_FILE" <<\'REMOTE_SCRIPT\'\n',
        loader_start,
    )
    buffered_script_end = script.index("\nREMOTE_SCRIPT\n", buffered_script_start)
    loader_end = script.index("\nREMOTE_LOADER\n", buffered_script_end)
    public_verify = script.index(
        'echo "[verify] independently checking public release identity"',
        loader_end,
    )
    done = script.index(
        'echo "[done] released $EXPECTED_RELEASE_ID ($EXPECTED_COMMIT) to $PUBLIC_URL"'
    )

    loader = script[loader_start:loader_end]
    assert "set -euo pipefail" in loader
    assert 'ORIGINAL_UMASK="$(umask)"' in loader
    assert "umask 077" in loader
    assert 'mktemp /tmp/.astra-production-deploy.XXXXXX' in loader
    assert 'umask "$ORIGINAL_UMASK"' in loader
    assert 'trap \'rm -f "$REMOTE_SCRIPT_FILE"\' EXIT' in loader
    assert 'bash "$REMOTE_SCRIPT_FILE" "$@" < /dev/null' in loader
    assert (
        loader.index("umask 077")
        < loader.index('mktemp /tmp/.astra-production-deploy.XXXXXX')
        < loader.index('umask "$ORIGINAL_UMASK"')
        < loader.index('cat > "$REMOTE_SCRIPT_FILE"')
    )
    assert loader_start < buffered_script_start < buffered_script_end < loader_end
    assert loader_end < public_verify < done
    assert "require_cmd curl" in script
    assert '"$PUBLIC_URL/api/health?release=$EXPECTED_RELEASE_ID"' in script
    assert '"$PUBLIC_URL/api/version?release=$EXPECTED_RELEASE_ID"' in script
    assert 'version.get("commit") != expected_commit' in script
    assert 'public release identity does not match the sealed artifact' in script


def test_remote_smoke_credential_writer_is_exclusive_and_symlink_safe(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    marker = "source = r'''\n"
    source = script.split(marker, 1)[1].split("\n'''", 1)[0]
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    credential = runtime_root / "candidate.smoke-credentials.json"
    payload = b'{"SMOKE_TENANT_EMAIL":"release@example.com"}\n'
    digest = hashlib.sha256(payload).hexdigest()

    written = subprocess.run(
        [sys.executable, "-c", source, str(credential), digest],
        input=payload,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert written.returncode == 0, written.stderr.decode()
    assert credential.read_bytes() == payload
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600

    replacement = b'{"SMOKE_TENANT_EMAIL":"attacker@example.com"}\n'
    rejected_overwrite = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(credential),
            hashlib.sha256(replacement).hexdigest(),
        ],
        input=replacement,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert rejected_overwrite.returncode != 0
    assert credential.read_bytes() == payload

    credential.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"do-not-overwrite")
    credential.symlink_to(outside)
    rejected_symlink = subprocess.run(
        [sys.executable, "-c", source, str(credential), digest],
        input=payload,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert rejected_symlink.returncode != 0
    assert outside.read_bytes() == b"do-not-overwrite"


def test_browser_smoke_runtime_without_current_upload_removes_all_stale_credentials(
    tmp_path,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    cleanup = _shell_function_source(
        script,
        "prepare_browser_smoke_runtime_root",
        "cleanup_browser_smoke_runtime",
    )
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    stale = runtime_root / "interrupted.smoke-credentials.json"
    stale.write_text("remove-without-current-deploy", encoding="utf-8")
    stale.chmod(0o600)
    harness = f"""set -e
BROWSER_SMOKE_RUNTIME_ROOT={shlex.quote(str(runtime_root))}
SMOKE_ENV_FILE={shlex.quote(str(runtime_root / "unused.smoke-credentials.json"))}
RUN_REMOTE_SMOKE=0
{cleanup}
prepare_browser_smoke_runtime_root
"""

    cleaned = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert cleaned.returncode == 0, cleaned.stderr
    assert not stale.exists()


def test_media_credit_inventory_fences_migration_for_every_company():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    stop_writers = script.index("stop --timeout 90 worker frontend backend")
    pre_inventory = script.index(
        "media-credit-inventory.pre-migration.json",
        stop_writers,
    )
    migration = script.index(
        "--entrypoint alembic backend upgrade head",
        pre_inventory,
    )
    post_inventory = script.index(
        "media-credit-inventory.post-migration.json",
        migration,
    )
    candidate_start = script.index(
        "up -d --no-deps backend",
        post_inventory,
    )

    assert stop_writers < pre_inventory < migration < post_inventory < candidate_start
    inventory_module = "-m app.scripts.inventory_legacy_media_reservations"
    assert script.count(inventory_module) == 2
    assert script.count("--fail-on-blocking") == 2
    assert "--fail-on-blocking --require-no-legacy" in script
    assert 'abort_release "media Credits inventory' in script


def test_mcp_host_egress_guard_is_a_pre_mutation_release_gate():
    deploy_script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    guard_path = ROOT / "scripts/manage-production-mcp-egress-guard.sh"
    contract_path = ROOT / "deploy/security-contracts/mcp-egress-v1"
    guard = guard_path.read_text(encoding="utf-8")
    contract = contract_path.read_text(encoding="utf-8")

    assert guard_path.is_file()
    assert contract_path.is_file()
    assert "ASTRA_MCP_EGRESS_V1" in guard
    assert "DOCKER-USER" in guard
    assert "198.18.0.0/15" in guard
    assert "169.254.0.0/16" in guard
    assert "astra-mcp-egress-public-v1" in guard
    assert "astra-mcp-egress-repair-v1" in guard
    assert "MCP egress chain must have exactly one DOCKER-USER jump" in guard
    assert "repair can interrupt" in guard
    assert "flock -w 30" in guard
    assert 'NETWORK="${2:-}"' in guard
    assert 'NETWORK="${2:-astra_network}"' not in guard
    assert "network_attached_container_count" in guard
    assert "Docker network with no attached containers" in guard
    install_watchdog = guard[guard.index("install_watchdog()") : guard.index('case "$ACTION"')]
    assert install_watchdog.index('attached_container_count="$(network_attached_container_count)"') < (
        install_watchdog.index('install -d -m 0700 "$INSTALL_DIR"')
    )
    # POSIX awk implementations reserve `index` as a built-in function name.
    # Using it as a loop variable passed source inspection but failed on the
    # Ubuntu production host before the guard could be installed.
    assert "for (index =" not in guard
    assert "for (field_idx =" in guard
    assert "public-port allowlist" in contract
    assert "astra-mcp-egress-guard.timer" in guard
    assert "OnUnitActiveSec=30s" in guard
    assert "Normal application deployment only verifies this contract" in contract

    data_plane = deploy_script.index(
        'echo "[remote] verifying unique production data-plane DNS"'
    )
    gate = deploy_script.index('echo "[remote] verifying host-level MCP egress contract"')
    recovery = deploy_script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', gate)
    backup = deploy_script.index('echo "[remote] backing up database')
    maintenance = deploy_script.index(
        'echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"',
        gate,
    )
    migration = deploy_script.index("backend upgrade head", maintenance)
    assert data_plane < gate < recovery < backup < maintenance < migration
    assert 'manage-production-mcp-egress-guard.sh" verify' in deploy_script
    assert 'DOCKER_NETWORK_NAME="astra_network"' not in deploy_script
    assert "current release environment must define DOCKER_NETWORK" in deploy_script

    missing_network = subprocess.run(
        ["bash", str(guard_path), "verify"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_network.returncode != 0
    assert "invalid Docker network name" in missing_network.stderr


def test_production_database_backup_is_created_owner_only():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    start = script.index('echo "[remote] backing up database to $BACKUP/db.sql.gz"')
    end = script.index('echo "[remote] validating persisted MCP endpoint policy"', start)
    backup = script[start:end]

    assert "(\n    umask 077" in backup
    assert 'gzip > "$BACKUP/db.sql.gz"' in backup
    assert 'chmod 0600 "$BACKUP/db.sql.gz"' in backup
    assert 'test -s "$BACKUP/db.sql.gz"' in backup


def test_mcp_host_egress_guard_installation_uses_the_live_network():
    workflow = (ROOT / ".agents/workflows/deploy-production.md").read_text(encoding="utf-8")

    assert "/opt/astra-poc/current/.env" in workflow
    assert '"$DOCKER_NETWORK_NAME" deploy/security-contracts/mcp-egress-v1' in workflow
    assert "目标网络必须\n已有应用容器" in workflow
    assert "禁止使用脚本默认值或对空网络安装" in workflow


def test_normal_production_deploy_force_disables_code_activation():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    for expected in (
        '"CODE_EXECUTION_ENABLED": "false"',
        '"CODE_EXECUTION_ALLOWED_TENANT_IDS": ""',
        '"CODE_EXECUTION_ALLOWED_TOOL_NAMES": ""',
        '"CODE_EXECUTION_ALLOWED_SANDBOX_TYPES": ""',
        '"CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS": ""',
        '"CODE_EXECUTION_REQUIRE_APPROVAL": "true"',
        '"SANDBOX_ALLOW_NETWORK": "false"',
        '"SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING": "false"',
    ):
        assert expected in script


def test_mcp_deployment_contract_is_fail_closed_and_matches_runtime_classifier():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    sanitizer = ROOT / "backend/app/scripts/secure_mcp_quarantine.py"
    marker = ROOT / "deploy/security-contracts/mcp-assignment-v1"

    assert marker.is_file()
    assert sanitizer.is_file()
    assert "regexp_split_to_table" in script
    assert "([a-z0-9])([A-Z])" in script
    assert "_access_key" in script
    assert "_apikey" in script
    assert "astra_deploy_mcp_quarantine_state" in script
    assert "secure_mcp_quarantine" in script
    assert "restore_mcp_quarantine_for_safe_release" in script
    assert "astra_deploy_mcp_quarantine_tools_guard" in script
    assert "astra_deploy_mcp_quarantine_assignments_guard" in script
    assert "NEW.enabled := false" in script
    assert "NEW.mcp_server_url := NULL" in script
    assert "RETURN NULL" in script
    assert "NEW.tenant_id := OLD.tenant_id" in script
    assert "NEW.agent_id := OLD.agent_id" in script
    assert "NEW.is_default := OLD.is_default" in script
    assert "NEW.is_default := false" in script
    assert "SET LOCAL astra.mcp_quarantine_restore = 'on'" in script

    quarantine_start = script.index("quarantine_mcp_for_unsafe_release() {")
    restore_start = script.index("restore_mcp_quarantine_for_safe_release() {")
    quarantine = script[quarantine_start:restore_start]
    restore = script[restore_start : script.index("project_for_slot() {", restore_start)]
    assert quarantine.index("CREATE TRIGGER astra_deploy_mcp_quarantine_tools_guard") < (
        quarantine.index("UPDATE tools")
    )
    assert "SELECT pg_advisory_xact_lock" not in quarantine
    assert "PERFORM pg_advisory_xact_lock" in quarantine
    assert restore.index("SET LOCAL astra.mcp_quarantine_restore") < restore.index("UPDATE tools AS tool")
    assert restore.index("UPDATE agent_tools AS assignment") < restore.index(
        "DELETE FROM astra_deploy_mcp_quarantine_state"
    )
    assert restore.index("DELETE FROM astra_deploy_mcp_quarantine_state") < (restore.index("DROP TRIGGER"))

    trap = script.index("trap 'on_error $?' ERR")
    build = script.index('echo "[remote] building candidate slot')
    migration_quarantine = script.index(
        '"$PREVIOUS" "migration-${RELEASE_ID}"',
        build,
    )
    migration_gate = script.index(
        "ROLLBACK_REQUIRES_MCP_QUARANTINE=1",
        build,
    )
    migration = script.index("backend upgrade head", migration_quarantine)
    assert "ROLLBACK_REQUIRES_MCP_QUARANTINE=0" in script[:trap]
    assert trap < build < migration_gate < migration_quarantine < migration

    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    assert 'if [ "$ROLLBACK_REQUIRES_MCP_QUARANTINE" = "1" ]' in rollback
    assert rollback.index("ROLLBACK_REQUIRES_MCP_QUARANTINE") < rollback.index("quarantine_mcp_for_unsafe_release")


def test_pre_migration_rollback_never_quarantines_live_mcp(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    harness = f"""set -eu
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / "missing-smoke"))}
PREVIOUS={shlex.quote(str(tmp_path / "previous"))}
PREVIOUS_RELEASE_ID=release-old
PREVIOUS_VERSION=1.10.11
PREVIOUS_COMMIT=old1234
RELEASE={shlex.quote(str(tmp_path / "candidate"))}
RELEASE_ID=release-new
ACTIVE_SLOT=b
CANDIDATE_SLOT=a
ROLLBACK_REQUIRES_MCP_QUARANTINE=0
CANDIDATE_READY_FOR_FALLBACK=0
NGINX_CONFIG_TOUCHED=0
CURRENT={shlex.quote(str(tmp_path / "current"))}
OLD_PROJECT=astra-app-b
OLD_PORT=3009
CANDIDATE_PROJECT=astra-app-a
COMPOSE_FILE=docker-compose.prod.yml
write_cutover_state() {{ :; }}
approval_schema_forward_state() {{ printf '0'; }}
preserve_forward_only_maintenance() {{ echo unexpected_forward_only; return 1; }}
quarantine_mcp_for_unsafe_release() {{ echo unexpected_mcp_quarantine; return 1; }}
ensure_old_application_ready() {{ :; }}
write_atomic_symlink() {{ :; }}
wait_for_public_release() {{ :; }}
activate_worker_release() {{ :; }}
commit_active_state() {{ :; }}
cancel_pending_drain_for_active_release() {{ :; }}
compose_project() {{ :; }}
{rollback}
rollback
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected_mcp_quarantine" not in result.stdout
    assert "unexpected_forward_only" not in result.stdout
    assert 'rollback_legacy "$PREVIOUS_COMMIT"' in rollback


def test_forward_only_rollback_preserves_maintenance_and_never_starts_old_code(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    harness = f"""set +e
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / "missing-smoke"))}
PREVIOUS={shlex.quote(str(tmp_path / "previous"))}
PREVIOUS_RELEASE_ID=release-old
RELEASE={shlex.quote(str(tmp_path / "candidate"))}
RELEASE_ID=release-new
ACTIVE_SLOT=b
CANDIDATE_SLOT=a
ROLLBACK_REQUIRES_MCP_QUARANTINE=1
NGINX_CONFIG_TOUCHED=1
write_cutover_state() {{ :; }}
approval_schema_forward_state() {{ printf '1'; }}
preserve_forward_only_maintenance() {{ echo maintenance_preserved; }}
ensure_old_application_ready() {{ echo unexpected_old_app; }}
restore_previous_nginx() {{ echo unexpected_old_nginx; }}
activate_worker_release() {{ echo unexpected_old_worker; }}
quarantine_mcp_for_unsafe_release() {{ echo unexpected_quarantine; }}
{rollback}
rollback
status=$?
echo status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
    assert "maintenance_preserved" in result.stdout
    assert "unexpected_" not in result.stdout


def test_production_deploy_health_checks_candidate_before_nginx_cutover():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    candidate_identity = script.index(
        'if ! wait_for_local_release \\\n    "$CANDIDATE_PORT" "$VERSION" "$COMMIT" "$RELEASE_ID" 60'
    )
    nginx_reload = script.index("reload_nginx_with_worker_snapshot", candidate_identity)
    assert candidate_identity < nginx_reload
    assert "ACTIVE_SLOT_FILE" in script
    assert "DRAIN_TIMEOUT_SECONDS" in script
    assert "(cd backend && uv run --frozen --extra dev pytest -q)" in script
    assert "JWT_ROTATION_MARKER" in script
    assert "SSO_PASSWORD_ROTATION_MARKER" in script
    assert 'write_atomic_line "$SSO_PASSWORD_ROTATION_MARKER" "$RELEASE_ID"' in script
    assert 'install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT"' in script
    assert '"$RELEASE/scripts/configure_production_nginx.py"' in script
    configurator = NGINX_CONFIGURATOR.read_text(encoding="utf-8")
    assert "astra_no_args" in configurator
    assert "if len(old_matches) == 1 and not candidate_matches:" in configurator
    assert "audit_effective_config" in configurator
    assert "active_upstream_port" in configurator
    assert "CLIENT_IP_PROXY_HEADERS" in configurator
    assert "X-Real-IP" in configurator
    assert "X-Forwarded-For" in configurator
    assert "run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null" in script
    alembic_upgrade = script.index(
        "run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null"
    )
    checkpoint_setup = script.index("-m app.scripts.setup_langgraph_checkpoints", alembic_upgrade)
    candidate_start = script.index(
        'compose_project "$CANDIDATE_PROJECT" "$RELEASE/.env" "$RELEASE/$COMPOSE_FILE" up -d --no-deps backend',
        checkpoint_setup,
    )
    assert alembic_upgrade < checkpoint_setup < candidate_start


def test_douyin_ui_cannot_bypass_immutable_approval_preview():
    source = (ROOT / "frontend/src/pages/agent-detail/tabs/DouyinTab.tsx").read_text(encoding="utf-8")

    assert "/approve" not in source
    assert "/run" not in source
    assert "settings#approvals" in source
    assert "查看完整参数后审批" in source


def test_nginx_cutover_redacts_query_strings_in_every_server_block():
    configurator = _load_nginx_configurator()
    original = """server {
    listen 80;
    access_log /var/log/nginx/access.log combined;
}

server {
    listen 443 ssl;
    location / {
        access_log /tmp/raw-request.log main;
        proxy_pass http://127.0.0.1:3008;
    }
}
"""

    configured, server_count = configurator.configure_site(original, "3008", "3009")

    assert server_count == 2
    assert configured.count(configurator.REDACTED_ACCESS_LOG) == 2
    assert "proxy_pass http://127.0.0.1:3009;" in configured
    assert "proxy_pass http://127.0.0.1:3008;" not in configured
    assert "combined" not in configured
    assert "/tmp/raw-request.log" not in configured
    assert configured.count("proxy_set_header X-Real-IP $remote_addr;") == 1
    assert configured.count("proxy_set_header X-Forwarded-For $remote_addr;") == 1


def test_nginx_cutover_overwrites_spoofable_client_ip_headers_in_proxy_scope():
    configurator = _load_nginx_configurator()
    original = """server {
    listen 443 ssl;
    location / {
        proxy_set_header X-Real-IP $http_x_real_ip;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:3008;
    }
}
"""

    configured, _ = configurator.configure_site(original, "3008", "3009")

    assert "$http_x_real_ip" not in configured
    assert "$proxy_add_x_forwarded_for" not in configured
    assert configured.count("proxy_set_header X-Real-IP $remote_addr;") == 1
    assert configured.count("proxy_set_header X-Forwarded-For $remote_addr;") == 1


def test_inner_nginx_preserves_host_verified_client_ip_with_peer_fallback():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    source = (ROOT / "frontend/nginx.conf.template").read_text(encoding="utf-8")

    assert "context: ./frontend" in compose
    assert (
        "COPY nginx.conf.template /etc/nginx/templates/default.conf.template"
        in dockerfile
    )
    assert "map $http_x_real_ip $astra_client_ip" in source
    assert '"" $remote_addr;' in source
    assert source.count("proxy_set_header X-Real-IP $astra_client_ip;") >= 4
    assert source.count("proxy_set_header X-Forwarded-For $astra_client_ip;") >= 4


def test_backend_image_uses_resilient_configurable_debian_package_source():
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    compose_files = (
        ROOT / "docker-compose.yml",
        ROOT / "deploy/docker-compose.yml",
        ROOT / "deploy/docker-compose-multi.yml",
        ROOT / "deploy/astra-poc/docker-compose.prod.yml",
    )

    assert dockerfile.count("ARG CLAWITH_APT_MIRROR=deb.debian.org") == 2
    assert "mirrors.ustc.edu.cn" not in dockerfile
    assert dockerfile.count("invalid CLAWITH_APT_MIRROR host") == 2
    assert dockerfile.count("0 < len(host) <= 253") == 2
    assert dockerfile.count("label_pattern.fullmatch(label)") == 2
    for compose_file in compose_files:
        compose = compose_file.read_text(encoding="utf-8")
        assert "CLAWITH_APT_MIRROR: ${CLAWITH_APT_MIRROR:-deb.debian.org}" in compose
    for env_example in (ROOT / ".env.example", ROOT / "deploy/.env.example"):
        assert "# CLAWITH_APT_MIRROR=mirrors.aliyun.com" in env_example.read_text(
            encoding="utf-8"
        )
    for readme in (ROOT / "README.md", ROOT / "README_zh-CN.md"):
        documentation = readme.read_text(encoding="utf-8")
        assert "export CLAWITH_APT_MIRROR=mirrors.aliyun.com" in documentation
        assert "RUN sed -i 's|deb.debian.org" not in documentation


def test_backend_production_image_installs_bounded_cjk_fonts_and_refreshes_cache():
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")

    for package in (
        "fontconfig",
        "fonts-wqy-microhei",
        "fonts-wqy-zenhei",
        "fonts-noto-cjk",
        "fonts-noto-cjk-extra",
    ):
        assert package in dockerfile
    for oversized_or_unused_package in (
        "fonts-noto-extra",
        "fonts-noto-color-emoji",
        "fonts-freefont-ttf",
    ):
        assert oversized_or_unused_package not in dockerfile
    assert "fc-cache -f" in dockerfile


def test_nginx_cutover_is_idempotent_and_can_switch_back():
    configurator = _load_nginx_configurator()
    original = """server {
    listen 443 ssl;
    location / {
        proxy_pass http://127.0.0.1:3008;
    }
}
"""

    first, _ = configurator.configure_site(original, "3008", "3009")
    retry, _ = configurator.configure_site(first, "3008", "3009")
    switched_back, _ = configurator.configure_site(retry, "3009", "3008")

    assert retry == first
    assert "proxy_pass http://127.0.0.1:3008;" in switched_back
    assert switched_back.count(configurator.REDACTED_ACCESS_LOG) == 1


def test_nginx_maintenance_is_idempotent_and_removed_only_by_cutover():
    configurator = _load_nginx_configurator()
    original = """server {
    listen 80;
}
server {
    listen 443 ssl;
    location / { proxy_pass http://127.0.0.1:3008; }
}
"""

    first, count = configurator.configure_maintenance(original)
    second, retry_count = configurator.configure_maintenance(first)

    assert count == retry_count == 2
    assert second == first
    assert configurator.maintenance_enabled(first)
    assert first.count("return 503;") == 2
    assert first.count('add_header Retry-After "60" always;') == 2
    assert first.count('add_header Cache-Control "no-store" always;') == 2
    assert configurator.active_upstream_port(first) == "3008"

    cutover, _ = configurator.configure_site(first, "3008", "3009")
    assert not configurator.maintenance_enabled(cutover)
    assert configurator.MAINTENANCE_BEGIN not in cutover
    assert "return 503;" not in cutover
    assert configurator.active_upstream_port(cutover) == "3009"


def test_nginx_maintenance_rejects_partial_owned_markers():
    configurator = _load_nginx_configurator()
    partial = f"""server {{
    {configurator.MAINTENANCE_BEGIN}
    return 503;
    location / {{ proxy_pass http://127.0.0.1:3008; }}
}}
"""

    with pytest.raises(ValueError, match="unterminated"):
        configurator.configure_maintenance(partial)


def test_nginx_cutover_ignores_commented_upstream_examples():
    configurator = _load_nginx_configurator()
    original = """# proxy_pass http://127.0.0.1:3009;
server {
    # proxy_pass http://127.0.0.1:3008;
    location / {
        proxy_pass http://127.0.0.1:3008;
    }
}
"""

    configured, _ = configurator.configure_site(original, "3008", "3009")
    retried, _ = configurator.configure_site(configured, "3008", "3009")

    assert retried == configured
    assert configured.count("proxy_pass http://127.0.0.1:3009;") == 2


def test_nginx_cutover_rewrites_inline_access_log_override():
    configurator = _load_nginx_configurator()
    original = """server {
    location / { access_log /tmp/unsafe.log combined; proxy_pass http://127.0.0.1:3008; }
}
"""

    configured, server_count = configurator.configure_site(original, "3008", "3009")

    assert server_count == 1
    assert "/tmp/unsafe.log" not in configured
    assert configured.count(configurator.REDACTED_ACCESS_LOG) == 1


def test_nginx_cutover_handles_comments_split_braces_and_inline_blocks():
    configurator = _load_nginx_configurator()
    original = """server { # HTTP redirect
    listen 80;
}
server
{
    listen 443 ssl;
    location / { proxy_pass http://127.0.0.1:3008; }
}
"""

    configured, server_count = configurator.configure_site(original, "3008", "3009")

    assert server_count == 2
    assert configured.count(configurator.REDACTED_ACCESS_LOG) == 2
    assert configurator.active_upstream_port(configured) == "3009"


def test_nginx_cutover_accepts_production_map_entries():
    configurator = _load_nginx_configurator()
    original = """map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
server {
    listen 80;
}
server {
    listen 443 ssl;
    location / { proxy_pass http://127.0.0.1:3009; }
}
"""

    assert configurator.active_upstream_port(original) == "3009"
    configured, server_count = configurator.configure_site(original, "3008", "3009")
    assert server_count == 2
    assert configured.count(configurator.REDACTED_ACCESS_LOG) == 2
    assert "    '' close;" in configured

    effective = f"""http {{
{configurator.REDACTED_LOG_FORMAT.strip()}
map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
}}
server {{ {configurator.REDACTED_ACCESS_LOG} listen 80; }}
    server {{
        {configurator.REDACTED_ACCESS_LOG}
        location / {{
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_pass http://127.0.0.1:3009;
        }}
    }}
}}
"""
    assert configurator.audit_effective_config(effective) == (2, 2)


def test_nginx_cutover_treats_plain_directive_shaped_map_keys_as_data():
    configurator = _load_nginx_configurator()
    original = """map $request_method $mapped_log_value {
    access_log mapped_access_log;
    proxy_pass http://127.0.0.1:3009;
    log_format mapped_log_format;
}
server {
    access_log /tmp/unsafe.log combined;
    location / { proxy_pass http://127.0.0.1:3008; }
}
"""

    assert configurator.active_upstream_port(original) == "3008"
    configured, server_count = configurator.configure_site(original, "3008", "3009")

    assert server_count == 1
    assert "access_log mapped_access_log;" in configured
    assert "proxy_pass http://127.0.0.1:3009;" in configured
    assert "log_format mapped_log_format;" in configured
    assert "/tmp/unsafe.log" not in configured
    assert configurator.active_upstream_port(configured) == "3009"


@pytest.mark.parametrize("map_include", ["include", '"include"', r"incl\ude"])
def test_nginx_cutover_rejects_quoted_or_escaped_map_include(map_include):
    configurator = _load_nginx_configurator()
    original = f"""map $http_upgrade $connection_upgrade {{
    {map_include} /etc/nginx/map-values.conf;
}}
server {{ location / {{ proxy_pass http://127.0.0.1:3008; }} }}
"""

    with pytest.raises(ValueError, match="map include"):
        configurator.configure_site(original, "3008", "3009")


@pytest.mark.parametrize(
    "nested_map",
    [
        "map $x $y { access_log /tmp/raw.log; }",
        'map $x $y { "access_log" /tmp/raw.log; }',
        'location /nested { map $x $y { "proxy_pass" http://127.0.0.1:3009; } }',
    ],
)
def test_nginx_cutover_rejects_map_entries_in_request_contexts(nested_map):
    configurator = _load_nginx_configurator()
    original = f"""server {{
    {nested_map}
    location / {{ proxy_pass http://127.0.0.1:3008; }}
}}
"""

    with pytest.raises(ValueError, match="map entry has an invalid context"):
        configurator.configure_site(original, "3008", "3009")


def test_nginx_cutover_rejects_server_scoped_include():
    configurator = _load_nginx_configurator()
    original = """server {
    include /etc/nginx/snippets/*.conf;
    location / { proxy_pass http://127.0.0.1:3008; }
}
"""

    with pytest.raises(ValueError, match="include"):
        configurator.configure_site(original, "3008", "3009")


def test_nginx_cutover_rejects_top_level_include():
    configurator = _load_nginx_configurator()
    original = """include /etc/nginx/sites-extra/*.conf;
server {
    location / { proxy_pass http://127.0.0.1:3008; }
}
"""

    with pytest.raises(ValueError, match="include"):
        configurator.configure_site(original, "3008", "3009")


@pytest.mark.parametrize(
    "directive",
    ['"include"', r"incl\ude", 'inc"lu"de', "acc'ess_log"],
)
def test_nginx_cutover_rejects_quoted_or_escaped_directive_names(directive):
    configurator = _load_nginx_configurator()
    original = f"""{directive} /etc/nginx/sites-extra/*.conf;
server {{
    location / {{ proxy_pass http://127.0.0.1:3008; }}
}}
"""

    with pytest.raises(ValueError, match="quoted or escaped"):
        configurator.configure_site(original, "3008", "3009")


def test_nginx_effective_audit_rejects_quoted_access_log_directive():
    configurator = _load_nginx_configurator()
    effective = f"""http {{
    {configurator.REDACTED_LOG_FORMAT.strip()}
    server {{
        {configurator.REDACTED_ACCESS_LOG}
        location / {{ "access_log" /tmp/raw.log combined; }}
    }}
}}
"""

    with pytest.raises(ValueError, match="quoted or escaped"):
        configurator.audit_effective_config(effective)


@pytest.mark.parametrize(
    "upstreams",
    [
        "proxy_pass http://127.0.0.1:3010;",
        "\n".join(
            [
                "proxy_pass http://127.0.0.1:3008;",
                "proxy_pass http://127.0.0.1:3008;",
            ]
        ),
        "\n".join(
            [
                "proxy_pass http://127.0.0.1:3008;",
                "proxy_pass http://127.0.0.1:3009;",
            ]
        ),
    ],
)
def test_nginx_cutover_rejects_ambiguous_upstreams(upstreams):
    configurator = _load_nginx_configurator()
    original = f"server {{\n    location / {{\n{upstreams}\n    }}\n}}\n"

    with pytest.raises(ValueError, match="exactly one old or already-installed"):
        configurator.configure_site(original, "3008", "3009")


def test_nginx_access_log_format_contains_only_safe_operational_fields():
    configurator = _load_nginx_configurator()
    variables = set(re.findall(r"\$[a-z0-9_]+", configurator.REDACTED_LOG_FORMAT))

    assert variables == {
        "$body_bytes_sent",
        "$request_id",
        "$request_time",
        "$status",
        "$time_iso8601",
        "$upstream_response_time",
        "$upstream_status",
    }
    assert not variables & {
        "$args",
        "$http_referer",
        "$http_user_agent",
        "$remote_addr",
        "$request",
        "$request_method",
        "$uri",
    }


def test_nginx_effective_config_audit_covers_all_server_blocks():
    configurator = _load_nginx_configurator()
    effective = f"""http {{
    {configurator.REDACTED_LOG_FORMAT.strip()}
    server {{
        {configurator.REDACTED_ACCESS_LOG}
        listen 80;
    }}
    server {{ # TLS
        {configurator.REDACTED_ACCESS_LOG}
        listen 443 ssl;
        location / {{
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_pass http://127.0.0.1:3009;
        }}
    }}
}}
"""

    assert configurator.audit_effective_config(effective) == (2, 2)


def test_nginx_effective_config_audit_rejects_any_unsafe_override():
    configurator = _load_nginx_configurator()
    effective = f"""http {{
    {configurator.REDACTED_LOG_FORMAT.strip()}
    server {{
        {configurator.REDACTED_ACCESS_LOG}
        location / {{ access_log /tmp/raw.log combined; }}
    }}
}}
"""

    with pytest.raises(ValueError, match="unsafe"):
        configurator.audit_effective_config(effective)


def test_nginx_effective_config_audit_rejects_braced_log_variable():
    configurator = _load_nginx_configurator()
    unsafe_format = configurator.REDACTED_LOG_FORMAT.replace(
        "time=$time_iso8601",
        "path=${uri} time=$time_iso8601",
    )
    effective = f"""http {{
    {unsafe_format.strip()}
    server {{
        {configurator.REDACTED_ACCESS_LOG}
    }}
}}
"""

    with pytest.raises(ValueError, match="braced"):
        configurator.audit_effective_config(effective)


def test_nginx_effective_config_audit_scopes_to_clawith_site():
    configurator = _load_nginx_configurator()
    target = "/etc/nginx/sites-enabled/astra-poc.conf"
    effective = f"""# configuration file /etc/nginx/conf.d/00-astra-log-redaction.conf:
{configurator.REDACTED_LOG_FORMAT.strip()}
# configuration file /etc/nginx/sites-enabled/unrelated.conf:
server {{ listen 8080; }}
# configuration file {target}:
server {{
    {configurator.REDACTED_ACCESS_LOG}
    listen 80;
}}
server {{
    {configurator.REDACTED_ACCESS_LOG}
    listen 443 ssl;
    location / {{
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_pass http://127.0.0.1:3009;
    }}
}}
"""

    assert configurator.audit_effective_config(effective, target) == (2, 2)


def test_nginx_effective_config_audit_rejects_target_top_level_include():
    configurator = _load_nginx_configurator()
    target = "/etc/nginx/sites-enabled/astra-poc.conf"
    effective = f"""# configuration file /etc/nginx/conf.d/00-astra-log-redaction.conf:
{configurator.REDACTED_LOG_FORMAT.strip()}
# configuration file {target}:
include /etc/nginx/sites-extra/*.conf;
server {{
    {configurator.REDACTED_ACCESS_LOG}
    location / {{ proxy_pass http://127.0.0.1:3009; }}
}}
# configuration file /etc/nginx/sites-extra/unsafe.conf:
server {{ access_log /tmp/raw.log combined; }}
"""

    with pytest.raises(ValueError, match="include"):
        configurator.audit_effective_config(effective, target)


def test_nginx_atomic_write_preserves_symlink(tmp_path):
    configurator = _load_nginx_configurator()
    target = tmp_path / "site.available"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    link = tmp_path / "site.enabled"
    link.symlink_to(target)

    configurator._atomic_write(link, "after")

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_nginx_atomic_write_fsyncs_content_and_parent_directory(monkeypatch, tmp_path):
    configurator = _load_nginx_configurator()
    target = tmp_path / "nginx.conf"
    target.write_text("before", encoding="utf-8")
    fsync_calls = []

    monkeypatch.setattr(
        configurator.os,
        "fsync",
        lambda file_descriptor: fsync_calls.append(file_descriptor),
    )

    configurator._atomic_write(target, "after")

    assert target.read_text(encoding="utf-8") == "after"
    assert len(fsync_calls) == 2


def test_nginx_atomic_write_failure_keeps_original(monkeypatch, tmp_path):
    configurator = _load_nginx_configurator()
    target = tmp_path / "nginx.conf"
    target.write_text("original", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(configurator.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        configurator._atomic_write(target, "candidate")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_nginx_install_site_write_failure_keeps_old_site_and_safe_format(
    monkeypatch,
    tmp_path,
):
    configurator = _load_nginx_configurator()
    site = tmp_path / "site.conf"
    site.write_text(
        "server { location / { proxy_pass http://127.0.0.1:3008; } }\n",
        encoding="utf-8",
    )
    log_format = tmp_path / "log-format.conf"
    log_format.write_text("original format\n", encoding="utf-8")
    real_atomic_write = configurator._atomic_write
    calls = 0

    def fail_second_write(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second install write failed")
        real_atomic_write(path, content)

    monkeypatch.setattr(configurator, "_atomic_write", fail_second_write)
    with pytest.raises(OSError, match="second install write failed"):
        configurator.install_configuration(site, "3008", "3009", log_format)

    assert "proxy_pass http://127.0.0.1:3008;" in site.read_text(encoding="utf-8")
    assert log_format.read_text(encoding="utf-8") == configurator.REDACTED_LOG_FORMAT


def test_production_deploy_rollback_keeps_privacy_safe_nginx_format():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    restore_start = script.index("restore_previous_nginx() {")
    restore_end = script.index("recover_candidate_traffic() {", restore_start)
    restore = script[restore_start:restore_end]
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    assert "NGINX_CONFIG_TOUCHED=0" in script[:rollback_start]
    assert 'install "$NGINX_SITE" "$CANDIDATE_PORT" "$OLD_PORT"' in restore
    assert '"$NGINX_LOG_FORMAT"' in restore
    assert " restore " not in restore
    assert "reload_nginx_with_worker_snapshot" in restore
    assert "ensure_old_application_ready" in rollback
    assert "restore_previous_nginx" in rollback
    assert "candidate remains running" in rollback

    site_backup = script.index('sudo cp "$NGINX_SITE" "$NGINX_BACKUP"')
    format_backup = script.index('sudo cp "$NGINX_LOG_FORMAT" "$NGINX_LOG_FORMAT_BACKUP"')
    maintenance_call = script.index(
        "if ! enable_web_maintenance",
        format_backup,
    )
    assert site_backup < format_backup < maintenance_call
    maintenance_helper = _shell_function_source(
        script,
        "enable_web_maintenance",
        "preserve_forward_only_maintenance",
    )
    assert "maintenance-on" in maintenance_helper
    assert "NGINX_CONFIG_TOUCHED=1" in maintenance_helper
    assert "sudo nginx -t" in maintenance_helper
    assert "audit_effective_nginx" in maintenance_helper
    assert "reload_nginx_with_worker_snapshot" in maintenance_helper


def test_production_deploy_error_trap_is_terminal_inside_helpers():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    remote_script = script.split("<<'REMOTE_SCRIPT'\n", 1)[1]
    assert remote_script.startswith("{ set +x; } 2>/dev/null\nset -Eeuo pipefail\n")
    handler_start = script.index("on_error() {")
    trap_line = "trap 'on_error $?' ERR"
    handler_end = script.index(trap_line, handler_start) + len(trap_line)
    handler = script[handler_start:handler_end]
    harness = f"""set -Eeuo pipefail
rollback() {{ echo rollback_called; return 0; }}
{handler}
compose_helper() {{
    false
    echo continued_inside_helper
}}
compose_helper
echo continued_after_failure
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "rollback_called" in result.stdout
    assert "continued_inside_helper" not in result.stdout
    assert "continued_after_failure" not in result.stdout


def test_production_deploy_signal_trap_is_terminal_and_rolls_back():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    handler_start = script.index("on_signal() {")
    trap_line = "trap 'on_signal TERM 143' TERM"
    handler_end = script.index(trap_line, handler_start) + len(trap_line)
    handler = script[handler_start:handler_end]
    harness = f"""set -e
rollback() {{ echo rollback_called; return 0; }}
{handler}
kill -TERM $$
echo continued_after_signal
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 143
    assert "rollback_called" in result.stdout
    assert "continued_after_signal" not in result.stdout


def test_production_deploy_rollback_refuses_mutation_without_durable_intent(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    smoke = tmp_path / "missing.smoke"
    previous = tmp_path / "previous-release"
    previous.mkdir()
    release = tmp_path / "candidate-release"
    release.mkdir()
    harness = f"""set +e
SMOKE_ENV_FILE={shlex.quote(str(smoke))}
PREVIOUS={shlex.quote(str(previous))}
RELEASE={shlex.quote(str(release))}
RELEASE_ID=candidate-release
ACTIVE_SLOT=b
PREVIOUS_RELEASE_ID=previous-release
CANDIDATE_SLOT=a
NGINX_CONFIG_TOUCHED=1
CURRENT={shlex.quote(str(tmp_path / "current"))}
ACTIVE_SLOT_FILE={shlex.quote(str(tmp_path / "active-slot"))}
ACTIVE_RELEASE_FILE={shlex.quote(str(tmp_path / "active-release"))}
OLD_WORKER_STOP_REQUESTED=0
COMPOSE_PROJECT=astra
CANDIDATE_PROJECT=astra-app-a
OLD_PROJECT=astra-app-b
write_cutover_state() {{ echo state_write_failed; return 1; }}
write_atomic_line() {{ :; }}
ensure_old_application_ready() {{ echo unexpected_old_start; return 0; }}
restore_previous_nginx() {{ echo unexpected_nginx_mutation; return 0; }}
recover_candidate_traffic() {{ echo unexpected_candidate_recovery; return 0; }}
wait_for_public_release() {{ echo unexpected_public_check; return 0; }}
compose_project() {{ echo unexpected_compose; return 0; }}
cleanup_browser_smoke_runtime() {{ :; }}
{rollback}
rollback
status=$?
echo rollback_status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "rollback_status=1" in result.stdout
    assert "state_write_failed" in result.stdout
    assert "unexpected_" not in result.stdout


def test_production_deploy_explicit_failures_invoke_rollback_before_exit():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    abort_start = script.index("abort_release() {")
    abort_end = script.index("on_error() {", abort_start)
    abort_function = script[abort_start:abort_end]

    assert "rollback" in abort_function
    assert "trap - ERR" in abort_function
    assert "exit 1" in abort_function
    assert ('abort_release "public cutover did not expose expected release $VERSION/$COMMIT"') in script
    assert ('abort_release "browser smoke image did not pass its pre-mutation launch self-test"') in script
    assert 'abort_release "authenticated candidate API/browser smoke failed"' in script


def test_production_deploy_starts_candidate_worker_before_unfreezing_public_traffic():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    verifier_start = script.index("release_payloads_match() {")
    verifier_end = script.index("audit_effective_nginx() {", verifier_start)
    verifier = script[verifier_start:verifier_end]
    old_stop = script.index(
        'compose_project "$OLD_PROJECT" "$PREVIOUS/.env" "$PREVIOUS/$COMPOSE_FILE" \\\n'
        "    stop --timeout 90 worker frontend backend"
    )
    migration = script.index("backend upgrade head", old_stop)
    worker_handoff = script.index(
        'if ! activate_worker_release \\\n    "$CANDIDATE_PROJECT"',
        migration,
    )
    public_gate = script.index('echo "[remote] verifying public cutover identity"')

    assert old_stop < migration < worker_handoff < public_gate
    assert "'Cache-Control: no-cache'" in verifier
    assert 'health.get("version") != expected_version' in verifier
    assert 'version.get("commit") != expected_commit' in verifier
    assert 'wait_for_public_release "$VERSION" "$COMMIT"' in script[public_gate:]
    assert 'abort_release "public cutover did not expose expected release' in script[public_gate:]


def test_single_worker_assertion_compares_full_container_ids(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    worker_helpers = _shell_function_source(
        script,
        "managed_worker_ids",
        "remaining_old_nginx_workers",
    )
    full_id = "a" * 64
    short_id = full_id[:12]
    call_log = tmp_path / "docker-calls.log"
    harness = f"""set +e
COMPOSE_PROJECT=astra-poc
CALL_LOG={shlex.quote(str(call_log))}
compose_project() {{
    printf '%s\n' {full_id}
}}
wait_for_worker_release() {{
    return 0
}}
docker() {{
    if [ "$1" = ps ]; then
        printf '%s\n' "$*" >> "$CALL_LOG"
        case "$*" in
            *com.docker.compose.project=astra-poc-app-a*)
                case " $* " in
                    *" --no-trunc "*) printf '%s\n' {full_id} ;;
                    *) printf '%s\n' {short_id} ;;
                esac
                ;;
        esac
        return 0
    fi
    if [ "$1" = inspect ]; then
        printf '%s\n' astra-poc-app-a
        return 0
    fi
    return 1
}}
{worker_helpers}
assert_single_active_worker astra-poc-app-a env compose release-a
status=$?
echo "status=$status"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "status=0" in result.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3
    assert all("ps --no-trunc -q" in call for call in calls)


@pytest.mark.parametrize("runtime_slot", ["b", "a"], ids=["pre_reload", "post_reload"])
def test_production_deploy_recovers_interrupted_cutover_before_cleanup(
    tmp_path,
    runtime_slot,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)

    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "candidate-release")
    fallback_release = _write_test_release(
        app_root,
        "previous-release",
        version="1.10.11",
        commit="old1234",
    )
    (app_root / "slot-a-release").write_text(
        f"{target_release}\n",
        encoding="utf-8",
    )
    (app_root / "slot-b-release").write_text(
        f"{fallback_release}\n",
        encoding="utf-8",
    )
    active_slot = app_root / "active-slot"
    active_slot.write_text("b\n", encoding="utf-8")
    active_release = app_root / "active-release"
    cutover_state = app_root / "cutover-state"
    current = app_root / "current"

    harness = f"""set -e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(app_root / "releases" / "incoming-release"))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(active_release))}
ACTIVE_STATE_FILE={shlex.quote(str(app_root / "active-state"))}
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
RECORDED_SLOT=b
DISK_SLOT=a
RUNTIME_SLOT={runtime_slot}
WORKER_SLOT=b
write_atomic_line() {{ printf '%s\n' "$2" > "$1"; echo "write $(basename "$1")=$2"; }}
write_cutover_state() {{ write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"; }}
write_atomic_symlink() {{ command ln -sfn "$2" "$1"; }}
commit_active_state() {{
    write_atomic_line "$ACTIVE_STATE_FILE" "slot=$1 release=$2"
    write_atomic_line "$ACTIVE_SLOT_FILE" "$1"
    write_atomic_line "$ACTIVE_RELEASE_FILE" "$2"
}}
wait_for_local_release() {{
    test "$1" = 3008
    test "$2" = 1.10.12
    test "$3" = abc1234
    echo local_identity_verified
}}
wait_for_public_release() {{
    test "$RUNTIME_SLOT" = a
    test "$1" = 1.10.12
    test "$2" = abc1234
    echo public_identity_verified
}}
audit_effective_nginx() {{ echo nginx_effective_audited; }}
reload_nginx_with_worker_snapshot() {{
    RUNTIME_SLOT=a
    echo "runtime_converged=$RUNTIME_SLOT"
}}
retire_pre_reload_nginx_workers() {{ echo nginx_workers_retired; }}
activate_worker_release() {{
    test "$1" = astra-app-a
    test "$2" = {shlex.quote(str(target_release / ".env"))}
    test "$4" = candidate-release
    echo old_worker_stopped
    WORKER_SLOT=a
    echo target_worker_started
}}
compose_project() {{
    local project="$1"
    local env_file="$2"
    if [ "$project" = astra-app-a ]; then
        test "$env_file" = {shlex.quote(str(target_release / ".env"))}
    elif [ "$project" = astra-app-b ]; then
        test "$env_file" = {shlex.quote(str(fallback_release / ".env"))}
    else
        return 1
    fi
    shift 3
    if [ "$1" = stop ] && [ "$2" = --timeout ] && [ "$4" = worker ]; then
        test "$project" = astra-app-b
        WORKER_SLOT=stopped
        echo fallback_application_stopped
        return 0
    fi
    if [ "$1" = run ] && [ "$project" = astra-app-a ]; then
        echo target_schema_migrated
        return 0
    fi
    if [ "$1" = up ] && [ "$2" = -d ] && [ "${{!#}}" = frontend ]; then
        test "$project" = astra-app-a
        echo target_web_started
        return 0
    fi
    if [ "$1" = up ] && [ "${{!#}}" = worker ]; then
        test "$project" = astra-app-a
        WORKER_SLOT=a
        echo target_worker_started
        return 0
    fi
    if [ "$1" = ps ] && [ "$2" = -q ] && [ "$3" = worker ]; then
        test "$project" = astra-app-a
        echo worker-a
        return 0
    fi
    return 1
}}
docker() {{
    test "$1" = inspect
    test "$4" = worker-a
    case "$3" in
        *State.Health*) echo healthy ;;
        *Config.Image*) echo astra-backend:candidate-release ;;
        *) return 1 ;;
    esac
}}
sudo() {{
    if [ "$1" = python3 ]; then
        case "$3" in
            maintenance-on)
                echo maintenance_enabled
                return 0
                ;;
            install)
                echo "normalized $5->$6"
                return 0
                ;;
        esac
    fi
    if [ "$1" = nginx ]; then
        test "$2" = -t
        echo nginx_tested
        return 0
    fi
    return 1
}}
{port_function}
{recovery_helpers}
{recovery_function}
recover_indeterminate_cutover b a 3008 candidate-release
echo "runtime_slot=$RUNTIME_SLOT"
echo "worker_slot=$WORKER_SLOT"
echo "recorded_slot=$RECORDED_SLOT"
echo "active_slot=$(tr -d '\n' < "$ACTIVE_SLOT_FILE")"
echo "active_release=$(tr -d '\n' < "$ACTIVE_RELEASE_FILE")"
echo "active_state=$(tr -d '\n' < "$ACTIVE_STATE_FILE")"
echo "current=$(readlink "$CURRENT")"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "local_identity_verified" in result.stdout
    assert "normalized 3009->3008" in result.stdout
    assert "runtime_converged=a" in result.stdout
    assert "public_identity_verified" in result.stdout
    assert "old_worker_stopped" in result.stdout
    assert "target_worker_started" in result.stdout
    assert "fallback_application_stopped" in result.stdout
    assert "target_schema_migrated" in result.stdout
    assert "target_web_started" in result.stdout
    assert "runtime_slot=a" in result.stdout
    assert "worker_slot=a" in result.stdout
    assert "recorded_slot=a" in result.stdout
    assert "active_slot=a" in result.stdout
    assert "active_release=candidate-release" in result.stdout
    assert "active_state=slot=a release=candidate-release" in result.stdout
    assert f"current={target_release}" in result.stdout


@pytest.mark.parametrize(
    ("cutover_phase", "initial_evidence", "expects_regeneration"),
    [
        ("maintenance_enabled", "0", True),
        ("migration_started", "0", True),
        ("schema_forward_only", "0", True),
        ("candidate_services_ready", "0", True),
        ("candidate_business_verified", "1", False),
    ],
)
def test_recovery_rebuilds_preverified_candidate_before_public_cutover(
    tmp_path,
    cutover_phase,
    initial_evidence,
    expects_regeneration,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)

    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "candidate-release")
    fallback_release = _write_test_release(
        app_root,
        "previous-release",
        version="1.10.11",
        commit="old1234",
    )
    (app_root / "slot-a-release").write_text(f"{target_release}\n", encoding="utf-8")
    (app_root / "slot-b-release").write_text(f"{fallback_release}\n", encoding="utf-8")

    harness = f"""set -e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(app_root / "releases" / "incoming-release"))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(app_root / "current"))}
CUTOVER_STATE_FILE={shlex.quote(str(app_root / "cutover-state"))}
CUTOVER_PHASE={cutover_phase}
RECORDED_SLOT=b
EVIDENCE={initial_evidence}
write_atomic_line() {{ printf '%s\n' "$2" > "$1"; }}
write_cutover_state() {{ write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"; }}
write_atomic_symlink() {{ command ln -sfn "$2" "$1"; }}
commit_active_state() {{ echo "active_committed:$1:$2"; }}
wait_for_local_release() {{ echo local_identity_verified; }}
wait_for_public_release() {{ test "$EVIDENCE" = 1; echo public_identity_verified; }}
audit_effective_nginx() {{ :; }}
reload_nginx_with_worker_snapshot() {{ :; }}
retire_pre_reload_nginx_workers() {{ :; }}
activate_worker_release() {{ echo candidate_worker_ready; }}
compose_project() {{
    local project="$1"
    shift 3
    case "$1" in
        stop) echo fallback_stopped ;;
        run) echo schema_converged ;;
        up) echo candidate_services_ready ;;
        *) return 1 ;;
    esac
}}
sudo() {{
    if [ "$1" = python3 ] && [ "$3" = install ]; then
        test "$EVIDENCE" = 1
        echo public_install_after_evidence
        return 0
    fi
    if [ "$1" = python3 ] || [ "$1" = nginx ]; then
        return 0
    fi
    return 1
}}
{port_function}
{recovery_helpers}
{recovery_function}
candidate_business_evidence_valid() {{ test "$EVIDENCE" = 1; }}
regenerate_candidate_business_evidence() {{
    echo authenticated_candidate_smoke
    EVIDENCE=1
}}
recover_indeterminate_cutover b a 3008 candidate-release
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    events = result.stdout
    if expects_regeneration:
        assert "authenticated_candidate_smoke" in events
        assert events.index("authenticated_candidate_smoke") < events.index("public_install_after_evidence")
    else:
        assert "authenticated_candidate_smoke" not in events
    assert (
        events.index("candidate_worker_ready")
        < events.index("public_install_after_evidence")
        < events.index("public_identity_verified")
    )


def test_verified_recovery_rejects_missing_evidence_before_public_cutover(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)
    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "candidate-release")
    fallback_release = _write_test_release(
        app_root,
        "previous-release",
        version="1.10.11",
        commit="old1234",
    )
    (app_root / "slot-a-release").write_text(f"{target_release}\n", encoding="utf-8")
    (app_root / "slot-b-release").write_text(f"{fallback_release}\n", encoding="utf-8")
    current = app_root / "current"

    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(app_root / "releases" / "incoming-release"))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
CUTOVER_STATE_FILE={shlex.quote(str(app_root / "cutover-state"))}
CUTOVER_PHASE=candidate_business_verified
RECORDED_SLOT=b
write_atomic_line() {{ printf '%s\n' "$2" > "$1"; }}
write_cutover_state() {{ write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"; }}
write_atomic_symlink() {{ command ln -sfn "$2" "$1"; }}
wait_for_local_release() {{ :; }}
wait_for_public_release() {{ echo unexpected_public_check; return 1; }}
audit_effective_nginx() {{ :; }}
reload_nginx_with_worker_snapshot() {{ :; }}
retire_pre_reload_nginx_workers() {{ :; }}
activate_worker_release() {{ :; }}
compose_project() {{ :; }}
sudo() {{
    if [ "$1" = python3 ] && [ "$3" = install ]; then
        echo unexpected_public_install
        return 1
    fi
    return 0
}}
{port_function}
{recovery_helpers}
{recovery_function}
candidate_business_evidence_valid() {{ return 1; }}
regenerate_candidate_business_evidence() {{ echo unexpected_regeneration; return 0; }}
recover_indeterminate_cutover b a 3008 candidate-release
status=$?
echo "status=$status"
test ! -e "$CURRENT"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "status=1" in result.stdout
    assert "unexpected_regeneration" not in result.stdout
    assert "unexpected_public_install" not in result.stdout
    assert "unexpected_public_check" not in result.stdout


def test_production_deploy_failed_recovery_preserves_both_slots(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)

    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "candidate-release")
    fallback_release = _write_test_release(
        app_root,
        "previous-release",
        version="1.10.11",
        commit="old1234",
    )
    (app_root / "slot-a-release").write_text(
        f"{target_release}\n",
        encoding="utf-8",
    )
    (app_root / "slot-b-release").write_text(
        f"{fallback_release}\n",
        encoding="utf-8",
    )
    active_slot = app_root / "active-slot"
    active_slot.write_text("b\n", encoding="utf-8")

    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(app_root / "releases" / "incoming-release"))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(app_root / "current"))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(app_root / "active-release"))}
CUTOVER_STATE_FILE={shlex.quote(str(app_root / "cutover-state"))}
RECORDED_SLOT=b
write_atomic_line() {{ printf '%s\n' "$2" > "$1"; }}
write_cutover_state() {{ write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"; }}
wait_for_local_release() {{ echo target_identity_failed; return 1; }}
wait_for_public_release() {{ echo unexpected_public_check; return 1; }}
audit_effective_nginx() {{ echo maintenance_audited; return 0; }}
reload_nginx_with_worker_snapshot() {{ echo maintenance_reloaded; return 0; }}
retire_pre_reload_nginx_workers() {{ echo maintenance_workers_retired; return 0; }}
compose_project() {{
    local project="$1"
    shift 3
    if [ "$project" = astra-app-b ] && [ "$1" = stop ]; then
        echo fallback_application_stopped
        return 0
    fi
    if [ "$project" = astra-app-a ] && [ "$1" = run ]; then
        echo target_schema_migrated
        return 0
    fi
    if [ "$project" = astra-app-a ] && [ "$1" = up ]; then
        echo target_web_started
        return 0
    fi
    echo unexpected_compose
    return 1
}}
docker() {{ echo unexpected_docker; return 1; }}
sudo() {{
    if [ "$1" = python3 ] || [ "$1" = nginx ]; then
        echo maintenance_configured
        return 0
    fi
    echo unexpected_mutation
    return 1
}}
{port_function}
{recovery_helpers}
{recovery_function}
recover_indeterminate_cutover b a 3008 candidate-release
status=$?
echo "status=$status"
echo "active_slot=$(tr -d '\n' < "$ACTIVE_SLOT_FILE")"
test ! -e "$CURRENT"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "target_identity_failed" in result.stdout
    assert "fallback_application_stopped" in result.stdout
    assert "target_schema_migrated" in result.stdout
    assert "target_web_started" in result.stdout
    assert "status=1" in result.stdout
    assert "active_slot=b" in result.stdout
    assert "unexpected_public_check" not in result.stdout
    assert "unexpected_compose" not in result.stdout
    assert "unexpected_docker" not in result.stdout
    assert "unexpected_mutation" not in result.stdout


def test_recovery_fails_closed_without_committing_an_unhealthy_target_worker(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)

    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "candidate-release")
    fallback_release = _write_test_release(
        app_root,
        "previous-release",
        version="1.10.11",
        commit="old1234",
    )
    (app_root / "slot-a-release").write_text(
        f"{target_release}\n",
        encoding="utf-8",
    )
    (app_root / "slot-b-release").write_text(
        f"{fallback_release}\n",
        encoding="utf-8",
    )
    active_slot = app_root / "active-slot"
    active_slot.write_text("b\n", encoding="utf-8")
    cutover_state = app_root / "cutover-state"
    current = app_root / "current"
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(app_root / "releases" / "incoming-release"))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(app_root / "active-release"))}
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
RECORDED_SLOT=b
WORKER_SLOT=b
write_atomic_line() {{ printf '%s\n' "$2" > "$1"; }}
write_cutover_state() {{ write_atomic_line "$CUTOVER_STATE_FILE" "$1 slot=$2 release=$3"; }}
write_atomic_symlink() {{ command ln -sfn "$2" "$1"; }}
commit_active_state() {{ echo unexpected_active_commit; return 1; }}
sleep() {{ :; }}
wait_for_local_release() {{ echo local_identity_verified; return 0; }}
wait_for_public_release() {{ echo public_identity_verified; return 0; }}
audit_effective_nginx() {{ return 0; }}
reload_nginx_with_worker_snapshot() {{ echo nginx_reloaded; return 0; }}
retire_pre_reload_nginx_workers() {{ echo nginx_converged; return 0; }}
activate_worker_release() {{
    test "$1" = astra-app-a
    WORKER_SLOT=stopped
    echo target_worker_started
    echo target_worker_unhealthy
    return 1
}}
compose_project() {{
    local project="$1"
    local env_file="$2"
    if [ "$project" = astra-app-a ]; then
        test "$env_file" = {shlex.quote(str(target_release / ".env"))} || return 1
    elif [ "$project" = astra-app-b ]; then
        test "$env_file" = {shlex.quote(str(fallback_release / ".env"))} || return 1
    else
        return 1
    fi
    shift 3
    if [ "$1" = stop ] && [ "$2" = --timeout ] && [ "$4" = worker ]; then
        if [ "$project" = astra-app-b ]; then
            echo fallback_stopped
        else
            echo target_partial_stopped
        fi
        WORKER_SLOT=stopped
        return 0
    fi
    if [ "$1" = run ] && [ "$project" = astra-app-a ]; then
        echo target_schema_migrated
        return 0
    fi
    if [ "$1" = up ] && [ "$2" = -d ] && [ "${{!#}}" = frontend ]; then
        test "$project" = astra-app-a
        echo target_web_started
        return 0
    fi
    if [ "$1" = up ] && [ "${{!#}}" = worker ]; then
        if [ "$project" = astra-app-a ]; then
            WORKER_SLOT=a
            echo target_worker_started
        else
            WORKER_SLOT=b
            echo fallback_restarted
        fi
        return 0
    fi
    if [ "$1" = ps ] && [ "$2" = -q ] && [ "$3" = worker ]; then
        if [ "$project" = astra-app-a ] && [ "$WORKER_SLOT" = a ]; then
            echo worker-a
        elif [ "$project" = astra-app-b ] && [ "$WORKER_SLOT" = b ]; then
            echo worker-b
        fi
        return 0
    fi
    return 1
}}
docker() {{
    test "$1" = inspect || return 1
    case "$4:$3" in
        worker-a:*State.Health*) echo unhealthy ;;
        worker-a:*Config.Image*) echo astra-backend:candidate-release ;;
        worker-b:*State.Health*) echo healthy ;;
        worker-b:*Config.Image*) echo astra-backend:previous-release ;;
        *) return 1 ;;
    esac
}}
sudo() {{
    if [ "$1" = python3 ] || [ "$1" = nginx ]; then
        return 0
    fi
    return 1
}}
{port_function}
{recovery_helpers}
{recovery_function}
recover_indeterminate_cutover b a 3008 candidate-release
status=$?
echo "status=$status"
echo "worker_slot=$WORKER_SLOT"
echo "active_slot=$(tr -d '\n' < "$ACTIVE_SLOT_FILE")"
echo "cutover_state=$(tr -d '\n' < "$CUTOVER_STATE_FILE")"
test ! -e "$ACTIVE_RELEASE_FILE"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
    events = result.stdout
    assert "target_worker_started" in events
    assert "target_worker_unhealthy" in events
    assert "fallback_restarted" not in events
    assert "worker_slot=stopped" in events
    assert "active_slot=b" in events
    assert "cutover_state=recovery_incomplete slot=a release=candidate-release" in events
    assert "unexpected_active_commit" not in events


def test_production_deploy_reconciles_live_nginx_before_clearing_inactive_slot():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    active_port = script.index('active-port "$NGINX_SITE"')
    recovery_call = script.index("if ! recover_indeterminate_cutover", active_port)
    pending_drain = script.index("if ! complete_pending_drain", recovery_call)
    clear_candidate = script.index('echo "[remote] clearing inactive slot')
    recovery_start = script.index("recover_indeterminate_cutover() {")
    recovery_end = script.index("\nif ! CURRENT_TARGET=", recovery_start)
    recovery = script[recovery_start:recovery_end]
    assert active_port < recovery_call < pending_drain < clear_candidate
    recovery_maintenance = recovery.index("maintenance-on")
    recovery_old_stop = recovery.index("stop --timeout 90 worker frontend backend")
    recovery_migration = recovery.index("backend upgrade head")
    recovery_local = recovery.index("wait_for_local_release", recovery_migration)
    recovery_worker = recovery.index("activate_worker_release", recovery_local)
    recovery_install = recovery.index('install "$NGINX_SITE"', recovery_worker)
    recovery_public = recovery.index("wait_for_public_release", recovery_install)
    recovery_commit = recovery.index("commit_active_state", recovery_public)
    assert (
        recovery_maintenance
        < recovery_old_stop
        < recovery_migration
        < recovery_local
        < recovery_worker
        < recovery_install
        < recovery_public
        < recovery_commit
    )
    assert "rm -sf" not in recovery

    candidate_journal = script.index('write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"')
    maintenance = script.index("if ! enable_web_maintenance", candidate_journal)
    old_stop = script.index(
        "stop --timeout 90 worker frontend backend",
        maintenance,
    )
    migration = script.index("backend upgrade head", old_stop)
    schema_fence = script.index("write_cutover_state schema_forward_only", migration)
    candidate_worker = script.index("if ! activate_worker_release", schema_fence)
    cutover_install = script.index(
        'install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT"',
        candidate_worker,
    )
    public_identity = script.index(
        'wait_for_public_release "$VERSION" "$COMMIT"',
        cutover_install,
    )
    active_state_persist = script.index(
        'commit_active_state "$CANDIDATE_SLOT" "$RELEASE_ID"',
        public_identity,
    )
    assert (
        candidate_journal
        < maintenance
        < old_stop
        < migration
        < schema_fence
        < candidate_worker
        < cutover_install
        < public_identity
        < active_state_persist
    )
    assert "CUTOVER_STATE_FILE" in script
    assert "logrotate -f" not in script


@pytest.mark.parametrize(
    "phase",
    [
        "candidate_ready",
        "candidate_business_verified",
        "nginx_reloaded",
        "public_verified",
        "traffic_and_worker_committed",
        "rollback_started",
        "rollback_incomplete",
        "rollback_partial",
        "rollback_recovering_candidate",
        "recovery_started",
        "recovery_incomplete",
    ],
)
def test_nonterminal_cutover_state_forces_recovery_when_slot_files_agree(
    tmp_path,
    phase,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_start = script.index("port_for_slot() {")
    port_end = script.index("release_payloads_match() {", port_start)
    port_function = script[port_start:port_end]
    state_start = script.index("parse_cutover_state() {")
    state_end = script.index("recover_indeterminate_cutover() {", state_start)
    state_functions = script[state_start:state_end]
    cutover_state = tmp_path / "cutover-state"
    cutover_state.write_text(
        f"{phase} slot=a release=previous-release\n",
        encoding="utf-8",
    )
    harness = f"""set -e
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
RECORDED_SLOT=a
DISK_SLOT=a
NGINX_ACTIVE_PORT=3008
{port_function}
{state_functions}
parse_cutover_state
select_recovery_target
echo "$RECOVERY_REQUIRED:$RECOVERY_TARGET_SLOT:$RECOVERY_TARGET_PORT:$RECOVERY_TARGET_RELEASE_ID"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1:a:3008:previous-release"


@pytest.mark.parametrize(
    "state",
    [
        "unknown_phase slot=a release=previous-release\n",
        "rollback_started slot=a release=../outside\n",
        "rollback_started slot=a release=one\ncomplete slot=a release=two\n",
    ],
)
def test_invalid_cutover_state_is_rejected_before_recovery(tmp_path, state):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    state_start = script.index("parse_cutover_state() {")
    state_end = script.index("select_recovery_target() {", state_start)
    parse_function = script[state_start:state_end]
    cutover_state = tmp_path / "cutover-state"
    cutover_state.write_text(state, encoding="utf-8")
    harness = f"""set +e
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
{parse_function}
parse_cutover_state
status=$?
echo "status=$status"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status=1" in result.stdout


@pytest.mark.parametrize("link_kind", ["regular", "dangling"])
def test_cutover_state_symlinks_are_rejected_before_recovery(tmp_path, link_kind):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    parse_function = _shell_function_source(
        script,
        "parse_cutover_state",
        "validate_nonterminal_recovery_state",
    )
    cutover_state = tmp_path / "cutover-state"
    if link_kind == "regular":
        external_state = tmp_path / "external-cutover-state"
        external_state.write_text(
            "rollback_started slot=a release=release-a\n",
            encoding="utf-8",
        )
        cutover_state.symlink_to(external_state)
    else:
        cutover_state.symlink_to(tmp_path / "missing-cutover-state")
    harness = f"""set +e
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
{parse_function}
parse_cutover_state
status=$?
echo "status=$status"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status=1" in result.stdout


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("current_recorded", 0),
        ("current_target", 0),
        ("rollback_current_candidate", 0),
        ("rollback_partial_current_candidate", 1),
        ("rollback_current_unjournaled", 1),
        ("rollback_missing_source_journal", 1),
        ("current_unrelated", 1),
        ("active_release_mismatch", 1),
        ("target_release_mismatch", 1),
        ("missing_recorded_journal", 1),
        ("missing_target_journal", 1),
        ("legacy_state_source", 1),
    ],
)
def test_nonterminal_recovery_accepts_only_the_atomic_recorded_release_pair(
    tmp_path,
    mutation,
    expected_status,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    release_functions = _shell_function_source(
        script,
        "canonical_managed_release",
        "wait_for_worker_release",
    )
    validation_function = _shell_function_source(
        script,
        "validate_nonterminal_recovery_state",
        "select_recovery_target",
    )
    app_root = tmp_path / "app"
    recorded_release = _write_test_release(app_root, "recorded-release")
    target_release = _write_test_release(app_root, "target-release")
    unrelated_release = _write_test_release(app_root, "unrelated-release")
    recorded_journal = app_root / "slot-b-release"
    target_journal = app_root / "slot-a-release"
    recorded_journal.write_text(f"{recorded_release}\n", encoding="utf-8")
    target_journal.write_text(f"{target_release}\n", encoding="utf-8")
    current = app_root / "current"
    current_release = (
        target_release
        if mutation
        in {
            "current_target",
            "rollback_current_candidate",
            "rollback_partial_current_candidate",
            "rollback_missing_source_journal",
        }
        else recorded_release
    )
    if mutation in {"current_unrelated", "rollback_current_unjournaled"}:
        current_release = unrelated_release
    current.symlink_to(current_release)
    if mutation == "missing_recorded_journal":
        recorded_journal.unlink()
    elif mutation in {"missing_target_journal", "rollback_missing_source_journal"}:
        target_journal.unlink()
    active_release_id = (
        "wrong-recorded-release"
        if mutation == "active_release_mismatch"
        else "recorded-release"
    )
    target_release_id = (
        "wrong-target-release"
        if mutation == "target_release_mismatch"
        else "target-release"
    )
    cutover_phase = "candidate_ready"
    cutover_slot = "a"
    if mutation.startswith("rollback_"):
        cutover_phase = (
            "rollback_partial"
            if mutation == "rollback_partial_current_candidate"
            else "rollback_started"
        )
        cutover_slot = "b"
        target_release_id = "recorded-release"
    active_state_source = "legacy-pair" if mutation == "legacy_state_source" else "atomic"
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_FILE=docker-compose.prod.yml
CURRENT={shlex.quote(str(current))}
CUTOVER_NONTERMINAL=1
CUTOVER_PHASE={cutover_phase}
CUTOVER_SLOT={cutover_slot}
CUTOVER_RELEASE_ID={target_release_id}
ACTIVE_STATE_PRESENT=1
ACTIVE_STATE_SOURCE={active_state_source}
RECORDED_SLOT=b
ACTIVE_RELEASE_ID={active_release_id}
CURRENT_TARGET=unchanged
{release_functions}
{validation_function}
validate_nonterminal_recovery_state
status=$?
echo "status=$status current_target=$CURRENT_TARGET"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status, result.stderr
    assert f"status={expected_status}" in result.stdout
    if expected_status == 0:
        assert f"current_target={current_release}" in result.stdout
    else:
        assert "current_target=unchanged" in result.stdout


def test_production_deploy_rollback_converges_public_and_worker_before_state():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]

    intent = rollback.index("rollback_started")
    old_app = rollback.index("ensure_old_application_ready", intent)
    nginx = rollback.index("restore_previous_nginx", old_app)
    public = rollback.index("wait_for_public_release", nginx)
    worker_handoff = rollback.index("activate_worker_release", public)
    active_state = rollback.index("commit_active_state", worker_handoff)
    terminal = rollback.index("rollback_complete", active_state)

    assert intent < old_app < nginx < public < worker_handoff
    assert worker_handoff < active_state < terminal
    assert 'rollback_incomplete "$CANDIDATE_SLOT"' not in rollback


def test_failed_old_worker_handoff_recovers_candidate_without_committing_old_state(
    tmp_path,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    smoke = tmp_path / "missing.smoke"
    previous = tmp_path / "previous-release"
    release = tmp_path / "candidate-release"
    previous.mkdir()
    release.mkdir()
    harness = f"""set +e
SMOKE_ENV_FILE={shlex.quote(str(smoke))}
PREVIOUS={shlex.quote(str(previous))}
PREVIOUS_RELEASE_ID=previous-release
PREVIOUS_VERSION=1.10.11
PREVIOUS_COMMIT=old1234
RELEASE={shlex.quote(str(release))}
RELEASE_ID=candidate-release
ACTIVE_SLOT=b
CANDIDATE_SLOT=a
NGINX_CONFIG_TOUCHED=1
CURRENT={shlex.quote(str(tmp_path / "current"))}
ACTIVE_SLOT_FILE={shlex.quote(str(tmp_path / "active-slot"))}
ACTIVE_RELEASE_FILE={shlex.quote(str(tmp_path / "active-release"))}
OLD_WORKER_STOP_REQUESTED=1
CANDIDATE_PROJECT=astra-app-a
OLD_PROJECT=astra-app-b
COMPOSE_FILE=docker-compose.prod.yml
ROLLBACK_REQUIRES_MCP_QUARANTINE=0
write_cutover_state() {{ echo "state:$1:$2:$3"; return 0; }}
approval_schema_forward_state() {{ printf '0'; }}
preserve_forward_only_maintenance() {{ echo unexpected_forward_only; return 1; }}
write_atomic_line() {{ echo "unexpected_active_write:$1:$2"; return 0; }}
commit_active_state() {{ echo "unexpected_active_commit:$1:$2"; return 0; }}
ensure_old_application_ready() {{ echo old_app_ready; return 0; }}
restore_previous_nginx() {{ echo nginx_old; return 0; }}
wait_for_public_release() {{ echo public_old; return 0; }}
activate_worker_release() {{
    echo old_worker_started
    echo old_worker_unhealthy
    return 1
}}
recover_candidate_traffic() {{ echo candidate_recovered; return 0; }}
compose_project() {{
    local project="$1"
    shift 3
    if [ "$project" = astra-app-a ] && [ "$1" = stop ]; then
        echo candidate_worker_stopped
        return 0
    fi
    if [ "$project" = astra-app-b ] && [ "$1" = up ]; then
        echo old_worker_started
        return 0
    fi
    if [ "$project" = astra-app-b ] && [ "$1" = stop ]; then
        echo partial_old_worker_stopped
        return 0
    fi
    return 1
}}
{rollback}
rollback
status=$?
echo "status=$status"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
    events = result.stdout
    assert events.index("public_old") < events.index("old_worker_started")
    assert events.index("old_worker_started") < events.index("old_worker_unhealthy")
    assert events.index("old_worker_unhealthy") < events.index("candidate_recovered")
    assert "unexpected_active_write" not in events
    assert "unexpected_active_commit" not in events
    assert "state:rollback_started:b:previous-release" in events


def test_nginx_reload_validates_public_before_retiring_old_workers():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper_start = script.index("remaining_old_nginx_workers() {")
    helper_end = script.index("parse_cutover_state() {", helper_start)
    helper = script[helper_start:helper_end]
    candidate_worker = script.index(
        "if ! activate_worker_release",
        script.index("write_cutover_state schema_forward_only"),
    )
    cutover_start = script.index(
        'echo "[remote] switching the verified maintenance fence to candidate traffic"',
        candidate_worker,
    )
    cutover = script.index("reload_nginx_with_worker_snapshot", cutover_start)
    reloaded_state = script.index("write_cutover_state nginx_reloaded", cutover)
    public_gate = script.index('echo "[remote] verifying public cutover identity"', cutover)
    public_identity = script.index('wait_for_public_release "$VERSION" "$COMMIT"', public_gate)
    retirement = script.index("if ! retire_pre_reload_nginx_workers", public_identity)
    active_commit = script.index('commit_active_state "$CANDIDATE_SLOT" "$RELEASE_ID"', retirement)

    assert script.count("sudo systemctl reload nginx") == 1
    assert "NGINX_RELOAD_OLD_WORKERS=" in helper
    assert helper.index("NGINX_RELOAD_OLD_WORKERS=") < helper.index("sudo systemctl reload nginx")
    assert "sudo kill -TERM" in helper
    assert "sudo systemctl is-active --quiet nginx" in helper
    assert "${NGINX_WORKER_GRACE_SECONDS:-$DRAIN_TIMEOUT_SECONDS}" in helper
    assert candidate_worker < cutover_start < cutover < reloaded_state
    assert reloaded_state < public_gate < public_identity < retirement < active_commit


def test_maintenance_writer_fence_is_durable_before_schema_migration():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    candidate_journal = script.index('write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"')
    maintenance = script.index(
        "if ! enable_web_maintenance",
        candidate_journal,
    )
    maintenance_state = script.index(
        'write_cutover_state maintenance_enabled "$CANDIDATE_SLOT" "$RELEASE_ID"',
        maintenance,
    )
    old_stop = script.index(
        "stop --timeout 90 worker frontend backend",
        maintenance_state,
    )
    migration_state = script.index(
        'write_cutover_state migration_started "$CANDIDATE_SLOT" "$RELEASE_ID"',
        old_stop,
    )
    migration = script.index("backend upgrade head", migration_state)
    schema_state = script.index(
        'write_cutover_state schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID"',
        migration,
    )
    pending_drain = script.index(
        'write_atomic_line "$PENDING_DRAIN_FILE" "$OLD_PROJECT $OLD_PORT $PREVIOUS"',
        schema_state,
    )

    assert candidate_journal < maintenance < maintenance_state < old_stop < migration_state < migration < schema_state
    assert schema_state < pending_drain


def test_production_deploy_does_not_rebuild_the_live_project_in_place():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'compose .env "$COMPOSE_FILE" up -d --build' not in script
    assert "build backend frontend" in script
    assert "stop frontend backend" in script


def test_deploy_state_is_serialized_and_durably_committed_before_legacy_mirrors():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    atomic_line = _shell_function_source(script, "write_atomic_line", "remove_durable_file")
    atomic_symlink = _shell_function_source(
        script,
        "write_atomic_symlink",
        "commit_active_state",
    )
    commit = _shell_function_source(script, "commit_active_state", "load_active_state")

    lock = script.index('exec 9>"$APP_ROOT/deploy.lock"')
    lock_acquired = script.index("flock -n 9", lock)
    current_read = script.index('CURRENT_TARGET="$(canonical_current_release "$CURRENT")"')
    state_read = script.index("if ! load_active_state", current_read)
    cutover_read = script.index("if ! parse_cutover_state", state_read)
    recovery_state_validation = script.index(
        "if ! validate_nonterminal_recovery_state",
        cutover_read,
    )
    release_create = script.index('mkdir -p "$RELEASE" "$BACKUP"')
    assert (
        lock
        < lock_acquired
        < current_read
        < state_read
        < cutover_read
        < recovery_state_validation
        < release_create
    )
    assert 'NONCE="$(python3 -c' in script
    assert "os.fsync(handle.fileno())" in atomic_line
    assert "os.fsync(directory_fd)" in atomic_line
    assert "os.replace(temporary_name, path)" in atomic_symlink
    assert "os.fsync(directory_fd)" in atomic_symlink
    authority = commit.index('write_atomic_line "$ACTIVE_STATE_FILE"')
    slot_mirror = commit.index('write_atomic_line "$ACTIVE_SLOT_FILE"')
    release_mirror = commit.index('write_atomic_line "$ACTIVE_RELEASE_FILE"')
    assert authority < slot_mirror < release_mirror


def test_atomic_active_state_is_authoritative_and_heals_legacy_mirrors(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helpers_start = script.index("write_atomic_line() {")
    helpers_end = script.index("write_cutover_state() {", helpers_start)
    helpers = script[helpers_start:helpers_end]
    active_state = tmp_path / "active-state"
    active_slot = tmp_path / "active-slot"
    active_release = tmp_path / "active-release"
    harness = f"""set -e
ACTIVE_STATE_FILE={shlex.quote(str(active_state))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(active_release))}
{helpers}
commit_active_state a release-a
printf 'b\n' > "$ACTIVE_SLOT_FILE"
printf 'stale-release\n' > "$ACTIVE_RELEASE_FILE"
load_active_state
echo "loaded=$RECORDED_SLOT:$ACTIVE_RELEASE_ID:$ACTIVE_STATE_SOURCE"
commit_active_state "$RECORDED_SLOT" "$ACTIVE_RELEASE_ID"
echo "state=$(tr -d '\n' < "$ACTIVE_STATE_FILE")"
echo "slot=$(tr -d '\n' < "$ACTIVE_SLOT_FILE")"
echo "release=$(tr -d '\n' < "$ACTIVE_RELEASE_FILE")"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "loaded=a:release-a:atomic" in result.stdout
    assert "state=slot=a release=release-a" in result.stdout
    assert "slot=a" in result.stdout
    assert "release=release-a" in result.stdout


def test_malformed_atomic_active_state_fails_closed_even_with_valid_mirrors(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    load_state = _shell_function_source(script, "load_active_state", "write_cutover_state")
    active_state = tmp_path / "active-state"
    active_state.write_text("slot=a release=../outside\n", encoding="utf-8")
    active_slot = tmp_path / "active-slot"
    active_slot.write_text("a\n", encoding="utf-8")
    active_release = tmp_path / "active-release"
    active_release.write_text("release-a\n", encoding="utf-8")
    harness = f"""set +e
ACTIVE_STATE_FILE={shlex.quote(str(active_state))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(active_release))}
{load_state}
load_active_state
status=$?
echo status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status=1" in result.stdout
    assert "invalid active state format" in result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "outside_root",
        "nested_release",
        "relative_path",
        "dotdot_path",
        "duplicate_slash",
        "shell_metacharacters",
        "unicode_name",
        "release_symlink",
        "metadata_symlink",
        "missing_metadata",
        "empty_metadata",
        "directory_metadata",
    ],
)
def test_canonical_managed_release_accepts_only_exact_complete_direct_children(
    tmp_path,
    mutation,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    canonical_function = _shell_function_source(
        script,
        "canonical_managed_release",
        "release_for_slot",
    )
    app_root = tmp_path / "app"
    valid_release = _write_test_release(app_root, "release-a")
    candidate = str(valid_release)
    sentinel = tmp_path / "shell-metacharacters-executed"

    if mutation == "outside_root":
        candidate = str(_write_test_release(tmp_path / "outside-app", "release-a"))
    elif mutation == "nested_release":
        candidate = str(_write_test_release(app_root, "nested/release-a"))
    elif mutation == "relative_path":
        candidate = "releases/release-a"
    elif mutation == "dotdot_path":
        (app_root / "releases" / "nested").mkdir()
        candidate = f"{app_root}/releases/nested/../release-a"
    elif mutation == "duplicate_slash":
        candidate = f"{app_root}/releases//release-a"
    elif mutation == "shell_metacharacters":
        candidate = str(
            _write_test_release(
                app_root,
                f"release-$(touch${{IFS}}{sentinel})",
            )
        )
    elif mutation == "unicode_name":
        candidate = str(_write_test_release(app_root, "发布候选"))
    elif mutation == "release_symlink":
        release_link = app_root / "releases" / "release-link"
        release_link.symlink_to(valid_release, target_is_directory=True)
        candidate = str(release_link)
    elif mutation == "metadata_symlink":
        external_metadata = tmp_path / "external-version"
        external_metadata.write_text("1.10.12\n", encoding="utf-8")
        (valid_release / "VERSION").unlink()
        (valid_release / "VERSION").symlink_to(external_metadata)
    elif mutation == "missing_metadata":
        (valid_release / "COMMIT").unlink()
    elif mutation == "empty_metadata":
        (valid_release / ".env").write_bytes(b"")
    elif mutation == "directory_metadata":
        compose_file = valid_release / "docker-compose.prod.yml"
        compose_file.unlink()
        compose_file.mkdir()

    harness = f"""set +e
APP_ROOT="$1"
COMPOSE_FILE=docker-compose.prod.yml
{canonical_function}
canonical_managed_release "$2"
"""
    result = subprocess.run(
        ["bash", "-c", harness, "canonical-release-test", str(app_root), candidate],
        cwd=app_root,
        check=False,
        capture_output=True,
        text=True,
    )

    expected_status = 0 if mutation == "valid" else 1
    assert result.returncode == expected_status, result.stderr
    if mutation == "valid":
        assert result.stdout == str(valid_release.resolve())
    else:
        assert result.stdout == ""
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("valid", 0),
        ("existing_valid_journal", 0),
        ("release_mismatch", 1),
        ("nginx_mismatch", 1),
        ("symlink_journal", 1),
        ("dangling_symlink_journal", 1),
        ("invalid_journal", 1),
        ("multiline_journal", 1),
        ("unicode_control_journal", 1),
        ("unicode_control_current_without_journal", 1),
        ("empty_journal", 1),
        ("directory_journal", 1),
        ("current_relative", 1),
        ("current_dotdot", 1),
        ("current_duplicate_slash", 1),
        ("current_nested_release", 1),
        ("current_release_symlink", 1),
        ("current_outside_root", 1),
        ("current_regular_file", 1),
        ("current_dangling_symlink", 1),
    ],
)
def test_legacy_active_pair_slot_journal_is_strictly_migrated(
    tmp_path,
    mutation,
    expected_status,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    app_root = tmp_path / "app"
    release_b = _write_test_release(app_root, "release-b")
    current = app_root / "current"
    current.symlink_to(release_b)
    active_slot = app_root / "active-slot"
    active_slot.write_text("b\n", encoding="utf-8")
    active_release = app_root / "active-release"
    active_release.write_text(
        f"{'wrong-release' if mutation == 'release_mismatch' else 'release-b'}\n",
        encoding="utf-8",
    )
    slot_journal = app_root / "slot-b-release"
    external_journal = tmp_path / "external-slot-journal"
    unicode_control_release = app_root / "releases" / "\u0085" / "release-b"
    if mutation == "existing_valid_journal":
        slot_journal.write_text(f"{release_b}\n", encoding="utf-8")
    elif mutation == "symlink_journal":
        external_journal.write_text(f"{release_b}\n", encoding="utf-8")
        slot_journal.symlink_to(external_journal)
    elif mutation == "dangling_symlink_journal":
        slot_journal.symlink_to(tmp_path / "missing-slot-journal")
    elif mutation == "invalid_journal":
        slot_journal.write_text("not-a-release\n", encoding="utf-8")
    elif mutation == "multiline_journal":
        slot_journal.write_text(
            f"{str(release_b)[:-1]}\n{str(release_b)[-1:]}\n",
            encoding="utf-8",
        )
    elif mutation in {
        "unicode_control_journal",
        "unicode_control_current_without_journal",
    }:
        unicode_control_release = _write_test_release(app_root, "\u0085/release-b")
        current.unlink()
        current.symlink_to(unicode_control_release)
        if mutation == "unicode_control_journal":
            slot_journal.write_text(f"{unicode_control_release}\n", encoding="utf-8")
    elif mutation == "empty_journal":
        slot_journal.touch()
    elif mutation == "directory_journal":
        slot_journal.mkdir()
    elif mutation == "current_relative":
        current.unlink()
        current.symlink_to("releases/release-b")
    elif mutation == "current_dotdot":
        (app_root / "releases" / "nested").mkdir()
        current.unlink()
        current.symlink_to(f"{app_root}/releases/nested/../release-b")
    elif mutation == "current_duplicate_slash":
        current.unlink()
        current.symlink_to(f"{app_root}/releases//release-b")
    elif mutation == "current_nested_release":
        nested_release = _write_test_release(app_root, "nested/release-b")
        current.unlink()
        current.symlink_to(nested_release)
    elif mutation == "current_release_symlink":
        release_link = app_root / "releases" / "release-link"
        release_link.symlink_to(release_b, target_is_directory=True)
        current.unlink()
        current.symlink_to(release_link)
    elif mutation == "current_outside_root":
        outside_release = _write_test_release(tmp_path / "outside-app", "release-b")
        current.unlink()
        current.symlink_to(outside_release)
    elif mutation == "current_regular_file":
        current.unlink()
        current.write_text(str(release_b), encoding="utf-8")
    elif mutation == "current_dangling_symlink":
        current.unlink()
        current.symlink_to(app_root / "releases" / "missing-release")
    state_helpers = _shell_function_source(
        script,
        "write_atomic_line",
        "write_cutover_state",
    )
    port_function = _shell_function_source(
        script,
        "port_for_slot",
        "release_payloads_match",
    )
    release_function = _shell_function_source(
        script,
        "canonical_managed_release",
        "wait_for_worker_release",
    )
    validate_function = _shell_function_source(
        script,
        "validate_stable_state",
        "recover_indeterminate_cutover",
    )
    nginx_port = "3008" if mutation == "nginx_mismatch" else "3009"
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_FILE=docker-compose.prod.yml
CURRENT={shlex.quote(str(current))}
ACTIVE_STATE_FILE={shlex.quote(str(app_root / "active-state"))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(active_release))}
CUTOVER_PHASE=
NGINX_ACTIVE_PORT={nginx_port}
{state_helpers}
{port_function}
{release_function}
{validate_function}
load_active_state
echo "source_before=$ACTIVE_STATE_SOURCE"
validate_stable_state
status=$?
if [ "$status" != 0 ]; then
    echo "status=$status"
    exit "$status"
fi
PREVIOUS=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' "$CURRENT")
ACTIVE_SLOT=$RECORDED_SLOT
PREVIOUS_RELEASE_ID=$(basename "$PREVIOUS")
write_atomic_line "$APP_ROOT/slot-${{ACTIVE_SLOT}}-release" "$PREVIOUS"
commit_active_state "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID"
load_active_state
validate_stable_state
status=$?
echo "status=$status source_after=$ACTIVE_STATE_SOURCE"
echo "state=$(tr -d '\n' < "$ACTIVE_STATE_FILE")"
echo "journal=$(tr -d '\n' < "$APP_ROOT/slot-b-release")"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status, result.stderr
    assert f"status={expected_status}" in result.stdout
    assert "source_before=legacy-pair" in result.stdout
    if expected_status == 0:
        assert "source_after=atomic" in result.stdout
        assert "state=slot=b release=release-b" in result.stdout
        assert f"journal={release_b}" in result.stdout
    else:
        assert not (app_root / "active-state").exists()
        if mutation == "symlink_journal":
            assert slot_journal.is_symlink()
            assert external_journal.read_text(encoding="utf-8") == f"{release_b}\n"
        elif mutation == "dangling_symlink_journal":
            assert slot_journal.is_symlink()
            assert not slot_journal.exists()
        elif mutation == "invalid_journal":
            assert slot_journal.read_text(encoding="utf-8") == "not-a-release\n"
        elif mutation == "multiline_journal":
            assert slot_journal.read_text(encoding="utf-8") == (f"{str(release_b)[:-1]}\n{str(release_b)[-1:]}\n")
        elif mutation == "unicode_control_journal":
            assert slot_journal.read_text(encoding="utf-8") == (
                f"{unicode_control_release}\n"
            )
        elif mutation == "empty_journal":
            assert slot_journal.read_bytes() == b""
        elif mutation == "directory_journal":
            assert slot_journal.is_dir()
        else:
            assert not slot_journal.exists()

    previous_id = script.index('PREVIOUS_RELEASE_ID="$(basename "$PREVIOUS")"')
    slot_journal = script.index(
        'write_atomic_line "$APP_ROOT/slot-${ACTIVE_SLOT}-release" "$PREVIOUS"',
        previous_id,
    )
    canonical_commit = script.index(
        'commit_active_state "$ACTIVE_SLOT" "$PREVIOUS_RELEASE_ID"',
        slot_journal,
    )
    pending_drain = script.index("if ! complete_pending_drain", canonical_commit)
    assert previous_id < slot_journal < canonical_commit < pending_drain


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("valid", 0),
        ("release_mismatch", 1),
        ("current_mismatch", 1),
        ("cutover_mismatch", 1),
        ("nginx_mismatch", 1),
    ],
)
def test_terminal_state_semantics_are_cross_validated(tmp_path, mutation, expected_status):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    app_root = tmp_path / "app"
    release_a = _write_test_release(app_root, "release-a")
    release_b = _write_test_release(app_root, "release-b")
    (app_root / "slot-a-release").write_text(f"{release_a}\n", encoding="utf-8")
    current = app_root / "current"
    current.symlink_to(release_b if mutation == "current_mismatch" else release_a)
    port_function = _shell_function_source(
        script,
        "port_for_slot",
        "release_payloads_match",
    )
    release_function = _shell_function_source(
        script,
        "canonical_managed_release",
        "wait_for_worker_release",
    )
    validate_start = script.index("validate_stable_state() {")
    validate_end = script.index("recover_indeterminate_cutover() {", validate_start)
    validate_function = script[validate_start:validate_end]
    active_release_id = "wrong-release" if mutation == "release_mismatch" else "release-a"
    cutover_slot = "b" if mutation == "cutover_mismatch" else "a"
    nginx_port = "3009" if mutation == "nginx_mismatch" else "3008"
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_FILE=docker-compose.prod.yml
CURRENT={shlex.quote(str(current))}
ACTIVE_STATE_PRESENT=1
RECORDED_SLOT=a
ACTIVE_RELEASE_ID={active_release_id}
CUTOVER_PHASE=complete
CUTOVER_SLOT={cutover_slot}
CUTOVER_RELEASE_ID=release-a
NGINX_ACTIVE_PORT={nginx_port}
{port_function}
{release_function}
{validate_function}
validate_stable_state
status=$?
echo status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status, result.stderr
    assert f"status={expected_status}" in result.stdout


def test_pre_candidate_rollback_recovery_does_not_require_opposite_slot_journal(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    port_function, recovery_helpers, recovery_function = _recovery_shell_source(script)
    app_root = tmp_path / "app"
    target_release = _write_test_release(app_root, "release-a")
    (app_root / "slot-a-release").write_text(f"{target_release}\n", encoding="utf-8")
    current = app_root / "current"
    cutover_state = app_root / "cutover-state"
    harness = f"""set -e
APP_ROOT={shlex.quote(str(app_root))}
COMPOSE_PROJECT=astra
COMPOSE_FILE=docker-compose.prod.yml
RELEASE={shlex.quote(str(target_release))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
CUTOVER_STATE_FILE={shlex.quote(str(cutover_state))}
RECORDED_SLOT=a
CUTOVER_PHASE=rollback_started
SCHEMA_FORWARD_STATE=0
write_cutover_state() {{ printf '%s\n' "$1 slot=$2 release=$3" > "$CUTOVER_STATE_FILE"; }}
write_atomic_symlink() {{ command ln -sfn "$2" "$1"; }}
wait_for_local_release() {{ echo local_ready; }}
wait_for_public_release() {{ echo public_ready; }}
audit_effective_nginx() {{ :; }}
reload_nginx_with_worker_snapshot() {{ :; }}
retire_pre_reload_nginx_workers() {{ :; }}
activate_worker_release() {{ echo worker_ready; }}
commit_active_state() {{ echo "active=$1:$2"; }}
compose_project() {{
    local project="$1"
    shift 3
    test "$project" = astra-app-a
    if [ "$1" = run ]; then
        echo schema_ready
        return 0
    fi
    if [ "$1" = up ]; then
        echo web_ready
        return 0
    fi
    return 1
}}
sudo() {{ :; }}
{port_function}
{recovery_helpers}
{recovery_function}
recover_indeterminate_cutover a a 3008 release-a
echo "state=$(tr -d '\n' < "$CUTOVER_STATE_FILE")"
test ! -e "$APP_ROOT/slot-b-release"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "local_ready" in result.stdout
    assert "public_ready" in result.stdout
    assert "worker_ready" in result.stdout
    assert "active=a:release-a" in result.stdout
    assert "state=recovery_complete slot=a release=release-a" in result.stdout


@pytest.mark.parametrize(("worker_image_id", "expected_status"), [("sha256:same", 0), ("sha256:wrong", 1)])
def test_worker_handoff_enforces_one_exact_healthy_release(
    worker_image_id,
    expected_status,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    identity_helper = _shell_function_source(
        script,
        "inspect_worker_runtime_identity",
        "run_candidate_alert_canary",
    )
    worker_start = script.index("wait_for_worker_release() {")
    worker_end = script.index("remaining_old_nginx_workers() {", worker_start)
    worker_functions = identity_helper + script[worker_start:worker_end]
    harness = f"""set +e
COMPOSE_PROJECT=astra
RUNNING_BASE=legacy-worker
RUNNING_A=old-a-worker
RUNNING_B=
WORKER_IMAGE_ID={worker_image_id}
sleep() {{ :; }}
compose_project() {{
    local project="$1"
    shift 3
    if [ "$1" = ps ] && [ "$2" = -q ] && [ "$3" = backend ]; then
        echo backend-b
        return 0
    fi
    if [ "$1" = ps ] && [ "$2" = -q ] && [ "$3" = worker ]; then
        [ -n "$RUNNING_B" ] && echo "$RUNNING_B"
        return 0
    fi
    if [ "$1" = up ] && [ "${{!#}}" = worker ]; then
        RUNNING_B=worker-b
        echo target_worker_started
        return 0
    fi
    if [ "$1" = stop ] && [ "$2" = worker ]; then
        RUNNING_B=
        echo target_worker_stopped
        return 0
    fi
    return 1
}}
docker() {{
    local command="$1"
    shift
    if [ "$command" = ps ]; then
        local project=
        local argument
        for argument in "$@"; do
            case "$argument" in
                label=com.docker.compose.project=*) project="${{argument##*=}}" ;;
            esac
        done
        case "$project" in
            astra) [ -n "$RUNNING_BASE" ] && echo "$RUNNING_BASE" ;;
            astra-app-a) [ -n "$RUNNING_A" ] && echo "$RUNNING_A" ;;
            astra-app-b) [ -n "$RUNNING_B" ] && echo "$RUNNING_B" ;;
        esac
        return 0
    fi
    if [ "$command" = stop ]; then
        shift 2
        local container
        for container in "$@"; do
            case "$container" in
                legacy-worker) RUNNING_BASE= ;;
                old-a-worker) RUNNING_A= ;;
            esac
            echo "stopped=$container"
        done
        return 0
    fi
    if [ "$command" = inspect ]; then
        local template="$2"
        local container="$3"
        case "$template:$container" in
            *State.Health*:*) echo healthy ;;
            *Config.Image*:worker-b) echo astra-backend:release-b ;;
            *.Image*:backend-b) echo sha256:same ;;
            *.Image*:worker-b) echo "$WORKER_IMAGE_ID" ;;
            *Config.Env*:worker-b)
                printf '%s\n' \
                    ASTRA_RELEASE_ID=release-b \
                    ASTRA_RELEASE_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
                    ASTRA_ALERT_WORKER_ACTOR_ID=00000000-0000-4000-8000-000000000001 \
                    PROCESS_ROLE=worker,connector
                ;;
            *Config.Labels*:worker-b) echo astra-app-b ;;
            *) return 1 ;;
        esac
        return 0
    fi
    return 1
}}
{worker_functions}
activate_worker_release astra-app-b env compose release-b 1
status=$?
echo "status=$status base=${{RUNNING_BASE:-none}} a=${{RUNNING_A:-none}} b=${{RUNNING_B:-none}}"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == expected_status, result.stderr
    assert "target_worker_started" in result.stdout
    if expected_status == 0:
        assert "status=0 base=none a=none b=worker-b" in result.stdout
        assert "target_worker_stopped" not in result.stdout
    else:
        assert "target_worker_stopped" in result.stdout
        assert "status=1 base=none a=none b=none" in result.stdout


def test_nginx_reload_terminates_only_pre_reload_worker_snapshot():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper_start = script.index("remaining_old_nginx_workers() {")
    helper_end = script.index("parse_cutover_state() {", helper_start)
    helper = script[helper_start:helper_end]
    harness = f"""set +e
NGINX_WORKER_GRACE_SECONDS=0
NGINX_WORKER_TERM_SECONDS=0
LIVE_101=1
LIVE_102=1
KILLS=
date() {{ echo 0; }}
sleep() {{ :; }}
sudo() {{
    case "$1" in
        cat) echo 999 ;;
        pgrep) printf '%s\n' 101 102 ;;
        ps)
            local pid="${{!#}}"
            case "$pid" in
                101) [ "$LIVE_101" = 1 ] && echo 999 ;;
                102) [ "$LIVE_102" = 1 ] && echo 999 ;;
                201) echo 999 ;;
            esac
            ;;
        kill)
            local pid="$3"
            KILLS="$KILLS $pid"
            [ "$pid" = 101 ] && LIVE_101=0
            [ "$pid" = 102 ] && LIVE_102=0
            ;;
        systemctl) return 0 ;;
        *) return 1 ;;
    esac
}}
{helper}
reload_nginx_with_worker_snapshot
retire_pre_reload_nginx_workers
status=$?
echo "status=$status kills=$KILLS"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "status=0" in result.stdout
    assert "101" in result.stdout and "102" in result.stdout
    assert "201" not in result.stdout


def test_nginx_old_workers_receive_the_application_drain_grace(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    helper_start = script.index("remaining_old_nginx_workers() {")
    helper_end = script.index("parse_cutover_state() {", helper_start)
    helper = script[helper_start:helper_end]
    clock = tmp_path / "clock"
    live = tmp_path / "live"
    harness = f"""set +e
DRAIN_TIMEOUT_SECONDS=3
NGINX_WORKER_TERM_SECONDS=0
CLOCK={shlex.quote(str(clock))}
LIVE={shlex.quote(str(live))}
printf 0 > "$CLOCK"
printf 1 > "$LIVE"
date() {{
    local value
    value=$(cat "$CLOCK")
    echo "$value"
    printf '%s' "$((value + 1))" > "$CLOCK"
}}
sleep() {{ echo "grace_wait=$1"; }}
sudo() {{
    case "$1" in
        cat) echo 999 ;;
        pgrep) echo 101 ;;
        ps) [ "$(cat "$LIVE")" = 1 ] && echo 999 ;;
        kill) printf 0 > "$LIVE"; echo term_sent ;;
        systemctl) return 0 ;;
        *) return 1 ;;
    esac
}}
{helper}
reload_nginx_with_worker_snapshot
retire_pre_reload_nginx_workers
status=$?
echo "status=$status clock=$(cat "$CLOCK")"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("grace_wait=1") == 2
    assert "term_sent" in result.stdout
    assert "status=0" in result.stdout


@pytest.mark.parametrize(("connections", "expected_status"), [(0, 0), (2, 1)])
def test_pending_drain_blocks_live_connections_and_clears_only_after_zero(
    tmp_path,
    connections,
    expected_status,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    pending_functions = _shell_function_source(
        script,
        "count_established_connections",
        "release_for_slot",
    )
    app_root = tmp_path / "app"
    previous = _write_test_release(app_root, "release-b")
    pending = _write_test_release(app_root, "release-a")
    marker = app_root / "pending-drain"
    marker.write_text(f"astra-app-a 3008 {pending}\n", encoding="utf-8")
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
COMPOSE_PROJECT=astra
COMPOSE_FILE=docker-compose.prod.yml
OLD_PROJECT=astra-app-b
CANDIDATE_PORT=3008
PREVIOUS={shlex.quote(str(previous))}
CONNECTIONS={connections}
ss() {{
    test "$1" = -Htn
    test "$2" = state
    test "$3" = established
    test "$4" = "sport = :3008"
    local index=0
    while [ "$index" -lt "$CONNECTIONS" ]; do
        index=$((index + 1))
        echo connection-$index
    done
}}
compose_project() {{ echo "stopped=$1:${{4}}:${{5}}:${{6}}"; }}
remove_durable_file() {{ rm -f "$1"; }}
{pending_functions}
complete_pending_drain
status=$?
if [ -e "$PENDING_DRAIN_FILE" ]; then marker=present; else marker=removed; fi
echo "status=$status marker=$marker"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status, result.stderr
    if expected_status == 0:
        assert "stopped=astra-app-a:stop:worker:frontend" in result.stdout
        assert "status=0 marker=removed" in result.stdout
    else:
        assert "stopped=" not in result.stdout
        assert "status=1 marker=present" in result.stdout
        assert "refusing slot reuse" in result.stderr


def test_pending_drain_marker_is_cancelled_after_exact_rollback(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    pending_functions = _shell_function_source(
        script,
        "count_established_connections",
        "release_for_slot",
    )
    app_root = tmp_path / "app"
    previous = _write_test_release(app_root, "release-b")
    marker = app_root / "pending-drain"
    marker.write_text(f"astra-app-b 3009 {previous}\n", encoding="utf-8")
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
COMPOSE_PROJECT=astra
COMPOSE_FILE=docker-compose.prod.yml
OLD_PROJECT=astra-app-b
OLD_PORT=3009
CANDIDATE_PORT=3008
PREVIOUS={shlex.quote(str(previous))}
compose_project() {{ echo unexpected_compose; return 1; }}
ss() {{ echo unexpected_socket_probe; return 1; }}
remove_durable_file() {{ rm -f "$1"; echo marker_cancelled; }}
{pending_functions}
complete_pending_drain
status=$?
if [ -e "$PENDING_DRAIN_FILE" ]; then marker=present; else marker=removed; fi
echo "status=$status marker=$marker"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "marker_cancelled" in result.stdout
    assert "status=0 marker=removed" in result.stdout
    assert "unexpected_" not in result.stdout


@pytest.mark.parametrize("matches_active_release", [True, False])
def test_rollback_cancels_only_its_exact_pending_drain_marker(
    tmp_path,
    matches_active_release,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    cancel_function = _shell_function_source(
        script,
        "cancel_pending_drain_for_active_release",
        "complete_pending_drain",
    )
    marker = tmp_path / "pending-drain"
    expected = f"astra-app-b 3009 {tmp_path / 'release-b'}"
    marker_state = expected if matches_active_release else f"astra-app-a 3008 {tmp_path / 'release-a'}"
    marker.write_text(f"{marker_state}\n", encoding="utf-8")
    harness = f"""set +e
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
OLD_PROJECT=astra-app-b
OLD_PORT=3009
PREVIOUS={shlex.quote(str(tmp_path / "release-b"))}
remove_durable_file() {{ rm -f "$1"; echo marker_removed; }}
{cancel_function}
cancel_pending_drain_for_active_release
status=$?
if [ -e "$PENDING_DRAIN_FILE" ]; then marker=present; else marker=removed; fi
echo "status=$status marker=$marker"
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    expected_status = 0 if matches_active_release else 1
    assert result.returncode == expected_status, result.stderr
    if matches_active_release:
        assert "marker_removed" in result.stdout
        assert "status=0 marker=removed" in result.stdout
    else:
        assert "marker_removed" not in result.stdout
        assert "status=1 marker=present" in result.stdout
        assert "does not match the restored active release" in result.stderr

    rollback = _shell_function_source(script, "rollback", "abort_release")
    assert "cancel_pending_drain_for_active_release" in rollback
    assert 'remove_durable_file "$PENDING_DRAIN_FILE"' not in rollback


def test_pending_drain_treats_command_substitution_text_as_data(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    pending_functions = _shell_function_source(
        script,
        "count_established_connections",
        "release_for_slot",
    )
    app_root = tmp_path / "app"
    previous = _write_test_release(app_root, "release-b")
    marker = app_root / "pending-drain"
    sentinel = tmp_path / "executed"
    marker.write_text(
        f"astra-app-a 3008 $(touch${{IFS}}{sentinel})\n",
        encoding="utf-8",
    )
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
COMPOSE_PROJECT=astra
COMPOSE_FILE=docker-compose.prod.yml
OLD_PROJECT=astra-app-b
CANDIDATE_PORT=3008
PREVIOUS={shlex.quote(str(previous))}
compose_project() {{ echo unexpected_mutation; }}
remove_durable_file() {{ echo unexpected_removal; }}
ss() {{ echo unexpected_socket_probe; }}
{pending_functions}
complete_pending_drain
status=$?
echo status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert not sentinel.exists()
    assert "unexpected_" not in result.stdout


@pytest.mark.parametrize(
    "mutation",
    [
        "outside_root",
        "nested_release",
        "relative_path",
        "dotdot_path",
        "duplicate_slash",
        "shell_metacharacters",
        "unicode_name",
        "release_symlink",
        "metadata_symlink",
        "missing_metadata",
        "empty_metadata",
        "directory_metadata",
    ],
)
def test_pending_drain_rejects_every_noncanonical_release_before_mutation(
    tmp_path,
    mutation,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    pending_functions = _shell_function_source(
        script,
        "count_established_connections",
        "release_for_slot",
    )
    app_root = tmp_path / "app"
    previous = _write_test_release(app_root, "release-b")
    valid_pending = _write_test_release(app_root, "release-a")
    candidate = str(valid_pending)
    sentinel = tmp_path / "shell-metacharacters-executed"

    if mutation == "outside_root":
        candidate = str(_write_test_release(tmp_path / "outside-app", "release-a"))
    elif mutation == "nested_release":
        candidate = str(_write_test_release(app_root, "nested/release-a"))
    elif mutation == "relative_path":
        candidate = "releases/release-a"
    elif mutation == "dotdot_path":
        (app_root / "releases" / "nested").mkdir()
        candidate = f"{app_root}/releases/nested/../release-a"
    elif mutation == "duplicate_slash":
        candidate = f"{app_root}/releases//release-a"
    elif mutation == "shell_metacharacters":
        candidate = str(
            _write_test_release(
                app_root,
                f"release-$(touch${{IFS}}{sentinel})",
            )
        )
    elif mutation == "unicode_name":
        candidate = str(_write_test_release(app_root, "发布候选"))
    elif mutation == "release_symlink":
        release_link = app_root / "releases" / "release-link"
        release_link.symlink_to(valid_pending, target_is_directory=True)
        candidate = str(release_link)
    elif mutation == "metadata_symlink":
        external_metadata = tmp_path / "external-version"
        external_metadata.write_text("1.10.12\n", encoding="utf-8")
        (valid_pending / "VERSION").unlink()
        (valid_pending / "VERSION").symlink_to(external_metadata)
    elif mutation == "missing_metadata":
        (valid_pending / "COMMIT").unlink()
    elif mutation == "empty_metadata":
        (valid_pending / ".env").write_bytes(b"")
    elif mutation == "directory_metadata":
        compose_file = valid_pending / "docker-compose.prod.yml"
        compose_file.unlink()
        compose_file.mkdir()

    marker = app_root / "pending-drain"
    marker.write_text(f"astra-app-a 3008 {candidate}\n", encoding="utf-8")
    harness = f"""set +e
APP_ROOT={shlex.quote(str(app_root))}
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
COMPOSE_PROJECT=astra
COMPOSE_FILE=docker-compose.prod.yml
OLD_PROJECT=astra-app-b
OLD_PORT=3009
CANDIDATE_PORT=3008
PREVIOUS={shlex.quote(str(previous))}
compose_project() {{ echo unexpected_compose; return 1; }}
remove_durable_file() {{ echo unexpected_removal; return 1; }}
ss() {{ echo unexpected_socket_probe; return 1; }}
{pending_functions}
complete_pending_drain
status=$?
if [ -e "$PENDING_DRAIN_FILE" ]; then marker=present; else marker=removed; fi
echo "status=$status marker=$marker"
exit "$status"
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=app_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status=1 marker=present" in result.stdout
    assert "unexpected_" not in result.stdout
    assert not sentinel.exists()


def test_candidate_journal_precedes_writer_fence_and_identity_gate():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    journal = script.index('write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"')
    maintenance = script.index("if ! enable_web_maintenance", journal)
    schema_fence = script.index(
        'write_cutover_state schema_forward_only "$CANDIDATE_SLOT" "$RELEASE_ID"',
        maintenance,
    )
    identity = script.index(
        'if ! wait_for_local_release \\\n    "$CANDIDATE_PORT" "$VERSION" "$COMMIT" "$RELEASE_ID" 60'
    )
    fallback = script.index("CANDIDATE_READY_FOR_FALLBACK=1", identity)
    candidate_state = script.index(
        "write_cutover_state candidate_services_ready",
        fallback,
    )

    assert journal < maintenance < schema_fence < identity < fallback < candidate_state


def test_pending_drain_intent_is_durable_before_nginx_traffic_mutation():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    business_verified = script.index(
        'write_cutover_state candidate_business_verified "$CANDIDATE_SLOT" "$RELEASE_ID"'
    )
    pending_drain = script.index(
        'write_atomic_line "$PENDING_DRAIN_FILE" "$OLD_PROJECT $OLD_PORT $PREVIOUS"',
        business_verified,
    )
    nginx_install = script.index(
        'install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT"',
        pending_drain,
    )
    nginx_reload = script.index("reload_nginx_with_worker_snapshot", nginx_install)

    assert script.count(
        'write_atomic_line "$PENDING_DRAIN_FILE" "$OLD_PROJECT $OLD_PORT $PREVIOUS"'
    ) == 1
    assert business_verified < pending_drain < nginx_install < nginx_reload


@pytest.mark.parametrize("failure_point", ["old_application", "nginx_rollback"])
def test_rollback_uses_strictly_ready_candidate_when_old_path_cannot_converge(
    tmp_path,
    failure_point,
):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    ready_start = script.index("recover_candidate_if_ready() {")
    rollback_end = script.index("abort_release() {", ready_start)
    recovery_and_rollback = script[ready_start:rollback_end]
    harness = f"""set +e
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / "smoke"))}
PREVIOUS={shlex.quote(str(tmp_path / "previous"))}
PREVIOUS_RELEASE_ID=release-old
PREVIOUS_VERSION=1.10.11
PREVIOUS_COMMIT=old1234
RELEASE={shlex.quote(str(tmp_path / "candidate"))}
RELEASE_ID=release-new
ACTIVE_SLOT=b
CANDIDATE_SLOT=a
CANDIDATE_READY_FOR_FALLBACK=1
NGINX_CONFIG_TOUCHED=1
OLD_PROJECT=astra-app-b
CANDIDATE_PROJECT=astra-app-a
COMPOSE_FILE=docker-compose.prod.yml
FAILURE_POINT={failure_point}
write_cutover_state() {{ echo "state=$1:$2:$3"; }}
approval_schema_forward_state() {{ printf '0'; }}
preserve_forward_only_maintenance() {{ echo unexpected_forward_only; return 1; }}
ensure_old_application_ready() {{
    [ "$FAILURE_POINT" != old_application ]
}}
restore_previous_nginx() {{
    [ "$FAILURE_POINT" != nginx_rollback ]
}}
recover_candidate_traffic() {{ echo candidate_recovered; }}
wait_for_public_release() {{ echo unexpected_public; return 1; }}
activate_worker_release() {{ echo unexpected_worker; return 1; }}
commit_active_state() {{ echo unexpected_commit; return 1; }}
compose_project() {{ echo unexpected_compose; return 1; }}
cleanup_browser_smoke_runtime() {{ :; }}
{recovery_and_rollback}
rollback
status=$?
echo status=$status
exit $status
"""

    result = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
    assert "candidate_recovered" in result.stdout
    assert "state=rollback_started:b:release-old" in result.stdout
    assert "rollback_incomplete" not in result.stdout
    assert "unexpected_" not in result.stdout


def test_remote_preflight_requires_timeout_before_deploy_lock_mutation():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    timeout_check = script.index("if ! command -v timeout")
    deploy_lock = script.index('exec 9>"$APP_ROOT/deploy.lock"')
    first_timed_call = script.index("compose_project_timed 45 5")

    assert timeout_check < deploy_lock < first_timed_call


def test_model_route_preflight_declares_recursive_fallback_walk():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    preflight = _shell_function_source(
        script,
        "model_route_credential_preflight",
        "m3_route_post_migration_preflight",
    )

    assert "WITH RECURSIVE expected_minimax_capability" in preflight
    assert "fallback_walk(start_id, id, fallback_route_id, path, cycle) AS" in preflight


def test_recovery_persists_existing_proof_requirements_across_interruptions():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    recovery_start = script.index("recover_indeterminate_cutover() {")
    recovery_end = script.index("\nif ! CURRENT_TARGET=", recovery_start)
    recovery = script[recovery_start:recovery_end]

    for phase in (
        "recovery_started_alert_proof",
        "recovery_incomplete_alert_proof",
        "recovery_started_business_proof",
        "recovery_incomplete_business_proof",
    ):
        assert phase in script
    assert 'write_cutover_state "$recovery_incomplete_phase"' in recovery
    assert "recovery_started_phase=recovery_started_business_proof" in recovery
    business_proof = recovery.rindex(
        'write_cutover_state "$recovery_started_phase"'
    )
    nginx_install = recovery.index("install \"$NGINX_SITE\"", business_proof)
    assert business_proof < nginx_install


def test_alert_canary_evidence_is_bound_to_the_actual_worker_actor():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    inspect_identity = _shell_function_source(
        script,
        "inspect_worker_runtime_identity",
        "run_candidate_alert_canary",
    )
    canary = _shell_function_source(
        script,
        "run_candidate_alert_canary",
        "publish_alert_canary_evidence",
    )

    assert "ASTRA_ALERT_WORKER_ACTOR_ID" in inspect_identity
    assert "ASTRA_RELEASE_COMMIT" in inspect_identity
    assert 'delivery.get("attribution_version") != 1' in canary
    assert 'delivered_by.get("worker_actor_id") != sys.argv[9]' in canary


DATA_PLANE_CHECKER = ROOT / "scripts/assert_production_data_plane_dns.py"
FAKE_DOCKER = r"""
#!/usr/bin/env python3
import json
import sys
from pathlib import Path

spec = json.loads(Path(__file__).with_name("docker-spec.json").read_text(encoding="utf-8"))
args = sys.argv[1:]
log_path = Path(__file__).with_name("docker-calls.log")
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

if args[:2] == ["network", "inspect"]:
    network = spec["networks"].get(args[2])
    if network is None:
        print("network not found", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([network]))
    raise SystemExit(0)

if args and args[0] == "ps":
    filters = []
    index = 1
    while index < len(args):
        if args[index] == "--filter" and index + 1 < len(args):
            filters.append(args[index + 1])
            index += 2
            continue
        index += 1
    key = filters[0] if len(filters) == 1 else "|".join(filters)
    for container_id in spec.get("ps", {}).get(key, []):
        print(container_id)
    raise SystemExit(0)

if args and args[0] == "inspect":
    containers = []
    for container_id in args[1:]:
        container = spec.get("containers", {}).get(container_id)
        if container is None:
            print("no such container", file=sys.stderr)
            raise SystemExit(1)
        containers.append(container)
    print(json.dumps(containers))
    raise SystemExit(0)

if args[:1] == ["exec"] and args[2:4] == ["getent", "hosts"]:
    payload = spec.get("getent", {}).get(args[1], {}).get(args[4], {})
    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    raise SystemExit(payload.get("returncode", 1))

print("forbidden docker command: " + " ".join(args), file=sys.stderr)
raise SystemExit(99)
"""


def _data_plane_container(
    container_id: str,
    name: str,
    *,
    project: str,
    service: str,
    ip: str,
    aliases: list[str],
    network: str = "astra_network",
    network_id: str = "netid",
) -> dict:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            }
        },
        "NetworkSettings": {
            "Networks": {
                network: {
                    "NetworkID": network_id,
                    "IPAddress": ip,
                    "Aliases": aliases,
                }
            }
        },
    }


def _write_fake_docker(tmp_path: Path, spec: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    docker_path = tmp_path / "docker"
    (tmp_path / "docker-spec.json").write_text(json.dumps(spec), encoding="utf-8")
    if sys.platform == "darwin":
        # macOS 26 can indefinitely quarantine freshly-created executable
        # scripts before their shebang interpreter starts.  Keep the fixture
        # semantically identical while executing each Docker subcommand as an
        # ordinary Python input through the already-trusted test interpreter.
        docker_path.symlink_to(sys.executable)
        command_source = FAKE_DOCKER.strip().replace(
            "args = sys.argv[1:]",
            "args = [Path(__file__).name, *sys.argv[1:]]",
        )
        for command in ("network", "ps", "inspect", "exec"):
            (tmp_path / command).write_text(command_source + "\n", encoding="utf-8")
        return docker_path
    docker_path.write_text(FAKE_DOCKER.strip() + "\n", encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IXUSR)
    return docker_path


def _healthy_data_plane_spec(*, getent: dict | None = None) -> dict:
    postgres = _data_plane_container(
        "id-postgres",
        "astra-poc-postgres-1",
        project="astra-poc",
        service="postgres",
        ip="172.18.0.2",
        aliases=["postgres", "astra-poc-postgres-1"],
    )
    redis = _data_plane_container(
        "id-redis",
        "astra-poc-redis-1",
        project="astra-poc",
        service="redis",
        ip="172.18.0.3",
        aliases=["redis", "astra-poc-redis-1"],
    )
    backend = _data_plane_container(
        "id-backend",
        "astra-poc-app-b-backend-1",
        project="astra-poc-app-b",
        service="backend",
        ip="172.18.0.10",
        aliases=["backend", "astra-poc-app-b-backend"],
    )
    containers = {
        "id-postgres": postgres,
        "id-redis": redis,
        "id-backend": backend,
    }
    ps = {
        "network=astra_network": ["id-postgres", "id-redis", "id-backend"],
        "label=com.docker.compose.service=postgres": ["id-postgres"],
        "label=com.docker.compose.service=redis": ["id-redis"],
    }
    return {
        "networks": {
            "astra_network": {"Id": "netid", "Name": "astra_network"},
        },
        "ps": ps,
        "containers": containers,
        "getent": getent
        if getent is not None
        else {
            "id-backend": {
                "postgres": {
                    "stdout": "172.18.0.2\tpostgres\n",
                    "returncode": 0,
                },
                "redis": {
                    "stdout": "172.18.0.3\tredis\n",
                    "returncode": 0,
                },
            }
        },
    }


def _run_data_plane_checker(
    tmp_path: Path, spec: dict, *cli_args: str
) -> subprocess.CompletedProcess[str]:
    docker_path = _write_fake_docker(tmp_path, spec)
    return subprocess.run(
        [
            sys.executable,
            str(DATA_PLANE_CHECKER),
            "--docker",
            str(docker_path),
            *cli_args,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path if sys.platform == "darwin" else ROOT,
    )


def test_production_cutover_operating_procedure_is_registered_and_fail_closed():
    procedure = (
        ROOT / ".agents/workflows/production-cutover-operating-procedure.md"
    ).read_text(encoding="utf-8")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".agents/workflows/deploy-production.md").read_text(
        encoding="utf-8"
    )

    assert ".agents/workflows/production-cutover-operating-procedure.md" in skill
    assert "production-cutover-operating-procedure.md" in workflow
    assert "REQUEST CHANGES" in procedure
    assert "--no-deps" in procedure
    assert "astra-poc" in procedure
    assert "SMOKE_TENANT_" in procedure
    assert "cutover-state" in procedure
    assert "已经改了" in procedure
    assert len(skill.splitlines()) < 250


def test_slot_compose_up_never_publishes_a_second_postgres_dns_name():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )

    assert "ALLOW_DIRTY" not in script
    assert "working tree is dirty; production releases require a reviewed commit" in script
    assert "postgres:\n        condition: service_healthy" in compose
    assert "redis:\n        condition: service_healthy" in compose

    up_lines = [
        line.strip()
        for line in script.splitlines()
        if re.search(r"\bup -d\b", line)
    ]
    assert up_lines
    for line in up_lines:
        assert "--no-deps" in line
        assert "postgres" not in line
        assert "redis" not in line

    assert 'compose_project "$COMPOSE_PROJECT"' in script
    assert "exec -T postgres" in script
    assert not re.search(
        r'compose_project "\$CANDIDATE_PROJECT".*up(?: -d)?(?: --no-deps)? postgres',
        script,
        flags=re.DOTALL,
    )
    assert not re.search(
        r'compose_project "\$OLD_PROJECT".*up(?: -d)?(?: --no-deps)? postgres',
        script,
        flags=re.DOTALL,
    )


def test_production_data_plane_preflight_is_local_and_remote_and_read_only():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    checker = DATA_PLANE_CHECKER.read_text(encoding="utf-8")

    local = script.index('echo "[local] verifying unique production data-plane DNS"')
    remote = script.index('echo "[remote] verifying unique production data-plane DNS"')
    upload = script.index('echo "[remote] uploading package"')
    alembic = script.index('echo "[local] checking Alembic heads"')
    mcp = script.index('echo "[remote] verifying host-level MCP egress contract"')
    recovery = script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', mcp)

    assert local < alembic < upload < remote < mcp < recovery
    assert '< "$ROOT_DIR/scripts/assert_production_data_plane_dns.py"' in script
    assert 'python3 "$RELEASE/scripts/assert_production_data_plane_dns.py"' in script
    assert '--expected-network "$DOCKER_NETWORK_NAME"' in script
    assert "--app-root" in script
    assert "docker start" not in checker
    assert "docker stop" not in checker
    assert "docker rm" not in checker
    assert "compose up" not in checker


def test_data_plane_dns_preflight_accepts_unique_shared_postgres(tmp_path):
    result = _run_data_plane_checker(
        tmp_path,
        _healthy_data_plane_spec(),
        "--network",
        "astra_network",
        "--compose-project",
        "astra-poc",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    calls = [
        json.loads(line)
        for line in (tmp_path / "docker-calls.log").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert calls
    assert all(call[0] in {"network", "ps", "inspect", "exec"} for call in calls)
    assert not any(call[0] in {"start", "stop", "rm", "kill", "compose"} for call in calls)


def test_data_plane_dns_preflight_ignores_other_products_on_other_networks(tmp_path):
    spec = _healthy_data_plane_spec()
    spec["containers"]["id-quant-postgres"] = _data_plane_container(
        "id-quant-postgres",
        "quantagent-postgres-1",
        project="quantagent",
        service="postgres",
        ip="172.19.0.2",
        aliases=["postgres", "quantagent-postgres-1"],
        network="quantagent_default",
        network_id="quant-netid",
    )
    spec["containers"]["id-quant-redis"] = _data_plane_container(
        "id-quant-redis",
        "quantagent-redis-1",
        project="quantagent",
        service="redis",
        ip="172.19.0.3",
        aliases=["redis", "quantagent-redis-1"],
        network="quantagent_default",
        network_id="quant-netid",
    )
    spec["ps"]["label=com.docker.compose.service=postgres"].append(
        "id-quant-postgres"
    )
    spec["ps"]["label=com.docker.compose.service=redis"].append("id-quant-redis")

    result = _run_data_plane_checker(
        tmp_path,
        spec,
        "--network",
        "astra_network",
        "--compose-project",
        "astra-poc",
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda spec: spec["ps"].__setitem__(
                "network=astra_network",
                ["id-postgres", "id-slot-postgres", "id-redis", "id-backend"],
            )
            or spec["ps"].__setitem__(
                "label=com.docker.compose.service=postgres",
                ["id-postgres", "id-slot-postgres"],
            )
            or spec["containers"].__setitem__(
                "id-slot-postgres",
                _data_plane_container(
                    "id-slot-postgres",
                    "astra-poc-app-a-postgres-1",
                    project="astra-poc-app-a",
                    service="postgres",
                    ip="172.18.0.22",
                    aliases=["postgres", "astra-poc-app-a-postgres-1"],
                ),
            ),
            "slot compose must not publish postgres DNS",
        ),
        (
            lambda spec: spec["ps"].__setitem__(
                "network=astra_network",
                ["id-redis", "id-backend"],
            )
            or spec["ps"].__setitem__(
                "label=com.docker.compose.service=postgres",
                [],
            )
            or spec["containers"].pop("id-postgres", None),
            "exactly one 'postgres' alias",
        ),
        (
            lambda spec: spec["getent"].__setitem__(
                "id-backend",
                {
                    "postgres": {
                        "stdout": "172.18.0.2\tpostgres\n172.18.0.22\tpostgres\n",
                        "returncode": 0,
                    },
                    "redis": {
                        "stdout": "172.18.0.3\tredis\n",
                        "returncode": 0,
                    },
                },
            ),
            "resolved 2 addresses for 'postgres'",
        ),
    ],
    ids=["dual_postgres", "zero_postgres", "split_getent"],
)
def test_data_plane_dns_preflight_fails_closed(tmp_path, mutate, needle):
    spec = _healthy_data_plane_spec()
    mutate(spec)
    result = _run_data_plane_checker(
        tmp_path,
        spec,
        "--network",
        "astra_network",
        "--compose-project",
        "astra-poc",
    )

    assert result.returncode == 1, result.stderr
    assert needle in result.stderr
    assert "supersecret" not in result.stderr
    assert "POSTGRES_PASSWORD" not in result.stderr


def test_data_plane_dns_preflight_reads_current_env_and_redacts_secrets(tmp_path):
    app_root = tmp_path / "app"
    release = app_root / "releases" / "live"
    release.mkdir(parents=True)
    (release / ".env").write_text(
        'POSTGRES_PASSWORD="supersecret"\nDOCKER_NETWORK="astra_network"\n',
        encoding="utf-8",
    )
    (app_root / "current").symlink_to(release)
    spec = _healthy_data_plane_spec()
    spec["ps"]["network=astra_network"] = ["id-redis", "id-backend"]
    spec["ps"]["label=com.docker.compose.service=postgres"] = []
    spec["containers"].pop("id-postgres")

    result = _run_data_plane_checker(
        tmp_path / "docker-home",
        spec,
        "--app-root",
        str(app_root),
        "--compose-project",
        "astra-poc",
        "--expected-network",
        "astra_network",
    )

    assert result.returncode == 1, result.stderr
    assert "exactly one 'postgres' alias" in result.stderr
    assert "supersecret" not in result.stdout
    assert "supersecret" not in result.stderr


def test_data_plane_dns_preflight_skips_missing_getent_when_inspect_is_unique(tmp_path):
    spec = _healthy_data_plane_spec(
        getent={
            "id-backend": {
                "postgres": {
                    "stderr": "getent: not found\n",
                    "returncode": 127,
                },
                "redis": {
                    "stderr": "executable file not found\n",
                    "returncode": 127,
                },
            }
        }
    )
    result = _run_data_plane_checker(
        tmp_path,
        spec,
        "--network",
        "astra_network",
        "--compose-project",
        "astra-poc",
    )

    assert result.returncode == 0, result.stderr
