"""Identity-level TOTP MFA setup, login challenge, and recovery APIs."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth_rate_limit import (
    enforce_auth_rate_limit,
    mfa_rate_limit_policy,
    password_reauth_rate_limit_policy,
)
from app.core.security import (
    create_access_token,
    get_authenticated_user,
    get_current_user,
    identity_auth_version,
    verify_password_async,
)
from app.database import get_db
from app.models.audit import AuditLog
from app.models.identity_mfa import IdentityMfaChallenge, IdentityMfaRecoveryCode
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.mfa_service import (
    MfaChallengeError,
    create_mfa_challenge,
    decode_challenge_token,
    generate_totp_secret,
    identity_recommends_mfa,
    matching_totp_step,
    open_mfa_secret,
    provisioning_uri,
    record_challenge_failure,
    recovery_codes_remaining,
    replace_recovery_codes,
    require_live_challenge,
    seal_mfa_secret,
    utc_now,
    verify_identity_factor,
)


router = APIRouter(prefix="/auth/mfa", tags=["auth", "mfa"])


class MfaChallengeRequest(BaseModel):
    challenge_token: str = Field(min_length=40, max_length=2048)


class MfaCodeRequest(MfaChallengeRequest):
    code: str = Field(min_length=6, max_length=64)


class MfaSetupRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


class MfaSensitiveMutation(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=64)


class MfaAdministrativeReset(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=10, max_length=500)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _challenge_http_error(exc: MfaChallengeError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": exc.code,
            "message": "MFA challenge is invalid or expired",
        },
    )


async def _rate_limit_challenge(request: Request, token: str) -> dict[str, object]:
    try:
        claims = decode_challenge_token(token)
    except MfaChallengeError as exc:
        # Invalid signed tokens still consume a client/global bucket without
        # disclosing whether any Identity exists.
        await enforce_auth_rate_limit(
            request,
            identity="invalid-mfa-challenge",
            policy=mfa_rate_limit_policy(),
        )
        raise _challenge_http_error(exc) from exc
    await enforce_auth_rate_limit(
        request,
        identity=f"identity:{claims['identity_id']}",
        policy=mfa_rate_limit_policy(),
    )
    return claims


async def _locked_identity(db: AsyncSession, identity_id: uuid.UUID) -> Identity | None:
    result = await db.execute(
        select(Identity).where(Identity.id == identity_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def _challenge_principal(
    db: AsyncSession,
    challenge: IdentityMfaChallenge,
) -> tuple[Identity, User]:
    identity = await _locked_identity(db, challenge.identity_id)
    result = await db.execute(
        select(User)
        .where(User.id == challenge.user_id)
        .options(selectinload(User.identity))
        .with_for_update()
    )
    user = result.scalar_one_or_none()
    if (
        identity is None
        or user is None
        or not identity.is_active
        or not user.is_active
        or user.identity_id != identity.id
        or challenge.auth_version != identity_auth_version(identity)
    ):
        raise MfaChallengeError("challenge_principal_unavailable")
    if user.tenant_id is not None:
        tenant = await db.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise MfaChallengeError("challenge_principal_unavailable")
    user.identity = identity
    return identity, user


async def _serialize_token_response(user: User, token: str) -> dict[str, Any]:
    from app.api.auth import serialize_user_with_access

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": await serialize_user_with_access(user),
        "needs_company_setup": user.tenant_id is None,
    }


async def _password_snapshot(
    request: Request,
    user: User,
    password: str,
) -> str:
    identity = user.identity
    await enforce_auth_rate_limit(
        request,
        identity=f"identity:{identity.id}",
        policy=password_reauth_rate_limit_policy(),
    )
    if (
        not identity.password_login_enabled
        or not identity.password_hash
        or not await verify_password_async(password, identity.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    return identity.password_hash


async def _ensure_password_snapshot(
    identity: Identity,
    expected_hash: str,
) -> None:
    if (
        not identity.password_login_enabled
        or not identity.password_hash
        or identity.password_hash != expected_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "reauthentication_stale",
                "message": "Account credentials changed; authenticate again",
            },
        )


@router.get("/status")
async def mfa_status(
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Return only non-secret MFA posture for the current Identity."""

    identity = current_user.identity
    recommended = identity_recommends_mfa(current_user)
    return {
        "enabled": bool(identity.mfa_enabled),
        "required": False,
        "recommended": recommended,
        "confirmed_at": identity.mfa_confirmed_at,
        "recovery_codes_remaining": (
            await recovery_codes_remaining(db, identity_id=identity.id)
            if identity.mfa_enabled
            else 0
        ),
    }


