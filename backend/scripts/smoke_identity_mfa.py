"""Exercise Identity-level MFA against local PostgreSQL and the live HTTP API.

This script creates isolated QA identities and tenants, verifies the complete
MFA lifecycle through HTTP, inspects encrypted/digested persistence, and then
removes only the rows it created. It never prints a TOTP seed, factor, password,
recovery code, challenge token, or access token.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

import httpx
from sqlalchemy import delete, or_, select, update

from app.core.security import decode_access_token, hash_password_async
from app.database import async_session, engine
from app.models.audit import AuditLog
from app.models.identity_mfa import IdentityMfaChallenge, IdentityMfaRecoveryCode
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.mfa_service import RECOVERY_CODE_COUNT, totp_code


QA_PASSWORD = "G9-Local-Only-Password!"


class SmokeFailure(RuntimeError):
    """A sanitized smoke failure that cannot disclose submitted credentials."""


@dataclass
class Fixture:
    run_tag: str
    tenant_ids: set[uuid.UUID] = field(default_factory=set)
    identity_ids: set[uuid.UUID] = field(default_factory=set)
    user_ids: set[uuid.UUID] = field(default_factory=set)
    emails: dict[str, str] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    tenants: dict[str, uuid.UUID] = field(default_factory=dict)
    identities: dict[str, uuid.UUID] = field(default_factory=dict)


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise SmokeFailure(label)


def _error_code(response: httpx.Response) -> str | None:
    try:
        detail = response.json().get("detail")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(detail, dict):
        code = detail.get("code")
        return str(code) if code is not None else None
    return None


def _expect_json(
    response: httpx.Response,
    *,
    status_code: int = 200,
    label: str,
) -> dict[str, object]:
    if response.status_code != status_code:
        raise SmokeFailure(
            f"{label}: expected_http={status_code} actual_http={response.status_code} "
            f"error_code={_error_code(response) or 'none'}"
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{label}: response_not_json") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{label}: response_not_object")
    return payload


def _expect_error(
    response: httpx.Response,
    *,
    status_code: int,
    code: str | None,
    label: str,
) -> None:
    if response.status_code != status_code or (
        code is not None and _error_code(response) != code
    ):
        raise SmokeFailure(
            f"{label}: expected_http={status_code} expected_code={code or 'any'} "
            f"actual_http={response.status_code} actual_code={_error_code(response) or 'none'}"
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_fixture() -> Fixture:
    run_tag = uuid.uuid4().hex[:12]
    fixture = Fixture(run_tag=run_tag)
    password_hash = await hash_password_async(QA_PASSWORD)

    identities: dict[str, Identity] = {}
    for name, is_platform in (
        ("owner", False),
        ("member", False),
        ("org_admin", False),
        ("second_owner", False),
        ("outside_member", False),
        ("platform", True),
    ):
        identity = Identity(
            email=f"g9-{name}-{run_tag}@local.clawith.test",
            username=f"g9{name}{run_tag}",
            password_hash=password_hash,
            password_login_enabled=True,
            email_verified=True,
            is_active=True,
            is_platform_admin=is_platform,
        )
        identities[name] = identity

    async with async_session() as db:
        db.add_all(identities.values())
        await db.flush()

        first_tenant = Tenant(
            name=f"G9 MFA Primary {run_tag}",
            slug=f"g9-mfa-primary-{run_tag}",
            im_provider="web_only",
            is_active=True,
        )
        second_tenant = Tenant(
            name=f"G9 MFA Secondary {run_tag}",
            slug=f"g9-mfa-secondary-{run_tag}",
            im_provider="web_only",
            is_active=True,
        )
        db.add_all([first_tenant, second_tenant])
        await db.flush()

        users = {
            "owner": User(
                identity_id=identities["owner"].id,
                tenant_id=first_tenant.id,
                display_name="G9 Owner",
                role="org_owner",
                is_active=True,
            ),
            "member": User(
                identity_id=identities["member"].id,
                tenant_id=first_tenant.id,
                display_name="G9 Member",
                role="member",
                is_active=True,
            ),
            "org_admin": User(
                identity_id=identities["org_admin"].id,
                tenant_id=first_tenant.id,
                display_name="G9 Org Admin",
                role="org_admin",
                is_active=True,
            ),
            "second_owner": User(
                identity_id=identities["second_owner"].id,
                tenant_id=second_tenant.id,
                display_name="G9 Secondary Owner",
                role="org_owner",
                is_active=True,
            ),
            "outside_member": User(
                identity_id=identities["outside_member"].id,
                tenant_id=second_tenant.id,
                display_name="G9 Outside Member",
                role="member",
                is_active=True,
            ),
            "platform": User(
                identity_id=identities["platform"].id,
                tenant_id=None,
                display_name="G9 Platform Operator",
                role="platform_admin",
                is_active=True,
            ),
        }
        db.add_all(users.values())
        await db.flush()
        first_tenant.owner_user_id = users["owner"].id
        second_tenant.owner_user_id = users["second_owner"].id
        await db.commit()

    fixture.tenant_ids.update({first_tenant.id, second_tenant.id})
    fixture.identity_ids.update(identity.id for identity in identities.values())
    fixture.user_ids.update(user.id for user in users.values())
    fixture.emails.update({name: identity.email or "" for name, identity in identities.items()})
    fixture.users.update({name: user.id for name, user in users.items()})
    fixture.tenants.update({"primary": first_tenant.id, "secondary": second_tenant.id})
    fixture.identities.update({name: identity.id for name, identity in identities.items()})
    return fixture


async def _cleanup_fixture(fixture: Fixture) -> None:
    async with async_session() as db:
        await db.rollback()
        await db.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.user_id.in_(fixture.user_ids),
                    AuditLog.tenant_id.in_(fixture.tenant_ids),
                )
            )
        )
        await db.execute(
            delete(IdentityMfaChallenge).where(
                IdentityMfaChallenge.identity_id.in_(fixture.identity_ids)
            )
        )
        await db.execute(
            delete(IdentityMfaRecoveryCode).where(
                IdentityMfaRecoveryCode.identity_id.in_(fixture.identity_ids)
            )
        )
        await db.execute(
            update(Tenant)
            .where(Tenant.id.in_(fixture.tenant_ids))
            .values(
                owner_user_id=None,
                initialized_by_user_id=None,
                deletion_requested_by_user_id=None,
            )
        )
        await db.execute(delete(User).where(User.id.in_(fixture.user_ids)))
        await db.execute(delete(Tenant).where(Tenant.id.in_(fixture.tenant_ids)))
        await db.execute(delete(Identity).where(Identity.id.in_(fixture.identity_ids)))
        await db.commit()


async def _login(
    client: httpx.AsyncClient,
    email: str,
    *,
    tenant_id: uuid.UUID | None = None,
    label: str,
) -> dict[str, object]:
    body: dict[str, str] = {
        "login_identifier": email,
        "password": QA_PASSWORD,
    }
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return _expect_json(
        await client.post("/api/auth/login", json=body),
        label=label,
    )


async def _safe_previous_code(secret: str) -> str:
    phase = int(time.time()) % 30
    if phase > 22:
        await asyncio.sleep(31 - phase)
    now = datetime.now(timezone.utc)
    return totp_code(secret, at_time=now - timedelta(seconds=30))


async def _bootstrap_required_identity(
    client: httpx.AsyncClient,
    *,
    email: str,
    tenant_id: uuid.UUID | None = None,
    label: str,
) -> tuple[str, list[str], str]:
    login = await _login(client, email, tenant_id=tenant_id, label=f"{label}.login")
    _require(login.get("requires_mfa_setup") is True, f"{label}.bootstrap_gate_missing")
    _require("access_token" not in login, f"{label}.bootstrap_leaked_access_token")
    challenge = str(login.get("challenge_token") or "")
    setup = _expect_json(
        await client.post(
            "/api/auth/mfa/bootstrap/setup",
            json={"challenge_token": challenge},
        ),
        label=f"{label}.bootstrap_setup",
    )
    secret = str(setup.get("secret") or "")
    _require(len(secret) >= 32, f"{label}.secret_shape")
    _require(
        str(setup.get("provisioning_uri") or "").startswith("otpauth://totp/"),
        f"{label}.provisioning_uri",
    )
    confirmed = _expect_json(
        await client.post(
            "/api/auth/mfa/setup/confirm",
            json={
                "challenge_token": challenge,
                "code": await _safe_previous_code(secret),
            },
        ),
        label=f"{label}.confirm",
    )
    token = str(confirmed.get("access_token") or "")
    recovery_codes = list(confirmed.get("recovery_codes") or [])
    _require(len(recovery_codes) == RECOVERY_CODE_COUNT, f"{label}.recovery_count")
    _require(decode_access_token(token).get("mfa") is True, f"{label}.token_assurance")
    return token, [str(code) for code in recovery_codes], secret


async def _enroll_optional_identity(
    client: httpx.AsyncClient,
    *,
    token: str,
    label: str,
) -> tuple[str, list[str], str]:
    setup = _expect_json(
        await client.post(
            "/api/auth/mfa/setup",
            headers=_bearer(token),
            json={"current_password": QA_PASSWORD},
        ),
        label=f"{label}.setup",
    )
    challenge = str(setup.get("challenge_token") or "")
    secret = str(setup.get("secret") or "")
    confirmed = _expect_json(
        await client.post(
            "/api/auth/mfa/setup/confirm",
            json={
                "challenge_token": challenge,
                "code": await _safe_previous_code(secret),
            },
        ),
        label=f"{label}.confirm",
    )
    access_token = str(confirmed.get("access_token") or "")
    recovery_codes = [str(code) for code in list(confirmed.get("recovery_codes") or [])]
    _require(len(recovery_codes) == RECOVERY_CODE_COUNT, f"{label}.recovery_count")
    return access_token, recovery_codes, secret


async def _assert_encrypted_storage(
    *,
    identity_id: uuid.UUID,
    secret: str,
    recovery_codes: list[str],
) -> None:
    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        _require(identity is not None, "storage.identity_missing")
        _require(identity.mfa_secret_envelope is not None, "storage.secret_missing")
        _require(secret not in identity.mfa_secret_envelope, "storage.raw_secret_persisted")
        rows = list(
            (
                await db.execute(
                    select(IdentityMfaRecoveryCode).where(
                        IdentityMfaRecoveryCode.identity_id == identity_id,
                        IdentityMfaRecoveryCode.used_at.is_(None),
                    )
                )
            ).scalars()
        )
        _require(len(rows) == RECOVERY_CODE_COUNT, "storage.recovery_row_count")
        for code in recovery_codes:
            _require(
                all(code not in row.code_hash for row in rows),
                "storage.raw_recovery_code_persisted",
            )


async def _add_second_membership(fixture: Fixture) -> uuid.UUID:
    second_membership = User(
        identity_id=fixture.identities["member"],
        tenant_id=fixture.tenants["secondary"],
        display_name="G9 Member Secondary",
        role="member",
        is_active=True,
    )
    async with async_session() as db:
        db.add(second_membership)
        await db.commit()
    fixture.user_ids.add(second_membership.id)
    fixture.users["member_secondary"] = second_membership.id
    return second_membership.id


async def _assert_audit_safety(
    fixture: Fixture,
    sensitive_values: list[str],
) -> int:
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.user_id.in_(fixture.user_ids),
                        AuditLog.action.like("mfa_%"),
                    )
                )
            ).scalars()
        )
    actions = {row.action for row in rows}
    required_actions = {
        "mfa_enabled",
        "mfa_login_verified",
        "mfa_recovery_codes_rotated",
        "mfa_disabled",
        "mfa_administratively_reset",
    }
    _require(required_actions.issubset(actions), "audit.required_actions_missing")
    serialized = json.dumps([row.details for row in rows], sort_keys=True)
    for value in sensitive_values:
        if value:
            _require(value not in serialized, "audit.sensitive_value_persisted")
    return len(rows)


async def _run(base_url: str) -> dict[str, int]:
    fixture = await _seed_fixture()
    sensitive_values = [QA_PASSWORD]
    assertions = 0
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
            owner_token, owner_recovery, owner_secret = await _bootstrap_required_identity(
                client,
                email=fixture.emails["owner"],
                label="owner",
            )
            sensitive_values.extend([owner_secret, *owner_recovery])
            await _assert_encrypted_storage(
                identity_id=fixture.identities["owner"],
                secret=owner_secret,
                recovery_codes=owner_recovery,
            )
            assertions += 5

            status_payload = _expect_json(
                await client.get(
                    "/api/auth/mfa/status",
                    headers=_bearer(owner_token),
                ),
                label="owner.status",
            )
            _require(status_payload.get("enabled") is True, "owner.status.enabled")
            _require(status_payload.get("required") is True, "owner.status.required")
            _require(
                status_payload.get("recovery_codes_remaining") == RECOVERY_CODE_COUNT,
                "owner.status.recovery_count",
            )

            login_challenge = await _login(
                client,
                fixture.emails["owner"],
                label="owner.totp_login",
            )
            _require(login_challenge.get("requires_mfa") is True, "owner.login_gate")
            challenge_token = str(login_challenge.get("challenge_token") or "")
            totp_verified = _expect_json(
                await client.post(
                    "/api/auth/mfa/challenge/verify",
                    json={
                        "challenge_token": challenge_token,
                        "code": totp_code(owner_secret),
                    },
                ),
                label="owner.totp_verify",
            )
            owner_totp_token = str(totp_verified.get("access_token") or "")
            _require(
                decode_access_token(owner_totp_token).get("mfa") is True,
                "owner.totp_token_assurance",
            )
            _expect_error(
                await client.post(
                    "/api/auth/mfa/challenge/verify",
                    json={"challenge_token": challenge_token, "code": "000000"},
                ),
                status_code=401,
                code=None,
                label="owner.challenge_replay",
            )
            assertions += 6

            recovery_login = await _login(
                client,
                fixture.emails["owner"],
                label="owner.recovery_login",
            )
            recovery_verified = _expect_json(
                await client.post(
                    "/api/auth/mfa/challenge/verify",
                    json={
                        "challenge_token": recovery_login["challenge_token"],
                        "code": owner_recovery[0],
                    },
                ),
                label="owner.recovery_verify",
            )
            owner_recovery_token = str(recovery_verified.get("access_token") or "")

            reused_recovery_login = await _login(
                client,
                fixture.emails["owner"],
                label="owner.reused_recovery_login",
            )
            _expect_error(
                await client.post(
                    "/api/auth/mfa/challenge/verify",
                    json={
                        "challenge_token": reused_recovery_login["challenge_token"],
                        "code": owner_recovery[0],
                    },
                ),
                status_code=401,
                code="mfa_code_invalid",
                label="owner.recovery_replay",
            )

            rotated = _expect_json(
                await client.post(
                    "/api/auth/mfa/recovery-codes/rotate",
                    headers=_bearer(owner_recovery_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "code": owner_recovery[1],
                    },
                ),
                label="owner.rotate_recovery",
            )
            rotated_owner_token = str(rotated.get("access_token") or "")
            rotated_owner_recovery = [
                str(code) for code in list(rotated.get("recovery_codes") or [])
            ]
            sensitive_values.extend(rotated_owner_recovery)
            _require(
                len(rotated_owner_recovery) == RECOVERY_CODE_COUNT,
                "owner.rotated_recovery_count",
            )
            _expect_error(
                await client.get("/api/tenants/me", headers=_bearer(owner_recovery_token)),
                status_code=401,
                code=None,
                label="owner.old_token_revoked",
            )
            _expect_json(
                await client.get("/api/tenants/me", headers=_bearer(rotated_owner_token)),
                label="owner.rotated_token_live",
            )
            _expect_error(
                await client.post(
                    "/api/auth/mfa/disable",
                    headers=_bearer(rotated_owner_token),
                    json={"current_password": QA_PASSWORD, "code": "000000"},
                ),
                status_code=409,
                code="mfa_required_by_role",
                label="owner.disable_forbidden",
            )
            assertions += 7

            member_login = await _login(
                client,
                fixture.emails["member"],
                tenant_id=fixture.tenants["primary"],
                label="member.initial_login",
            )
            member_plain_token = str(member_login.get("access_token") or "")
            _require(
                decode_access_token(member_plain_token).get("mfa") is False,
                "member.initial_assurance",
            )
            member_token, member_recovery, member_secret = await _enroll_optional_identity(
                client,
                token=member_plain_token,
                label="member.first_enrollment",
            )
            sensitive_values.extend([member_secret, *member_recovery])
            disabled = _expect_json(
                await client.post(
                    "/api/auth/mfa/disable",
                    headers=_bearer(member_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "code": totp_code(member_secret),
                    },
                ),
                label="member.disable",
            )
            disabled_member_token = str(disabled.get("access_token") or "")
            _require(disabled.get("requires_setup") is False, "member.disable_policy")
            _require(
                decode_access_token(disabled_member_token).get("mfa") is False,
                "member.disabled_assurance",
            )
            _expect_json(
                await client.get("/api/tenants/me", headers=_bearer(disabled_member_token)),
                label="member.disabled_token_live",
            )
            assertions += 5

            member_token, member_recovery, member_secret = await _enroll_optional_identity(
                client,
                token=disabled_member_token,
                label="member.second_enrollment",
            )
            sensitive_values.extend([member_secret, *member_recovery])
            company_reset = _expect_json(
                await client.post(
                    f"/api/auth/mfa/admin/reset/{fixture.users['member']}",
                    headers=_bearer(rotated_owner_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "reason": "Local G9 single-company recovery verification",
                    },
                ),
                label="company_admin.single_scope_reset",
            )
            _require(company_reset.get("requires_setup") is False, "company_reset.policy")
            _expect_error(
                await client.get("/api/tenants/me", headers=_bearer(member_token)),
                status_code=401,
                code=None,
                label="company_reset.revokes_target_token",
            )

            _expect_error(
                await client.post(
                    f"/api/auth/mfa/admin/reset/{fixture.users['outside_member']}",
                    headers=_bearer(rotated_owner_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "reason": "Local G9 cross-company negative verification",
                    },
                ),
                status_code=403,
                code="mfa_reset_scope_forbidden",
                label="company_admin.cross_tenant_forbidden",
            )
            _expect_error(
                await client.post(
                    f"/api/auth/mfa/admin/reset/{fixture.users['org_admin']}",
                    headers=_bearer(rotated_owner_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "reason": "Local G9 privilege-boundary negative verification",
                    },
                ),
                status_code=403,
                code="mfa_reset_privilege_forbidden",
                label="company_admin.admin_reset_forbidden",
            )
            assertions += 5

            await _add_second_membership(fixture)
            multi_member_login = await _login(
                client,
                fixture.emails["member"],
                tenant_id=fixture.tenants["primary"],
                label="member.multi_scope_login",
            )
            multi_member_token, multi_recovery, multi_secret = await _enroll_optional_identity(
                client,
                token=str(multi_member_login.get("access_token") or ""),
                label="member.multi_scope_enrollment",
            )
            sensitive_values.extend([multi_secret, *multi_recovery])
            _expect_error(
                await client.post(
                    f"/api/auth/mfa/admin/reset/{fixture.users['member']}",
                    headers=_bearer(rotated_owner_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "reason": "Local G9 global Identity scope verification",
                    },
                ),
                status_code=409,
                code="global_identity_reset_requires_platform_operator",
                label="company_admin.multi_scope_forbidden",
            )

            platform_token, platform_recovery, platform_secret = (
                await _bootstrap_required_identity(
                    client,
                    email=fixture.emails["platform"],
                    label="platform_operator",
                )
            )
            sensitive_values.extend([platform_secret, *platform_recovery])
            platform_reset = _expect_json(
                await client.post(
                    f"/api/auth/mfa/admin/reset/{fixture.users['member']}",
                    headers=_bearer(platform_token),
                    json={
                        "current_password": QA_PASSWORD,
                        "reason": "Local G9 platform recovery verification",
                    },
                ),
                label="platform_operator.multi_scope_reset",
            )
            _require(platform_reset.get("ok") is True, "platform_reset.result")
            _expect_error(
                await client.get("/api/tenants/me", headers=_bearer(multi_member_token)),
                status_code=401,
                code=None,
                label="platform_reset.revokes_target_token",
            )
            assertions += 5

            audit_count = await _assert_audit_safety(fixture, sensitive_values)
            assertions += 2
            return {"assertions": assertions, "audit_rows": audit_count}
    finally:
        await _cleanup_fixture(fixture)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()

    async def run_and_dispose() -> dict[str, int]:
        try:
            return await _run(args.base_url.rstrip("/"))
        finally:
            await engine.dispose()

    result = asyncio.run(run_and_dispose())
    print(
        "identity_mfa_http_postgres_smoke=passed "
        f"assertions={result['assertions']} audit_rows={result['audit_rows']} qa_cleanup=passed"
    )


if __name__ == "__main__":
    main()
