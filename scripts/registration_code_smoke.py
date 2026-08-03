#!/usr/bin/env python3
"""Product smoke test for the platform registration-code signup gate.

This script intentionally validates the user-visible business path through HTTP:

1. ensure a local smoke platform admin can log in;
2. create a platform registration code from the admin API;
3. enable the registration-code gate;
4. verify no-code and invalid-code signups are rejected;
5. verify a valid code signs up exactly one user and is consumed;
6. restore the previous gate setting and deactivate the test code.

The optional ``--ui`` check only verifies that the registration form exposes a
code field. It is off by default so active frontend development does not hide
backend contract regressions behind transient layout failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_API_BASE = "http://localhost:3008/api"
DEFAULT_FRONTEND_URL = "http://localhost:3008"
DEFAULT_ADMIN_EMAIL = "registration-smoke-admin@clawith-smoke.com"
DEFAULT_ADMIN_PASSWORD = "SmokePass123!"
DEFAULT_DOMAIN = "clawith-smoke.com"


class SmokeFailure(RuntimeError):
    """Raised for a failed smoke-stage assertion."""

    def __init__(self, stage: str, detail: Any):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail!r}")


def json_print(payload: dict[str, Any], *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream or sys.stdout)


def call_api(method: str, api_base: str, path: str, data: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(api_base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise SmokeFailure("service_reachable", {"error": str(exc), "api_base": api_base}) from exc


def require(condition: bool, stage: str, detail: Any) -> None:
    if not condition:
        raise SmokeFailure(stage, detail)


def smoke_username_for(email: str) -> str:
    """Derive a stable username that does not collide with older smoke runs."""
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", email.lower()).strip("_")
    return f"smoke_{stem}"[:100]


async def ensure_smoke_platform_admin(email: str, password: str) -> dict[str, str]:
    """Create or reset a local smoke platform admin in the configured dev DB."""

    sys.path.insert(0, str(BACKEND_DIR))

    from sqlalchemy import select

    from app.core.security import hash_password
    from app.database import async_session
    from app.models.tenant import Tenant  # noqa: F401 - load metadata for users.tenant_id
    from app.models.user import Identity, User

    username = smoke_username_for(email)
    async with async_session() as db:
        identity = (await db.execute(select(Identity).where(Identity.email == email))).scalar_one_or_none()
        if identity is None:
            identity = Identity(email=email, username=username)
            db.add(identity)
            await db.flush()
        else:
            # Never turn a real company member into a global smoke admin.  A
            # previous local run accepted an arbitrary ``--admin-email`` and
            # silently rebound the identity, which made browser validation
            # enter the platform console instead of the intended tenant.
            tenant_membership = (
                await db.execute(
                    select(User.id)
                    .where(
                        User.identity_id == identity.id,
                        User.tenant_id.is_not(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if tenant_membership is not None:
                raise SmokeFailure(
                    "smoke_admin_identity_safety",
                    {
                        "reason": "existing_identity_has_company_membership",
                        "hint": "Use a dedicated smoke email or pass --no-admin-bootstrap with an existing platform admin.",
                    },
                )

        identity.username = username
        identity.password_hash = hash_password(password)
        identity.password_login_enabled = True
        identity.auth_version = int(identity.auth_version or 0) + 1
        identity.is_active = True
        identity.is_platform_admin = True
        identity.email_verified = True

        user = (
            await db.execute(
                select(User).where(
                    User.identity_id == identity.id,
                    User.tenant_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                identity_id=identity.id,
                tenant_id=None,
                display_name="Registration Smoke Admin",
                role="platform_admin",
            )
            db.add(user)
            await db.flush()

        user.display_name = "Registration Smoke Admin"
        user.role = "platform_admin"
        user.is_active = True
        user.registration_source = "smoke"

        await db.commit()
        return {
            "email": email,
            "user_id": str(user.id),
            "identity_id": str(identity.id),
            "role": user.role,
        }


def run_ui_check(frontend_url: str, playwright_cli: str | None) -> dict[str, Any]:
    pwcli = playwright_cli or os.getenv("PWCLI") or str(Path.home() / ".codex/skills/playwright/scripts/playwright_cli.sh")
    if not Path(pwcli).exists():
        raise SmokeFailure("ui_playwright_cli_found", {"path": pwcli})

    code = f"""
