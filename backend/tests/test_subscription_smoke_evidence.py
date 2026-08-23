import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

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
        "SMOKE_MEMBER_EMAIL": "member@example.com",
        "SMOKE_MEMBER_PASSWORD": "member-secret",
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
        "evidence_schema_version": 3,
        "evidence_kind": "release_business_api",
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
            "tenant_billing_manage_capability_ok",
            "billing_manual_semantics_ok",
            "member_login_ok",
            "member_personal_usage_ok",
            "member_sensitive_billing_denied_ok",
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
        ],
        "subscription_summary": {
            "plan_code": "pro",
            "balance": 100,
            "available_balance": 90,
            "reserved": 10,
        },
        "billing_mode": {
            "provider": "manual",
            "status": "manual",
            "checkout_enabled": True,
            "native_payment_enabled": False,
            "webhook_ready": False,
        },
        "business_flow": {
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
        "evidence_schema_version": 3,
        "evidence_kind": "release_business_browser",
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
        "tolerated_runtime_state_conflicts": 1,
        "checks": [
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
            "ui_no_unexpected_console_error_ok",
            "ui_no_server_error_ok",
        ],
        "subscription_summary": {
            "plan_code": "pro",
            "balance": 100,
            "available_balance": 90,
            "reserved": 10,
        },
        "business_flow": {
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
            "release-v3",
            "--evidence-nonce",
            "1" * 32,
            "--runner-bundle-sha256",
            bundle or f"sha256:{'2' * 64}",
            "--browser-image-id",
            f"sha256:{'3' * 64}",
            "--qa-tooling-release-id",
            "qa-tooling-release",
            "--qa-tooling-commit",
            "b" * 40,
            "--qa-tooling-package-sha256",
            "4" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_composite_smoke_evidence_requires_matching_api_and_browser_proof(tmp_path):
    commit = "a" * 40
    release_id = "release-v3"
    nonce = "1" * 32
    api = _api_evidence(commit, release_id, nonce)
    ui = _ui_evidence(commit, release_id, nonce)

    valid = _merge(tmp_path, api, ui)
    assert valid.returncode == 0, valid.stderr
    combined = json.loads(valid.stdout)
    assert combined["evidence_kind"] == "release_business_composite"
    assert combined["release_identity"]["release_id"] == release_id
    assert combined["qa_tooling_identity"] == {
        "release_id": "qa-tooling-release",
        "commit": "b" * 40,
        "package_sha256": "4" * 64,
    }
    assert combined["ui"]["final_path"] == "/account/subscription"
    assert combined["ui"]["tolerated_runtime_state_conflicts"] == 1
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
    duplicate_credit_charge = _merge(
        tmp_path,
        {
            **api,
            "business_flow": {
                **api["business_flow"],
                "credits": {
                    **api["business_flow"]["credits"],
                    "replay_transaction_delta": 1,
                },
            },
        },
        ui,
    )
    assert duplicate_credit_charge.returncode != 0
    missing_chat_recovery = _merge(
        tmp_path,
        api,
        {
            **ui,
            "business_flow": {
                **ui["business_flow"],
                "direct_chat": {
                    **ui["business_flow"]["direct_chat"],
                    "durable_after_reload": False,
                },
            },
        },
    )
    assert missing_chat_recovery.returncode != 0

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


def test_manual_billing_and_credit_snapshots_are_fail_closed_and_identity_free():
    runner = _load_api_runner()
    assert runner.summarize_manual_billing_config(
        {
            "provider": "manual",
            "status": "manual",
            "checkout_enabled": True,
            "native_payment_enabled": False,
            "webhook_ready": False,
            "next_action": "private operator copy is intentionally excluded",
        }
    ) == {
        "provider": "manual",
        "status": "manual",
        "checkout_enabled": True,
        "native_payment_enabled": False,
        "webhook_ready": False,
    }

    with pytest.raises(runner.SmokeFailure, match="billing_manual_semantics"):
        runner.summarize_manual_billing_config(
            {
                "provider": "wechat",
                "status": "misconfigured",
                "checkout_enabled": False,
                "native_payment_enabled": False,
                "webhook_ready": False,
            }
        )

    private_transaction_id = "22222222-2222-4222-8222-222222222222"
    snapshot = runner._credit_snapshot(
        {
            "balance": 99,
            "available_balance": 99,
            "reserved": 0,
            "consumed_credits": 1,
        },
        [{"id": private_transaction_id, "actor_label": "private-user"}],
        stage="credits",
    )
    assert snapshot["transaction_count"] == 1
    assert private_transaction_id not in repr(snapshot)
    assert "private-user" not in repr(snapshot)


def test_credit_snapshot_reader_paginates_the_complete_ledger(monkeypatch):
    runner = _load_api_runner()
    transaction_ids = [str(uuid.uuid4()) for _ in range(101)]
    requested_paths: list[str] = []

    def fake_call_api(method, api_base, path, data=None, token=None, **kwargs):
        del method, api_base, data, token, kwargs
        requested_paths.append(path)
        if path == "/subscription/summary":
            return 200, {
                "balance": 99,
                "available_balance": 99,
                "reserved": 0,
                "consumed_credits": 1,
            }
        if "page=1" in path:
            return 200, [{"id": value} for value in transaction_ids[:100]]
        if "page=2" in path:
            return 200, [{"id": transaction_ids[100]}]
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(runner, "call_api", fake_call_api)
    snapshot = runner._read_credit_snapshot("http://candidate/api", "token", stage="credits")

    assert snapshot["transaction_count"] == 101
    assert requested_paths == [
        "/subscription/summary",
        "/subscription/credit-transactions?page=1&limit=100",
        "/subscription/credit-transactions?page=2&limit=100",
    ]


def test_work_executor_preflight_gate_requires_available_without_leaking_agent_ids():
    runner = _load_api_runner()
    assert runner.summarize_work_executor_preflight(
        {
            "capability_status": "available",
            "reasons": [],
            "confirmation_fingerprint": "a" * 64,
            "work_statement": {"executor": {"agent_id": "private-agent-id"}},
        },
        executor_kind="personal_assistant",
    ) == {"capability_status": "available", "reason_count": 0}

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner.summarize_work_executor_preflight(
            {
                "capability_status": "unavailable",
                "reasons": ["text_route_unavailable:private-agent-id"],
                "confirmation_fingerprint": "b" * 64,
            },
            executor_kind="personal_assistant",
        )

    assert exc_info.value.detail == {
        "code": "personal_assistant_route_unavailable",
        "capability_status": "unavailable",
        "reason_count": 1,
    }
    assert "private-agent-id" not in repr(exc_info.value.detail)


def test_release_qa_employee_is_reused_idempotently_without_recruiting(monkeypatch):
    runner = _load_api_runner()
    employee_id = "22222222-2222-4222-8222-222222222222"
    calls: list[tuple[str, str]] = []

    def fake_call_api(method, api_base, path, data=None, token=None, **kwargs):
        del api_base, data, token, kwargs
        calls.append((method, path))
        if path == "/agents/":
            return 200, [
                {
                    "id": employee_id,
                    "name": runner.RELEASE_QA_EMPLOYEE_NAME,
                    "product_role": "agent_employee",
                    "status": "idle",
                }
            ]
        if path == f"/agents/{employee_id}":
            return 200, {
                "id": employee_id,
                "product_role": "agent_employee",
                "status": "idle",
            }
        raise AssertionError(path)

    monkeypatch.setattr(runner, "call_api", fake_call_api)

    assert runner._ensure_release_qa_employee("http://candidate/api", "token") == (
        employee_id,
        False,
    )
    assert calls == [("GET", "/agents/"), ("GET", f"/agents/{employee_id}")]


def test_release_qa_employee_is_recruited_through_the_real_agent_api(monkeypatch):
    runner = _load_api_runner()
    employee_id = "33333333-3333-4333-8333-333333333333"
    calls: list[tuple[str, str, object]] = []

    def fake_call_api(method, api_base, path, data=None, token=None, **kwargs):
        del api_base, token, kwargs
        calls.append((method, path, data))
        if method == "GET" and path == "/agents/":
            return 200, []
        if method == "POST" and path == "/agents/":
            return 201, {"id": employee_id, "status": "creating"}
        if method == "GET" and path == f"/agents/{employee_id}":
            return 200, {
                "id": employee_id,
                "product_role": "agent_employee",
                "status": "idle",
            }
        raise AssertionError(path)

    monkeypatch.setattr(runner, "call_api", fake_call_api)

    assert runner._ensure_release_qa_employee("http://candidate/api", "token") == (
        employee_id,
        True,
    )
    create_payload = calls[1][2]
    assert isinstance(create_payload, dict)
    assert create_payload["name"] == runner.RELEASE_QA_EMPLOYEE_NAME
    assert create_payload["agent_type"] == "native"
    assert create_payload["permission_scope_type"] == "company"


def test_private_assistant_with_the_release_employee_name_is_rejected():
    runner = _load_api_runner()

    with pytest.raises(runner.SmokeFailure) as exc_info:
        runner._release_qa_employee_from_roster(
            [
                {
                    "id": "44444444-4444-4444-8444-444444444444",
                    "name": runner.RELEASE_QA_EMPLOYEE_NAME,
                    "product_role": "personal_assistant",
                    "status": "idle",
                }
            ]
        )

    assert exc_info.value.detail == {
        "code": "release_qa_employee_has_wrong_product_role"
    }


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
