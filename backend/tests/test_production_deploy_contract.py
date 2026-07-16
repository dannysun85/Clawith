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
            "approval_schema_forward_state() { printf '%s' \"${SCHEMA_FORWARD_STATE:-1}\"; }",
            _shell_function_source(
                script,
                "project_for_slot",
                "count_established_connections",
            ),
            _shell_function_source(
                script,
                "release_for_slot",
                "wait_for_worker_release",
            ),
        ]
    )
    recovery_start = script.index("recover_indeterminate_cutover() {")
    recovery_end = script.index("\nif ! load_active_state", recovery_start)
    return port_function, recovery_helpers, script[recovery_start:recovery_end]


def test_production_compose_splits_api_and_worker_roles_and_shares_durable_volume():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "API_PROCESS_ROLE:-api,bootstrap" in compose
    assert "WORKER_PROCESS_ROLE:-worker,connector" in compose
    assert "AGENT_DATA_VOLUME:-astra-poc_agentdata" in compose
    assert "BACKEND_NETWORK_ALIAS" in compose


def test_production_code_execution_defaults_fail_closed():
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert 'CODE_EXECUTION_ENABLED: "false"' in compose
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
    version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip("docker compose plugin is not installed")

    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "contract-test",
        "SECRET_KEY": "contract-test-secret",
        "JWT_SECRET_KEY": "contract-test-jwt",
        "CORS_ORIGINS": "https://example.test",
        "PUBLIC_BASE_URL": "https://example.test",
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
            "docker", "compose", "-f",
            "deploy/astra-poc/docker-compose.prod.yml",
            "config", "--format", "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
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


def test_production_release_gate_covers_code_execution_security():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".agents/workflows/deploy-production.md").read_text(encoding="utf-8")

    assert '(cd backend && uv run pytest -q)' in script
    assert "production releases cannot disable local release gates" in script
    assert "scripts/ruff_diff_gate.py" in script
    assert "git describe --tags --abbrev=0 HEAD^" in script
    assert '--base "$RELEASE_BASE_COMMIT" --target HEAD' in script
    assert '"$RELEASE/BASE_COMMIT"' in script
    assert "bash scripts/postgres-migration-smoke.sh" in script
    assert "docker compose -f deploy/astra-poc/docker-compose.prod.yml config --quiet" in script
    assert 'git archive --format=tar --output="$PACKAGE_TAR" "$COMMIT"' in script
    assert 'git get-tar-commit-id < "$PACKAGE_TAR"' in script
    assert 'gzip -n -c "$PACKAGE_TAR" > "$PACKAGE"' in script
    assert '"$PACKAGE_SHA256" <<\'REMOTE_SCRIPT\'' in script
    assert "release package digest mismatch" in script
    assert 'write_atomic_line "$RELEASE/PACKAGE_SHA256" "$PACKAGE_SHA256"' in script
    assert script.index('echo "[local] running full backend suite"') < script.index(
        'git archive --format=tar'
    ) < script.index(
        'echo "[remote] uploading package"'
    )
    assert script.index("release package digest mismatch") < script.index(
        'tar -xzf "$PACKAGE" -C "$RELEASE"'
    )
    assert "Code 激活状态为\n`BLOCKED`" in workflow
    assert "精确 tenant UUID" in workflow
    assert "生产禁止 `subprocess`、`docker`" in workflow


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
    assert "subscription-production-smoke.sh" in script
    consume = script.index("consume_break_glass_approval.py")
    recovery = script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', consume)
    assert consume < recovery


