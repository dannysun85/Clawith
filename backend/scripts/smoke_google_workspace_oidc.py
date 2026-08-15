#!/usr/bin/env python3
"""Local HTTP acceptance smoke for tenant-managed Google Workspace OIDC.

The script talks to the running Vite proxy, backend, Redis and the loopback
OIDC emulator.  It creates an isolated tenant fixture, proves positive and
negative browser-bound flows, and removes only rows owned by that fixture.

This is local acceptance evidence.  It does not validate a real Google tenant,
DNS, TLS, reverse proxy configuration, or production credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import os
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import delete, func, select, update

from app.config import get_settings
from app.core.events import get_redis
from app.database import async_session
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider, SSOScanSession
from app.models.org import OrgMember
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.google_workspace_oauth import (
    GOOGLE_SSO_CODE_CLAIM_PREFIX,
    GOOGLE_SSO_STATE_PREFIX,
)


FRONTEND_ORIGIN = os.environ.get(
    "G10_FRONTEND_ORIGIN",
    "http://127.0.0.1:3008",
).rstrip("/")
EMULATOR_ORIGIN = os.environ.get(
    "G10_OIDC_EMULATOR_ORIGIN",
    "http://127.0.0.1:8911",
).rstrip("/")
CLIENT_ID = "clawith-local-oidc"
CLIENT_SECRET = "clawith-local-secret"
SUBJECT = "local-workspace-member-001"
HOSTED_DOMAIN = "example.test"
FIXTURE_SLUG_PREFIX = "g10-oidc-"
SSO_SETTING_KEY = "sso_custom_domain_redirect_enabled"


class SmokeFailure(RuntimeError):
    """Raised when an acceptance assertion fails."""


class _ApproveLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.approve_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        if values.get("id") == "approve-login" and values.get("href"):
            self.approve_url = html.unescape(str(values["href"]))


@dataclass(frozen=True)
class Fixture:
    tenant_id: uuid.UUID
    second_tenant_id: uuid.UUID
    provider_id: uuid.UUID
    slug: str
    previous_setting: dict | None


@dataclass(frozen=True)
class AuthorizationAttempt:
    session_id: uuid.UUID
    authorization_url: str
    callback_url: str
    state: str
    code: str


def require(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _single_query_value(url: str, key: str) -> str:
    values = parse_qs(urlparse(url).query).get(key, [])
    require(len(values) == 1 and bool(values[0]), f"missing or ambiguous {key}")
    return values[0]


async def _service_preflight() -> None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        frontend = await client.get(FRONTEND_ORIGIN)
        emulator = await client.get(f"{EMULATOR_ORIGIN}/healthz")
    require(frontend.status_code == 200, "Vite frontend is not reachable on port 3008")
    require(emulator.status_code == 200, "local OIDC emulator is not reachable on port 8911")
    require(
        emulator.json().get("provider") == "local_idp_emulated",
        "port 8911 is not the expected local OIDC emulator",
    )


async def seed_fixture() -> Fixture:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = uuid.uuid4()
    second_tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    slug = f"{FIXTURE_SLUG_PREFIX}{suffix}"

    async with async_session() as db:
        existing_domain = (
            await db.execute(
                select(Tenant.id).where(Tenant.sso_domain == FRONTEND_ORIGIN)
            )
        ).scalar_one_or_none()
        require(existing_domain is None, f"SSO fixture origin is already owned by tenant {existing_domain}")

        setting = await db.get(SystemSetting, SSO_SETTING_KEY, with_for_update=True)
        previous_setting = dict(setting.value) if setting is not None else None
        if setting is None:
            db.add(SystemSetting(key=SSO_SETTING_KEY, value={"enabled": True}))
        else:
            setting.value = {"enabled": True}

        db.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="G10 Local OIDC Company",
                    slug=slug,
                    im_provider="web_only",
                    is_active=True,
                    sso_enabled=True,
                    sso_domain=FRONTEND_ORIGIN,
                ),
                Tenant(
                    id=second_tenant_id,
                    name="G10 Isolation Company",
                    slug=f"{slug}-isolation",
                    im_provider="web_only",
                    is_active=True,
                    sso_enabled=False,
                ),
                IdentityProvider(
                    id=provider_id,
                    provider_type="google_workspace",
                    name="Local Workspace OIDC",
                    is_active=True,
                    sso_login_enabled=True,
                    tenant_id=tenant_id,
                    config={
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "local_oidc_emulator_base_url": EMULATOR_ORIGIN,
                        "jit_provisioning_enabled": True,
                        "jit_allowed_domains": [HOSTED_DOMAIN],
                    },
                ),
            ]
        )
        await db.commit()
    return Fixture(tenant_id, second_tenant_id, provider_id, slug, previous_setting)


async def _assert_fixture_owned(tenant_id: uuid.UUID) -> Tenant:
    async with async_session() as db:
        tenant = await db.get(Tenant, tenant_id)
        require(tenant is not None, "fixture tenant does not exist")
        require(
            str(tenant.slug).startswith(FIXTURE_SLUG_PREFIX),
            "refusing to clean a tenant outside the G10 fixture namespace",
        )
        return tenant


async def cleanup_fixture(
    fixture: Fixture | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
    restore_setting: str | None = None,
) -> dict[str, int]:
    target_tenant_id = fixture.tenant_id if fixture else tenant_id
    require(target_tenant_id is not None, "cleanup requires a fixture tenant ID")
    tenant = await _assert_fixture_owned(target_tenant_id)
    target_slug = str(tenant.slug)

    async with async_session() as db:
        second_ids = list(
            (
                await db.execute(
                    select(Tenant.id).where(Tenant.slug == f"{target_slug}-isolation")
                )
            ).scalars()
        )
        tenant_ids = [target_tenant_id, *second_ids]
        user_rows = (
            await db.execute(
                select(User.id, User.identity_id).where(User.tenant_id.in_(tenant_ids))
            )
        ).all()
        user_ids = [row.id for row in user_rows]
        identity_ids = [row.identity_id for row in user_rows if row.identity_id is not None]

        counts: dict[str, int] = {}
        for name, statement in (
            ("audit_logs", delete(AuditLog).where(AuditLog.tenant_id.in_(tenant_ids))),
            ("sso_sessions", delete(SSOScanSession).where(SSOScanSession.tenant_id.in_(tenant_ids))),
            ("org_members", delete(OrgMember).where(OrgMember.tenant_id.in_(tenant_ids))),
            ("users", delete(User).where(User.id.in_(user_ids)) if user_ids else None),
            (
                "identities",
                delete(Identity).where(Identity.id.in_(identity_ids)) if identity_ids else None,
            ),
            (
                "providers",
                delete(IdentityProvider).where(IdentityProvider.tenant_id.in_(tenant_ids)),
            ),
            ("tenants", delete(Tenant).where(Tenant.id.in_(tenant_ids))),
        ):
            if statement is None:
                counts[name] = 0
                continue
            result = await db.execute(statement)
            counts[name] = max(0, int(getattr(result, "rowcount", 0) or 0))

        setting = await db.get(SystemSetting, SSO_SETTING_KEY, with_for_update=True)
        previous_setting = fixture.previous_setting if fixture else None
        setting_mode = restore_setting or ("absent" if previous_setting is None else "json")
        if setting_mode == "absent":
            if setting is not None:
                await db.delete(setting)
        elif setting_mode == "disabled":
            if setting is None:
                db.add(SystemSetting(key=SSO_SETTING_KEY, value={"enabled": False}))
            else:
                setting.value = {"enabled": False}
        elif setting_mode == "enabled":
            if setting is None:
                db.add(SystemSetting(key=SSO_SETTING_KEY, value={"enabled": True}))
            else:
                setting.value = {"enabled": True}
        elif setting_mode == "json":
            require(previous_setting is not None, "missing prior setting payload")
            if setting is None:
                db.add(SystemSetting(key=SSO_SETTING_KEY, value=previous_setting))
            else:
                setting.value = previous_setting
        else:
            raise SmokeFailure(f"unsupported restore setting mode: {setting_mode}")
        await db.commit()

    redis = await get_redis()
    # Opaque state keys are fixture-specific, but every exercised state is
    # normally consumed.  Delete only keys whose payload names this tenant.
    async for raw_key in redis.scan_iter(match=f"{GOOGLE_SSO_STATE_PREFIX}*"):
        raw_value = await redis.get(raw_key)
        try:
            payload = json.loads(raw_value) if raw_value else {}
        except (TypeError, ValueError):
            payload = {}
        if str(payload.get("tenant_id") or "") == str(target_tenant_id):
            await redis.delete(raw_key)
    return counts


async def _start_authorization(
    client: httpx.AsyncClient,
    tenant_id: uuid.UUID,
) -> AuthorizationAttempt:
    created = await client.post(f"/api/sso/session?tenant_id={tenant_id}")
    require(created.status_code == 200, f"SSO session creation failed: {created.status_code}")
    session_id = uuid.UUID(created.json()["session_id"])

    config = await client.get("/api/sso/config", params={"sid": str(session_id)})
    require(config.status_code == 200, f"SSO config failed: {config.status_code}")
    providers = config.json()
    require(len(providers) == 1, "fixture must expose exactly one SSO provider")
    provider = providers[0]
    require(provider.get("provider_type") == "google_workspace", "wrong provider type")
    authorization_url = str(provider.get("url") or "")
    parsed_authorization = urlparse(authorization_url)
    require(
        f"{parsed_authorization.scheme}://{parsed_authorization.netloc}" == EMULATOR_ORIGIN,
        "authorization was not routed to the explicit loopback emulator",
    )
    state = _single_query_value(authorization_url, "state")
    require(state.startswith("gwsso."), "SSO state is not opaque server-owned state")
    require(_single_query_value(authorization_url, "code_challenge_method") == "S256", "PKCE S256 missing")
    require(bool(_single_query_value(authorization_url, "code_challenge")), "PKCE challenge missing")
    require(bool(_single_query_value(authorization_url, "nonce")), "OIDC nonce missing")
    require(
        _single_query_value(authorization_url, "redirect_uri")
        == f"{FRONTEND_ORIGIN}/api/auth/google_workspace/callback",
        "callback redirect is not bound to the tenant origin",
    )

    consent = await client.get(authorization_url)
    require(consent.status_code == 200, f"OIDC consent failed: {consent.status_code}")
    parser = _ApproveLinkParser()
    parser.feed(consent.text)
    require(parser.approve_url, "OIDC emulator did not render its approve link")
    approved = await client.get(str(parser.approve_url))
    require(approved.status_code == 302, f"OIDC approval failed: {approved.status_code}")
    callback_url = str(approved.headers.get("location") or "")
    require(callback_url.startswith(f"{FRONTEND_ORIGIN}/api/"), "OIDC callback left the local app origin")
    require(_single_query_value(callback_url, "state") == state, "provider did not preserve state")
    code = _single_query_value(callback_url, "code")
    return AuthorizationAttempt(session_id, authorization_url, callback_url, state, code)


async def _assert_terminal_error(
    client: httpx.AsyncClient,
    attempt: AuthorizationAttempt,
    expected_fragment: str,
) -> None:
    status_response = await client.get(f"/api/sso/session/{attempt.session_id}/status")
    require(status_response.status_code == 200, "failed SSO status could not be read")
    payload = status_response.json()
    require(payload.get("status") == "completed", "failed SSO session did not become terminal")
    require(
        expected_fragment.casefold() in str(payload.get("error_msg") or "").casefold(),
        f"failed SSO session did not expose safe recovery text containing {expected_fragment!r}",
    )


async def _consume_success(
    client: httpx.AsyncClient,
    attempt: AuthorizationAttempt,
) -> dict:
    status_response = await client.get(f"/api/sso/session/{attempt.session_id}/status")
    require(status_response.status_code == 200, "authorized status could not be read")
    require(status_response.json().get("status") == "authorized", "SSO session was not authorized")
    consumed = await client.post(
        f"/api/sso/session/{attempt.session_id}/consume",
        headers={"x-astra-sso-session": str(attempt.session_id)},
    )
    require(consumed.status_code == 200, "authorized SSO session could not be consumed")
    payload = consumed.json()
    require(payload.get("status") == "authorized", "SSO consume did not return authorized")
    require(payload.get("user", {}).get("role") == "member", "JIT membership was not member-only")
    require(bool(payload.get("user", {}).get("tenant_id")), "JIT response omitted its company membership")
    require(
        "work" in payload.get("user", {}).get("available_surfaces", []),
        "JIT response omitted the ordinary employee work surface",
    )
    token = str(payload.get("access_token") or "")
    require(bool(token), "SSO consume did not return its one-time access token")
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    require(me.status_code == 200, "SSO-issued access token could not authenticate /auth/me")
    require(me.json().get("role") == "member", "authenticated SSO role was not member")
    replay = await client.post(
        f"/api/sso/session/{attempt.session_id}/consume",
        headers={"x-astra-sso-session": str(attempt.session_id)},
    )
    require(replay.status_code == 403, "consumed session cookie was not cleared")
    payload.pop("access_token", None)
    return payload


async def _tenant_counts(tenant_id: uuid.UUID) -> dict[str, int]:
    async with async_session() as db:
        async def count(model, predicate) -> int:
            value = await db.scalar(select(func.count()).select_from(model).where(predicate))
            return int(value or 0)

        return {
            "users": await count(User, User.tenant_id == tenant_id),
            "identities": int(
                await db.scalar(
                    select(func.count())
                    .select_from(Identity)
                    .join(User, User.identity_id == Identity.id)
                    .where(User.tenant_id == tenant_id)
                )
                or 0
            ),
            "members": await count(OrgMember, OrgMember.tenant_id == tenant_id),
        }


async def _assert_jit_persistence(fixture: Fixture) -> None:
    async with async_session() as db:
        user = (
            await db.execute(
                select(User).where(User.tenant_id == fixture.tenant_id)
            )
        ).scalar_one()
        identity = await db.get(Identity, user.identity_id)
        provider_member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == fixture.tenant_id,
                    OrgMember.provider_id == fixture.provider_id,
                    OrgMember.external_id == SUBJECT,
                )
            )
        ).scalar_one()
        success_audits = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == fixture.tenant_id,
                        AuditLog.action == "enterprise_sso_login_succeeded",
                    )
                )
            ).scalars()
        )
    require(user.role == "member" and user.is_active, "JIT created an invalid membership")
    require(user.registration_source == "google_workspace", "JIT registration source was not preserved")
    require(identity is not None, "JIT user has no isolated Identity")
    require(identity.email is None, "provider email was incorrectly promoted to global ownership")
    require(not identity.password_login_enabled, "JIT Identity unexpectedly permits password login")
    require(provider_member.user_id == user.id, "provider subject was not linked to the JIT membership")
    require(
        any(
            audit.details.get("jit_provisioned") is True
            and audit.details.get("membership_role") == "member"
            for audit in success_audits
        ),
        "JIT success audit is missing member-only evidence",
    )


async def _assert_failure_audits(fixture: Fixture) -> None:
    async with async_session() as db:
        failures = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == fixture.tenant_id,
                        AuditLog.action == "enterprise_sso_login_failed",
                    )
                )
            ).scalars()
        )
    codes = {str(row.details.get("error_code") or "") for row in failures}
    require("authorization_code_replayed" in codes, "code replay was not audited")
    require("provider_disabled" in codes, "disabled provider rejection was not audited")
    require(
        all("code" not in row.details and "state" not in row.details for row in failures),
        "audit details contain reusable OAuth credentials",
    )


async def _delete_code_claim(provider_id: uuid.UUID, code: str) -> None:
    settings = get_settings()
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        f"{provider_id}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    redis = await get_redis()
    await redis.delete(f"{GOOGLE_SSO_CODE_CLAIM_PREFIX}{digest}")


async def run_http_smoke(fixture: Fixture) -> dict[str, object]:
    await _service_preflight()
    async with httpx.AsyncClient(
        base_url=FRONTEND_ORIGIN,
        timeout=20,
        follow_redirects=False,
    ) as primary_client:
        initial_counts = await _tenant_counts(fixture.tenant_id)
        retired = await primary_client.post(
            "/api/auth/register/sso",
            json={"provider": "google", "code": "not-a-real-code"},
        )
        require(retired.status_code == 410, "retired public social registration route did not return 410")
        legacy = await primary_client.post(
            "/api/auth/register",
            json={
                "username": f"social-{uuid.uuid4().hex[:8]}",
                "email": f"social-{uuid.uuid4().hex[:8]}@example.com",
                "password": "local-only-password",
                "provider": "github",
                "provider_code": "not-a-real-code",
            },
        )
        require(legacy.status_code == 410, "legacy public social signup branch did not return 410")
        require(await _tenant_counts(fixture.tenant_id) == initial_counts, "public social signup created tenant data")

        invalid_state = await primary_client.get(
            "/api/auth/google_workspace/callback",
            params={"state": "gwsso.invalid", "code": "invalid"},
        )
        require(invalid_state.status_code == 400, "invalid state did not fail closed")
        require(await _tenant_counts(fixture.tenant_id) == initial_counts, "invalid state created tenant data")

        first = await _start_authorization(primary_client, fixture.tenant_id)
        async with httpx.AsyncClient(
            base_url=FRONTEND_ORIGIN,
            timeout=20,
            follow_redirects=False,
        ) as wrong_browser:
            wrong_browser_response = await wrong_browser.get(first.callback_url)
        require(wrong_browser_response.status_code == 403, "wrong browser was not rejected")

        first_callback = await primary_client.get(first.callback_url)
        require(first_callback.status_code == 200, "correct browser could not finish after wrong-browser rejection")
        require("SSO login successful" in first_callback.text, "success callback did not render recovery redirect")
        used_state = await primary_client.get(first.callback_url)
        require(used_state.status_code == 400, "used state was accepted twice")
        await _consume_success(primary_client, first)
        await _assert_jit_persistence(fixture)

        baseline_after_jit = await _tenant_counts(fixture.tenant_id)
        replay = await _start_authorization(primary_client, fixture.tenant_id)
        replay_url = replay.callback_url.replace(
            f"code={replay.code}",
            f"code={first.code}",
            1,
        )
        replay_response = await primary_client.get(replay_url)
        require(replay_response.status_code == 200, "code replay did not enter safe error recovery")
        await _assert_terminal_error(primary_client, replay, "already used")
        require(await _tenant_counts(fixture.tenant_id) == baseline_after_jit, "code replay changed JIT data")

        disabled = await _start_authorization(primary_client, fixture.tenant_id)
        async with async_session() as db:
            await db.execute(
                update(IdentityProvider)
                .where(IdentityProvider.id == fixture.provider_id)
                .values(sso_login_enabled=False)
            )
            await db.commit()
        disabled_response = await primary_client.get(disabled.callback_url)
        require(disabled_response.status_code == 200, "disabled provider did not enter safe error recovery")
        await _assert_terminal_error(primary_client, disabled, "no longer available")
        async with async_session() as db:
            await db.execute(
                update(IdentityProvider)
                .where(IdentityProvider.id == fixture.provider_id)
                .values(sso_login_enabled=True)
            )
            await db.commit()
        require(await _tenant_counts(fixture.tenant_id) == baseline_after_jit, "disabled provider changed JIT data")

        wrong_tenant = await _start_authorization(primary_client, fixture.tenant_id)
        async with async_session() as db:
            await db.execute(
                update(SSOScanSession)
                .where(SSOScanSession.id == wrong_tenant.session_id)
                .values(tenant_id=fixture.second_tenant_id)
            )
            await db.commit()
        wrong_tenant_response = await primary_client.get(wrong_tenant.callback_url)
        require(wrong_tenant_response.status_code == 400, "changed tenant binding was not rejected")
        async with async_session() as db:
            await db.execute(
                update(SSOScanSession)
                .where(SSOScanSession.id == wrong_tenant.session_id)
                .values(tenant_id=fixture.tenant_id)
            )
            await db.commit()
        recovered_tenant = await primary_client.get(wrong_tenant.callback_url)
        require(recovered_tenant.status_code == 200, "tenant mismatch burned otherwise valid state")
        await _consume_success(primary_client, wrong_tenant)
        require(await _tenant_counts(fixture.tenant_id) == baseline_after_jit, "repeat SSO duplicated JIT data")

        await _assert_failure_audits(fixture)
        await _delete_code_claim(fixture.provider_id, first.code)
        await _delete_code_claim(fixture.provider_id, wrong_tenant.code)
        return {
            "assertions": 46,
            "tenant_id": str(fixture.tenant_id),
            "jit_counts": baseline_after_jit,
            "public_social_signup": "sign_in_only",
            "wrong_browser": "rejected_without_burning_state",
            "state_replay": "rejected",
            "code_replay": "rejected_and_audited",
            "wrong_tenant": "rejected_without_burning_state",
            "disabled_provider": "rejected_and_audited",
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=("smoke", "seed-browser", "cleanup-browser"),
        nargs="?",
        default="smoke",
    )
    parser.add_argument("--tenant-id", type=uuid.UUID)
    parser.add_argument(
        "--restore-setting",
        choices=("absent", "disabled", "enabled"),
        default="absent",
    )
    args = parser.parse_args()

    if args.mode == "cleanup-browser":
        require(args.tenant_id is not None, "cleanup-browser requires --tenant-id")
        counts = await cleanup_fixture(
            tenant_id=args.tenant_id,
            restore_setting=args.restore_setting,
        )
        print(json.dumps({"cleanup": "passed", "deleted": counts}, sort_keys=True))
        return

    fixture = await seed_fixture()
    if args.mode == "seed-browser":
        print(
            json.dumps(
                {
                    "fixture": "ready",
                    "tenant_id": str(fixture.tenant_id),
                    "provider": "local_idp_emulated",
                    "restore_setting": "absent" if fixture.previous_setting is None else "enabled",
                },
                sort_keys=True,
            )
        )
        return

    try:
        result = await run_http_smoke(fixture)
        print(json.dumps({"smoke": "passed", **result}, sort_keys=True))
    finally:
        counts = await cleanup_fixture(fixture)
        print(json.dumps({"cleanup": "passed", "deleted": counts}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