@router.post("/setup")
async def start_authenticated_setup(
    data: MfaSetupRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Start optional/self-service MFA setup after password reauthentication."""

    _no_store(response)
    expected_hash = await _password_snapshot(request, current_user, data.current_password)
    identity = await _locked_identity(db, current_user.identity_id)
    if identity is None or not identity.is_active:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    await _ensure_password_snapshot(identity, expected_hash)
    if identity.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "mfa_already_enabled", "message": "MFA is already enabled"},
        )
    secret = generate_totp_secret()
    _challenge, token = await create_mfa_challenge(
        db,
        identity_id=identity.id,
        user_id=current_user.id,
        auth_version=identity_auth_version(identity),
        purpose="setup",
        secret=secret,
    )
    db.add(
        AuditLog(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="mfa_setup_started",
            details={"identity_id": str(identity.id)},
        )
    )
    await db.commit()
    return {
        "challenge_token": token,
        "secret": secret,
        "provisioning_uri": provisioning_uri(
            secret,
            account_name=identity.email or identity.username or str(identity.id),
        ),
        "expires_in_seconds": 5 * 60,
    }


@router.post("/bootstrap/setup")
async def start_bootstrap_setup(
    data: MfaChallengeRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Prepare the seed for a privileged password login with no access token."""

    _no_store(response)
    await _rate_limit_challenge(request, data.challenge_token)
    try:
        challenge = await require_live_challenge(
            db, data.challenge_token, purposes={"bootstrap"}
        )
        identity, user = await _challenge_principal(db, challenge)
    except MfaChallengeError as exc:
        raise _challenge_http_error(exc) from exc
    if identity.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "mfa_already_enabled", "message": "MFA is already enabled"},
        )
    if not identity_recommends_mfa(user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "mfa_bootstrap_not_required", "message": "MFA setup is not required"},
        )
    if challenge.secret_envelope:
        secret = open_mfa_secret(challenge.secret_envelope)
    else:
        secret = generate_totp_secret()
        challenge.secret_envelope = seal_mfa_secret(secret)
    await db.commit()
    return {
        "challenge_token": data.challenge_token,
        "secret": secret,
        "provisioning_uri": provisioning_uri(
            secret,
            account_name=identity.email or identity.username or str(identity.id),
        ),
        "expires_in_seconds": 5 * 60,
    }