def test_mcp_host_egress_guard_is_a_pre_mutation_release_gate():
    deploy_script = (ROOT / "scripts/deploy-astra-production.sh").read_text(
        encoding="utf-8"
    )
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
    # POSIX awk implementations reserve `index` as a built-in function name.
    # Using it as a loop variable passed source inspection but failed on the
    # Ubuntu production host before the guard could be installed.
    assert "for (index =" not in guard
    assert "for (field_idx =" in guard
    assert "public-port allowlist" in contract
    assert "astra-mcp-egress-guard.timer" in guard
    assert "OnUnitActiveSec=30s" in guard
    assert "Normal application deployment only verifies this contract" in contract

    gate = deploy_script.index(
        'echo "[remote] verifying host-level MCP egress contract"'
    )
    recovery = deploy_script.index('if [ "$RECOVERY_REQUIRED" = "1" ]', gate)
    backup = deploy_script.index('echo "[remote] backing up database')
    maintenance = deploy_script.index(
        'echo "[remote] enabling explicit Web/API/WebSocket maintenance fence"',
        gate,
    )
    migration = deploy_script.index("backend upgrade head", maintenance)
    assert gate < recovery < backup < maintenance < migration
    assert 'manage-production-mcp-egress-guard.sh" verify' in deploy_script


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
    restore = script[restore_start:script.index("project_for_slot() {", restore_start)]
    assert quarantine.index("CREATE TRIGGER astra_deploy_mcp_quarantine_tools_guard") < (
        quarantine.index("UPDATE tools")
    )
    assert restore.index("SET LOCAL astra.mcp_quarantine_restore") < restore.index(
        "UPDATE tools AS tool"
    )
    assert restore.index("UPDATE agent_tools AS assignment") < restore.index(
        "DELETE FROM astra_deploy_mcp_quarantine_state"
    )
    assert restore.index("DELETE FROM astra_deploy_mcp_quarantine_state") < (
        restore.index("DROP TRIGGER")
    )

    trap = script.index("trap 'on_error $?' ERR")
    build = script.index('echo "[remote] building candidate slot')
    migration_quarantine = script.index(
        '"$PREVIOUS" "migration-${RELEASE_ID}"',
        build,
    )
    migration_gate = script.index(
        "ROLLBACK_REQUIRES_MCP_QUARANTINE=1",
        migration_quarantine,
    )
    migration = script.index("backend upgrade head", migration_gate)
    assert "ROLLBACK_REQUIRES_MCP_QUARANTINE=0" in script[:trap]
    assert trap < build < migration_quarantine < migration_gate < migration

    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    assert 'if [ "$ROLLBACK_REQUIRES_MCP_QUARANTINE" = "1" ]' in rollback
    assert rollback.index("ROLLBACK_REQUIRES_MCP_QUARANTINE") < rollback.index(
        "quarantine_mcp_for_unsafe_release"
    )


