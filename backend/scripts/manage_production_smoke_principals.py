#!/usr/bin/env python3
"""Manage dedicated production release-smoke principals safely.

This is deliberately narrower than a general user-management command.  It may
only touch a tenant whose name and slug identify it as a Release QA tenant, an
existing Release QA owner, one dedicated ordinary member, and one temporary
tenantless platform operator.  Customer identities and ordinary platform
administrators are rejected before any write.

Credentials are read from the same seven-key JSON contract used by the formal
production deployment.  The command never emits email addresses, password
hashes, passwords, tokens, or credential-file contents.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Any

from sqlalchemy import select, text

from app.core.identity_canonicalization import canonicalize_email
from app.core.security import hash_password_async
from app.database import async_session
from app.models.audit import AuditLog
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User


REQUIRED_CREDENTIAL_KEYS = frozenset(
    {
        "SMOKE_TENANT_EMAIL",
        "SMOKE_TENANT_PASSWORD",
        "SMOKE_TENANT_ID",
        "SMOKE_PLATFORM_ADMIN_EMAIL",
        "SMOKE_PLATFORM_ADMIN_PASSWORD",
        "SMOKE_MEMBER_EMAIL",
        "SMOKE_MEMBER_PASSWORD",
    }
)
RELEASE_SMOKE_SOURCE = "release_smoke"
SYNTHETIC_EMAIL_SUFFIX = "@release-smoke.invalid"
RECEIPT_ACTIONS = {
    "provision": "production_smoke_principals_provisioned",
    "deactivate-platform": "production_smoke_platform_principal_deactivated",
}
MAX_CREDENTIAL_FILE_BYTES = 16_384


class PrincipalManagerError(RuntimeError):
    """A stable, non-secret safety failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SmokeCredentials:
    tenant_email: str
    tenant_password: str = field(repr=False)
    tenant_id: uuid.UUID
    platform_email: str
    platform_password: str = field(repr=False)
    member_email: str
    member_password: str = field(repr=False)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PrincipalManagerError(code)


