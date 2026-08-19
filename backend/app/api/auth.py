"""Authentication API routes."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from app.core.security import (
    _request_is_secure,
    access_context_mfa_verified,
    clear_browser_session_cookie,
    create_access_token,
    get_authenticated_user,
    get_current_user,
    get_verification_user,
    hash_password_async,
    identity_auth_version,
    set_browser_session_cookie,
    verify_password_async,
)
from app.core.auth_rate_limit import (
    discovery_rate_limit_policy,
    email_action_rate_limit_policy,
    enforce_auth_rate_limit,
    login_lookup_rate_limit_policy,
    login_rate_limit_policy,
    oauth_exchange_rate_limit_policy,
    oauth_start_rate_limit_policy,
    password_change_rate_limit_policy,
    password_reauth_rate_limit_policy,
    password_registration_rate_limit_policy,
)
from app.core.identity_canonicalization import (
    canonicalize_email,
    canonicalize_phone,
    normalize_username,
    username_looks_like_contact,
)
from app.dao import identity_dao, system_setting_dao, tenant_dao, user_dao
from app.database import async_session, transaction
from app.models.user import Identity, User
from app.schemas.schemas import (
    ForgotPasswordRequest,
    IdentityBindRequest,
    IdentityOut,
    IdentityUnbindRequest,
    MultiTenantResponse,
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    RegisterInitRequest,
    RegisterInitResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SSORegisterRequest,
    TenantChoice,
    TenantSwitchRequest,
    TenantSwitchResponse,
    TokenResponse,
    UserLogin,
    UserOut,
    UserRegister,
    SelfUserUpdate,
    VerifyEmailRequest,
)
from app.services.external_identity_policy import (
    external_user_can_authenticate,
    require_stable_external_subject,
)
from app.services.identity_login_namespace import (
    acquire_identity_login_namespace_lock,
    normalize_safe_username,
    validate_identity_login_namespace,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Keep a stable reference so focused tests that replace the public serializer
# schema do not accidentally open a real database session. Production always
# returns this concrete schema before resolving live access.
_ACCESS_AWARE_USER_OUT_TYPE = UserOut


async def _acquire_registration_bootstrap_lock(db: AsyncSession) -> None:
    """Serialize bootstrap and login-namespace decisions across workers."""
    await acquire_identity_login_namespace_lock(db)


async def _resolve_auth_email_config():
    """Resolve one authoritative verification-policy snapshot or fail closed."""
    from app.services.system_email_service import (
        SystemEmailConfigResolutionError,
        resolve_email_config_async,
    )

    try:
        return await resolve_email_config_async(raise_on_error=True)
    except SystemEmailConfigResolutionError as exc:
        logger.error("[AUTH] Email verification policy is temporarily unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable. Please try again later.",
        ) from exc


async def _resolve_password_registration_email_config():
    """Resolve mail policy and reject unsafe production password signup."""
    from app.config import unverified_local_signup_allowed

    email_config = await _resolve_auth_email_config()
    if email_config or unverified_local_signup_allowed():
        return email_config
    logger.error("[AUTH] Password registration refused because email delivery is unavailable")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Password registration is temporarily unavailable. Please try again later.",
    )


async def _password_registration_available() -> bool:
    """Mirror the password-registration fail-closed policy for public UI readiness."""
    from app.config import unverified_local_signup_allowed

    email_config = await _resolve_auth_email_config()
    return bool(email_config or unverified_local_signup_allowed())


def serialize_user(user: User | None) -> UserOut | None:
    if user is None:
        return None
    data = UserOut.model_validate(user)
    is_platform_admin = bool(getattr(getattr(user, "identity", None), "is_platform_admin", False))
    if isinstance(data, dict):
        data["is_platform_admin"] = is_platform_admin
    else:
        data.is_platform_admin = is_platform_admin
    return data


async def serialize_user_with_access(user: User | None) -> UserOut | None:
    """Serialize a user with one live, server-authoritative access snapshot.

    ``serialize_user`` remains the narrow legacy serializer so older unit
    tests and internal callers can replace it without opening a database
    session.  Production response paths call this async wrapper.
    """

    data = serialize_user(user)
    if user is None or data is None:
        return None
    # Several focused policy tests intentionally replace the legacy serializer
    # with a plain object/dict.  Do not turn those isolated tests into database
    # integration tests.
    if not isinstance(data, _ACCESS_AWARE_USER_OUT_TYPE):
        return data

    from app.services.access_control import resolve_effective_access

    async with async_session() as session:
        access = await resolve_effective_access(session, user)
    data.membership_id = access.membership_id
    data.membership_role = access.membership_role
    data.global_roles = list(access.global_roles)
    data.effective_capabilities = list(access.effective_capabilities)
    data.available_surfaces = list(access.available_surfaces)
    data.pending_invitation_count = access.pending_invitation_count
    data.current_support_session = access.current_support_session
    return data


async def _reauthenticate_sensitive_identity_action(
    request: Request,
    current_user: User,
    current_password: str | None,
) -> tuple[uuid.UUID, str]:
    """Require a bounded password proof without holding a DB connection."""

    user = await user_dao.get_with_identity(current_user.id)
    identity = getattr(user, "identity", None) if user else None
    if not user or not identity or user.identity_id != current_user.identity_id:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    if not identity.password_login_enabled or not identity.password_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Password reauthentication is required for this action. "
                "SSO-only accounts must contact an administrator."
            ),
        )
    if not current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_password is required for this action",
        )
    await enforce_auth_rate_limit(
        request,
        identity=f"identity:{identity.id}",
        policy=password_reauth_rate_limit_policy(),
    )
    if not await verify_password_async(current_password, identity.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    return identity.id, identity.password_hash


def _normalize_invitation_code(code: str | None) -> str:
    """Normalize human-entered registration/invitation codes."""
    return (code or "").strip().upper()


@dataclass(slots=True)
class SignupAccess:
    """Validated registration gate without implicitly joining a tenant."""

    kind: str
    record: object
    tenant_id: uuid.UUID | None = None
    membership_role: str = "member"


async def _get_valid_signup_code(
    db: AsyncSession,
    invitation_code: str | None,
    *,
    identity_email: str | None = None,
    requested_tenant_id: uuid.UUID | None = None,
):
    """Validate either a platform grant or a pending organization credential.

    Registration grants are consumed after account creation. Organization
    credentials remain pending until the authenticated user explicitly accepts
    them through ``/tenants/join``.
    """
    from app.services.identity_governance import (
        GovernanceCredentialError,
        resolve_organization_credential,
        resolve_registration_grant,
    )

    code = _normalize_invitation_code(invitation_code)
    if not code:
        raise HTTPException(status_code=400, detail="Registration code is required")

    try:
        grant = await resolve_registration_grant(db, code, for_update=True)
        if grant:
            if requested_tenant_id is not None:
                raise HTTPException(
                    status_code=403,
                    detail="A platform registration grant is not an organization invitation.",
                )
            return SignupAccess(kind="registration_grant", record=grant)

        organization_access = await resolve_organization_credential(
            db,
            code,
            identity_email=identity_email,
            target_tenant_id=requested_tenant_id,
            for_update=True,
        )
    except GovernanceCredentialError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if not organization_access:
        raise HTTPException(status_code=400, detail="Invalid registration grant or organization invitation")
    return SignupAccess(
        kind=organization_access.kind,
        record=organization_access,
        tenant_id=organization_access.tenant_id,
        membership_role=organization_access.role,
    )


async def _prepare_signup_code_if_required(
    db: AsyncSession,
    invitation_code: str | None,
    *,
    is_first_user: bool,
    identity_email: str | None = None,
    requested_tenant_id: uuid.UUID | None = None,
):
    """Validate signup code for all non-bootstrap signups."""
    if is_first_user:
        return None
    return await _get_valid_signup_code(
        db,
        invitation_code,
        identity_email=identity_email,
        requested_tenant_id=requested_tenant_id,
    )


def _consume_signup_code_if_needed(code_obj) -> None:
    """Consume only a platform registration grant after account creation."""
    if code_obj is not None and code_obj.kind == "registration_grant":
        from app.services.identity_governance import consume_registration_grant

        consume_registration_grant(code_obj.record)


async def _resolve_signup_tenant(
    db: AsyncSession,
    code_obj,
    *,
    requested_tenant_id: uuid.UUID | None = None,
):
    """Return pending organization context without creating a membership."""
    if code_obj is None or code_obj.tenant_id is None:
        if requested_tenant_id is not None:
            raise HTTPException(
                status_code=403,
                detail="The registration code does not belong to the required organization.",
            )
        return None, "member"

    if requested_tenant_id is not None and requested_tenant_id != code_obj.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="The registration code does not belong to the required organization.",
        )
    # Explicit acceptance is F04/F05. Registration never consumes or applies
    # the organization role and therefore cannot auto-promote the first user.
    return None, "member"


async def _grant_company_creation_from_registration_policy(
    db: AsyncSession,
    *,
    identity,
    is_first_user: bool,
) -> None:
    """Materialize the account-level ``company.create`` policy at signup."""
    from app.models.system_settings import SystemSetting
    from app.services.identity_governance import COMPANY_CREATE_CAPABILITY, grant_identity_capability

    should_grant = is_first_user
    if not should_grant:
        result = await db.execute(
            select(SystemSetting).where(SystemSetting.key == "allow_self_create_company")
        )
        setting = result.scalar_one_or_none()
        should_grant = setting.value.get("enabled", True) if setting else True
    if should_grant:
        await grant_identity_capability(
            db,
            identity_id=identity.id,
            capability=COMPANY_CREATE_CAPABILITY,
            granted_by_identity_id=None,
        )


async def _require_password_registration_ownership(identity, email: str, password: str) -> None:
    """Revalidate local credential ownership inside the registration transaction.

    The public preflight lookup is only an early error path.  An SSO or sync
    worker can create the Identity between that lookup and the transactional
    find-or-create call, so no tenant User or JWT may be created until the
    returned Identity proves the submitted local password.
    """
    if identity.email and canonicalize_email(identity.email) != canonicalize_email(email):
        logger.warning("[REGISTER] Identity email mismatch identity={}", identity.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken. Please choose a different username.",
        )
    if not identity.password_hash or not identity.password_login_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Email already registered via SSO/sync. Please use password reset "
                "to set a password, or log in via SSO."
            ),
        )
    if not await verify_password_async(password, identity.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email already registered. Incorrect password.",
        )


async def _activate_email_verification_pending_memberships(identity_id) -> User | None:
    """Activate explicit email-pending memberships and return one activated row."""
    users = await user_dao.get_by_identity_id(identity_id)
    activated_user = None
    for user in users:
        if getattr(user, "activation_pending_email_verification", False):
            user.activation_pending_email_verification = False
            user.is_active = True
            if activated_user is None:
                activated_user = user
    return activated_user


@router.get("/registration-config")
async def get_registration_config():
    """Public endpoint — returns registration requirements (no auth needed)."""
    # The first account is the platform bootstrap user and is already exempted
    # by register_init. Expose the same rule to the UI so a fresh deployment is
    # not deadlocked waiting for a code that no administrator exists to create.
    is_first_user = await identity_dao.is_empty()
    return {
        "invitation_code_required": not is_first_user,
        "password_registration_available": await _password_registration_available(),
    }


@router.get("/check-duplicate")
async def check_duplicate(
    request: Request,
    email: str | None = Query(None, description="Email to check"),
    username: str | None = Query(None, description="Username to check"),
):
    """Check if email or username already exists."""
    normalized_username = normalize_username(username)
    await enforce_auth_rate_limit(
        request,
        identity=(
            f"email:{canonicalize_email(email) or ''}|"
            f"username:{(username or '').strip().lower()}"
        ),
        policy=discovery_rate_limit_policy(),
    )
    result = {"email_exists": False, "username_exists": False, "conflicts": []}

    if email:
        # Check Identity email
        if await identity_dao.get_by_email(email):
            result["email_exists"] = True
            result["conflicts"].append({"type": "email", "scope": "global", "message": "Email already registered"})

    if normalized_username:
        username_conflict = username_looks_like_contact(normalized_username) or any(
            (
                await identity_dao.get_by_username(normalized_username),
                await identity_dao.get_by_email(normalized_username),
                await identity_dao.get_by_phone(normalized_username),
            )
        )
        if username_conflict:
            result["username_exists"] = True
            result["conflicts"].append({"type": "username", "scope": "global", "message": "Username already taken"})

    result["has_conflict"] = result["email_exists"] or result["username_exists"]
    return result


async def _send_verification_email_task(
    user: User,
    background_tasks: BackgroundTasks,
    request: Request | None = None,
) -> None:
    """Create a token and persist an auditable email delivery request."""
    from app.services.email_verification_service import email_verification_service
    from app.services.outbound_email_service import dispatch_outbound_email, persist_template_email

    try:
        identity = await identity_dao.get(user.identity_id)

        if not identity:
            logger.warning(f"No identity found for user {user.id}. Cannot send verification.")
            return

        raw_code, expires_at = await email_verification_service.create_email_verification_token(
            identity.id, identity.email
        )
        expiry_minutes = int((expires_at - datetime.now(timezone.utc)).total_seconds() // 60)
        from app.services.platform_service import platform_service

        async with async_session() as url_db:
            base_url = await platform_service.get_public_base_url(url_db, request=request)
        verification_url = await email_verification_service.build_email_verification_url(
            base_url,
            raw_code,
        )
        delivery = await persist_template_email(
            purpose="email_verification",
            to=identity.email,
            scenario_key="email_verification",
            variables={
                "display_name": user.display_name or identity.username or "User",
                "verification_url": verification_url,
                "verification_code": raw_code,
                "expiry_minutes": str(expiry_minutes),
            },
            identity_id=identity.id,
        )
        if delivery.status in {"queued", "retry_wait"}:
            background_tasks.add_task(dispatch_outbound_email, delivery.id)
    except Exception as exc:
        logger.warning(
            "Failed to persist verification email user={} error_type={}",
            user.id,
            type(exc).__name__,
        )


@router.post("/register", response_model=Any, status_code=status.HTTP_201_CREATED)
async def register(
    data: UserRegister,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
):
    """Legacy registration endpoint - kept for backward compatibility.

    For new implementations, use:
    - /register/init - Step 1: Initialize password registration
    - /verify-email - Step 3: Verify email

    Public social OAuth is sign-in-only in this release. Organization-managed
    SSO account creation remains available through the tenant SSO callbacks.
    """
    # The legacy payload advertised a social-signup branch that cannot safely
    # complete the provider redirect binding (Google requires the exact
    # redirect_uri and browser-bound state). Fail explicitly before provider,
    # SMTP, bcrypt, or database I/O instead of exposing a half-working flow.
    if data.provider or data.provider_code:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "Public social registration is not available. Register with "
                "email and password, or use organization-managed SSO."
            ),
        )

    # Keep old clients functional while making the canonical path and removal
    # window machine-readable.  Rate limiting and all mutation live solely in
    # register_init, so compatibility cannot drift into a second signup flow.
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Mon, 16 Nov 2026 00:00:00 GMT"
    response.headers["Link"] = '</api/auth/register/init>; rel="successor-version"'
    return await register_init(
        RegisterInitRequest(
            username=data.username,
            email=data.email,
            password=data.password,
            display_name=data.display_name,
            invitation_code=data.invitation_code,
        ),
        background_tasks,
        request,
    )


@router.post("/register/init", response_model=RegisterInitResponse, status_code=status.HTTP_201_CREATED)
async def register_init(
    data: RegisterInitRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """Step 1: Initialize registration with account credentials.

    Creates/finds a global Identity and a tenant-scoped User.
    """
    from app.services.registration_service import registration_service

    logger.info("[REGISTER_INIT] Starting registration")
    email = canonicalize_email(data.email)
    username = normalize_safe_username(data.username)
    if email is None:
        raise HTTPException(status_code=400, detail="A valid email is required")
    await enforce_auth_rate_limit(
        request,
        identity=email,
        policy=password_registration_rate_limit_policy(),
    )

    # 1. Resolve email config outside transaction
    email_config = await _resolve_password_registration_email_config()

    # 2. Compute hash first (without DB connection checked out)
    password_hash = None
    if data.password:
        password_hash = await hash_password_async(data.password)

    # 3. Check duplicate/existing identity first (outside transaction)
    identity = await identity_dao.get_by_email(email)
    if identity:
        # Defense-in-depth: verify the returned identity actually belongs to the submitted email.
        if canonicalize_email(identity.email) != email:
            logger.warning(f"[REGISTER_INIT] Identity email mismatch identity={identity.id} — rejecting")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken. Please choose a different username.",
            )

        # Reject registration if the identity exists but has no password set (SSO/synced users)
        if identity.password_hash is None or not identity.password_login_enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered via SSO/sync. Please use password reset to set a password, or log in via SSO.",
            )

        # Verify password outside transaction
        if identity.password_hash and not await verify_password_async(data.password, identity.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Email already registered. Incorrect password."
            )

    async with transaction() as session:
        await _acquire_registration_bootstrap_lock(session)
        is_first_user = await identity_dao.is_empty()
        signup_code = await _prepare_signup_code_if_required(
            session,
            data.invitation_code,
            is_first_user=is_first_user,
            identity_email=email,
            requested_tenant_id=data.target_tenant_id,
        )
        namespace_owner = await identity_dao.get_by_email(email)
        await validate_identity_login_namespace(
            username=username,
            email=email,
            owned_identity_id=(namespace_owner.id if namespace_owner else None),
        )

        tenant_uuid = None
        membership_role = "member"
        pending_tenant_id = data.target_tenant_id
        if not is_first_user:
            _invited_tenant, membership_role = await _resolve_signup_tenant(
                session,
                signup_code,
                requested_tenant_id=data.target_tenant_id,
            )
            pending_tenant_id = signup_code.tenant_id if signup_code else pending_tenant_id

        # Find or Create Identity inside transaction (handles concurrent creation safely)
        identity = await registration_service.find_or_create_identity(
            email=email,
            username=username,
            password=data.password,
            is_platform_admin=is_first_user,
            email_config=email_config,
            password_hash=password_hash,
        )
        await _require_password_registration_ownership(
            identity,
            email,
            data.password,
        )
        await _grant_company_creation_from_registration_policy(
            session,
            identity=identity,
            is_first_user=is_first_user,
        )

        # Create User (tenant-scoped)
        created_user = False
        if tenant_uuid:
            user = await user_dao.get_by_identity_and_tenant(identity.id, tenant_uuid)
        else:
            user = await user_dao.get_by_identity_and_tenant(identity.id, None)

        if not user:
            user = await registration_service.create_user_with_identity(
                identity=identity,
                display_name=data.display_name or data.username,
                role=membership_role,
                tenant_id=tenant_uuid,
                email_config=email_config,
            )
            user.email_verified = identity.email_verified
            await session.flush()
            created_user = True
        else:
            user.identity = identity

        if created_user:
            _consume_signup_code_if_needed(signup_code)

    # 5. Generate token outside transaction
    token = create_access_token(
        str(user.id), user.role, auth_version=identity_auth_version(identity)
    )

    # 6. Send verification email if not verified (outside transaction)
    if not identity.email_verified:
        await _send_verification_email_task(user, background_tasks, request=request)

    return RegisterInitResponse(
        user_id=user.id,
        email=identity.email,
        access_token=token,
        user=await serialize_user_with_access(user),
        message="Registration initiated. Please verify your email."
        if not identity.email_verified
        else "Registration successful.",
        needs_company_setup=user.tenant_id is None,
        target_tenant_id=user.tenant_id or pending_tenant_id,
    )


@router.post("/register/sso", response_model=TokenResponse, deprecated=True)
async def register_sso(
    data: SSORegisterRequest,
    request: Request,
):
    """Retired incomplete public social-registration compatibility route.

    Public Google/GitHub OAuth remains a sign-in path for already-linked
    identities. New users must use password registration or a tenant-managed
    SSO callback, both of which have a complete ownership and tenant-binding
    contract.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Public social registration is not available. Register with email "
            "and password, or use organization-managed SSO."
        ),
    )


