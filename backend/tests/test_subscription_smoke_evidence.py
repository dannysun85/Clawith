import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]
API_RUNNER = ROOT / "scripts/subscription_production_smoke.py"
MERGER = ROOT / "scripts/merge_subscription_smoke_evidence.py"


def _load_api_runner():
    spec = importlib.util.spec_from_file_location("subscription_production_smoke", API_RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_smoke_credentials_require_an_exact_regular_json_file(tmp_path):
    runner = _load_api_runner()
    credentials = {
        "SMOKE_TENANT_EMAIL": "tenant@example.com",
        "SMOKE_TENANT_PASSWORD": "tenant-secret",
        "SMOKE_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "SMOKE_PLATFORM_ADMIN_EMAIL": "admin@example.com",
        "SMOKE_PLATFORM_ADMIN_PASSWORD": "admin-secret",
    }
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")

    assert runner.load_credentials(str(path)) == credentials

    path.write_text(json.dumps({**credentials, "EXTRA": "forbidden"}), encoding="utf-8")
    with pytest.raises(runner.SmokeFailure, match="credentials_file"):
        runner.load_credentials(str(path))

    target = tmp_path / "target.json"
    target.write_text(json.dumps(credentials), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(runner.SmokeFailure, match="credentials_file"):
        runner.load_credentials(str(path))


def _api_evidence(commit: str, release_id: str, nonce: str) -> dict:
    return {
        "evidence_schema_version": 2,
        "evidence_kind": "subscription_api",
        "ok": True,
        "api_base": "http://127.0.0.1:3009/api",
        "frontend_url": "http://127.0.0.1:3009",
        "release_identity": {
            "version": "1.10.12",
            "commit": commit,
            "release_id": release_id,
        },
        "evidence_nonce": nonce,
        "checks": [
            "candidate_release_identity_ok",
            "tenant_login_ok",
            "tenant_me_ok",
            "tenant_scope_ok",
            "client_plans_ok",
            "client_subscription_summary_ok",
            "client_credit_transactions_ok",
            "client_orders_ok",
            "client_credit_packs_ok",
            "work_executor_preflight_ok",
            "platform_admin_login_ok",
            "saas_ledger_reconciliation_ok",
            "saas_payment_reconciliation_ok",
            "orders_csv_export_ok",
            "credit_transactions_csv_export_ok",
        ],
        "subscription_summary": {
            "plan_code": "pro",
            "balance": 100,
            "available_balance": 90,
            "reserved": 10,
        },
        "work_executor_preflight": {
            "capability_status": "available",
            "reason_count": 0,
        },
        "saas_ledger_reconciliation": {
            "checked_tenants": 4,
            "issue_count": 0,
        },
        "saas_payment_reconciliation": {
            "checked_orders": 2,
            "issue_count": 0,
        },
    }


def _ui_evidence(commit: str, release_id: str, nonce: str) -> dict:
    return {
        "evidence_schema_version": 2,
        "evidence_kind": "subscription_browser",
        "ok": True,
        "frontend_url": "http://127.0.0.1:3009",
        "browser_target": "isolated_candidate_frontend_network",
        "release_identity": {
            "version": "1.10.12",
            "commit": commit,
            "release_id": release_id,
        },
        "evidence_nonce": nonce,
        "final_path": "/account/subscription",
        "checks": [
            "ui_release_identity_ok",
            "ui_tenant_login_ok",
            "ui_tenant_scope_ok",
            "ui_subscription_summary_api_ok",
            "ui_subscription_balance_rendered_ok",
            "ui_subscription_page_ok",
            "ui_no_server_error_ok",
        ],
        "subscription_summary": {
            "plan_code": "pro",
            "balance": 100,
            "available_balance": 90,
            "reserved": 10,
        },
    }


def _merge(tmp_path: Path, api: dict, ui: dict, *, bundle: str | None = None):
    api_path = tmp_path / "api.json"
    ui_path = tmp_path / "ui.json"
    api_path.write_text(json.dumps(api), encoding="utf-8")
    ui_path.write_text(json.dumps(ui), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(MERGER),
            "--api-evidence",
            str(api_path),
            "--ui-evidence",
            str(ui_path),
            "--api-base",
            "http://127.0.0.1:3009/api",
            "--frontend-url",
            "http://127.0.0.1:3009",
            "--expected-version",
            "1.10.12",
            "--expected-commit",
            "a" * 40,
            "--expected-release-id",
            "release-v2",
            "--evidence-nonce",
            "1" * 32,
            "--runner-bundle-sha256",
            bundle or f"sha256:{'2' * 64}",
            "--browser-image-id",
            f"sha256:{'3' * 64}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_composite_smoke_evidence_requires_matching_api_and_browser_proof(tmp_path):
    commit = "a" * 40
    release_id = "release-v2"
    nonce = "1" * 32
    api = _api_evidence(commit, release_id, nonce)
    ui = _ui_evidence(commit, release_id, nonce)

    valid = _merge(tmp_path, api, ui)
    assert valid.returncode == 0, valid.stderr
    combined = json.loads(valid.stdout)
    assert combined["evidence_kind"] == "subscription_composite"
    assert combined["release_identity"]["release_id"] == release_id
    assert combined["ui"]["final_path"] == "/account/subscription"
    assert combined["saas_ledger_reconciliation"] == {
        "checked_tenants": 4,
        "issue_count": 0,
    }
    assert combined["saas_payment_reconciliation"] == {
        "checked_orders": 2,
        "issue_count": 0,
    }

    missing_check = _merge(tmp_path, api, {**ui, "checks": ui["checks"][:-1]})
    assert missing_check.returncode != 0
    missing_api_scope = _merge(
        tmp_path,
        {**api, "checks": [check for check in api["checks"] if check != "tenant_scope_ok"]},
        ui,
    )
    assert missing_api_scope.returncode != 0
    missing_ui_scope = _merge(
        tmp_path,
        api,
        {**ui, "checks": [check for check in ui["checks"] if check != "ui_tenant_scope_ok"]},
    )
    assert missing_ui_scope.returncode != 0
    mismatched_summary = _merge(
        tmp_path,
        api,
        {
            **ui,
            "subscription_summary": {**ui["subscription_summary"], "available_balance": 89},
        },
    )
    assert mismatched_summary.returncode != 0
    malformed_bundle = _merge(tmp_path, api, ui, bundle="sha256:short")
    assert malformed_bundle.returncode != 0

    unsafe_issue_rows = _merge(
        tmp_path,
        {
            **api,
            "saas_ledger_reconciliation": {
                **api["saas_ledger_reconciliation"],
                "issues": [{"tenant_id": "must-not-enter-evidence"}],
            },
        },
        ui,
    )
    assert unsafe_issue_rows.returncode != 0


def test_api_reconciliation_gate_fails_closed_without_leaking_issue_rows():
    runner = _load_api_runner()
    sensitive_issue = {
        "code": "balance_drift",
        "tenant_id": "private-tenant-id",
        "expected": 1000,
        "actual": 490,
    }

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner.summarize_reconciliation(
            {"checked_tenants": 1, "issues": [sensitive_issue]},
            checked_field="checked_tenants",
            stage="saas_ledger_reconciliation",
        )

    assert exc_info.value.detail == {
        "code": "reconciliation_issues_detected",
        "issue_count": 1,
    }
    assert "private-tenant-id" not in repr(exc_info.value.detail)
    assert runner.summarize_reconciliation(
        {"checked_orders": 0, "issues": []},
        checked_field="checked_orders",
        stage="saas_payment_reconciliation",
    ) == {"checked_orders": 0, "issue_count": 0}


def test_work_executor_preflight_gate_requires_available_without_leaking_agent_ids():
    runner = _load_api_runner()
    assert runner.summarize_work_executor_preflight(
        {
            "capability_status": "available",
            "reasons": [],
            "confirmation_fingerprint": "a" * 64,
            "work_statement": {"executor": {"agent_id": "private-agent-id"}},
        }
    ) == {"capability_status": "available", "reason_count": 0}

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner.summarize_work_executor_preflight(
            {
                "capability_status": "unavailable",
                "reasons": ["text_route_unavailable:private-agent-id"],
                "confirmation_fingerprint": "b" * 64,
            }
        )

    assert exc_info.value.detail == {
        "code": "personal_assistant_route_unavailable",
        "capability_status": "unavailable",
        "reason_count": 1,
    }
    assert "private-agent-id" not in repr(exc_info.value.detail)


def test_login_sends_an_explicit_tenant_id(monkeypatch):
    runner = _load_api_runner()
    tenant_id = "11111111-1111-4111-8111-111111111111"
    calls = []

    def fake_call_api(method, api_base, path, data):
        calls.append((method, api_base, path, data))
        return 200, {"access_token": "tenant-token"}

    monkeypatch.setattr(runner, "call_api", fake_call_api)

    result = runner.login(
        "https://candidate.example/api",
        "tenant@example.com",
        "secret",
        "tenant_login",
        tenant_id=tenant_id,
    )

    assert result == {"access_token": "tenant-token"}
    assert calls == [
        (
            "POST",
            "https://candidate.example/api",
            "/auth/login",
            {
                "login_identifier": "tenant@example.com",
                "password": "secret",
                "tenant_id": tenant_id,
            },
        )
    ]


def test_platform_admin_login_retries_with_the_smoke_tenant(monkeypatch):
    runner = _load_api_runner()
    tenant_id = "11111111-1111-4111-8111-111111111111"
    responses = iter(
        [
            (
                200,
                {
                    "requires_tenant_selection": True,
                    "tenants": [{"tenant_id": tenant_id, "tenant_name": "Default"}],
                },
            ),
            (200, {"access_token": "admin-token"}),
        ]
    )
    requests = []

    def fake_call_api(method, api_base, path, data):
        requests.append(data)
        return next(responses)

    monkeypatch.setattr(runner, "call_api", fake_call_api)

    result = runner.login(
        "https://candidate.example/api",
        "admin@example.com",
        "secret",
        "platform_admin_login",
        tenant_fallback_id=tenant_id,
    )

    assert result == {"access_token": "admin-token"}
    assert requests == [
        {"login_identifier": "admin@example.com", "password": "secret"},
        {
            "login_identifier": "admin@example.com",
            "password": "secret",
            "tenant_id": tenant_id,
        },
    ]


def test_platform_admin_login_fails_closed_when_smoke_tenant_is_unavailable(monkeypatch):
    runner = _load_api_runner()

    monkeypatch.setattr(
        runner,
        "call_api",
        lambda method, api_base, path, data: (
            200,
            {
                "requires_tenant_selection": True,
                "tenants": [
                    {
                        "tenant_id": "22222222-2222-4222-8222-222222222222",
                        "tenant_name": "Another tenant",
                    }
                ],
            },
        ),
    )

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner.login(
            "https://candidate.example/api",
            "admin@example.com",
            "secret",
            "platform_admin_login",
            tenant_fallback_id="11111111-1111-4111-8111-111111111111",
        )

    assert exc_info.value.detail == {"code": "target_tenant_not_available"}


def test_login_failure_does_not_copy_the_response_body(monkeypatch):
    runner = _load_api_runner()
    sensitive_body = {"detail": "private-provider-or-customer-response"}

    monkeypatch.setattr(
        runner,
        "call_api",
        lambda method, api_base, path, data: (401, sensitive_body),
    )

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner.login(
            "https://candidate.example/api",
            "tenant@example.com",
            "secret",
            "tenant_login",
        )

    assert exc_info.value.detail == {
        "code": "unexpected_login_response",
        "status": 401,
    }
    assert sensitive_body["detail"] not in repr(exc_info.value.detail)
