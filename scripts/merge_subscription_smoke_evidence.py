#!/usr/bin/env python3
"""Validate and combine API/browser subscription smoke evidence."""

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
}
UI_CHECKS = {
    "ui_release_identity_ok",
    "ui_tenant_login_ok",
    "ui_tenant_scope_ok",
    "ui_subscription_summary_api_ok",
    "ui_subscription_balance_rendered_ok",
    "ui_subscription_page_ok",
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
        or payload.get("evidence_schema_version") != 2
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
    args = parser.parse_args()

    api = read_evidence(args.api_evidence, "subscription_api")
    ui = read_evidence(args.ui_evidence, "subscription_browser")
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
    api_checks = set(api.get("checks") or [])
    ui_checks = set(ui.get("checks") or [])
    if not API_CHECKS.issubset(api_checks):
        raise ValueError("API evidence is missing required checks")
    if not UI_CHECKS.issubset(ui_checks):
        raise ValueError("UI evidence is missing required checks")

    work_executor_preflight = api.get("work_executor_preflight")
    if work_executor_preflight != {
        "capability_status": "available",
        "reason_count": 0,
    }:
        raise ValueError("Work executor preflight evidence is unavailable")

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
        "evidence_schema_version": 2,
        "evidence_kind": "subscription_composite",
        "ok": True,
        "api_base": args.api_base.rstrip("/"),
        "frontend_url": args.frontend_url.rstrip("/"),
        "release_identity": expected_identity,
        "evidence_nonce": args.evidence_nonce,
        "browser_gate": {
            "runner_bundle_sha256": args.runner_bundle_sha256,
            "image_id": args.browser_image_id,
        },
        "checks": sorted(api_checks | ui_checks),
        "subscription_summary": api_summary,
        "work_executor_preflight": work_executor_preflight,
        "ui": {
            "final_path": ui.get("final_path"),
            "browser_target": ui.get("browser_target"),
            "subscription_summary": ui.get("subscription_summary"),
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