async def _issue_password_login_mfa_challenge(
    *,
    identity: Identity,
    user: User,
) -> dict[str, Any] | None:
    """Fence privileged/enrolled password login behind a persisted MFA ceremony."""

    from app.models.audit import AuditLog
    from app.services.mfa_service import (
        MFA_CHALLENGE_MINUTES,
        create_mfa_challenge,
    )

    # Enrollment is recommended for privileged roles, never a login gate.
    # A challenge is issued only after the Identity has already enabled MFA.
    user.identity = identity
    if not bool(getattr(identity, "mfa_enabled", False)):
        return None

    verified_password_hash = identity.password_hash
    verified_auth_version = identity_auth_version(identity)
    async with async_session() as session:
        identity_result = await session.execute(
            select(Identity).where(Identity.id == identity.id).with_for_update()
        )
        locked_identity = identity_result.scalar_one_or_none()
        user_result = await session.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
        locked_user = user_result.scalar_one_or_none()
        if (
            locked_identity is None
            or locked_user is None
            or not locked_identity.is_active
            or not locked_user.is_active
            or locked_user.identity_id != locked_identity.id
            or locked_user.tenant_id != user.tenant_id
            or locked_user.role != user.role
            or not locked_identity.password_login_enabled
            or not locked_identity.password_hash
            or not verified_password_hash
            or not hmac.compare_digest(
                locked_identity.password_hash,
                verified_password_hash,
            )
            or identity_auth_version(locked_identity) != verified_auth_version
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "login_state_changed",
                    "message": "Account state changed; sign in again",
                },
            )

        locked_user.identity = locked_identity
        if not bool(getattr(locked_identity, "mfa_enabled", False)):
            # MFA was disabled after the first snapshot. Continue as a normal
            # password login instead of forcing enrollment.
            return None
        purpose = "login"

        _challenge, challenge_token = await create_mfa_challenge(
            session,
            identity_id=locked_identity.id,
            user_id=locked_user.id,
            auth_version=identity_auth_version(locked_identity),
            purpose=purpose,
        )
        session.add(
            AuditLog(
                tenant_id=locked_user.tenant_id,
                user_id=locked_user.id,
                action="mfa_login_challenge_issued",
                details={
                    "identity_id": str(locked_identity.id),
                    "purpose": purpose,
                },
            )
        )
        await session.commit()

    return {
        "requires_mfa": purpose == "login",
        "requires_mfa_setup": purpose == "bootstrap",
        "challenge_token": challenge_token,
        "expires_in_seconds": MFA_CHALLENGE_MINUTES * 60,
    }