def test_pre_migration_rollback_never_quarantines_live_mcp(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    harness = f"""set -eu
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / 'missing-smoke'))}
PREVIOUS={shlex.quote(str(tmp_path / 'previous'))}
PREVIOUS_RELEASE_ID=release-old
PREVIOUS_VERSION=1.10.11
PREVIOUS_COMMIT=old1234
RELEASE={shlex.quote(str(tmp_path / 'candidate'))}
RELEASE_ID=release-new
ACTIVE_SLOT=b
CANDIDATE_SLOT=a
ROLLBACK_REQUIRES_MCP_QUARANTINE=0
CANDIDATE_READY_FOR_FALLBACK=0
NGINX_CONFIG_TOUCHED=0
CURRENT={shlex.quote(str(tmp_path / 'current'))}
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


def test_forward_only_rollback_preserves_maintenance_and_never_starts_old_code(tmp_path):
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    rollback_start = script.index("rollback() {")
    rollback_end = script.index("abort_release() {", rollback_start)
    rollback = script[rollback_start:rollback_end]
    harness = f"""set +e
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / 'missing-smoke'))}
PREVIOUS={shlex.quote(str(tmp_path / 'previous'))}
PREVIOUS_RELEASE_ID=release-old
RELEASE={shlex.quote(str(tmp_path / 'candidate'))}
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
    assert '(cd backend && uv run pytest -q)' in script
    assert "JWT_ROTATION_MARKER" in script
    assert 'install "$NGINX_SITE" "$OLD_PORT" "$CANDIDATE_PORT"' in script
    assert '"$RELEASE/scripts/configure_production_nginx.py"' in script
    configurator = NGINX_CONFIGURATOR.read_text(encoding="utf-8")
    assert "astra_no_args" in configurator
    assert "if len(old_matches) == 1 and not candidate_matches:" in configurator
    assert "audit_effective_config" in configurator
    assert "active_upstream_port" in configurator
    assert "run --rm --no-deps -T --entrypoint alembic backend upgrade head < /dev/null" in script


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
    location / {{ proxy_pass http://127.0.0.1:3009; }}
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
        location / {{ proxy_pass http://127.0.0.1:3009; }}
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
    location / {{ proxy_pass http://127.0.0.1:3009; }}
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
    assert 'NGINX_CONFIG_TOUCHED=0' in script[:rollback_start]
    assert 'install "$NGINX_SITE" "$CANDIDATE_PORT" "$OLD_PORT"' in restore
    assert '"$NGINX_LOG_FORMAT"' in restore
    assert " restore " not in restore
    assert "reload_nginx_with_worker_snapshot" in restore
    assert "ensure_old_application_ready" in rollback
    assert "restore_previous_nginx" in rollback
    assert "candidate remains running" in rollback

    site_backup = script.index('sudo cp "$NGINX_SITE" "$NGINX_BACKUP"')
    format_backup = script.index(
        'sudo cp "$NGINX_LOG_FORMAT" "$NGINX_LOG_FORMAT_BACKUP"'
    )
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


def test_production_deploy_error_trap_is_terminal():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    handler_start = script.index("on_error() {")
    trap_line = "trap 'on_error $?' ERR"
    handler_end = script.index(trap_line, handler_start) + len(trap_line)
    handler = script[handler_start:handler_end]
    harness = f"""set -e
rollback() {{ echo rollback_called; return 0; }}
{handler}
false
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
CURRENT={shlex.quote(str(tmp_path / 'current'))}
ACTIVE_SLOT_FILE={shlex.quote(str(tmp_path / 'active-slot'))}
ACTIVE_RELEASE_FILE={shlex.quote(str(tmp_path / 'active-release'))}
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
    assert (
        'abort_release "public cutover did not expose expected release '
        '$VERSION/$COMMIT"'
    ) in script
    assert 'abort_release "remote smoke environment file is missing"' in script


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
    assert 'abort_release "public cutover did not expose expected release' in script[
        public_gate:
    ]


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
RELEASE={shlex.quote(str(app_root / 'releases' / 'incoming-release'))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(active_release))}
ACTIVE_STATE_FILE={shlex.quote(str(app_root / 'active-state'))}
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
    test "$2" = {shlex.quote(str(target_release / '.env'))}
    test "$4" = candidate-release
    echo old_worker_stopped
    WORKER_SLOT=a
    echo target_worker_started
}}
compose_project() {{
    local project="$1"
    local env_file="$2"
    if [ "$project" = astra-app-a ]; then
        test "$env_file" = {shlex.quote(str(target_release / '.env'))}
    elif [ "$project" = astra-app-b ]; then
        test "$env_file" = {shlex.quote(str(fallback_release / '.env'))}
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
RELEASE={shlex.quote(str(app_root / 'releases' / 'incoming-release'))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(app_root / 'current'))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(app_root / 'active-release'))}
CUTOVER_STATE_FILE={shlex.quote(str(app_root / 'cutover-state'))}
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
RELEASE={shlex.quote(str(app_root / 'releases' / 'incoming-release'))}
NGINX_SITE=/etc/nginx/sites-enabled/astra-poc.conf
NGINX_LOG_FORMAT=/etc/nginx/conf.d/00-astra-log-redaction.conf
CURRENT={shlex.quote(str(current))}
ACTIVE_SLOT_FILE={shlex.quote(str(active_slot))}
ACTIVE_RELEASE_FILE={shlex.quote(str(app_root / 'active-release'))}
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
        test "$env_file" = {shlex.quote(str(target_release / '.env'))} || return 1
    elif [ "$project" = astra-app-b ]; then
        test "$env_file" = {shlex.quote(str(fallback_release / '.env'))} || return 1
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
    recovery_end = script.index("\nif ! load_active_state", recovery_start)
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

    candidate_journal = script.index(
        'write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"'
    )
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
CURRENT={shlex.quote(str(tmp_path / 'current'))}
ACTIVE_SLOT_FILE={shlex.quote(str(tmp_path / 'active-slot'))}
ACTIVE_RELEASE_FILE={shlex.quote(str(tmp_path / 'active-release'))}
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
    public_identity = script.index(
        'wait_for_public_release "$VERSION" "$COMMIT"', public_gate
    )
    retirement = script.index("if ! retire_pre_reload_nginx_workers", public_identity)
    active_commit = script.index(
        'commit_active_state "$CANDIDATE_SLOT" "$RELEASE_ID"', retirement
    )

    assert script.count("sudo systemctl reload nginx") == 1
    assert "NGINX_RELOAD_OLD_WORKERS=" in helper
    assert helper.index("NGINX_RELOAD_OLD_WORKERS=") < helper.index(
        "sudo systemctl reload nginx"
    )
    assert "sudo kill -TERM" in helper
    assert "sudo systemctl is-active --quiet nginx" in helper
    assert '${NGINX_WORKER_GRACE_SECONDS:-$DRAIN_TIMEOUT_SECONDS}' in helper
    assert candidate_worker < cutover_start < cutover < reloaded_state
    assert reloaded_state < public_gate < public_identity < retirement < active_commit


def test_maintenance_writer_fence_is_durable_before_schema_migration():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    candidate_journal = script.index(
        'write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"'
    )
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

    assert (
        candidate_journal
        < maintenance
        < maintenance_state
        < old_stop
        < migration_state
        < migration
        < schema_state
    )
    assert (
        'write_atomic_line "$PENDING_DRAIN_FILE" '
        '"$OLD_PROJECT $OLD_PORT $PREVIOUS"'
    ) not in script[candidate_journal:]


def test_production_deploy_does_not_rebuild_the_live_project_in_place():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    assert 'compose .env "$COMPOSE_FILE" up -d --build' not in script
    assert 'build backend frontend' in script
    assert 'stop frontend backend' in script


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
    current_read = script.index('CURRENT_TARGET="$(python3 -c')
    release_create = script.index('mkdir -p "$RELEASE" "$BACKUP"')
    assert lock < lock_acquired < current_read < release_create
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
        ("empty_journal", 1),
        ("directory_journal", 1),
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
    elif mutation == "empty_journal":
        slot_journal.touch()
    elif mutation == "directory_journal":
        slot_journal.mkdir()
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
        "release_for_slot",
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
ACTIVE_STATE_FILE={shlex.quote(str(app_root / 'active-state'))}
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
            assert slot_journal.read_text(encoding="utf-8") == (
                f"{str(release_b)[:-1]}\n{str(release_b)[-1:]}\n"
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
        "release_for_slot",
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
    worker_start = script.index("wait_for_worker_release() {")
    worker_end = script.index("remaining_old_nginx_workers() {", worker_start)
    worker_functions = script[worker_start:worker_end]
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
                printf '%s\n' ASTRA_RELEASE_ID=release-b PROCESS_ROLE=worker,connector
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
    marker_state = (
        expected
        if matches_active_release
        else f"astra-app-a 3008 {tmp_path / 'release-a'}"
    )
    marker.write_text(f"{marker_state}\n", encoding="utf-8")
    harness = f"""set +e
PENDING_DRAIN_FILE={shlex.quote(str(marker))}
OLD_PROJECT=astra-app-b
OLD_PORT=3009
PREVIOUS={shlex.quote(str(tmp_path / 'release-b'))}
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


def test_candidate_journal_precedes_writer_fence_and_identity_gate():
    script = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")
    journal = script.index(
        'write_atomic_line "$APP_ROOT/slot-${CANDIDATE_SLOT}-release" "$RELEASE"'
    )
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
SMOKE_ENV_FILE={shlex.quote(str(tmp_path / 'smoke'))}
PREVIOUS={shlex.quote(str(tmp_path / 'previous'))}
PREVIOUS_RELEASE_ID=release-old
PREVIOUS_VERSION=1.10.11
PREVIOUS_COMMIT=old1234
RELEASE={shlex.quote(str(tmp_path / 'candidate'))}
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