@router.post("/setup/confirm")
async def confirm_setup(
    data: MfaCodeRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Confirm a pending seed, enable MFA, and show recovery codes once."""

    _no_store(response)
    await _rate_limit_challenge(request, data.challenge_token)
    try:
        challenge = await require_live_challenge(
            db, data.challenge_token, purposes={"bootstrap", "setup"}
        )
        identity, user = await _challenge_principal(db, challenge)
    except MfaChallengeError as exc:
        raise _challenge_http_error(exc) from exc
    if identity.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "mfa_already_enabled", "message": "MFA is already enabled"},
        )
    if not challenge.secret_envelope:
        raise _challenge_http_error(MfaChallengeError("setup_not_prepared"))
    secret = open_mfa_secret(challenge.secret_envelope)
    now = utc_now()
    matched_step = matching_totp_step(secret, data.code, at_time=now)
    if matched_step is None:
        record_challenge_failure(challenge, now=now)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "mfa_code_invalid", "message": "The MFA code is invalid"},
        )

    identity.mfa_secret_envelope = seal_mfa_secret(secret)
    identity.mfa_enabled = True
    identity.mfa_confirmed_at = now
    identity.mfa_last_totp_step = matched_step
    identity.auth_version = identity_auth_version(identity) + 1
    challenge.consumed_at = now
    recovery_codes = await replace_recovery_codes(db, identity_id=identity.id)
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="mfa_enabled",
            details={
                "identity_id": str(identity.id),
                "recovery_code_count": len(recovery_codes),
            },
        )
    )
    await db.commit()
    user.identity = identity
    token = create_access_token(
        str(user.id),
        user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=True,
    )
    token_response = await _serialize_token_response(user, token)
    return {**token_response, "recovery_codes": recovery_codes}


@router.post("/challenge/verify")
async def verify_login_challenge(
    data: MfaCodeRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Consume one login challenge using TOTP or a recovery code."""

    _no_store(response)
    await _rate_limit_challenge(request, data.challenge_token)
    try:
        challenge = await require_live_challenge(
            db, data.challenge_token, purposes={"login"}
        )
        identity, user = await _challenge_principal(db, challenge)
    except MfaChallengeError as exc:
        raise _challenge_http_error(exc) from exc
    if not identity.mfa_enabled or not identity.mfa_secret_envelope:
        raise _challenge_http_error(MfaChallengeError("mfa_not_enabled"))
    method = await verify_identity_factor(
        db,
        identity=identity,
        code=data.code,
    )
    if method is None:
        record_challenge_failure(challenge)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "mfa_code_invalid", "message": "The MFA code is invalid"},
        )
    challenge.consumed_at = utc_now()
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="mfa_login_verified",
            details={"identity_id": str(identity.id), "method": method},
        )
    )
    await db.commit()
    user.identity = identity
    token = create_access_token(
        str(user.id),
        user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=True,
    )
    return await _serialize_token_response(user, token)


async def _verify_sensitive_mutation(
    data: MfaSensitiveMutation,
    request: Request,
    current_user: User,
    db: AsyncSession,
) -> tuple[Identity, str]:
    expected_hash = await _password_snapshot(request, current_user, data.current_password)
    identity = await _locked_identity(db, current_user.identity_id)
    if identity is None or not identity.is_active or not identity.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA is not enabled")
    await _ensure_password_snapshot(identity, expected_hash)
    method = await verify_identity_factor(db, identity=identity, code=data.code)
    if method is None:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "mfa_code_invalid", "message": "The MFA code is invalid"},
        )
    return identity, method