@router.post("/login", response_model=Any)
async def login(data: UserLogin, background_tasks: BackgroundTasks, request: Request):
    """Login with email/phone/username and password. Supports multi-tenant selection."""
    # Protect unresolved namespace probes before database work. The later
    # Identity/bcrypt limiter remains separate so aliases for one account share
    # one credential-work bucket after resolution.
    await enforce_auth_rate_limit(
        request,
        identity=f"raw:{data.login_identifier}",
        policy=login_lookup_rate_limit_policy(),
    )

    # 1. Query Identity
    identity = await identity_dao.get_by_login_identifier(data.login_identifier)
    await enforce_auth_rate_limit(
        request,
        identity=(
            f"identity:{identity.id}"
            if identity is not None
            else f"unknown:{data.login_identifier}"
        ),
        policy=login_rate_limit_policy(),
    )

    if (
        not identity
        or not identity.password_login_enabled
        or not identity.password_hash
        or not await verify_password_async(data.password, identity.password_hash)
    ):
        logger.warning(f"[LOGIN] Invalid credentials identity_found={identity is not None}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # 2. Check Global Activity & Verification
    if not identity.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been disabled.")

    if not identity.email_verified:
        from app.config import unverified_local_signup_allowed
        email_config = await _resolve_auth_email_config()

        if not email_config:
            if not unverified_local_signup_allowed():
                logger.error(
                    "[LOGIN] Unverified password login refused because email delivery is unavailable"
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication is temporarily unavailable. Please try again later.",
                )

            # Explicit development/test escape hatch: auto-verify under a
            # transaction so local instances without SMTP remain usable.
            async with transaction():
                tx_identity = await identity_dao.get_for_update(identity.id)
                if not tx_identity or not tx_identity.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Your account has been disabled.",
                    )
                tx_identity.email_verified = True
                identity.email_verified = True
                await _activate_email_verification_pending_memberships(tx_identity.id)
        else:
            # Find any user record (just for the task)
            user = await user_dao.get_representative_user_for_identity(identity.id)

            # Trigger email delivery in background
            if user:
                await _send_verification_email_task(user, background_tasks, request=request)

            # Consistent with identity-first flow: Return 403 Forbidden with verification intent
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "needs_verification": True,
                    "email": identity.email,
                    "message": "Please verify your email to continue.",
                },
            )

    # 3. Find all User records (tenants)
    identity_users = await user_dao.get_by_identity_id(identity.id, include_identity=True)
    active_users = [candidate for candidate in identity_users if candidate.is_active]
    tenant_ids = [candidate.tenant_id for candidate in active_users if candidate.tenant_id]
    active_tenants = await tenant_dao.get_by_ids(tenant_ids) if tenant_ids else []
    tenants_map = {
        str(tenant.id): tenant
        for tenant in active_tenants
        if tenant.is_active
    }
    valid_users = [
        candidate
        for candidate in active_users
        if candidate.tenant_id is None or str(candidate.tenant_id) in tenants_map
    ]

    if not valid_users:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active organization membership is available.",
        )

    # 4. Handle Tenant Selection. A global platform administrator must remain
    # able to enter the platform console even when the same Identity is also a
    # member of one or more tenants. The tenant picker cannot represent the
    # global record with tenant_id=null as a selectable tenant, so prefer that
    # canonical record explicitly.
    platform_user = next(
        (
            candidate
            for candidate in valid_users
            if candidate.tenant_id is None and candidate.role == "platform_admin"
        ),
        None,
    )
    if not data.tenant_id:
        if identity.is_platform_admin and platform_user is not None:
            user = platform_user
        # If multiple tenants, return choice
        elif len(valid_users) > 1:
            tenant_choices = []
            for u in valid_users:
                tenant = tenants_map.get(str(u.tenant_id)) if u.tenant_id else None
                tenant_choices.append(
                    TenantChoice(
                        tenant_id=u.tenant_id,
                        tenant_name=tenant.name if tenant else "Create or Join Organization",
                        tenant_slug=tenant.slug if tenant else "",
                        logo_url=tenant.logo_url if tenant else None,
                        membership_role=(u.role if u.tenant_id else None),
                    )
                )

            return MultiTenantResponse(
                requires_tenant_selection=True,
                login_identifier=data.login_identifier,
                tenants=tenant_choices,
            )

        else:
            # Only one tenant
            user = valid_users[0]
    else:
        # Specific tenant requested (Dedicated Link flow)
        user = next((u for u in valid_users if u.tenant_id == data.tenant_id), None)

        # Cross-tenant access check
        if not user:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account does not belong to the selected organization.",
            )

    if user.tenant_id:
        tenant = tenants_map.get(str(user.tenant_id))
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your organization has been disabled.",
            )

    mfa_challenge = await _issue_password_login_mfa_challenge(
        identity=identity,
        user=user,
    )
    if mfa_challenge is not None:
        return mfa_challenge

    # 6. Generate Token
    token = create_access_token(
        str(user.id), user.role, auth_version=identity_auth_version(identity)
    )
    return TokenResponse(
        access_token=token,
        user=await serialize_user_with_access(user),
        identity=IdentityOut.model_validate(identity),
        needs_company_setup=user.tenant_id is None,
    )