def _valid_email(value: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+", value) is not None


def _strong_password(value: str) -> bool:
    return (
        20 <= len(value) <= 4096
        and any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
        and any(not character.isalnum() for character in value)
    )


def _canonical_required_email(value: object, code: str) -> str:
    _require(isinstance(value, str), code)
    canonical = canonicalize_email(value)
    _require(bool(canonical and _valid_email(canonical)), code)
    return canonical or ""


def parse_credentials(payload: object) -> SmokeCredentials:
    """Validate the exact deploy credential contract without leaking values."""

    _require(isinstance(payload, dict), "credentials_object_required")
    _require(set(payload) == REQUIRED_CREDENTIAL_KEYS, "credentials_exact_keys_required")
    _require(
        all(isinstance(value, str) and 0 < len(value) <= 4096 for value in payload.values()),
        "credentials_nonempty_strings_required",
    )
    tenant_email = _canonical_required_email(
        payload["SMOKE_TENANT_EMAIL"],
        "tenant_email_invalid",
    )
    platform_email = _canonical_required_email(
        payload["SMOKE_PLATFORM_ADMIN_EMAIL"],
        "platform_email_invalid",
    )
    member_email = _canonical_required_email(
        payload["SMOKE_MEMBER_EMAIL"],
        "member_email_invalid",
    )
    _require(
        len({tenant_email, platform_email, member_email}) == 3,
        "principal_emails_must_be_distinct",
    )
    _require(
        platform_email.endswith(SYNTHETIC_EMAIL_SUFFIX),
        "platform_email_must_be_release_smoke_synthetic",
    )
    _require(
        member_email.endswith(SYNTHETIC_EMAIL_SUFFIX),
        "member_email_must_be_release_smoke_synthetic",
    )
    try:
        tenant_id = uuid.UUID(payload["SMOKE_TENANT_ID"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise PrincipalManagerError("tenant_id_invalid") from exc

    passwords = (
        payload["SMOKE_TENANT_PASSWORD"],
        payload["SMOKE_PLATFORM_ADMIN_PASSWORD"],
        payload["SMOKE_MEMBER_PASSWORD"],
    )
    _require(all(_strong_password(value) for value in passwords), "strong_passwords_required")
    _require(len(set(passwords)) == len(passwords), "principal_passwords_must_be_distinct")
    return SmokeCredentials(
        tenant_email=tenant_email,
        tenant_password=passwords[0],
        tenant_id=tenant_id,
        platform_email=platform_email,
        platform_password=passwords[1],
        member_email=member_email,
        member_password=passwords[2],
    )


def load_credentials_file(path: Path, *, production: bool) -> SmokeCredentials:
    """Read one owner-only regular file and reject links or loose permissions."""

    try:
        path_stat = path.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise PrincipalManagerError("credentials_file_missing") from exc
    _require(stat.S_ISREG(path_stat.st_mode) and not path.is_symlink(), "credentials_file_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        raise PrincipalManagerError("credentials_file_unsafe") from exc
    try:
        file_stat = os.fstat(file_descriptor)
        _require(stat.S_ISREG(file_stat.st_mode), "credentials_file_unsafe")
        _require(
            (file_stat.st_dev, file_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino),
            "credentials_file_changed_during_open",
        )
        _require(0 < file_stat.st_size <= MAX_CREDENTIAL_FILE_BYTES, "credentials_file_size_invalid")
        _require(stat.S_IMODE(file_stat.st_mode) in {0o400, 0o600}, "credentials_file_mode_invalid")
        if production:
            _require(file_stat.st_uid == os.getuid(), "credentials_file_owner_invalid")
        chunks: list[bytes] = []
        remaining = MAX_CREDENTIAL_FILE_BYTES + 1
        while remaining:
            chunk = os.read(file_descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        _require(len(raw) == file_stat.st_size, "credentials_file_changed_during_read")
    finally:
        os.close(file_descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrincipalManagerError("credentials_file_json_invalid") from exc
    return parse_credentials(payload)


def _principal_username(kind: str, email: str) -> str:
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
    return f"release_smoke_{kind}_{digest}"


def _release_qa_tenant(tenant: Tenant | None) -> bool:
    return bool(
        tenant
        and tenant.is_active
        and tenant.name.startswith("Release QA ")
        and tenant.slug.startswith("release-qa-")
        and tenant.deletion_requested_at is None
        and tenant.deletion_scheduled_for is None
    )


async def _identity_for_email(db, email: str, *, lock: bool) -> Identity | None:
    query = select(Identity).where(Identity.email == email)
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def _identity_memberships(db, identity_id: uuid.UUID, *, lock: bool) -> list[User]:
    query = select(User).where(User.identity_id == identity_id).order_by(User.id.asc())
    if lock:
        query = query.with_for_update()
    return list((await db.execute(query)).scalars().all())


async def _load_release_owner(
    db,
    credentials: SmokeCredentials,
    *,
    lock: bool,
) -> tuple[Tenant, Identity, User]:
    tenant_query = select(Tenant).where(Tenant.id == credentials.tenant_id)
    if lock:
        tenant_query = tenant_query.with_for_update()
    tenant = (await db.execute(tenant_query)).scalar_one_or_none()
    _require(_release_qa_tenant(tenant), "release_qa_tenant_required")
    assert tenant is not None

    identity = await _identity_for_email(db, credentials.tenant_email, lock=lock)
    _require(identity is not None, "release_qa_owner_identity_missing")
    assert identity is not None
    memberships = await _identity_memberships(db, identity.id, lock=lock)
    _require(len(memberships) == 1, "release_qa_owner_must_have_one_membership")
    owner = memberships[0]
    _require(owner.tenant_id == tenant.id, "release_qa_owner_tenant_mismatch")
    _require(owner.role == "org_owner", "release_qa_owner_role_mismatch")
    _require(owner.registration_source == RELEASE_SMOKE_SOURCE, "release_qa_owner_source_mismatch")
    _require(not bool(identity.is_platform_admin), "release_qa_owner_must_not_be_platform_admin")
    _require(not bool(identity.mfa_enabled), "release_qa_owner_mfa_not_automatable")
    _require(
        tenant.owner_user_id in {None, owner.id},
        "release_qa_tenant_owner_pointer_mismatch",
    )
    return tenant, identity, owner


async def _load_optional_member(
    db,
    credentials: SmokeCredentials,
    *,
    lock: bool,
) -> tuple[Identity | None, User | None]:
    identity = await _identity_for_email(db, credentials.member_email, lock=lock)
    if identity is None:
        return None, None
    memberships = await _identity_memberships(db, identity.id, lock=lock)
    _require(not bool(identity.is_platform_admin), "release_smoke_member_platform_flag_forbidden")
    _require(not bool(identity.mfa_enabled), "release_smoke_member_mfa_not_automatable")
    _require(len(memberships) <= 1, "release_smoke_member_multiple_memberships_forbidden")
    if not memberships:
        return identity, None
    member = memberships[0]
    _require(member.tenant_id == credentials.tenant_id, "release_smoke_member_tenant_mismatch")
    _require(member.role == "member", "release_smoke_member_role_mismatch")
    _require(member.registration_source == RELEASE_SMOKE_SOURCE, "release_smoke_member_source_mismatch")
    return identity, member


async def _load_optional_platform(
    db,
    credentials: SmokeCredentials,
    *,
    lock: bool,
) -> tuple[Identity | None, User | None]:
    identity = await _identity_for_email(db, credentials.platform_email, lock=lock)
    if identity is None:
        return None, None
    memberships = await _identity_memberships(db, identity.id, lock=lock)
    _require(not bool(identity.mfa_enabled), "release_smoke_platform_mfa_not_automatable")
    _require(
        all(user.tenant_id is None for user in memberships),
        "release_smoke_platform_company_membership_forbidden",
    )
    _require(len(memberships) <= 1, "release_smoke_platform_multiple_users_forbidden")
    if not memberships:
        return identity, None
    platform_user = memberships[0]
    _require(platform_user.role == "platform_admin", "release_smoke_platform_role_mismatch")
    _require(
        platform_user.registration_source == RELEASE_SMOKE_SOURCE,
        "release_smoke_platform_source_mismatch",
    )
    return identity, platform_user


def _identity_login_ready(identity: Identity | None) -> bool:
    return bool(
        identity
        and identity.is_active
        and identity.password_login_enabled
        and identity.password_hash
        and identity.email_verified
        and not identity.mfa_enabled
    )


async def inventory(credentials: SmokeCredentials) -> dict[str, Any]:
    async with async_session() as db:
        tenant, owner_identity, owner = await _load_release_owner(
            db,
            credentials,
            lock=False,
        )
        member_identity, member = await _load_optional_member(
            db,
            credentials,
            lock=False,
        )
        platform_identity, platform_user = await _load_optional_platform(
            db,
            credentials,
            lock=False,
        )
        return {
            "ok": True,
            "action": "inventory",
            "tenant": {
                "release_qa_boundary": True,
                "owner_pointer_valid": tenant.owner_user_id in {None, owner.id},
            },
            "principals": {
                "owner": {
                    "present": True,
                    "login_ready": _identity_login_ready(owner_identity) and owner.is_active,
                },
                "member": {
                    "present": member_identity is not None and member is not None,
                    "login_ready": _identity_login_ready(member_identity)
                    and bool(member and member.is_active),
                    "ordinary_member": bool(member and member.role == "member"),
                },
                "platform": {
                    "present": platform_identity is not None and platform_user is not None,
                    "login_ready": _identity_login_ready(platform_identity)
                    and bool(platform_user and platform_user.is_active)
                    and bool(platform_identity and platform_identity.is_platform_admin),
                    "authority_active": bool(
                        platform_identity
                        and platform_identity.is_active
                        and platform_identity.is_platform_admin
                        and platform_user
                        and platform_user.is_active
                    ),
                    "tenantless": bool(platform_user and platform_user.tenant_id is None),
                },
            },
        }


def _advisory_lock_parts(operation_id: uuid.UUID) -> tuple[int, int]:
    first = int.from_bytes(operation_id.bytes[:4], "big", signed=True)
    second = int.from_bytes(operation_id.bytes[4:8], "big", signed=True)
    return first, second


async def _acquire_operation_lock(db, operation_id: uuid.UUID) -> None:
    first, second = _advisory_lock_parts(operation_id)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:first, :second)"),
        {"first": first, "second": second},
    )


async def _receipt_exists(db, *, action: str, operation_id: uuid.UUID) -> bool:
    result = await db.execute(
        text(
            "SELECT 1 FROM audit_logs "
            "WHERE action = :action AND details ->> 'operation_id' = :operation_id "
            "LIMIT 1"
        ),
        {"action": action, "operation_id": str(operation_id)},
    )
    return result.scalar_one_or_none() is not None


def _activate_identity(identity: Identity, *, password_hash: str, platform: bool) -> None:
    identity.password_hash = password_hash
    identity.password_login_enabled = True
    identity.auth_version = int(identity.auth_version or 0) + 1
    identity.is_active = True
    identity.is_platform_admin = platform
    identity.email_verified = True


async def _ensure_member_participant(db, member: User) -> None:
    participant = (
        await db.execute(
            select(Participant).where(
                Participant.type == "user",
                Participant.ref_id == member.id,
            )
        )
    ).scalar_one_or_none()
    if participant is None:
        db.add(
            Participant(
                type="user",
                ref_id=member.id,
                display_name=member.display_name,
            )
        )


async def provision(
    credentials: SmokeCredentials,
    *,
    operation_id: uuid.UUID,
    release_version: str,
) -> dict[str, Any]:
    password_hashes = await asyncio.gather(
        hash_password_async(credentials.tenant_password),
        hash_password_async(credentials.member_password),
        hash_password_async(credentials.platform_password),
    )
    action = RECEIPT_ACTIONS["provision"]
    async with async_session() as db:
        async with db.begin():
            await _acquire_operation_lock(db, operation_id)
            if await _receipt_exists(db, action=action, operation_id=operation_id):
                return {
                    "ok": True,
                    "action": "provision",
                    "status": "already_applied",
                    "operation_id": str(operation_id),
                }

            tenant, owner_identity, owner = await _load_release_owner(
                db,
                credentials,
                lock=True,
            )
            member_identity, member = await _load_optional_member(
                db,
                credentials,
                lock=True,
            )
            platform_identity, platform_user = await _load_optional_platform(
                db,
                credentials,
                lock=True,
            )

            _activate_identity(owner_identity, password_hash=password_hashes[0], platform=False)
            owner.is_active = True
            owner.activation_pending_email_verification = False
            tenant.owner_user_id = owner.id

            if member_identity is None:
                member_identity = Identity(
                    email=credentials.member_email,
                    username=_principal_username("member", credentials.member_email),
                )
                db.add(member_identity)
                await db.flush()
            _activate_identity(member_identity, password_hash=password_hashes[1], platform=False)
            if member is None:
                member = User(
                    identity_id=member_identity.id,
                    tenant_id=tenant.id,
                    display_name="Release Smoke Member",
                    role="member",
                    registration_source=RELEASE_SMOKE_SOURCE,
                    is_active=True,
                )
                db.add(member)
                await db.flush()
            member.is_active = True
            member.activation_pending_email_verification = False
            await _ensure_member_participant(db, member)

            if platform_identity is None:
                platform_identity = Identity(
                    email=credentials.platform_email,
                    username=_principal_username("platform", credentials.platform_email),
                )
                db.add(platform_identity)
                await db.flush()
            _activate_identity(platform_identity, password_hash=password_hashes[2], platform=True)
            if platform_user is None:
                platform_user = User(
                    identity_id=platform_identity.id,
                    tenant_id=None,
                    display_name="Release Smoke Platform Operator",
                    role="platform_admin",
                    registration_source=RELEASE_SMOKE_SOURCE,
                    is_active=True,
                )
                db.add(platform_user)
                await db.flush()
            platform_user.is_active = True
            platform_user.activation_pending_email_verification = False

            db.add(
                AuditLog(
                    tenant_id=tenant.id,
                    user_id=None,
                    action=action,
                    details={
                        "operation_id": str(operation_id),
                        "release_version": release_version,
                        "scope": "release_qa_only",
                        "owner_rotated": True,
                        "ordinary_member_ready": True,
                        "temporary_platform_operator_ready": True,
                    },
                )
            )
        return {
            "ok": True,
            "action": "provision",
            "status": "applied",
            "operation_id": str(operation_id),
            "principals": {
                "owner": "rotated",
                "member": "ready",
                "platform": "temporary_ready",
            },
        }


async def deactivate_platform(
    credentials: SmokeCredentials,
    *,
    operation_id: uuid.UUID,
    release_version: str,
) -> dict[str, Any]:
    action = RECEIPT_ACTIONS["deactivate-platform"]
    async with async_session() as db:
        async with db.begin():
            await _acquire_operation_lock(db, operation_id)
            if await _receipt_exists(db, action=action, operation_id=operation_id):
                return {
                    "ok": True,
                    "action": "deactivate-platform",
                    "status": "already_applied",
                    "operation_id": str(operation_id),
                }
            tenant, _owner_identity, _owner = await _load_release_owner(
                db,
                credentials,
                lock=True,
            )
            platform_identity, platform_user = await _load_optional_platform(
                db,
                credentials,
                lock=True,
            )
            _require(
                platform_identity is not None and platform_user is not None,
                "release_smoke_platform_identity_missing",
            )
            assert platform_identity is not None and platform_user is not None
            platform_identity.password_hash = None
            platform_identity.password_login_enabled = False
            platform_identity.auth_version = int(platform_identity.auth_version or 0) + 1
            platform_identity.is_active = False
            platform_identity.is_platform_admin = False
            platform_user.is_active = False
            db.add(
                AuditLog(
                    tenant_id=tenant.id,
                    user_id=None,
                    action=action,
                    details={
                        "operation_id": str(operation_id),
                        "release_version": release_version,
                        "scope": "temporary_platform_operator_only",
                        "password_removed": True,
                        "tokens_revoked": True,
                        "platform_authority_removed": True,
                    },
                )
            )
        return {
            "ok": True,
            "action": "deactivate-platform",
            "status": "applied",
            "operation_id": str(operation_id),
            "platform": "deactivated",
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory, provision, or deactivate dedicated production release-smoke principals."
    )
    parser.add_argument(
        "--action",
        choices=("inventory", "provision", "deactivate-platform"),
        required=True,
    )
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--confirm-environment", choices=("development", "test", "production"), required=True)
    parser.add_argument("--confirm-tenant-id", required=True)
    parser.add_argument("--release-version", default="1.12.2")
    parser.add_argument("--operation-id")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    production = args.confirm_environment == "production"
    credentials = load_credentials_file(args.credentials_file, production=production)
    _require(str(credentials.tenant_id) == args.confirm_tenant_id, "confirmed_tenant_id_mismatch")
    _require(args.release_version == "1.12.2", "release_version_mismatch")
    if args.action == "inventory":
        _require(not args.apply, "inventory_must_be_read_only")
        _require(args.operation_id is None, "inventory_operation_id_forbidden")
        return await inventory(credentials)

    _require(args.apply, "mutating_action_requires_apply")
    _require(production, "mutating_action_requires_production_confirmation")
    operation_id_value = args.operation_id
    _require(isinstance(operation_id_value, str), "operation_id_invalid")
    try:
        operation_id = uuid.UUID(operation_id_value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PrincipalManagerError("operation_id_invalid") from exc
    _require(
        operation_id.int != 0 and str(operation_id) == operation_id_value.lower(),
        "operation_id_invalid",
    )
    if args.action == "provision":
        return await provision(
            credentials,
            operation_id=operation_id,
            release_version=args.release_version,
        )
    return await deactivate_platform(
        credentials,
        operation_id=operation_id,
        release_version=args.release_version,
    )


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except PrincipalManagerError as exc:
        print(
            json.dumps(
                {"ok": False, "action": args.action, "error_code": exc.code},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {"ok": False, "action": args.action, "error_code": "internal_error"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