@router.post("/recovery-codes/rotate")
async def rotate_recovery_codes(
    data: MfaSensitiveMutation,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all recovery codes after password and current-factor proof."""

    _no_store(response)
    identity, method = await _verify_sensitive_mutation(
        data, request, current_user, db
    )
    codes = await replace_recovery_codes(db, identity_id=identity.id)
    identity.auth_version = identity_auth_version(identity) + 1
    db.add(
        AuditLog(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="mfa_recovery_codes_rotated",
            details={
                "identity_id": str(identity.id),
                "verification_method": method,
                "recovery_code_count": len(codes),
            },
        )
    )
    await db.commit()
    current_user.identity = identity
    token = create_access_token(
        str(current_user.id),
        current_user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=True,
    )
    return {"access_token": token, "recovery_codes": codes}


@router.post("/disable")
async def disable_mfa(
    data: MfaSensitiveMutation,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable MFA after fresh factor proof. Privileged roles may still disable it."""

    _no_store(response)
    identity, method = await _verify_sensitive_mutation(
        data, request, current_user, db
    )
    identity.mfa_enabled = False
    identity.mfa_secret_envelope = None
    identity.mfa_confirmed_at = None
    identity.mfa_last_totp_step = None
    identity.auth_version = identity_auth_version(identity) + 1
    await db.execute(
        delete(IdentityMfaRecoveryCode).where(
            IdentityMfaRecoveryCode.identity_id == identity.id
        )
    )
    await db.execute(
        delete(IdentityMfaChallenge).where(
            IdentityMfaChallenge.identity_id == identity.id
        )
    )
    db.add(
        AuditLog(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="mfa_disabled",
            details={
                "identity_id": str(identity.id),
                "verification_method": method,
                "requires_setup": False,
            },
        )
    )
    await db.commit()
    access_token = create_access_token(
        str(current_user.id),
        current_user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=False,
    )
    return {
        "ok": True,
        "requires_setup": False,
        "access_token": access_token,
    }


@router.post("/admin/reset/{target_user_id}")
async def administratively_reset_mfa(
    target_user_id: uuid.UUID,
    data: MfaAdministrativeReset,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset another Identity with explicit global-versus-company boundaries."""

    _no_store(response)
    if target_user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "mfa_self_reset_forbidden", "message": "Use self-service recovery for your own account"},
        )

    expected_hash = await _password_snapshot(request, current_user, data.current_password)
    caller_identity = await _locked_identity(db, current_user.identity_id)
    if caller_identity is None or not caller_identity.is_active:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    await _ensure_password_snapshot(caller_identity, expected_hash)

    target_result = await db.execute(
        select(User)
        .where(User.id == target_user_id)
        .options(selectinload(User.identity))
        .with_for_update()
    )
    target_user = target_result.scalar_one_or_none()
    if (
        target_user is None
        or not target_user.is_active
        or target_user.identity is None
        or not target_user.identity.is_active
        or target_user.identity_id == current_user.identity_id
    ):
        raise HTTPException(status_code=404, detail="Target account is unavailable")

    is_platform_operator = bool(
        getattr(caller_identity, "is_platform_admin", False)
        or current_user.role == "platform_admin"
    )
    if not is_platform_operator:
        if (
            current_user.tenant_id is None
            or target_user.tenant_id != current_user.tenant_id
            or current_user.role not in {"org_owner", "org_admin"}
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "mfa_reset_scope_forbidden", "message": "MFA reset is outside your company scope"},
            )
        if target_user.role != "member":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "mfa_reset_privilege_forbidden", "message": "Only a platform operator can reset an administrator"},
            )

    memberships_result = await db.execute(
        select(User)
        .where(
            User.identity_id == target_user.identity_id,
            User.is_active.is_(True),
        )
        .order_by(User.id)
        .with_for_update()
    )
    active_memberships = list(memberships_result.scalars().all())
    target_identity = await _locked_identity(db, target_user.identity_id)
    if target_identity is None or not target_identity.is_active:
        raise HTTPException(status_code=404, detail="Target account is unavailable")

    active_tenant_ids = {
        membership.tenant_id
        for membership in active_memberships
        if membership.tenant_id is not None
    }
    if not is_platform_operator and (
        active_tenant_ids != {current_user.tenant_id}
        or bool(getattr(target_identity, "is_platform_admin", False))
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "global_identity_reset_requires_platform_operator",
                "message": "This Identity belongs to multiple security scopes",
            },
        )

    was_enabled = bool(target_identity.mfa_enabled)
    target_identity.mfa_enabled = False
    target_identity.mfa_secret_envelope = None
    target_identity.mfa_confirmed_at = None
    target_identity.mfa_last_totp_step = None
    target_identity.auth_version = identity_auth_version(target_identity) + 1
    await db.execute(
        delete(IdentityMfaRecoveryCode).where(
            IdentityMfaRecoveryCode.identity_id == target_identity.id
        )
    )
    await db.execute(
        delete(IdentityMfaChallenge).where(
            IdentityMfaChallenge.identity_id == target_identity.id
        )
    )
    requires_setup = bool(
        getattr(target_identity, "is_platform_admin", False)
        or any(
            membership.role in {"platform_admin", "org_owner", "org_admin"}
            for membership in active_memberships
        )
    )
    db.add(
        AuditLog(
            tenant_id=target_user.tenant_id,
            user_id=current_user.id,
            action="mfa_administratively_reset",
            details={
                "target_identity_id": str(target_identity.id),
                "target_user_id": str(target_user.id),
                "reason": data.reason.strip(),
                "was_enabled": was_enabled,
                "requires_setup": requires_setup,
                "active_tenant_count": len(active_tenant_ids),
                "operator_scope": "platform" if is_platform_operator else "company",
            },
        )
    )
    await db.commit()
    return {
        "ok": True,
        "target_user_id": str(target_user.id),
        "requires_setup": requires_setup,
    }


__all__ = ["router"]