@router.get("/email-hint")
async def get_email_hint(request: Request, username: str):
    """Return a hinted email address for a given username."""
    await enforce_auth_rate_limit(
        request,
        identity=username,
        policy=discovery_rate_limit_policy(),
    )
    identity = await identity_dao.get_by_username(username)

    if not identity or not identity.email:
        raise HTTPException(status_code=404, detail="Account not found.")

    email = identity.email
    parts = email.split("@")
    if len(parts) == 2:
        name, domain = parts

        # Obfuscate name
        if len(name) <= 2:
            obs_name = name[0] + "***"
        else:
            obs_name = name[:2] + "***" + name[-1]

        # Obfuscate domain
        domain_parts = domain.split(".")
        if len(domain_parts) >= 2:
            d_name = domain_parts[0]
            d_ext = ".".join(domain_parts[1:])
            if len(d_name) <= 2:
                obs_domain = d_name[0] + "***." + d_ext
            else:
                obs_domain = d_name[0] + "***" + d_name[-1] + "." + d_ext
            hint = f"{obs_name}@{obs_domain}"
        else:
            hint = f"{obs_name}@{domain}"
    else:
        hint = email[:3] + "***"

    return {"hint": hint}


@router.post("/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """Request a password reset link for a global Identity."""
    await enforce_auth_rate_limit(
        request,
        identity=str(data.email),
        policy=email_action_rate_limit_policy(),
    )
    generic_response = {
        "ok": True,
        "message": "If an account with that email exists, a password reset email has been sent.",
    }

    identity = None
    try:
        from app.services.password_reset_service import build_password_reset_url, create_password_reset_token
        from app.services.outbound_email_service import dispatch_outbound_email, persist_template_email

        # Serialize reset issuance with email changes.  The second lookup under
        # the row lock closes the read/issue race on the old address.
        async with transaction():
            requested_email = canonicalize_email(data.email)
            identity = await identity_dao.get_by_email(data.email)
            if not identity:
                return generic_response
            identity = await identity_dao.get_for_update(identity.id)
            if (
                not identity
                or not identity.is_active
                or not identity.email_verified
                or canonicalize_email(identity.email) != requested_email
            ):
                return generic_response
            raw_token, expires_at = await create_password_reset_token(
                identity.id,
                str(identity.email),
                identity_auth_version(identity),
            )

        reset_url = await build_password_reset_url(raw_token)
        expiry_minutes = int((expires_at - datetime.now(timezone.utc)).total_seconds() // 60)
        delivery = await persist_template_email(
            purpose="password_reset",
            to=identity.email,
            scenario_key="password_reset",
            variables={
                "display_name": identity.username or "User",
                "reset_url": reset_url,
                "expiry_minutes": str(expiry_minutes),
            },
            identity_id=identity.id,
        )
        if delivery.status in {"queued", "retry_wait"}:
            background_tasks.add_task(dispatch_outbound_email, delivery.id)
    except Exception as exc:
        logger.warning(
            "Failed to process password reset email error_type={}",
            type(exc).__name__,
        )

    return generic_response


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """Reset a password using a valid single-use token."""
    from app.services.password_reset_service import consume_password_reset_token

    # Consume token outside transaction
    token_data = await consume_password_reset_token(data.token)
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    identity_id = token_data["identity_id"]
    token_email = canonicalize_email(token_data.get("email"))
    try:
        token_auth_version = int(token_data["auth_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset token",
        ) from exc

    # Hash new password outside transaction (CPU intensive)
    new_hash = await hash_password_async(data.new_password)

    # Perform DB update in a brief transaction (single select and update)
    async with transaction():
        identity = await identity_dao.get_for_update(identity_id)
        if (
            not identity
            or not identity.is_active
            or not identity.email_verified
            or token_auth_version != identity_auth_version(identity)
            or not token_email
            or token_email != canonicalize_email(identity.email)
        ):
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        identity.password_hash = new_hash
        identity.password_login_enabled = True
        identity.auth_version = int(getattr(identity, "auth_version", 0) or 0) + 1
        # Password recovery changes only the global password capability.  It
        # must not silently complete the separate email-verification workflow
        # or reactivate any tenant membership.  In particular, a Web signup
        # that is still pending verification must remain eligible for resend +
        # verify-email instead of becoming verified-but-permanently-inactive.

    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_verification_user)):
    """Get current user profile."""
    return await serialize_user_with_access(current_user)


@router.post("/browser-session", status_code=status.HTTP_204_NO_CONTENT)
async def create_browser_session(
    request: Request,
    response: Response,
    current_user: User = Depends(get_verification_user),
):
    """Mirror a validated bearer token into a same-origin HttpOnly cookie."""
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    # Returning FastAPI's injected Response object bypasses the decorator's
    # response construction. Give it an explicit status so Starlette does not
    # emit ``http.response.start`` with ``status=None``.
    response.status_code = status.HTTP_204_NO_CONTENT
    set_browser_session_cookie(response, token.strip(), request)
    return response


@router.delete("/browser-session", status_code=status.HTTP_204_NO_CONTENT)
async def delete_browser_session(request: Request, response: Response):
    """Clear the browser-only credential during logout or account switching."""
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_browser_session_cookie(response, request)
    return response


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: SelfUserUpdate,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """Update a profile; global login/recovery fields require password proof."""
    update_data = data.model_dump(exclude_unset=True, exclude={"current_password"})
    if "email" in update_data and update_data["email"] is not None:
        update_data["email"] = canonicalize_email(update_data["email"])
    if "username" in update_data and update_data["username"] is not None:
        update_data["username"] = normalize_safe_username(update_data["username"])
    if "primary_mobile" in update_data and update_data["primary_mobile"]:
        update_data["primary_mobile"] = canonicalize_phone(
            update_data["primary_mobile"]
        )

    # Verify the current password without holding a pooled database connection.
    # A password hash snapshot is rechecked under the row lock below so a
    # concurrent password reset/change cannot authorize this mutation.
    preflight_user = await user_dao.get_with_identity(current_user.id)
    if not preflight_user or not preflight_user.identity:
        raise HTTPException(status_code=404, detail="User not found")
    preflight_identity = preflight_user.identity
    requested_global_fields = {"email", "username", "primary_mobile"} & set(
        update_data
    )
    preflight_values = {
        "email": preflight_identity.email,
        "username": preflight_identity.username,
        "primary_mobile": preflight_identity.phone,
    }
    preflight_changed_fields = {
        field
        for field in requested_global_fields
        if update_data[field] != preflight_values[field]
    }
    verified_password_hash: str | None = None
    if preflight_changed_fields:
        if (
            not preflight_identity.password_login_enabled
            or not preflight_identity.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Password reauthentication is required for login or recovery "
                    "field changes. SSO-only accounts must contact an administrator."
                ),
            )
        if not data.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_password is required for login or recovery field changes",
            )
        await enforce_auth_rate_limit(
            request,
            identity=f"identity:{preflight_identity.id}",
            policy=password_reauth_rate_limit_policy(),
        )
        if not await verify_password_async(
            data.current_password,
            preflight_identity.password_hash,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )
        verified_password_hash = preflight_identity.password_hash

    refreshed_token: str | None = None
    async with transaction() as session:
        await acquire_identity_login_namespace_lock(session)
        # Fetch current user in the transaction session
        user = await user_dao.get_with_identity(current_user.id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Lock the global Identity before changing login or recovery fields.
        global_fields = {"email", "username", "primary_mobile"} & set(update_data)
        locked_identity = None
        changed_global_fields: set[str] = set()
        if global_fields:
            locked_identity = await identity_dao.get_for_update(user.identity_id)
            if not locked_identity:
                raise HTTPException(status_code=404, detail="Identity not found")
            current_values = {
                "email": locked_identity.email,
                "username": locked_identity.username,
                "primary_mobile": locked_identity.phone,
            }
            changed_global_fields = {
                field
                for field in global_fields
                if update_data[field] != current_values[field]
            }

        email_changed = "email" in changed_global_fields
        username_changed = "username" in changed_global_fields
        phone_changed = "primary_mobile" in changed_global_fields

        if changed_global_fields and (
            verified_password_hash is None
            or not locked_identity
            or not locked_identity.password_login_enabled
            or not locked_identity.password_hash
            or not hmac.compare_digest(
                locked_identity.password_hash,
                verified_password_hash,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Credentials changed. Please sign in again.",
            )

        if email_changed and update_data["email"] is None:
            raise HTTPException(status_code=400, detail="Email cannot be cleared")
        if username_changed and update_data["username"] is None:
            raise HTTPException(status_code=400, detail="Username cannot be cleared")

        if changed_global_fields:
            await validate_identity_login_namespace(
                username=update_data.get("username", locked_identity.username),
                email=update_data.get("email", locked_identity.email),
                phone=update_data.get("primary_mobile", locked_identity.phone),
                owned_identity_id=user.identity_id,
            )

        for field, value in update_data.items():
            if field in global_fields:
                continue
            setattr(user, field, value)

        if email_changed:
            # A token issued to the old address must never verify the new one.
            # Redis invalidation is fail-closed: if it cannot complete, the DB
            # transaction is rolled back and the email remains unchanged.
            from app.services.email_verification_service import email_verification_service
            from app.services.password_reset_service import invalidate_password_reset_tokens

            await email_verification_service.invalidate_email_verification_tokens(
                user.identity_id
            )
            await invalidate_password_reset_tokens(user.identity_id)
            locked_identity.email = update_data["email"]
            locked_identity.email_verified = False
        if locked_identity is not None:
            try:
                if username_changed:
                    locked_identity.username = update_data["username"]
                if phone_changed:
                    locked_identity.phone = update_data["primary_mobile"]
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if changed_global_fields:
                locked_identity.auth_version = int(locked_identity.auth_version or 0) + 1
            user.identity = locked_identity

        try:
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Login or recovery field is already in use",
            ) from exc

        # Sync email/phone to OrgMember if changed
        if email_changed or phone_changed:
            from app.services.registration_service import registration_service

            await registration_service.sync_org_member_contact_from_user(
                user,
                sync_email=email_changed,
                sync_phone=phone_changed,
            )

        result = await serialize_user_with_access(user)
        if changed_global_fields:
            refreshed_token = create_access_token(
                str(user.id),
                user.role,
                auth_version=identity_auth_version(locked_identity),
                mfa_verified=access_context_mfa_verified(current_user),
            )

    if refreshed_token:
        response.headers["X-Astra-Access-Token"] = refreshed_token
        set_browser_session_cookie(response, refreshed_token, request)
    return result


@router.get("/my-tenants", response_model=list[TenantChoice])
async def get_my_tenants(
    current_user: User = Depends(get_authenticated_user),
):
    """Get all tenants associated with the current user's identity."""
    # 1. Get all user records for this identity
    users = [
        user
        for user in await user_dao.get_by_identity_id(current_user.identity_id)
        if user.is_active
    ]

    # 2. Extract tenant IDs
    tenant_ids = [u.tenant_id for u in users if u.tenant_id]
    if not tenant_ids:
        return []

    # 3. Get tenant details
    tenants = [tenant for tenant in await tenant_dao.get_by_ids(tenant_ids) if tenant.is_active]

    membership_by_tenant = {str(user.tenant_id): user for user in users if user.tenant_id}
    return [
        TenantChoice(
            tenant_id=t.id,
            tenant_name=t.name,
            tenant_slug=t.slug,
            logo_url=t.logo_url,
            membership_role=membership_by_tenant[str(t.id)].role,
        )
        for t in tenants
    ]


@router.post("/switch-tenant", response_model=TenantSwitchResponse)
async def switch_tenant(
    data: TenantSwitchRequest,
    request: Request,
    current_user: User = Depends(get_authenticated_user),
):
    """Switch to a different tenant and return a new token and redirect URL."""
    # 1. Verify membership
    target_user = await user_dao.get_by_identity_and_tenant(current_user.identity_id, data.tenant_id)

    if not target_user or not target_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this organization."
        )

    # 2. Get tenant details
    tenant = await tenant_dao.get(data.tenant_id)

    if not tenant or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This organization is currently unavailable."
        )

    # 3. Generate new token
    token = create_access_token(
        str(target_user.id),
        target_user.role,
        auth_version=identity_auth_version(current_user),
        mfa_verified=access_context_mfa_verified(current_user),
    )

    # 4. Determine redirect URL
    from app.services.platform_service import platform_service

    sso_redirect_enabled = await system_setting_dao.is_sso_custom_domain_redirect_enabled()

    if (
        not sso_redirect_enabled
        or not bool(getattr(tenant, "sso_enabled", False))
        or not str(getattr(tenant, "sso_domain", "") or "").strip()
    ):
        redirect_url = None
    else:
        async with tenant_dao.session() as session:
            redirect_url = await platform_service.get_tenant_sso_base_url(
                session, tenant, request, sso_redirect_enabled=sso_redirect_enabled
            )

    # URL fragments are not sent to reverse proxies or access logs. Bind the
    # candidate JWT to its intended tenant so the destination browser can
    # validate token tenant + declared tenant + current origin before commit.
    if redirect_url:
        separator = "&" if "#" in redirect_url else "#"
        redirect_fragment = urlencode({
            'session_token': token,
            'target_tenant_id': str(tenant.id),
        })
        redirect_url = f"{redirect_url}{separator}{redirect_fragment}"

    return TenantSwitchResponse(
        access_token=token,
        target_tenant_id=tenant.id,
        redirect_url=redirect_url,
        message="Switching organization...",
    )


