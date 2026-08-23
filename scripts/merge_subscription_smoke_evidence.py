#!/usr/bin/env python3
"""Validate and combine API/browser production business-flow evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


API_CHECKS = {
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
}
UI_CHECKS = {
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
}


def read_evidence(path_value: str, expected_kind: str) -> dict[str, Any]:
    path = Path(path_value)
    stat = path.lstat()
    if not path.is_file() or path.is_symlink() or not 0 < stat.st_size <= 1_048_576:
        raise ValueError(f"unsafe evidence file: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("evidence_schema_version") != 3
        or payload.get("evidence_kind") != expected_kind
    ):
        raise ValueError(f"wrong evidence schema: {path.name}")
    if payload.get("ok") is not True:
        raise ValueError(f"failed evidence: {path.name}")
    return payload


def read_reconciliation_summary(
    payload: dict[str, Any],
    *,
    key: str,
    checked_field: str,
) -> dict[str, int]:
    summary = payload.get(key)
    if not isinstance(summary, dict) or set(summary) != {
        checked_field,
        "issue_count",
    }:
        raise ValueError(f"unsafe reconciliation evidence: {key}")
    checked = summary.get(checked_field)
    issue_count = summary.get("issue_count")
    if type(checked) is not int or checked < 0 or issue_count != 0:
        raise ValueError(f"failed reconciliation evidence: {key}")
    return {checked_field: checked, "issue_count": 0}


def read_api_business_flow(payload: dict[str, Any]) -> dict[str, Any]:
    flow = payload.get("business_flow")
    if not isinstance(flow, dict) or set(flow) != {"work", "group", "topology", "credits"}:
        raise ValueError("unsafe API business-flow evidence")
    expected_work = {
        "executor_kind": "agent_employee",
        "execution_status": "completed",
        "output_marker_verified": True,
        "create_replayed": True,
        "result_review_status": "approved",
        "review_replayed": True,
    }
    if flow.get("work") != expected_work:
        raise ValueError("Work business-flow evidence is incomplete")
    group = flow.get("group")
    if (
        not isinstance(group, dict)
        or set(group)
        != {"member_count", "owner_message_persisted", "member_visibility", "message_replayed"}
        or type(group.get("member_count")) is not int
        or group["member_count"] < 2
        or any(
            group.get(key) is not True
            for key in ("owner_message_persisted", "member_visibility", "message_replayed")
        )
    ):
        raise ValueError("Group business-flow evidence is incomplete")
    topology = flow.get("topology")
    if (
        not isinstance(topology, dict)
        or set(topology) != {"node_count", "employee_visible", "completed_work_visible"}
        or type(topology.get("node_count")) is not int
        or topology["node_count"] < 1
        or topology.get("employee_visible") is not True
        or topology.get("completed_work_visible") is not True
    ):
        raise ValueError("topology business-flow evidence is incomplete")
    credits = flow.get("credits")
    if (
        not isinstance(credits, dict)
        or set(credits)
        != {
            "consumed_delta",
            "transaction_delta",
            "reserved_before",
            "reserved_after",
            "replay_balance_delta",
            "replay_transaction_delta",
        }
        or any(type(credits.get(key)) is not int for key in credits)
        or credits["consumed_delta"] <= 0
        or credits["transaction_delta"] <= 0
        or any(
            credits[key] != 0
            for key in (
                "reserved_before",
                "reserved_after",
                "replay_balance_delta",
                "replay_transaction_delta",
            )
        )
    ):
        raise ValueError("Credits exactly-once evidence is incomplete")
    return {
        "work": expected_work,
        "group": dict(group),
        "topology": dict(topology),
        "credits": dict(credits),
    }


def read_ui_business_flow(payload: dict[str, Any]) -> dict[str, Any]:
    flow = payload.get("business_flow")
    if not isinstance(flow, dict) or set(flow) != {
        "work",
        "group",
        "topology",
        "direct_chat",
        "credits",
    }:
        raise ValueError("unsafe UI business-flow evidence")
    if flow.get("work") != {"task_visible": True}:
        raise ValueError("UI Work evidence is incomplete")
    if flow.get("group") != {"group_visible": True, "message_restored": True}:
        raise ValueError("UI Group evidence is incomplete")
    if flow.get("topology") != {"completed_work_visible": True}:
        raise ValueError("UI topology evidence is incomplete")
    direct_chat = flow.get("direct_chat")
    if (
        not isinstance(direct_chat, dict)
        or set(direct_chat)
        != {"round_trip", "durable_after_reload", "message_count", "assistant_count"}
        or direct_chat.get("round_trip") is not True
        or direct_chat.get("durable_after_reload") is not True
        or type(direct_chat.get("message_count")) is not int
        or direct_chat["message_count"] < 2
        or type(direct_chat.get("assistant_count")) is not int
        or direct_chat["assistant_count"] < 1
    ):
        raise ValueError("UI Direct Chat evidence is incomplete")
    credits = flow.get("credits")
    if credits != {
        "settled_after_chat": True,
        "reserved_after": 0,
        "consumed_delta_positive": True,
    }:
        raise ValueError("UI post-chat Credits evidence is incomplete")
    return {
        "work": dict(flow["work"]),
        "group": dict(flow["group"]),
        "topology": dict(flow["topology"]),
        "direct_chat": dict(direct_chat),
        "credits": dict(credits),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-evidence", required=True)
    parser.add_argument("--ui-evidence", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--evidence-nonce", required=True)
    parser.add_argument("--runner-bundle-sha256", required=True)
    parser.add_argument("--browser-image-id", required=True)
    parser.add_argument("--qa-tooling-release-id", required=True)
    parser.add_argument("--qa-tooling-commit", required=True)
    parser.add_argument("--qa-tooling-package-sha256", required=True)
    args = parser.parse_args()

    api = read_evidence(args.api_evidence, "release_business_api")
    ui = read_evidence(args.ui_evidence, "release_business_browser")
    expected_identity = {
        "version": args.expected_version,
        "commit": args.expected_commit,
        "release_id": args.expected_release_id,
    }
    if api.get("api_base") != args.api_base.rstrip("/"):
        raise ValueError("API evidence belongs to another candidate slot")
    if api.get("frontend_url") != args.frontend_url.rstrip("/"):
        raise ValueError("API evidence contains another frontend URL")
    if ui.get("frontend_url") != args.frontend_url.rstrip("/"):
        raise ValueError("UI evidence belongs to another candidate slot")
    if api.get("release_identity") != expected_identity:
        raise ValueError("API evidence release identity mismatch")
    if ui.get("release_identity") != expected_identity:
        raise ValueError("UI evidence release identity mismatch")
    if ui.get("final_path") != "/account/subscription":
        raise ValueError("UI evidence did not finish on the subscription page")
    if ui.get("browser_target") != "isolated_candidate_frontend_network":
        raise ValueError("UI evidence did not use the isolated candidate frontend")
    tolerated_runtime_state_conflicts = ui.get("tolerated_runtime_state_conflicts")
    if (
        type(tolerated_runtime_state_conflicts) is not int
        or tolerated_runtime_state_conflicts < 0
    ):
        raise ValueError("UI runtime-state conflict evidence is invalid")
    if not len(args.evidence_nonce) == 32 or any(
        character not in "0123456789abcdef" for character in args.evidence_nonce
    ):
        raise ValueError("invalid evidence nonce")
    if api.get("evidence_nonce") != args.evidence_nonce:
        raise ValueError("API evidence nonce mismatch")
    if ui.get("evidence_nonce") != args.evidence_nonce:
        raise ValueError("UI evidence nonce mismatch")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", args.runner_bundle_sha256) is None:
        raise ValueError("invalid runner bundle digest")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", args.browser_image_id) is None:
        raise ValueError("invalid browser image ID")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", args.qa_tooling_release_id) is None:
        raise ValueError("invalid QA tooling release ID")
    if re.fullmatch(r"[0-9a-f]{40}", args.qa_tooling_commit) is None:
        raise ValueError("invalid QA tooling commit")
    if re.fullmatch(r"[0-9a-f]{64}", args.qa_tooling_package_sha256) is None:
        raise ValueError("invalid QA tooling package digest")
    api_checks = set(api.get("checks") or [])
    ui_checks = set(ui.get("checks") or [])
    if not API_CHECKS.issubset(api_checks):
        raise ValueError("API evidence is missing required checks")
    if not UI_CHECKS.issubset(ui_checks):
        raise ValueError("UI evidence is missing required checks")

    billing_mode = api.get("billing_mode")
    if billing_mode != {
        "provider": "manual",
        "status": "manual",
        "checkout_enabled": True,
        "native_payment_enabled": False,
        "webhook_ready": False,
    }:
        raise ValueError("billing mode evidence does not match production manual semantics")
    api_business_flow = read_api_business_flow(api)
    ui_business_flow = read_ui_business_flow(ui)

    work_executor_preflight = api.get("work_executor_preflight")
    expected_executor_preflight = {
        "personal_assistant": {
            "capability_status": "available",
            "reason_count": 0,
        },
        "agent_employee": {
            "capability_status": "available",
            "reason_count": 0,
        },
    }
    if work_executor_preflight != expected_executor_preflight:
        raise ValueError("Work executor preflight evidence is unavailable")
    agent_employee = api.get("agent_employee")
    if (
        not isinstance(agent_employee, dict)
        or set(agent_employee) != {"created_for_release_qa", "ready"}
        or type(agent_employee.get("created_for_release_qa")) is not bool
        or agent_employee.get("ready") is not True
    ):
        raise ValueError("Release QA Agent employee evidence is incomplete")

    api_summary = api.get("subscription_summary")
    ui_summary = ui.get("subscription_summary")
    if not isinstance(api_summary, dict) or not isinstance(ui_summary, dict):
        raise ValueError("subscription summary evidence is missing")
    for field in ("plan_code", "balance", "available_balance", "reserved"):
        if api_summary.get(field) != ui_summary.get(field):
            raise ValueError(f"API/UI subscription summary mismatch: {field}")
    ledger_reconciliation = read_reconciliation_summary(
        api,
        key="saas_ledger_reconciliation",
        checked_field="checked_tenants",
    )
    payment_reconciliation = read_reconciliation_summary(
        api,
        key="saas_payment_reconciliation",
        checked_field="checked_orders",
    )

    combined = {
        "evidence_schema_version": 3,
        "evidence_kind": "release_business_composite",
        "ok": True,
        "api_base": args.api_base.rstrip("/"),
        "frontend_url": args.frontend_url.rstrip("/"),
        "release_identity": expected_identity,
        "qa_tooling_identity": {
            "release_id": args.qa_tooling_release_id,
            "commit": args.qa_tooling_commit,
            "package_sha256": args.qa_tooling_package_sha256,
        },
        "evidence_nonce": args.evidence_nonce,
        "browser_gate": {
            "runner_bundle_sha256": args.runner_bundle_sha256,
            "image_id": args.browser_image_id,
        },
        "checks": sorted(api_checks | ui_checks),
        "subscription_summary": api_summary,
        "billing_mode": billing_mode,
        "business_flow": {
            "api": api_business_flow,
            "ui": ui_business_flow,
        },
        "work_executor_preflight": expected_executor_preflight,
        "agent_employee": dict(agent_employee),
        "ui": {
            "final_path": ui.get("final_path"),
            "browser_target": ui.get("browser_target"),
            "tolerated_runtime_state_conflicts": tolerated_runtime_state_conflicts,
            "subscription_summary": ui.get("subscription_summary"),
            "business_flow": ui_business_flow,
        },
        "saas_ledger_reconciliation": ledger_reconciliation,
        "saas_payment_reconciliation": payment_reconciliation,
    }
    print(json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "stage": "merge_evidence", "detail": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