async (page) => {{
  await page.goto('{frontend_url.rstrip("/")}/login');
  const link = page.getByRole('link', {{ name: /去注册|注册|Register|Sign up/i }}).last();
  await link.click();
  await page.waitForTimeout(500);
  const bodyText = await page.locator('body').innerText();
  const inputs = await page.locator('input').evaluateAll(nodes => nodes.map(input => ({{
    placeholder: input.getAttribute('placeholder') || '',
    type: input.getAttribute('type') || ''
  }})));
  const serialized = JSON.stringify({{ bodyText, inputs }});
  if (!serialized.includes('邀请码') && !serialized.includes('注册码') && !/invitation|code/i.test(serialized)) {{
    throw new Error('registration code field not visible');
  }}
  return {{ ok: true, inputs }};
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
            timeout=60,
        )
        if run_proc.returncode != 0:
            raise SmokeFailure("ui_registration_code_field_visible", {"stdout": run_proc.stdout, "stderr": run_proc.stderr})
        try:
            payload = json.loads(run_proc.stdout)
        except json.JSONDecodeError as exc:
            raise SmokeFailure("ui_registration_code_field_visible", {"stdout": run_proc.stdout, "stderr": run_proc.stderr}) from exc
        if payload.get("isError"):
            raise SmokeFailure("ui_registration_code_field_visible", payload)
        return {"ok": True, "result": payload.get("result", payload)}
    finally:
        subprocess.run([pwcli, "close"], cwd=REPO_ROOT, text=True, capture_output=True, timeout=30)


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    run_id = str(int(time.time()))
    api_base = args.api_base.rstrip("/")
    summary: dict[str, Any] = {
        "api_base": api_base,
        "checks": [],
        "mode": "api+ui" if args.ui else "api",
    }
    token: str | None = None
    previous_settings: dict[str, Any] | None = None
    code_id: str | None = None
    created_code: str | None = None

    try:
        if not args.no_admin_bootstrap:
            summary["smoke_admin"] = asyncio.run(ensure_smoke_platform_admin(args.admin_email, args.admin_password))
            summary["checks"].append("smoke_admin_ready")

        status, config = call_api("GET", api_base, "/auth/registration-config")
        require(status == 200 and isinstance(config, dict), "public_registration_config_read", {"status": status, "body": config})
        summary["checks"].append("public_registration_config_read_ok")

        status, login = call_api(
            "POST",
            api_base,
            "/auth/login",
            {
                "login_identifier": args.admin_email,
                "password": args.admin_password,
            },
        )
        require(status == 200 and login and login.get("access_token"), "admin_login", {"status": status, "body": login})
        token = login["access_token"]
        summary["checks"].append("admin_login_ok")

        status, previous_settings = call_api("GET", api_base, "/admin/platform-settings", token=token)
        require(
            status == 200 and isinstance(previous_settings, dict),
            "platform_settings_read",
            {"status": status, "body": previous_settings},
        )
        summary["previous_invitation_code_enabled"] = previous_settings.get("invitation_code_enabled")
        summary["checks"].append("platform_settings_read_ok")

        status, created = call_api("POST", api_base, "/admin/registration-codes", {"count": 1, "max_uses": 1}, token=token)
        require(
            status == 201 and created and len(created.get("codes", [])) == 1,
            "registration_code_create",
            {"status": status, "body": created},
        )
        created_code = created["codes"][0]
        summary["registration_code"] = created_code
        summary["checks"].append("registration_code_create_ok")

        encoded_code = urllib.parse.quote(created_code)
        status, code_list = call_api("GET", api_base, f"/admin/registration-codes?search={encoded_code}", token=token)
        require(status == 200 and code_list.get("items"), "registration_code_list", {"status": status, "body": code_list})
        code_row = next((item for item in code_list["items"] if item["code"] == created_code), code_list["items"][0])
        code_id = code_row["id"]
        summary["registration_code_id"] = code_id
        summary["checks"].append("registration_code_list_ok")

        status, settings = call_api("PUT", api_base, "/admin/platform-settings", {"invitation_code_enabled": True}, token=token)
        require(
            status == 200 and settings.get("invitation_code_enabled") is True,
            "registration_gate_enable",
            {"status": status, "body": settings},
        )
        summary["checks"].append("registration_gate_enable_ok")

        status, config = call_api("GET", api_base, "/auth/registration-config")
        require(
            status == 200 and config.get("invitation_code_required") is True,
            "public_config_required",
            {"status": status, "body": config},
        )
        summary["checks"].append("public_config_required_ok")

        if args.ui:
            summary["ui"] = run_ui_check(args.frontend_url, args.playwright_cli)
            summary["checks"].append("ui_registration_code_field_visible_ok")

        no_code_email = f"reg-smoke-nocode-{run_id}@{args.email_domain}"
        status, body = call_api(
            "POST",
            api_base,
            "/auth/register",
            {
                "username": f"reg_smoke_nocode_{run_id}",
                "email": no_code_email,
                "password": "SmokeUser123!",
                "display_name": "No Code Smoke",
            },
        )
        require(
            status == 400 and body and body.get("detail") == "Registration code is required",
            "register_without_code_rejected",
            {"status": status, "body": body},
        )
        summary["checks"].append("register_without_code_rejected_ok")

        invalid_email = f"reg-smoke-invalid-{run_id}@{args.email_domain}"
        status, body = call_api(
            "POST",
            api_base,
            "/auth/register",
            {
                "username": f"reg_smoke_invalid_{run_id}",
                "email": invalid_email,
                "password": "SmokeUser123!",
                "display_name": "Invalid Code Smoke",
                "invitation_code": "BADCODE123",
            },
        )
        require(
            status == 400 and body and body.get("detail") == "Invalid registration code",
            "register_invalid_code_rejected",
            {"status": status, "body": body},
        )
        summary["checks"].append("register_invalid_code_rejected_ok")

        valid_email = f"reg-smoke-valid-{run_id}@{args.email_domain}"
        status, valid_body = call_api(
            "POST",
            api_base,
            "/auth/register",
            {
                "username": f"reg_smoke_valid_{run_id}",
                "email": valid_email,
                "password": "SmokeUser123!",
                "display_name": "Valid Code Smoke",
                "invitation_code": created_code.lower(),
            },
        )
        require(
            status == 201 and valid_body and valid_body.get("user_id"),
            "register_valid_code",
            {"status": status, "body": valid_body},
        )
        summary["registered_user_id"] = valid_body.get("user_id")
        summary["checks"].append("register_valid_code_ok")

        exhausted_email = f"reg-smoke-exhausted-{run_id}@{args.email_domain}"
        status, body = call_api(
            "POST",
            api_base,
            "/auth/register",
            {
                "username": f"reg_smoke_exhausted_{run_id}",
                "email": exhausted_email,
                "password": "SmokeUser123!",
                "display_name": "Exhausted Code Smoke",
                "invitation_code": created_code,
            },
        )
        require(
            status == 400 and body and body.get("detail") == "Registration code has reached its usage limit",
            "registration_code_usage_limit",
            {"status": status, "body": body},
        )
        summary["checks"].append("registration_code_usage_limit_ok")

        status, code_list_after = call_api("GET", api_base, f"/admin/registration-codes?search={encoded_code}", token=token)
        require(status == 200 and code_list_after.get("items"), "registration_code_list_after", {"status": status, "body": code_list_after})
        used_row = next((item for item in code_list_after["items"] if item["code"] == created_code), code_list_after["items"][0])
        require(used_row.get("used_count") == 1, "registration_code_consumed_once", used_row)
        summary["used_count_after_register"] = used_row.get("used_count")
        summary["checks"].append("registration_code_consumed_once_ok")

        summary["status"] = "passed"
        return summary
    finally:
        cleanup: dict[str, Any] = {}
        if token and previous_settings is not None and not args.leave_gate_enabled:
            restore_value = bool(previous_settings.get("invitation_code_enabled", False))
            status, settings = call_api("PUT", api_base, "/admin/platform-settings", {"invitation_code_enabled": restore_value}, token=token)
            cleanup["restore_gate"] = {
                "status": status,
                "invitation_code_enabled": settings.get("invitation_code_enabled") if isinstance(settings, dict) else None,
            }
        if token and code_id and not args.keep_code:
            status, body = call_api("DELETE", api_base, f"/admin/registration-codes/{code_id}", token=token)
            cleanup["deactivate_code"] = {"status": status, "body": body}
        if cleanup:
            summary["cleanup"] = cleanup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the platform registration-code signup smoke test.")
    parser.add_argument("--api-base", default=os.getenv("CLAWITH_SMOKE_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--frontend-url", default=os.getenv("CLAWITH_SMOKE_FRONTEND_URL", DEFAULT_FRONTEND_URL))
    parser.add_argument("--admin-email", default=os.getenv("CLAWITH_SMOKE_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL))
    parser.add_argument("--admin-password", default=os.getenv("CLAWITH_SMOKE_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD))
    parser.add_argument("--email-domain", default=os.getenv("CLAWITH_SMOKE_EMAIL_DOMAIN", DEFAULT_DOMAIN))
    parser.add_argument("--no-admin-bootstrap", action="store_true", help="Do not create/reset the local smoke platform admin in the DB.")
    parser.add_argument("--keep-code", action="store_true", help="Keep the generated smoke registration code active after the run.")
    parser.add_argument("--leave-gate-enabled", action="store_true", help="Do not restore the previous invitation_code_enabled setting.")
    parser.add_argument("--ui", action="store_true", help="Also verify the registration page exposes the code field via Playwright CLI.")
    parser.add_argument("--playwright-cli", default=os.getenv("PWCLI"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = run_smoke(args)
        json_print(summary)
        return 0
    except SmokeFailure as exc:
        payload = {
            "status": "failed",
            "stage": exc.stage,
            "detail": exc.detail,
            "hint": "This smoke mutates local dev data and restores the registration gate unless --leave-gate-enabled is used.",
        }
        json_print(payload, stream=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level smoke reporter
        payload = {
            "status": "failed",
            "stage": "unexpected_error",
            "detail": repr(exc),
        }
        json_print(payload, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