@router.put("/me/password")
async def change_password(
    data: dict,
    request: Request,
    current_user: User = Depends(get_authenticated_user),
):
    """Change current user's password. Updates the global identity password."""
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Both old_password and new_password are required")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    # Look up user & identity outside transaction
    user = await user_dao.get_with_identity(current_user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    identity = user.identity
    await enforce_auth_rate_limit(
        request,
        identity=f"identity:{identity.id if identity else current_user.identity_id}",
        policy=password_change_rate_limit_policy(),
    )

    # Verify old password outside transaction (CPU intensive)
    if (
        not identity
        or not identity.password_login_enabled
        or not identity.password_hash
        or not await verify_password_async(old_password, identity.password_hash)
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    verified_password_hash = identity.password_hash

    # Compute new hash outside transaction (CPU intensive)
    new_hash = await hash_password_async(new_password)

    # Perform DB update in a brief transaction
    async with transaction():
        tx_identity = await identity_dao.get_for_update(identity.id)
        if (
            not tx_identity
            or not tx_identity.is_active
            or not tx_identity.password_login_enabled
            or not tx_identity.password_hash
            or not hmac.compare_digest(tx_identity.password_hash, verified_password_hash)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Credentials changed. Please sign in again.",
            )
        tx_identity.password_hash = new_hash
        tx_identity.password_login_enabled = True
        tx_identity.auth_version = int(
            getattr(tx_identity, "auth_version", 0) or 0
        ) + 1

    token = create_access_token(
        str(current_user.id),
        current_user.role,
        auth_version=tx_identity.auth_version,
        mfa_verified=access_context_mfa_verified(current_user),
    )
    return {"ok": True, "access_token": token}


# ─── SSO/OAuth Endpoints ─────────────────────────────────────────────


@router.get("/providers")
async def list_providers(
    tenant_id: uuid.UUID | None = Query(None, description="Optional tenant ID"),
):
    """List all available identity providers."""
    from app.services.auth_registry import auth_provider_registry

    providers = await auth_provider_registry.list_providers(str(tenant_id) if tenant_id else None)
    return [
        {"id": str(p.id), "provider_type": p.provider_type, "name": p.name, "is_active": p.is_active}
        for p in providers
    ]


# Redis keys for OAuth CSRF and two-step tenant selection
_OAUTH_STATE_PREFIX = "oauth_state:"
_OAUTH_PENDING_PREFIX = "oauth_pending:"
_OAUTH_PENDING_TTL = 600  # 10 minutes
_OAUTH_BROWSER_NONCE_COOKIE = "astra_oauth_nonce"
_OAUTH_DELETE_IF_UNCHANGED_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _cache_oauth_payload(prefix: str, token: str, payload: dict) -> None:
    """Store a short-lived OAuth control payload without provider secrets."""
    import json
    from app.core.events import get_redis

    r = await get_redis()
    await r.set(
        f"{prefix}{token}",
        json.dumps(payload, separators=(",", ":")),
        ex=_OAUTH_PENDING_TTL,
    )


async def _consume_oauth_payload(prefix: str, token: str) -> dict | None:
    """Atomically consume a short-lived OAuth control payload exactly once."""
    import json
    from app.core.events import get_redis

    r = await get_redis()
    raw = await r.getdel(f"{prefix}{token}")
    if not raw:
        return None
    return json.loads(raw)


async def _cache_oauth_state(state_token: str, payload: dict) -> None:
    await _cache_oauth_payload(_OAUTH_STATE_PREFIX, state_token, payload)


async def _consume_oauth_state(
    state_token: str,
    *,
    provider_type: str,
    redirect_uri: str,
    browser_nonce: str,
) -> dict | None:
    """Consume state only after every browser-bound callback field validates."""
    import json
    from app.core.events import get_redis

    if not state_token or not browser_nonce:
        return None

    redis = await get_redis()
    key = f"{_OAUTH_STATE_PREFIX}{state_token}"
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    expected_provider = str(payload.get("provider_type") or "")
    expected_redirect_uri = str(payload.get("redirect_uri") or "")
    expected_nonce_hash = str(payload.get("browser_nonce_hash") or "")
    actual_nonce_hash = hashlib.sha256(browser_nonce.encode("utf-8")).hexdigest()
    legacy_expected_nonce = str(payload.get("browser_nonce") or "")
    nonce_matches = (
        bool(expected_nonce_hash)
        and hmac.compare_digest(actual_nonce_hash, expected_nonce_hash)
    ) or (
        bool(legacy_expected_nonce)
        and hmac.compare_digest(browser_nonce, legacy_expected_nonce)
    )
    if (
        not expected_provider
        or not hmac.compare_digest(provider_type, expected_provider)
        or not expected_redirect_uri
        or not hmac.compare_digest(redirect_uri, expected_redirect_uri)
        or not nonce_matches
    ):
        return None

    consumed = await redis.eval(
        _OAUTH_DELETE_IF_UNCHANGED_SCRIPT,
        1,
        key,
        raw,
    )
    return payload if int(consumed or 0) == 1 else None


async def _cache_oauth_pending(
    pending_token: str,
    payload: dict,
    browser_nonce: str,
) -> None:
    """Store an allowlist under a key bound to the initiating browser."""
    nonce_hash = hashlib.sha256(browser_nonce.encode("utf-8")).hexdigest()
    await _cache_oauth_payload(
        _OAUTH_PENDING_PREFIX,
        f"{pending_token}:{nonce_hash}",
        payload,
    )


async def _get_oauth_pending(
    pending_token: str,
    browser_nonce: str,
) -> dict | None:
    """Consume an allowlist only from the browser that started OAuth."""
    if not browser_nonce:
        return None
    nonce_hash = hashlib.sha256(browser_nonce.encode("utf-8")).hexdigest()
    return await _consume_oauth_payload(
        _OAUTH_PENDING_PREFIX,
        f"{pending_token}:{nonce_hash}",
    )


@router.get("/{provider}/authorize", response_model=OAuthAuthorizeResponse)
async def authorize(
    provider: str,
    request: Request,
    response: Response,
    redirect_uri: str = Query(..., description="OAuth callback URI"),
):
    """Start a global social OAuth flow with server-owned CSRF state."""
    from app.services.auth_registry import auth_provider_registry

    if provider not in {"google", "github"}:
        raise HTTPException(status_code=404, detail="OAuth provider not supported")
    await enforce_auth_rate_limit(
        request,
        identity=provider,
        policy=oauth_start_rate_limit_policy(),
    )

    auth_provider = await auth_provider_registry.get_provider(
        provider,
        require_sso_login=True,
        allow_global_fallback=False,
    )
    if not auth_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    state_token = secrets.token_urlsafe(32)
    browser_nonce = request.cookies.get(_OAUTH_BROWSER_NONCE_COOKIE) or secrets.token_urlsafe(32)
    await _cache_oauth_state(
        state_token,
        {
            "provider_type": provider,
            "redirect_uri": redirect_uri,
            "browser_nonce_hash": hashlib.sha256(
                browser_nonce.encode("utf-8")
            ).hexdigest(),
        },
    )

    try:
        auth_url = await auth_provider.get_authorization_url(redirect_uri, state_token)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail="OAuth provider is not implemented") from exc
    except Exception as exc:
        logger.error(
            "Failed to generate authorization URL provider={} error_type={}",
            provider,
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="Failed to generate authorization URL") from exc

    response.set_cookie(
        key=_OAUTH_BROWSER_NONCE_COOKIE,
        value=browser_nonce,
        max_age=_OAUTH_PENDING_TTL,
        path="/",
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )
    return OAuthAuthorizeResponse(authorization_url=auth_url)


@router.post("/{provider}/callback", response_model=Any)
async def oauth_callback(
    provider: str,
    data: OAuthCallbackRequest,
    request: Request,
):
    """Handle global social OAuth with single-use CSRF and tenant allowlists."""
    import uuid as _uuid
    from sqlalchemy import or_
    from sqlalchemy.orm import selectinload

    from app.models.identity import IdentityProvider
    from app.models.org import OrgMember
    from app.models.tenant import Tenant
    from app.services.auth_registry import auth_provider_registry
    from app.services.external_identity_policy import acquire_external_subject_lock
    from app.services.identity_provider_lookup import get_login_identity_provider_by_id
    from app.services.sso_service import sso_service

    if provider not in {"google", "github"}:
        raise HTTPException(status_code=404, detail="OAuth provider not supported")
    await enforce_auth_rate_limit(
        request,
        identity=provider,
        policy=oauth_exchange_rate_limit_policy(),
    )

    # ── Step 2: consume the exact tenant allowlist ─────────────────────
    if data.pending_token and data.tenant_id:
        browser_nonce = request.cookies.get(_OAUTH_BROWSER_NONCE_COOKIE) or ""
        pending = await _get_oauth_pending(data.pending_token, browser_nonce)
        if not pending:
            raise HTTPException(
                status_code=400,
                detail="OAuth session expired or invalid. Please sign in again.",
            )

        if pending.get("provider_type") != provider:
            raise HTTPException(status_code=400, detail="OAuth session is invalid")

        selected = next(
            (
                item
                for item in pending.get("memberships", [])
                if str(item.get("tenant_id")) == str(data.tenant_id)
            ),
            None,
        )
        if not selected:
            raise HTTPException(status_code=403, detail="Tenant selection is not allowed")

        try:
            provider_id = _uuid.UUID(str(pending["provider_id"]))
            linked_user_id = _uuid.UUID(str(pending["linked_user_id"]))
            identity_id = _uuid.UUID(str(pending["identity_id"]))
            selected_user_id = _uuid.UUID(str(selected["user_id"]))
            selected_tenant_id = _uuid.UUID(str(selected["tenant_id"]))
            requested_tenant_id = _uuid.UUID(str(data.tenant_id))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="OAuth session is invalid") from exc

        lookup = pending.get("identity_lookup") or {}
        lookup_conditions = [
            getattr(OrgMember, field) == value
            for field in ("unionid", "external_id", "open_id")
            if (value := str(lookup.get(field) or "").strip())
        ]
        if not lookup_conditions:
            raise HTTPException(status_code=400, detail="OAuth session is invalid")

        async with transaction() as session:
            provider_result = await session.execute(
                select(IdentityProvider)
                .where(IdentityProvider.id == provider_id)
                .with_for_update()
            )
            provider_record = provider_result.scalar_one_or_none()
            if (
                not provider_record
                or not provider_record.is_active
                or not provider_record.sso_login_enabled
                or provider_record.provider_type != provider
                or provider_record.tenant_id is not None
            ):
                raise HTTPException(status_code=403, detail="OAuth provider is disabled")

            link_result = await session.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider_id,
                    OrgMember.tenant_id.is_(None),
                    OrgMember.user_id == linked_user_id,
                    OrgMember.status == "active",
                    or_(*lookup_conditions),
                )
            )
            if not link_result.scalars().first():
                raise HTTPException(status_code=403, detail="OAuth identity link changed")

            linked_user = await user_dao.get_with_identity(linked_user_id)
            if (
                not linked_user
                or linked_user.identity_id != identity_id
                or not getattr(getattr(linked_user, "identity", None), "is_active", False)
            ):
                raise HTTPException(status_code=403, detail="Account is disabled")

            user = await user_dao.get_with_identity(selected_user_id)
            if (
                not external_user_can_authenticate(user)
                or user.identity_id != identity_id
                or user.tenant_id != selected_tenant_id
                or selected_tenant_id != requested_tenant_id
            ):
                raise HTTPException(status_code=403, detail="Tenant selection is not allowed")

            tenant = await session.get(Tenant, selected_tenant_id)
            if not tenant or not tenant.is_active:
                raise HTTPException(status_code=403, detail="Tenant is disabled")

        jwt_token = create_access_token(
            str(user.id), user.role, auth_version=identity_auth_version(user)
        )
        return TokenResponse(
            access_token=jwt_token,
            user=await serialize_user_with_access(user),
            needs_company_setup=False,
        )

    if data.pending_token or data.tenant_id:
        raise HTTPException(status_code=400, detail="Incomplete tenant selection")

    # ── Step 1: Exchange code, detect multi-tenant ────────────────────────────
    if not data.code or not data.state or not data.redirect_uri:
        raise HTTPException(status_code=400, detail="Missing OAuth callback parameters")

    browser_nonce = request.cookies.get(_OAUTH_BROWSER_NONCE_COOKIE) or ""
    state_payload = await _consume_oauth_state(
        data.state,
        provider_type=provider,
        redirect_uri=data.redirect_uri,
        browser_nonce=browser_nonce,
    )
    if not state_payload:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")

    auth_provider = await auth_provider_registry.get_provider(
        provider,
        require_sso_login=True,
        allow_global_fallback=False,
    )
    provider_record = getattr(auth_provider, "provider", None) if auth_provider else None
    if not auth_provider or not provider_record:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    try:
        token_data = await auth_provider.exchange_code_for_token(data.code, data.redirect_uri)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token from provider")

        user_info = await auth_provider.get_user_info(access_token)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "OAuth callback failed provider={} error_type={}",
            provider,
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="OAuth authentication failed") from exc

    provider_subject = require_stable_external_subject(
        provider,
        user_info.provider_user_id or user_info.provider_union_id,
    )
    identity_data = auth_provider._identity_payload(user_info)
    raw_union_id, raw_open_id, raw_external_id = sso_service._extract_identity_ids(
        provider,
        provider_subject,
        identity_data,
    )
    identity_lookup = {
        "unionid": raw_union_id,
        "open_id": raw_open_id,
        "external_id": raw_external_id,
    }

    selected_user = None
    tenant_choices: list[TenantChoice] = []
    pending_payload = None
    provider_tenant_id = provider_record.tenant_id

    async with transaction() as session:
        current_provider = await get_login_identity_provider_by_id(
            session,
            provider_id=provider_record.id,
            provider_type=provider,
            tenant_id=provider_tenant_id,
            for_update=True,
        )
        if not current_provider:
            raise HTTPException(status_code=403, detail="OAuth provider is disabled")
        await acquire_external_subject_lock(
            session,
            provider_type=provider,
            tenant_id=provider_tenant_id,
            provider_subject=provider_subject,
        )
        linked_user = await sso_service.resolve_user_identity(
            session,
            provider_subject,
            provider,
            tenant_id=str(provider_tenant_id) if provider_tenant_id else None,
            identity_data=identity_data,
            provider_id=provider_record.id,
        )

        if linked_user:
            # A global provider link is anchored by one historical User row,
            # while authorization is tenant-membership specific. Disabling the
            # anchor membership must not revoke other active memberships; only
            # disabling the global Identity has that effect.
            if not getattr(getattr(linked_user, "identity", None), "is_active", False):
                raise HTTPException(status_code=403, detail="Account is disabled")

            users_result = await session.execute(
                select(User)
                .where(User.identity_id == linked_user.identity_id)
                .options(selectinload(User.identity))
                .order_by(User.created_at.asc(), User.id.asc())
            )
            identity_users = [
                item
                for item in users_result.scalars().all()
                if external_user_can_authenticate(item)
            ]
            tenant_users = [item for item in identity_users if item.tenant_id is not None]
            tenantless_users = [item for item in identity_users if item.tenant_id is None]

            tenants_result = (
                await session.execute(
                    select(Tenant).where(
                        Tenant.id.in_([item.tenant_id for item in tenant_users]),
                        Tenant.is_active.is_(True),
                    )
                )
                if tenant_users
                else None
            )
            tenants = list(tenants_result.scalars().all()) if tenants_result else []
            tenants_map = {str(item.id): item for item in tenants}
            tenant_users = [
                item for item in tenant_users if str(item.tenant_id) in tenants_map
            ]

            memberships_by_tenant: dict[str, User] = {}
            for membership in tenant_users:
                tenant_key = str(membership.tenant_id)
                if tenant_key in memberships_by_tenant:
                    raise HTTPException(
                        status_code=409,
                        detail="Duplicate tenant memberships require administrator review",
                    )
                memberships_by_tenant[tenant_key] = membership

            is_platform_admin = bool(
                getattr(getattr(linked_user, "identity", None), "is_platform_admin", False)
            )
            if is_platform_admin:
                if len(tenantless_users) != 1:
                    raise HTTPException(status_code=409, detail="Platform account state is ambiguous")
                selected_user = tenantless_users[0]
            elif len(memberships_by_tenant) > 1:
                tenant_choices = [
                    TenantChoice(
                        tenant_id=membership.tenant_id,
                        tenant_name=tenants_map[tenant_key].name,
                        tenant_slug=tenants_map[tenant_key].slug,
                        logo_url=tenants_map[tenant_key].logo_url,
                        membership_role=membership.role,
                    )
                    for tenant_key, membership in memberships_by_tenant.items()
                ]
                pending_payload = {
                    "provider_type": provider,
                    "provider_id": str(provider_record.id),
                    "linked_user_id": str(linked_user.id),
                    "identity_id": str(linked_user.identity_id),
                    "identity_lookup": identity_lookup,
                    "memberships": [
                        {"tenant_id": key, "user_id": str(membership.id)}
                        for key, membership in memberships_by_tenant.items()
                    ],
                }
            elif len(memberships_by_tenant) == 1:
                selected_user = next(iter(memberships_by_tenant.values()))
            elif len(tenantless_users) == 1:
                selected_user = tenantless_users[0]
            else:
                raise HTTPException(status_code=403, detail="No active account membership")
        else:
            # The public OAuth button is intentionally sign-in-only. Account
            # creation uses password registration or organization-managed SSO;
            # the retired public social-signup route never had a complete
            # browser-bound redirect and tenant-ownership contract.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No linked account exists for this OAuth identity.",
            )

    if pending_payload:
        pending_token = _uuid.uuid4().hex
        await _cache_oauth_pending(pending_token, pending_payload, browser_nonce)

        return MultiTenantResponse(
            requires_tenant_selection=True,
            login_identifier=user_info.email or "",
            tenants=tenant_choices,
            pending_token=pending_token,
        )

    if not selected_user:
        raise HTTPException(status_code=500, detail="Failed to resolve account")

    jwt_token = create_access_token(
        str(selected_user.id),
        selected_user.role,
        auth_version=identity_auth_version(selected_user),
    )
    return TokenResponse(
        access_token=jwt_token,
        user=await serialize_user_with_access(selected_user),
        needs_company_setup=selected_user.tenant_id is None,
    )


