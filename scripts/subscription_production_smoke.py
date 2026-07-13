#!/usr/bin/env python3
"""Production-oriented subscription and billing smoke test.

This smoke intentionally requires real credentials. It validates the money
surface through HTTP instead of unit-test fixtures:

1. tenant user can read subscription summary, ledger, orders, plans, packs;
2. platform admin can run read-only ledger/payment reconciliation;
3. platform admin can export order and credit CSVs;
4. optional UI check verifies subscription pages are reachable after login.

Environment variables:
  SMOKE_TENANT_EMAIL / SMOKE_TENANT_PASSWORD
  SMOKE_PLATFORM_ADMIN_EMAIL / SMOKE_PLATFORM_ADMIN_PASSWORD
  SMOKE_API_BASE (default: http://127.0.0.1:3008/api)
  SMOKE_FRONTEND_URL (default: http://127.0.0.1:3008)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_BASE = "http://127.0.0.1:3008/api"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3008"


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


def login(api_base: str, email: str, password: str, stage: str) -> dict[str, Any]:
    status, payload = call_api(
        "POST",
        api_base,
        "/auth/login",
        {"login_identifier": email, "password": password},
    )
    require(status == 200 and isinstance(payload, dict) and payload.get("access_token"), stage, {"status": status, "body": payload})
    return payload


def run_ui_check(frontend_url: str, tenant_email: str, tenant_password: str, playwright_cli: str | None) -> dict[str, Any]:
    pwcli = playwright_cli or os.getenv("PWCLI") or str(Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh")
    if not Path(pwcli).exists():
        raise SmokeFailure("ui_playwright_cli_found", {"path": pwcli})

    code = f"""
async (page) => {{
  await page.goto('{frontend_url.rstrip("/")}/login');
  await page.locator('input[type="text"], input[type="email"]').first().fill('{tenant_email}');
  await page.locator('input[type="password"]').first().fill('{tenant_password}');
  await page.getByRole('button', {{ name: /登录|Login|Sign in/i }}).click();
  await page.waitForURL(/\\/(dashboard|account|agents|plaza|$)/, {{ timeout: 30000 }}).catch(() => {{}});
  await page.goto('{frontend_url.rstrip("/")}/account/subscription');
  await page.waitForTimeout(1200);
  const text = await page.locator('body').innerText();
  if (!/Credits|积分|套餐详情|订阅|消耗明细/.test(text)) {{
    throw new Error('subscription detail page did not expose billing text');
  }}
  return {{ ok: true }};
}}
""".strip()
    open_proc = subprocess.run(
        [pwcli, "open", f"{frontend_url.rstrip('/')}/login"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if open_proc.returncode != 0:
        raise SmokeFailure("ui_open_login", {"stdout": open_proc.stdout, "stderr": open_proc.stderr})
    try:
        run_proc = subprocess.run(
            [pwcli, "run-code", code, "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=90,
        )
        if run_proc.returncode != 0:
            raise SmokeFailure("ui_subscription_page", {"stdout": run_proc.stdout, "stderr": run_proc.stderr})
        payload = json.loads(run_proc.stdout)
        if payload.get("isError"):
            raise SmokeFailure("ui_subscription_page", payload)
        return payload.get("result", payload)
    finally:
        subprocess.run([pwcli, "close"], cwd=REPO_ROOT, text=True, capture_output=True, timeout=30)


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    tenant_email = args.tenant_email or os.getenv("SMOKE_TENANT_EMAIL")
    tenant_password = args.tenant_password or os.getenv("SMOKE_TENANT_PASSWORD")
    admin_email = args.platform_admin_email or os.getenv("SMOKE_PLATFORM_ADMIN_EMAIL")
    admin_password = args.platform_admin_password or os.getenv("SMOKE_PLATFORM_ADMIN_PASSWORD")
    api_base = (args.api_base or os.getenv("SMOKE_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    frontend_url = (args.frontend_url or os.getenv("SMOKE_FRONTEND_URL") or DEFAULT_FRONTEND_URL).rstrip("/")

    require(bool(tenant_email and tenant_password), "tenant_credentials_configured", "Set SMOKE_TENANT_EMAIL and SMOKE_TENANT_PASSWORD")
    require(bool(admin_email and admin_password), "platform_admin_credentials_configured", "Set SMOKE_PLATFORM_ADMIN_EMAIL and SMOKE_PLATFORM_ADMIN_PASSWORD")

    summary: dict[str, Any] = {"api_base": api_base, "checks": []}

    tenant_login = login(api_base, tenant_email, tenant_password, "tenant_login")
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
        require(status == 200, stage, {"status": status, "body": body})
        summary["checks"].append(f"{stage}_ok")
        if stage == "client_subscription_summary":
            require(isinstance(body, dict) and "balance" in body and "available_balance" in body, stage, body)
            summary["subscription_summary"] = {
                "plan_code": body.get("plan_code"),
                "balance": body.get("balance"),
                "available_balance": body.get("available_balance"),
                "reserved": body.get("reserved"),
                "seats_used": body.get("seats_used"),
                "seats_total": body.get("seats_total"),
            }

    admin_login = login(api_base, admin_email, admin_password, "platform_admin_login")
    admin_token = admin_login["access_token"]
    summary["checks"].append("platform_admin_login_ok")

    for path, stage in [
        ("/saas/reconciliation/ledger", "saas_ledger_reconciliation"),
        ("/saas/reconciliation/payments", "saas_payment_reconciliation"),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token)
        require(status == 200 and isinstance(body, dict), stage, {"status": status, "body": body})
        summary[stage] = body
        summary["checks"].append(f"{stage}_ok")

    for path, stage in [
        ("/saas/orders/export.csv", "orders_csv_export"),
        ("/saas/credit-transactions/export.csv", "credit_transactions_csv_export"),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token, accept="text/csv")
        require(status == 200 and isinstance(body, str) and "," in body, stage, {"status": status, "body": body[:200] if isinstance(body, str) else body})
        summary["checks"].append(f"{stage}_ok")

    if args.ui:
        summary["ui"] = run_ui_check(frontend_url, tenant_email, tenant_password, args.playwright_cli)
        summary["checks"].append("ui_subscription_page_ok")

    summary["ok"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Production subscription/billing smoke")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--tenant-email", default=None)
    parser.add_argument("--tenant-password", default=None)
    parser.add_argument("--platform-admin-email", default=None)
    parser.add_argument("--platform-admin-password", default=None)
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--playwright-cli", default=None)
    args = parser.parse_args()

    try:
        json_print(run_smoke(args))
        return 0
    except SmokeFailure as exc:
        json_print({"ok": False, "stage": exc.stage, "detail": exc.detail}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
