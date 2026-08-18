#!/usr/bin/env python3
"""Production-oriented subscription and billing smoke test.

This smoke intentionally requires real credentials. It validates the money
surface through HTTP instead of unit-test fixtures:

1. company owner can read aggregates and owner-only billing data;
2. ordinary member can read only entitlements and attributed personal usage;
3. ordinary member is rejected from every sensitive billing endpoint;
4. platform admin can run read-only ledger/payment reconciliation and exports;
5. release identity is bound to the isolated candidate slot.

The browser portion is intentionally implemented by the pinned, isolated
``deploy/browser-smoke`` image. This standard-library runner never injects
credentials into JavaScript or process arguments.

Environment variables:
  SMOKE_TENANT_EMAIL / SMOKE_TENANT_PASSWORD / SMOKE_TENANT_ID
  SMOKE_MEMBER_EMAIL / SMOKE_MEMBER_PASSWORD
  SMOKE_PLATFORM_ADMIN_EMAIL / SMOKE_PLATFORM_ADMIN_PASSWORD
  SMOKE_API_BASE (default: http://127.0.0.1:3008/api)
  SMOKE_FRONTEND_URL (default: http://127.0.0.1:3008)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "http://127.0.0.1:3008/api"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3008"
REQUIRED_CREDENTIAL_KEYS = {
    "SMOKE_TENANT_EMAIL",
    "SMOKE_TENANT_PASSWORD",
    "SMOKE_TENANT_ID",
    "SMOKE_MEMBER_EMAIL",
    "SMOKE_MEMBER_PASSWORD",
    "SMOKE_PLATFORM_ADMIN_EMAIL",
    "SMOKE_PLATFORM_ADMIN_PASSWORD",
}


class SmokeFailure(RuntimeError):
    def __init__(self, stage: str, detail: Any):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail!r}")


def json_print(payload: dict[str, Any], *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream or sys.stdout)


def call_api(
    method: str,
    api_base: str,
    path: str,
    data: dict[str, Any] | None = None,
    token: str | None = None,
    *,
    accept: str = "application/json",
) -> tuple[int, Any]:
    body = None
    headers = {"Accept": accept}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(api_base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if "text/csv" in content_type or accept == "text/csv":
                return resp.status, raw.decode("utf-8")
            text = raw.decode("utf-8")
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise SmokeFailure("service_reachable", {"api_base": api_base, "error": str(exc)}) from exc


def require(condition: bool, stage: str, detail: Any) -> None:
    if not condition:
        raise SmokeFailure(stage, detail)


def summarize_reconciliation(
    payload: Any,
    *,
    checked_field: str,
    stage: str,
) -> dict[str, int]:
    """Fail closed on reconciliation drift without copying issue rows to logs."""
    require(
        isinstance(payload, dict),
        stage,
        {"code": "invalid_reconciliation_response"},
    )
    checked = payload.get(checked_field)
    issues = payload.get("issues")
    require(
        type(checked) is int and checked >= 0,
        stage,
        {"code": "invalid_checked_count", "field": checked_field},
    )
    require(
        isinstance(issues, list),
        stage,
        {"code": "invalid_issues_collection"},
    )
    require(
        not issues,
        stage,
        {
            "code": "reconciliation_issues_detected",
            "issue_count": len(issues),
        },
    )
    return {checked_field: checked, "issue_count": 0}


def summarize_work_executor_preflight(payload: Any) -> dict[str, Any]:
    """Require the real tenant's personal assistant to pass Work intake.

    The evidence intentionally excludes Agent IDs, names, and route details.
    This is a read-only preflight: it neither creates a Task nor calls a model.
    """

    require(
        isinstance(payload, dict),
        "work_executor_preflight",
        {"code": "invalid_work_preflight_response"},
    )
    capability_status = payload.get("capability_status")
    reasons = payload.get("reasons")
    fingerprint = payload.get("confirmation_fingerprint")
    require(
        capability_status == "available" and reasons == [],
        "work_executor_preflight",
        {
            "code": "personal_assistant_route_unavailable",
            "capability_status": capability_status,
            "reason_count": len(reasons) if isinstance(reasons, list) else None,
        },
    )
    require(
        isinstance(fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None,
        "work_executor_preflight",
        {"code": "invalid_confirmation_fingerprint"},
    )
    return {"capability_status": "available", "reason_count": 0}


def login(
    api_base: str,
    email: str,
    password: str,
    stage: str,
    *,
    tenant_id: str | None = None,
    tenant_fallback_id: str | None = None,
) -> dict[str, Any]:
    request_data = {"login_identifier": email, "password": password}
    if tenant_id:
        request_data["tenant_id"] = tenant_id
    status, payload = call_api(
        "POST",
        api_base,
        "/auth/login",
        request_data,
    )
    if (
        status == 200
        and tenant_fallback_id
        and isinstance(payload, dict)
        and payload.get("requires_tenant_selection") is True
    ):
        tenants = payload.get("tenants")
        require(
            isinstance(tenants, list)
            and any(
                isinstance(candidate, dict)
                and str(candidate.get("tenant_id")) == tenant_fallback_id
                for candidate in tenants
            ),
            stage,
            {"code": "target_tenant_not_available"},
        )
        status, payload = call_api(
            "POST",
            api_base,
            "/auth/login",
            {
                "login_identifier": email,
                "password": password,
                "tenant_id": tenant_fallback_id,
            },
        )
    require(
        status == 200 and isinstance(payload, dict) and payload.get("access_token"),
        stage,
        {"code": "unexpected_login_response", "status": status},
    )
    return payload


def load_credentials(path_value: str | None) -> dict[str, str]:
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise SmokeFailure("credentials_file", {"present": False}) from exc
    require(
        path.is_file() and not path.is_symlink() and 0 < stat.st_size <= 16_384,
        "credentials_file",
        {"regular_file": path.is_file() and not path.is_symlink(), "size": stat.st_size},
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("credentials_file", {"format": "invalid_json"}) from exc
    require(isinstance(payload, dict), "credentials_file", {"format": "object_required"})
    require(set(payload) == REQUIRED_CREDENTIAL_KEYS, "credentials_file", {"keys": "exact_required_keys_only"})
    for key in REQUIRED_CREDENTIAL_KEYS:
        value = payload[key]
        require(
            isinstance(value, str) and 0 < len(value) <= 4096,
            "credentials_file",
            {"invalid_key": key},
        )
    return payload


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    credentials = load_credentials(args.credentials_file)
    tenant_email = args.tenant_email or credentials.get("SMOKE_TENANT_EMAIL") or os.getenv("SMOKE_TENANT_EMAIL")
    tenant_password = (
        args.tenant_password or credentials.get("SMOKE_TENANT_PASSWORD") or os.getenv("SMOKE_TENANT_PASSWORD")
    )
    tenant_id_value = args.tenant_id or credentials.get("SMOKE_TENANT_ID") or os.getenv("SMOKE_TENANT_ID")
    member_email = args.member_email or credentials.get("SMOKE_MEMBER_EMAIL") or os.getenv("SMOKE_MEMBER_EMAIL")
    member_password = (
        args.member_password or credentials.get("SMOKE_MEMBER_PASSWORD") or os.getenv("SMOKE_MEMBER_PASSWORD")
    )
    admin_email = (
        args.platform_admin_email
        or credentials.get("SMOKE_PLATFORM_ADMIN_EMAIL")
        or os.getenv("SMOKE_PLATFORM_ADMIN_EMAIL")
    )
    admin_password = (
        args.platform_admin_password
        or credentials.get("SMOKE_PLATFORM_ADMIN_PASSWORD")
        or os.getenv("SMOKE_PLATFORM_ADMIN_PASSWORD")
    )
    api_base = (args.api_base or os.getenv("SMOKE_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    frontend_url = (args.frontend_url or os.getenv("SMOKE_FRONTEND_URL") or DEFAULT_FRONTEND_URL).rstrip("/")

    require(
        bool(tenant_email and tenant_password),
        "tenant_credentials_configured",
        "Set SMOKE_TENANT_EMAIL and SMOKE_TENANT_PASSWORD",
    )
    try:
        tenant_id = str(uuid.UUID(str(tenant_id_value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SmokeFailure("tenant_id_configured", {"code": "valid_uuid_required"}) from exc
    require(
        bool(admin_email and admin_password),
        "platform_admin_credentials_configured",
        "Set SMOKE_PLATFORM_ADMIN_EMAIL and SMOKE_PLATFORM_ADMIN_PASSWORD",
    )
    require(
        bool(member_email and member_password),
        "member_credentials_configured",
        "Set SMOKE_MEMBER_EMAIL and SMOKE_MEMBER_PASSWORD",
    )

    expected_identity = (args.expected_version, args.expected_commit, args.expected_release_id)
    require(
        all(expected_identity) or not any(expected_identity),
        "release_identity_arguments",
        "expected version, commit, and release ID must be supplied together",
    )
    if args.evidence_nonce is not None:
        require(
            len(args.evidence_nonce) == 32
            and all(character in "0123456789abcdef" for character in args.evidence_nonce),
            "evidence_nonce",
            "evidence nonce must contain 32 lowercase hexadecimal characters",
        )

    summary: dict[str, Any] = {
        "evidence_schema_version": 2,
        "evidence_kind": "subscription_api",
        "api_base": api_base,
        "frontend_url": frontend_url,
        "evidence_nonce": args.evidence_nonce,
        "checks": [],
    }
    status, release_identity = call_api("GET", api_base, "/version")
    require(
        status == 200 and isinstance(release_identity, dict),
        "release_identity",
        {
            "status": status,
            "body": release_identity,
        },
    )
    if all(expected_identity):
        require(
            release_identity.get("version") == args.expected_version
            and release_identity.get("commit") == args.expected_commit
            and release_identity.get("release_id") == args.expected_release_id,
            "release_identity",
            release_identity,
        )
    summary["release_identity"] = release_identity
    summary["checks"].append("candidate_release_identity_ok")

    tenant_login = login(
        api_base,
        tenant_email,
        tenant_password,
        "tenant_login",
        tenant_id=tenant_id,
    )
    tenant_token = tenant_login["access_token"]
    summary["checks"].append("tenant_login_ok")

    for path, stage in [
        ("/auth/me", "tenant_me"),
        ("/subscription/plans", "client_plans"),
        ("/subscription/summary", "client_subscription_summary"),
        ("/subscription/credit-transactions", "client_credit_transactions"),
        ("/subscription/orders", "client_orders"),
        ("/subscription/credit-packs", "client_credit_packs"),
    ]:
        status, body = call_api("GET", api_base, path, token=tenant_token)
        require(
            status == 200,
            stage,
            {"code": "unexpected_http_status", "status": status},
        )
        if stage == "tenant_me":
            require(
                isinstance(body, dict) and str(body.get("tenant_id")) == tenant_id,
                "tenant_scope",
                {"code": "unexpected_tenant_context"},
            )
            summary["checks"].append("tenant_scope_ok")
            capabilities = set(body.get("effective_capabilities") or [])
            require(
                {"company.billing.view", "company.billing.manage"}.issubset(capabilities),
                "tenant_billing_manage_capability",
                {"code": "company_owner_billing_capabilities_required"},
            )
            summary["checks"].append("tenant_billing_manage_capability_ok")
        summary["checks"].append(f"{stage}_ok")
        if stage == "client_subscription_summary":
            require(
                isinstance(body, dict) and "balance" in body and "available_balance" in body,
                stage,
                {"code": "invalid_subscription_summary"},
            )
            summary["subscription_summary"] = {
                "plan_code": body.get("plan_code"),
                "balance": body.get("balance"),
                "available_balance": body.get("available_balance"),
                "reserved": body.get("reserved"),
                "seats_used": body.get("seats_used"),
                "seats_total": body.get("seats_total"),
            }

    member_login = login(
        api_base,
        member_email,
        member_password,
        "member_login",
        tenant_id=tenant_id,
    )
    member_token = member_login["access_token"]
    summary["checks"].append("member_login_ok")

    status, member_me = call_api("GET", api_base, "/auth/me", token=member_token)
    member_capabilities = set(member_me.get("effective_capabilities") or []) if isinstance(member_me, dict) else set()
    require(
        status == 200
        and str(member_me.get("tenant_id")) == tenant_id
        and "company.billing.view" not in member_capabilities
        and "company.billing.manage" not in member_capabilities,
        "member_scope",
        {"code": "ordinary_member_without_billing_authority_required", "status": status},
    )
    for path in [
        "/subscription/plans",
        "/subscription/credit-packs",
        "/subscription/my-entitlements",
        "/subscription/usage/me",
    ]:
        status, body = call_api("GET", api_base, path, token=member_token)
        require(status == 200, "member_personal_usage", {"path": path, "status": status})
        if path == "/subscription/usage/me":
            require(
                isinstance(body, dict)
                and "consumed_credits" in body
                and not {"balance", "orders", "transactions", "tokens_used"}.intersection(body),
                "member_personal_usage",
                {"code": "unsafe_personal_usage_projection"},
            )
    summary["checks"].append("member_personal_usage_ok")

    sensitive_paths = [
        "/subscription/subscriptions",
        "/subscription/usage",
        "/subscription/credits",
        "/subscription/seats",
        "/subscription/summary",
        "/subscription/credit-transactions",
        "/subscription/orders",
        "/subscription/billing/profile",
    ]
    for path in sensitive_paths:
        status, _ = call_api("GET", api_base, path, token=member_token)
        require(status == 403, "member_sensitive_billing_denied", {"path": path, "status": status})
    summary["checks"].append("member_sensitive_billing_denied_ok")

    status, body = call_api(
        "POST",
        api_base,
        "/work/tasks/preflight",
        {
            "title": "Release work-route readiness preflight",
            "intent": "Verify the tenant personal assistant execution route without creating a task.",
            "work_type": "general",
            "priority": "low",
            "executor_kind": "personal_assistant",
        },
        token=tenant_token,
    )
    require(
        status == 200,
        "work_executor_preflight",
        {"code": "unexpected_http_status", "status": status},
    )
    summary["work_executor_preflight"] = summarize_work_executor_preflight(body)
    summary["checks"].append("work_executor_preflight_ok")

    admin_login = login(
        api_base,
        admin_email,
        admin_password,
        "platform_admin_login",
        tenant_fallback_id=tenant_id,
    )
    admin_token = admin_login["access_token"]
    summary["checks"].append("platform_admin_login_ok")

    for path, stage, checked_field in [
        (
            "/saas/reconciliation/ledger",
            "saas_ledger_reconciliation",
            "checked_tenants",
        ),
        (
            "/saas/reconciliation/payments",
            "saas_payment_reconciliation",
            "checked_orders",
        ),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token)
        require(
            status == 200,
            stage,
            {"code": "unexpected_http_status", "status": status},
        )
        summary[stage] = summarize_reconciliation(
            body,
            checked_field=checked_field,
            stage=stage,
        )
        summary["checks"].append(f"{stage}_ok")

    for path, stage in [
        ("/saas/orders/export.csv", "orders_csv_export"),
        ("/saas/credit-transactions/export.csv", "credit_transactions_csv_export"),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token, accept="text/csv")
        require(
            status == 200 and isinstance(body, str) and "," in body,
            stage,
            {"code": "unexpected_export_response", "status": status},
        )
        summary["checks"].append(f"{stage}_ok")

    summary["ok"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Production subscription/billing smoke")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--tenant-email", default=None)
    parser.add_argument("--tenant-password", default=None)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--member-email", default=None)
    parser.add_argument("--member-password", default=None)
    parser.add_argument("--platform-admin-email", default=None)
    parser.add_argument("--platform-admin-password", default=None)
    parser.add_argument("--credentials-file", default=None)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--expected-commit", default=None)
    parser.add_argument("--expected-release-id", default=None)
    parser.add_argument("--evidence-nonce", default=None)
    args = parser.parse_args()

    try:
        json_print(run_smoke(args))
        return 0
    except SmokeFailure as exc:
        json_print({"ok": False, "stage": exc.stage, "detail": exc.detail}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