@router.post("/{provider}/bind", response_model=UserOut)
async def bind_identity(
    provider: str,
    data: IdentityBindRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """Bind an external identity to the current user."""
    from sqlalchemy.orm import selectinload

    from app.models.tenant import Tenant
    from app.services.auth_registry import auth_provider_registry
    from app.services.identity_provider_lookup import get_login_identity_provider_by_id
    from app.services.sso_service import (
        ExternalIdentityAlreadyLinkedError,
        ExternalIdentityAmbiguousError,
        sso_service,
    )

    reauthenticated_identity_id, verified_password_hash = (
        await _reauthenticate_sensitive_identity_action(
            request,
            current_user,
            data.current_password,
        )
    )
    tenant_scope = str(current_user.tenant_id) if current_user.tenant_id else None
    auth_provider = await auth_provider_registry.get_provider(
        provider,
        tenant_scope,
        require_sso_login=True,
        allow_global_fallback=provider in {"google", "github"},
    )
    if not auth_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")
    provider_record = auth_provider.provider
    if not provider_record:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")
    if provider_record.tenant_id not in {None, current_user.tenant_id}:
        raise HTTPException(status_code=403, detail="Provider belongs to a different tenant")
    provider_tenant_id = provider_record.tenant_id

    try:
        # Exchange code for token (network call) outside transaction
        token_data = await auth_provider.exchange_code_for_token(data.code)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token from provider")

        # Get user info (network call) outside transaction
        user_info = await auth_provider.get_user_info(access_token)

        async with transaction() as session:
            # The provider, tenant membership, and global Identity may all be
            # disabled while the external network calls are in flight.  Lock
            # and revalidate the exact objects before persisting the link.
            current_provider = await get_login_identity_provider_by_id(
                session,
                provider_id=provider_record.id,
                provider_type=provider,
                tenant_id=provider_tenant_id,
                for_update=True,
            )
            if not current_provider:
                raise HTTPException(status_code=403, detail="Provider is disabled")

            locked_user_tenant = None
            if current_user.tenant_id is not None:
                tenant_result = await session.execute(
                    select(Tenant)
                    .where(Tenant.id == current_user.tenant_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                locked_user_tenant = tenant_result.scalar_one_or_none()
                if not locked_user_tenant or not locked_user_tenant.is_active:
                    raise HTTPException(status_code=403, detail="Tenant is disabled")

            user_result = await session.execute(
                select(User)
                .where(User.id == current_user.id)
                .options(selectinload(User.identity))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            fresh_user = user_result.scalar_one_or_none()
            identity = await identity_dao.get_for_update(current_user.identity_id)
            if (
                not fresh_user
                or fresh_user.identity_id != current_user.identity_id
                or fresh_user.tenant_id != current_user.tenant_id
                or not identity
                or identity.id != reauthenticated_identity_id
                or not identity.password_login_enabled
                or not identity.password_hash
                or not hmac.compare_digest(
                    identity.password_hash,
                    verified_password_hash,
                )
                or not external_user_can_authenticate(fresh_user)
            ):
                raise HTTPException(status_code=403, detail="Account is disabled")
            fresh_user.identity = identity

            if current_provider.tenant_id not in {None, fresh_user.tenant_id}:
                raise HTTPException(status_code=403, detail="Provider belongs to a different tenant")

            auth_provider.provider = current_provider
            auth_provider.config = dict(current_provider.config or {})

            # Check if identity is already linked to another user
            lookup_provider_user_id = require_stable_external_subject(
                provider,
                user_info.provider_user_id or user_info.provider_union_id,
            )
            identity_data = auth_provider._identity_payload(user_info)
            existing_user = await sso_service.check_duplicate_identity(
                session,
                provider,
                lookup_provider_user_id,
                tenant_id=str(provider_tenant_id) if provider_tenant_id else None,
                identity_data=identity_data,
                provider_id=current_provider.id,
            )
            if existing_user and existing_user.id != fresh_user.id:
                raise HTTPException(
                    status_code=409,
                    detail="This identity is already linked to another account",
                )

            # Link identity to current user
            await sso_service.link_identity(
                session,
                str(fresh_user.id),
                provider,
                lookup_provider_user_id,
                identity_data,
                tenant_id=str(provider_tenant_id) if provider_tenant_id else None,
                provider_id=current_provider.id,
            )
            identity.auth_version = int(
                getattr(identity, "auth_version", 0) or 0
            ) + 1

    except (ExternalIdentityAlreadyLinkedError, ExternalIdentityAmbiguousError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This identity cannot be linked because its ownership is ambiguous",
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Identity bind failed provider={} error_type={}",
            provider,
            type(e).__name__,
        )
        raise HTTPException(status_code=500, detail="Failed to bind identity")

    refreshed_token = create_access_token(
        str(fresh_user.id),
        fresh_user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=access_context_mfa_verified(current_user),
    )
    response.headers["X-Astra-Access-Token"] = refreshed_token
    set_browser_session_cookie(response, refreshed_token, request)
    return await serialize_user_with_access(fresh_user)


@router.post("/{provider}/unbind", response_model=UserOut)
async def unbind_identity(
    provider: str,
    data: IdentityUnbindRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """Unlink an external identity from the current user."""
    from app.services.auth_registry import auth_provider_registry
    from app.services.sso_service import sso_service
    from app.models.identity import IdentityProvider
    from app.models.org import OrgMember
    from app.models.tenant import Tenant

    reauthenticated_identity_id, verified_password_hash = (
        await _reauthenticate_sensitive_identity_action(
            request,
            current_user,
            data.current_password,
        )
    )
    tenant_scope = str(current_user.tenant_id) if current_user.tenant_id else None
    auth_provider = await auth_provider_registry.get_provider(
        provider,
        tenant_scope,
        require_sso_login=True,
        allow_global_fallback=provider in {"google", "github"},
    )
    provider_record = getattr(auth_provider, "provider", None) if auth_provider else None
    if not provider_record or provider_record.tenant_id not in {None, current_user.tenant_id}:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not supported")

    async with transaction() as session:
        identity = await identity_dao.get_for_update(current_user.identity_id)
        if (
            not identity
            or identity.id != reauthenticated_identity_id
            or not identity.is_active
            or not identity.password_login_enabled
            or not identity.password_hash
            or not hmac.compare_digest(
                identity.password_hash,
                verified_password_hash,
            )
        ):
            raise HTTPException(status_code=403, detail="Account is disabled")
        has_local_login = bool(
            identity.password_login_enabled
            and identity.password_hash
            and identity.email
            and identity.email_verified
        )
        if not has_local_login:
            alternative_result = await session.execute(
                select(OrgMember.id)
                .join(User, User.id == OrgMember.user_id)
                .join(IdentityProvider, IdentityProvider.id == OrgMember.provider_id)
                .outerjoin(Tenant, Tenant.id == IdentityProvider.tenant_id)
                .where(
                    User.identity_id == current_user.identity_id,
                    User.is_active.is_(True),
                    OrgMember.status == "active",
                    IdentityProvider.id != provider_record.id,
                    IdentityProvider.is_active.is_(True),
                    IdentityProvider.sso_login_enabled.is_(True),
                    or_(
                        IdentityProvider.tenant_id.is_(None),
                        and_(Tenant.is_active.is_(True), Tenant.sso_enabled.is_(True)),
                    ),
                )
                .limit(1)
            )
            if alternative_result.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Set and verify a recovery email and local password, or bind "
                        "another active login provider, before unlinking this identity."
                    ),
                )
        success = await sso_service.unlink_identity(
            session,
            str(current_user.id),
            provider,
            tenant_id=str(provider_record.tenant_id) if provider_record.tenant_id else None,
            provider_id=provider_record.id,
        )
        if not success:
            raise HTTPException(status_code=404, detail=f"No linked identity found for provider '{provider}'")

        identity.auth_version = int(getattr(identity, "auth_version", 0) or 0) + 1
        user = await user_dao.get_with_identity(current_user.id)
        if not user:
            raise HTTPException(status_code=403, detail="Account is unavailable")

    refreshed_token = create_access_token(
        str(user.id),
        user.role,
        auth_version=identity_auth_version(identity),
        mfa_verified=access_context_mfa_verified(current_user),
    )
    response.headers["X-Astra-Access-Token"] = refreshed_token
    set_browser_session_cookie(response, refreshed_token, request)
    return await serialize_user_with_access(user)


# ─── Email Verification Endpoints ──────────────────────────────────────


@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest):
    """Verify email address using a token from the verification email.

    On success, returns user info and access token to allow immediate login.
    """
    from app.services.email_verification_service import email_verification_service

    # Consume verification token outside transaction (Redis operation)
    token_data = await email_verification_service.consume_email_verification_token(data.token)
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    identity_id = token_data.get("identity_id")
    if not identity_id:
        raise HTTPException(status_code=400, detail="Token does not contain identity information")

    async with transaction() as session:
        # 1. Update Identity
        identity = await identity_dao.get_for_update(identity_id)
        if not identity or not identity.is_active:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        token_email = canonicalize_email(token_data.get("email"))
        current_email = canonicalize_email(identity.email)
        if not token_email or token_email != current_email:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        identity.email_verified = True

        # 2. Activate only memberships created in the explicit
        # email-verification-pending state.  Administrative disables remain
        # authoritative even when the Identity proves email ownership.
        user = await _activate_email_verification_pending_memberships(identity.id)

        # If no membership was activated by this verification, select only an
        # already-active membership.  A disabled representative must never be
        # used as the subject of a newly issued JWT.
        if user is None:
            user = await user_dao.get_active_representative_user_for_identity(identity.id)

        await session.flush()
        # Refresh inside transaction to ensure we have the committed model state
        await session.refresh(identity)

    if user is None:
        raise HTTPException(status_code=403, detail="No active organization membership is available")

    # 3. Generate a token only for the active membership selected above.
    token = create_access_token(
        str(user.id), user.role, auth_version=identity_auth_version(identity)
    )

    return TokenResponse(
        access_token=token,
        user=await serialize_user_with_access(user),
        identity=IdentityOut.model_validate(identity),
        needs_company_setup=user.tenant_id is None,
    )


@router.post("/resend-verification")
async def resend_verification(
    data: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """Resend email verification link."""
    from app.services.system_email_service import resolve_email_config_async

    # Always return success to prevent email enumeration
    generic_response = {
        "ok": True,
        "message": "If an account with that email exists, a verification email has been sent.",
    }
    await enforce_auth_rate_limit(
        request,
        identity=str(data.email),
        policy=email_action_rate_limit_policy(),
    )

    # Check if email is configured (DB-only, no env fallback) outside transaction (read-only)
    email_config = await resolve_email_config_async()
    if not email_config:
        return generic_response

    # Find Identity by email (read-only)
    identity = await identity_dao.get_by_email(data.email)

    # Don't reveal if user exists or already verified
    if not identity or not identity.is_active or identity.email_verified:
        return generic_response

    # Pick a representative user context (e.g. latest one)
    user = await user_dao.get_representative_user_for_identity(identity.id)

    if user:
        # Queue email task outside transaction
        await _send_verification_email_task(user, background_tasks, request=request)

    return generic_response
