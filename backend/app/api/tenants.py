"""Tenant (Company) management API.

Public endpoints for self-service company creation and joining.
Admin endpoints for platform-level company management.
"""

import re
import secrets
import uuid
import io
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete as sql_delete
from sqlalchemy import func as sqla_func
from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.secret_detection import looks_like_secret
from app.core.security import (
    access_context_mfa_verified,
    create_access_token,
    get_authenticated_user,
    get_company_analytics_viewer,
    get_company_governor,
    get_current_user,
    get_platform_operator,
    identity_auth_version,
    verify_password_async,
)
from app.database import get_db
from app.models.agent import Agent, AgentPermission
from app.models.audit import AuditLog
from app.models.identity_governance import TenantOwnershipTransfer
from app.models.tenant import Tenant
from app.models.tenant_deletion import TenantDeletionHold, TenantDeletionJob
from app.models.user import User
from app.services.storage import ensure_local_path, get_storage_backend, normalize_storage_key
from app.services.subscription_lifecycle import ensure_free_subscription_for_tenant
from app.services.platform_service import platform_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


# ─── Schemas ────────────────────────────────────────────

class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=50)
    country_region: str = Field(default="001", min_length=2, max_length=10)
    target_tenant_id: uuid.UUID | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    im_provider: str
    timezone: str = "UTC"
    country_region: str = "001"
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    a2a_async_enabled: bool = True
    default_model_id: uuid.UUID | None = None
    company_size: str = "unspecified"
    allow_member_private_agents: bool = False
    default_approval_policy: str = "high_risk"
    initialization_completed_at: datetime | None = None
    logo_url: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TenantUpdate(BaseModel):
    name: str | None = None
    im_provider: str | None = None
    timezone: str | None = None
    country_region: str | None = None
    is_active: bool | None = None
    sso_enabled: bool | None = None
    sso_domain: str | None = None
    a2a_async_enabled: bool | None = None
    company_size: str | None = None
    allow_member_private_agents: bool | None = None
    default_approval_policy: str | None = None

    @field_validator("sso_domain")
    @classmethod
    def validate_sso_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return platform_service.normalize_tenant_sso_domain(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("company_size")
    @classmethod
    def validate_company_size(cls, value: str | None) -> str | None:
        allowed = {"unspecified", "1-10", "11-50", "51-200", "201-1000", "1000+"}
        if value is not None and value not in allowed:
            raise ValueError("company_size is invalid")
        return value

    @field_validator("default_approval_policy")
    @classmethod
    def validate_approval_policy(cls, value: str | None) -> str | None:
        allowed = {"high_risk", "external_actions", "all_writes"}
        if value is not None and value not in allowed:
            raise ValueError("default_approval_policy is invalid")
        return value


class OwnershipTransferRequest(BaseModel):
    new_owner_user_id: uuid.UUID
    current_password: str = Field(min_length=1, max_length=128)


class TenantLeaveRequest(BaseModel):
    confirmation: str = Field(default="LEAVE", min_length=5, max_length=20)
    acknowledge_responsibilities: bool = False


class TenantDeletionRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    current_password: str = Field(min_length=1, max_length=128)


class TenantRestoreRequest(BaseModel):
    current_password: str | None = Field(default=None, min_length=1, max_length=128)


def _validated_tenant_name(name: str) -> str:
    normalized = name.strip()
    if looks_like_secret(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company name looks like a credential. Rotate the credential if it was pasted here, then enter a public company name.",
        )
    return normalized


def _tenant_logo_key(tenant_id: uuid.UUID) -> str:
    return normalize_storage_key(f"_tenant_logos/{tenant_id}.png")


def _tenant_logo_url(tenant_id: uuid.UUID) -> str:
    return f"/api/tenants/{tenant_id}/logo?v={int(datetime.utcnow().timestamp())}"


async def _get_updateable_tenant(
    tenant_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Tenant:
    if current_user.role not in {"org_owner", "org_admin"}:
        raise HTTPException(status_code=403, detail="Company governance access required")
    if not current_user.tenant_id or current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Can only update your own company")

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


# ─── Helpers ────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Generate a URL-friendly slug from a company name.

    Uses a layered transliteration strategy so non-Latin company names produce
    meaningful, readable slugs instead of collapsing to the generic 'company'
    placeholder:

      1. pypinyin   — CJK/Chinese characters → pinyin (e.g. '公司' → 'gongsi')
      2. anyascii   — remaining non-ASCII scripts → closest ASCII approximation
                      (Korean '안녕' → 'annyeong', Japanese 'ひらがな' → 'hiragana',
                       Arabic 'مرحبا' → 'mrhb', Cyrillic 'Привет' → 'Privet', …)
      3. NFKD norm  — accented Latin chars stripped of diacritics (é → e)

    A short random hex suffix is always appended to guarantee global uniqueness
    even when two tenants choose the same company name.
    """
    import unicodedata
    from pypinyin import lazy_pinyin
    from anyascii import anyascii

    # Step 1: Convert CJK characters to pinyin; non-CJK chars pass through unchanged.
    # lazy_pinyin with errors='default' keeps non-CJK chars as-is so they are
    # handled by the subsequent anyascii pass rather than being silently dropped.
    parts = lazy_pinyin(name, errors="default")
    text = "".join(parts)

    # Step 2: Convert remaining non-ASCII characters using anyascii.
    # anyascii is a no-op on ASCII input, so it is safe to apply to the whole
    # string after pypinyin has already processed the CJK portion.
    text = anyascii(text)

    # Step 3: Normalize any remaining accented Latin chars (é → e, ü → u, etc.)
    # and drop anything that still cannot be represented in ASCII.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Step 4: Lowercase, collapse non-alphanumeric runs to hyphens, trim to 40 chars.
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = slug.strip("-")[:40]

    if not slug:
        # Extremely unlikely after anyascii, but keep as a safety net
        # for inputs that are entirely punctuation or whitespace.
        slug = "company"

    # Add a short random hex suffix to ensure global uniqueness.
    slug = f"{slug}-{secrets.token_hex(3)}"
    return slug


class SelfCreateResponse(BaseModel):
    """Response for self-create company, includes token for context switching."""
    tenant: TenantOut
    access_token: str | None = None  # Fresh membership-scoped token for the created company.


def _idempotency_key_hash(raw_key: str | None) -> str | None:
    if raw_key is None:
        return None
    normalized = raw_key.strip()
    if len(normalized) < 8 or len(normalized) > 128:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "idempotency_key_invalid",
                "message": "Idempotency-Key must contain between 8 and 128 non-whitespace characters",
            },
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _lock_current_membership(
    db: AsyncSession,
    current_user: User,
) -> tuple[User, object]:
    """Refresh the membership and serialize tenant transitions per Identity."""

    user_result = await db.execute(
        select(User)
        .where(User.id == current_user.id)
        .options(selectinload(User.identity))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_user = user_result.scalar_one_or_none()
    if (
        not locked_user
        or not locked_user.is_active
        or locked_user.identity_id != current_user.identity_id
    ):
        raise HTTPException(status_code=403, detail="Account is unavailable")

    from app.dao import identity_dao

    locked_identity = await identity_dao.get_for_update(locked_user.identity_id)
    if not locked_identity or not locked_identity.is_active:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    locked_user.identity = locked_identity
    return locked_user, locked_identity


@router.post("/self-create", response_model=SelfCreateResponse, status_code=status.HTTP_201_CREATED)
async def self_create_company(
    data: TenantCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new company. The creator becomes its unique ``org_owner``.

    Supports both:
    - Registration flow (user has no tenant yet): assigns tenant directly
    - Switch-org flow (user already has a tenant): creates a new User record for the new tenant
    """
    mfa_verified = access_context_mfa_verified(current_user)

    # Block self-creation if locked to a specific tenant (Dedicated Link flow)
    if data.target_tenant_id is not None:
        raise HTTPException(status_code=403, detail="Company creation is not allowed via this link. Please join your assigned organization.")

    # Dependency state can be stale by the time two requests reach this route.
    # Lock and refresh before creating a Tenant/subscription or choosing
    # between moving the tenantless anchor and adding a new membership.
    current_user, locked_identity = await _lock_current_membership(db, current_user)

    # ``company.create`` is an account capability, not a tenant role. The
    # platform setting controls whether newly registered accounts receive the
    # grant; it is not evaluated as a request-time wildcard.
    from app.services.identity_governance import COMPANY_CREATE_CAPABILITY, identity_has_capability

    may_create_company = await identity_has_capability(
        db,
        identity_id=locked_identity.id,
        capability=COMPANY_CREATE_CAPABILITY,
    )
    if not may_create_company:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "company_create_capability_required",
                "message": "This account is not allowed to create a company",
            },
        )

    company_name = _validated_tenant_name(data.name)
    idempotency_key_hash = _idempotency_key_hash(idempotency_key)
    if idempotency_key_hash:
        existing_result = await db.execute(
            select(Tenant).where(
                Tenant.created_by_identity_id == locked_identity.id,
                Tenant.creation_idempotency_key_hash == idempotency_key_hash,
            )
        )
        existing_tenant = existing_result.scalar_one_or_none()
        if existing_tenant:
            if (
                existing_tenant.name != company_name
                or existing_tenant.timezone != data.timezone
                or existing_tenant.country_region != data.country_region.upper()
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_key_reused",
                        "message": "This Idempotency-Key was already used with different company data",
                    },
                )
            membership_result = await db.execute(
                select(User).where(
                    User.identity_id == locked_identity.id,
                    User.tenant_id == existing_tenant.id,
                    User.is_active.is_(True),
                )
            )
            owner_membership = membership_result.scalar_one_or_none()
            if (
                owner_membership is None
                or existing_tenant.owner_user_id != owner_membership.id
                or owner_membership.role != "org_owner"
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "company_creation_incomplete",
                        "message": "The original company creation requires operator recovery",
                    },
                )
            from app.core.security import create_access_token, identity_auth_version

            return SelfCreateResponse(
                tenant=TenantOut.model_validate(existing_tenant),
                access_token=create_access_token(
                    str(owner_membership.id),
                    owner_membership.role,
                    auth_version=identity_auth_version(locked_identity),
                    mfa_verified=mfa_verified,
                ),
            )

    slug = _slugify(company_name)
    tenant = Tenant(
        name=company_name,
        slug=slug,
        im_provider="web_only",
        timezone=data.timezone,
        country_region=data.country_region.upper(),
        created_by_identity_id=locked_identity.id,
        creation_idempotency_key_hash=idempotency_key_hash,
        owner_resolution_required=True,
    )
    db.add(tenant)
    await db.flush()
    await ensure_free_subscription_for_tenant(db, tenant.id, granted_by=current_user.id)

    from app.services.registration_service import registration_service
    from app.core.security import create_access_token, identity_auth_version

    if current_user.tenant_id is not None:
        # Multi-tenant: user already belongs to a company.
        # Create a NEW User record for the new tenant instead of overwriting.
        from app.models.participant import Participant

        new_user = User(
            identity_id=current_user.identity_id,
            tenant_id=tenant.id,
            display_name=current_user.display_name,
            role="org_owner",
            registration_source="web",
            is_active=current_user.is_active,
            quota_message_limit=tenant.default_message_limit,
            quota_message_period=tenant.default_message_period,
            quota_max_agents=tenant.default_max_agents,
            quota_agent_ttl_hours=tenant.default_agent_ttl_hours,
        )
        db.add(new_user)
        await db.flush()

        # Create Participant for the new user record
        db.add(Participant(
            type="user",
            ref_id=new_user.id,
            display_name=new_user.display_name,
            avatar_url=new_user.avatar_url,
        ))
        await db.flush()
        await registration_service.bind_org_member(new_user)
        tenant.owner_user_id = new_user.id
        tenant.owner_resolution_required = False

        owner_membership = new_user
    else:
        # Registration flow: user has no tenant yet, assign directly
        current_user.tenant_id = tenant.id
        current_user.role = "org_owner"
        tenant.owner_user_id = current_user.id
        tenant.owner_resolution_required = False
        # Inherit quota defaults from new tenant
        current_user.quota_message_limit = tenant.default_message_limit
        current_user.quota_message_period = tenant.default_message_period
        current_user.quota_max_agents = tenant.default_max_agents
        current_user.quota_agent_ttl_hours = tenant.default_agent_ttl_hours
        await db.flush()
        await registration_service.bind_org_member(current_user)
        owner_membership = current_user

    # Always return a freshly issued membership-scoped token. This also makes a
    # tenantless registration anchor switch contexts without relying on stale
    # client-side tenant state.
    access_token = create_access_token(
        str(owner_membership.id),
        owner_membership.role,
        auth_version=identity_auth_version(locked_identity),
        mfa_verified=mfa_verified,
    )

    db.add(
        AuditLog(
            user_id=owner_membership.id,
            action="tenant_created",
            details={
                "tenant_id": str(tenant.id),
                "owner_user_id": str(tenant.owner_user_id),
                "source": "self_service",
            },
        )
    )
    await db.commit()

    return SelfCreateResponse(
        tenant=TenantOut.model_validate(tenant),
        access_token=access_token,
    )


# ─── Self-Service: Join Company via Invite Code ─────────

class JoinRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=128)
    target_tenant_id: uuid.UUID | None = None


class JoinResponse(BaseModel):
    tenant: TenantOut
    role: str
    access_token: str | None = None  # Non-null when a new User record was created (multi-tenant switch)


def _governance_credential_error(exc) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


async def _accept_organization_credential(
    credential,
    *,
    current_user: User,
    locked_identity,
    mfa_verified: bool,
    db: AsyncSession,
) -> JoinResponse:
    """Atomically materialize one membership and consume its credential."""

    from app.core.security import create_access_token, identity_auth_version
    from app.services.identity_governance import consume_organization_credential
    from app.services.registration_service import registration_service

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == credential.tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant or not tenant.is_active:
        raise HTTPException(
            status_code=409,
            detail={"code": "organization_unavailable", "message": "Company is disabled or unavailable"},
        )

    membership_result = await db.execute(
        select(User).where(
            User.identity_id == current_user.identity_id,
            User.tenant_id == tenant.id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership:
        if not membership.is_active:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "organization_membership_disabled",
                    "message": "This organization membership is disabled and requires an administrator to reactivate it",
                },
            )

        # A reusable join link is idempotent for an existing member and must
        # not burn another use. Email-bound invitations may intentionally
        # promote an existing membership and are consumed exactly once.
        if credential.kind == "organization_invitation":
            if credential.role == "org_owner":
                if tenant.owner_user_id not in (None, membership.id):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "tenant_owner_already_assigned", "message": "Company already has an owner"},
                    )
                membership.role = "org_owner"
                tenant.owner_user_id = membership.id
                tenant.owner_resolution_required = False
            elif credential.role == "org_admin" and membership.role != "org_owner":
                membership.role = "org_admin"
            consume_organization_credential(credential, accepted_by_user_id=membership.id)
            db.add(
                AuditLog(
                    user_id=membership.id,
                    action="organization_invitation_accepted",
                    details={
                        "tenant_id": str(tenant.id),
                        "credential_kind": credential.kind,
                        "assigned_role": membership.role,
                        "existing_membership": True,
                    },
                )
            )
            await db.commit()

        return JoinResponse(
            tenant=TenantOut.model_validate(tenant),
            role=membership.role,
            access_token=create_access_token(
                str(membership.id),
                membership.role,
                auth_version=identity_auth_version(locked_identity),
                mfa_verified=mfa_verified,
            ),
        )

    assigned_role = credential.role
    if assigned_role == "org_owner" and tenant.owner_user_id is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "tenant_owner_already_assigned", "message": "Company already has an owner"},
        )

    if current_user.tenant_id is not None:
        from app.models.participant import Participant

        membership = User(
            identity_id=current_user.identity_id,
            tenant_id=tenant.id,
            display_name=current_user.display_name,
            role=assigned_role,
            registration_source="web",
            is_active=True,
            quota_message_limit=tenant.default_message_limit,
            quota_message_period=tenant.default_message_period,
            quota_max_agents=tenant.default_max_agents,
            quota_agent_ttl_hours=tenant.default_agent_ttl_hours,
        )
        db.add(membership)
        await db.flush()
        db.add(
            Participant(
                type="user",
                ref_id=membership.id,
                display_name=membership.display_name,
                avatar_url=membership.avatar_url,
            )
        )
        await db.flush()
    else:
        # Reuse the tenantless account anchor as the first concrete membership.
        membership = current_user
        membership.tenant_id = tenant.id
        membership.role = assigned_role
        membership.quota_message_limit = tenant.default_message_limit
        membership.quota_message_period = tenant.default_message_period
        membership.quota_max_agents = tenant.default_max_agents
        membership.quota_agent_ttl_hours = tenant.default_agent_ttl_hours
        await db.flush()

    await registration_service.bind_org_member(membership)
    if assigned_role == "org_owner":
        tenant.owner_user_id = membership.id
        tenant.owner_resolution_required = False

    consume_organization_credential(credential, accepted_by_user_id=membership.id)
    db.add(
        AuditLog(
            user_id=membership.id,
            action="organization_invitation_accepted",
            details={
                "tenant_id": str(tenant.id),
                "credential_kind": credential.kind,
                "assigned_role": assigned_role,
                "existing_membership": False,
            },
        )
    )
    await db.commit()
    return JoinResponse(
        tenant=TenantOut.model_validate(tenant),
        role=membership.role,
        access_token=create_access_token(
            str(membership.id),
            membership.role,
            auth_version=identity_auth_version(locked_identity),
            mfa_verified=mfa_verified,
        ),
    )


@router.post("/join", response_model=JoinResponse)
async def join_company(
    data: JoinRequest,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Join an existing company using an invitation or explicit join link.

    Supports both:
    - Registration flow (user has no tenant yet): assigns tenant directly
    - Switch-org flow (user already has a tenant): creates a new User record"""
    mfa_verified = access_context_mfa_verified(current_user)
    current_user, locked_identity = await _lock_current_membership(db, current_user)
    from app.services.identity_governance import (
        GovernanceCredentialError,
        resolve_organization_credential,
    )

    try:
        credential = await resolve_organization_credential(
            db,
            data.invitation_code,
            identity_email=locked_identity.email,
            target_tenant_id=data.target_tenant_id,
            for_update=True,
        )
    except GovernanceCredentialError as exc:
        raise _governance_credential_error(exc) from exc
    if credential is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "organization_credential_invalid", "message": "Invalid organization invitation or join link"},
        )

    return await _accept_organization_credential(
        credential,
        current_user=current_user,
        locked_identity=locked_identity,
        mfa_verified=mfa_verified,
        db=db,
    )


@router.post("/invitations/{invitation_id}/accept", response_model=JoinResponse)
async def accept_organization_invitation(
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a listed email-bound invitation without re-exposing its raw token."""

    mfa_verified = access_context_mfa_verified(current_user)
    current_user, locked_identity = await _lock_current_membership(db, current_user)
    from app.services.identity_governance import (
        GovernanceCredentialError,
        resolve_organization_invitation_by_id,
    )

    try:
        credential = await resolve_organization_invitation_by_id(
            db,
            invitation_id,
            identity_email=locked_identity.email,
            for_update=True,
        )
    except GovernanceCredentialError as exc:
        raise _governance_credential_error(exc) from exc
    if credential is None:
        raise HTTPException(status_code=404, detail="Organization invitation not found")
    return await _accept_organization_credential(
        credential,
        current_user=current_user,
        locked_identity=locked_identity,
        mfa_verified=mfa_verified,
        db=db,
    )


@router.post("/invitations/{invitation_id}/decline")
async def decline_organization_invitation(
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Decline only an invitation bound to the authenticated Identity email."""

    current_user, locked_identity = await _lock_current_membership(db, current_user)
    from app.services.identity_governance import (
        GovernanceCredentialError,
        resolve_organization_invitation_by_id,
    )

    try:
        credential = await resolve_organization_invitation_by_id(
            db,
            invitation_id,
            identity_email=locked_identity.email,
            for_update=True,
        )
    except GovernanceCredentialError as exc:
        raise _governance_credential_error(exc) from exc
    if credential is None:
        raise HTTPException(status_code=404, detail="Organization invitation not found")
    invitation = credential.record
    invitation.status = "declined"
    invitation.declined_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_invitation_declined",
            details={"tenant_id": str(credential.tenant_id), "invitation_id": str(invitation.id)},
        )
    )
    await db.commit()
    return {"status": "declined"}


# ─── Registration Config ───────────────────────────────

@router.get("/registration-config")
async def get_registration_config(db: AsyncSession = Depends(get_db)):
    """Public — returns whether self-creation of companies is allowed."""
    from app.models.system_settings import SystemSetting
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "allow_self_create_company")
    )
    s = result.scalar_one_or_none()
    allowed = s.value.get("enabled", True) if s else True
    return {"allow_self_create_company": allowed}


# ─── Public: Resolve Tenant by Domain ───────────────────

@router.get("/resolve-by-domain")
async def resolve_tenant_by_domain(
    domain: str,
    db: AsyncSession = Depends(get_db),
):
    """Resolve a tenant by its sso_domain or subdomain slug.

    sso_domain is stored as a full URL (e.g. "https://acme.astra.ai" or "http://1.2.3.4:3009").
    The incoming `domain` parameter is the host (without protocol).

    Only an explicitly configured tenant.sso_domain may match.  Synthetic
    ``{slug}.astra.ai`` hostnames are never inferred from a database slug.
    """
    raw_domain = str(domain or "").strip()
    parsed_domain = urlsplit(f"//{raw_domain}")
    if (
        not raw_domain
        or parsed_domain.username
        or parsed_domain.password
        or parsed_domain.path
        or parsed_domain.query
        or parsed_domain.fragment
        or not parsed_domain.hostname
    ):
        raise HTTPException(status_code=404, detail="Tenant not found")
    try:
        parsed_port = parsed_domain.port
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Tenant not found") from exc
    normalized_hostname = parsed_domain.hostname.lower().rstrip(".")
    normalized_domain = (
        f"[{normalized_hostname}]" if ":" in normalized_hostname else normalized_hostname
    )
    if parsed_port is not None:
        normalized_domain = f"{normalized_domain}:{parsed_port}"
    public_base_url = str(get_settings().PUBLIC_BASE_URL or "").strip()
    parsed_public_url = urlsplit(
        public_base_url if "://" in public_base_url else f"//{public_base_url}"
    )
    public_host = parsed_public_url.netloc.lower().rstrip(".")
    if public_host and normalized_domain == public_host:
        # The platform root is intentionally tenant-neutral.  A successful
        # empty response avoids treating the expected fallback as a browser
        # network error while retaining 404 for unknown tenant-specific hosts.
        return None

    if not await platform_service.is_sso_custom_domain_redirect_enabled(db):
        raise HTTPException(status_code=404, detail="Tenant not found or not active or SSO not enabled")

    exact_origins = (
        f"https://{normalized_domain}",
        f"http://{normalized_domain}",
    )
    tenant_result = await db.execute(
        select(Tenant).where(
            sqla_func.lower(Tenant.sso_domain).in_(exact_origins),
            Tenant.is_active.is_(True),
            Tenant.sso_enabled.is_(True),
        )
    )
    matching_tenants = tenant_result.scalars().all()
    if len(matching_tenants) != 1:
        # Duplicate origins are configuration corruption. Never select an
        # arbitrary tenant when browser identity is at stake.
        raise HTTPException(status_code=404, detail="Tenant not found or not active or SSO not enabled")
    tenant = matching_tenants[0]

    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "sso_enabled": tenant.sso_enabled,
        "sso_domain": tenant.sso_domain,
        "is_active": tenant.is_active,
    }

# ─── Authenticated: List / Get ──────────────────────────

@router.get("/", response_model=list[TenantOut])
async def list_tenants(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants (platform_admin only)."""
    result = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    return [TenantOut.model_validate(t) for t in result.scalars().all()]


@router.get("/me", response_model=TenantOut)
async def get_my_tenant(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's own tenant. Any authenticated member can read
    this — the wizard and the chat model switcher need default_model_id, which
    shouldn't require admin privileges.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")
    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.get("/me/token-usage")
async def get_my_tenant_token_usage(
    current_user: User = Depends(get_company_analytics_viewer),
    db: AsyncSession = Depends(get_db),
):
    """Return current-company resource usage to a membership-scoped governor.

    Deleted Agents are historical records and therefore do not participate in
    the current-company aggregate.  System Agents remain included because they
    consume the same tenant resource pool.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=404, detail="User is not in a tenant")

    row = (await db.execute(
        select(
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_today), 0).label("tokens_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_month), 0).label("tokens_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label("tokens_total"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_today), 0).label("cache_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_month), 0).label("cache_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_total), 0).label("cache_total"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_today), 0).label("cache_creation_today"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_month), 0).label("cache_creation_month"),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_creation_tokens_total), 0).label("cache_creation_total"),
        ).where(
            Agent.tenant_id == current_user.tenant_id,
            Agent.deleted_at.is_(None),
        )
    )).one()

    def bucket(total: int, cache_read: int, cache_creation: int) -> dict:
        total = int(total or 0)
        cache_read = int(cache_read or 0)
        return {
            "total_tokens": total,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": int(cache_creation or 0),
            "cache_hit_rate": round(cache_read / total, 4) if total > 0 else 0,
        }

    return {
        "today": bucket(row.tokens_today, row.cache_today, row.cache_creation_today),
        "month": bucket(row.tokens_month, row.cache_month, row.cache_creation_month),
        "total": bucket(row.tokens_total, row.cache_total, row.cache_creation_total),
    }


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Get the current owner's or administrator's company details."""
    if current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    data: TenantUpdate,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Update settings for the caller's own company."""
    if not current_user.tenant_id or current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Can only update your own company")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = data.model_dump(exclude_unset=True)
    if update_data.get("name") is not None:
        update_data["name"] = _validated_tenant_name(update_data["name"])
    
    for field, value in update_data.items():
        setattr(tenant, field, value)
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.get("/{tenant_id}/logo")
async def get_tenant_logo(tenant_id: uuid.UUID):
    """Serve a tenant logo. Logos are public UI assets, addressed by UUID."""
    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if not await storage.exists(key):
        raise HTTPException(status_code=404, detail="Logo not found")
    path = await ensure_local_path(key)
    return FileResponse(path, media_type="image/png")


@router.post("/{tenant_id}/logo", response_model=TenantOut)
async def upload_tenant_logo(
    tenant_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Upload a cropped square company logo.

    The frontend crops to a 1:1 PNG before upload. The backend keeps a hard
    1 MB limit and stores the image outside git-managed source files.
    """
    tenant = await _get_updateable_tenant(tenant_id, current_user, db)
    if file.content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail="Logo must be a PNG, JPEG, or WebP image")

    data = await file.read()
    if len(data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc
    if image.width != image.height:
        raise HTTPException(status_code=400, detail="Logo image must be a 1:1 square")

    output = io.BytesIO()
    image.convert("RGBA").save(output, format="PNG", optimize=True)
    png_data = output.getvalue()
    if len(png_data) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo image must be 1 MB or smaller after processing")

    storage = get_storage_backend()
    await storage.write_bytes(_tenant_logo_key(tenant_id), png_data, content_type="image/png")

    config = dict(tenant.im_config or {})
    config["logo_url"] = _tenant_logo_url(tenant_id)
    tenant.im_config = config
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.delete("/{tenant_id}/logo", response_model=TenantOut)
async def delete_tenant_logo(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Remove a custom company logo and fall back to the generated default."""
    tenant = await _get_updateable_tenant(tenant_id, current_user, db)

    storage = get_storage_backend()
    key = _tenant_logo_key(tenant_id)
    if await storage.exists(key):
        await storage.delete(key)

    config = dict(tenant.im_config or {})
    config.pop("logo_url", None)
    tenant.im_config = config
    await db.flush()
    return TenantOut.model_validate(tenant)


@router.put("/{tenant_id}/assign-user/{user_id}")
async def assign_user_to_tenant(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Retired unsafe membership reassignment endpoint."""
    del tenant_id, user_id, role, current_user, db
    raise HTTPException(
        status_code=410,
        detail={
            "code": "direct_membership_assignment_retired",
            "message": "Use an email-bound organization invitation and explicit acceptance",
        },
    )


async def _require_password_proof(current_user: User, current_password: str) -> None:
    identity = getattr(current_user, "identity", None)
    if not identity or not identity.password_login_enabled or not identity.password_hash:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "password_reauthentication_unavailable",
                "message": "Password reauthentication is required for this action",
            },
        )
    if not await verify_password_async(current_password, identity.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "password_reauthentication_failed", "message": "Current password is incorrect"},
        )


def _ownership_transfer_payload(transfer: TenantOwnershipTransfer) -> dict:
    return {
        "id": str(transfer.id),
        "tenant_id": str(transfer.tenant_id),
        "current_owner_user_id": str(transfer.current_owner_user_id),
        "proposed_owner_user_id": str(transfer.proposed_owner_user_id),
        "status": transfer.status,
        "expires_at": transfer.expires_at,
        "accepted_at": transfer.accepted_at,
        "cancelled_at": transfer.cancelled_at,
        "created_at": transfer.created_at,
    }


def _owner_candidate_has_verified_login(user: User) -> bool:
    """Require verified email ownership or a first-party SSO login provenance."""

    identity = getattr(user, "identity", None)
    if not identity or not identity.is_active:
        return False
    if identity.email_verified:
        return True
    return (user.registration_source or "").strip().lower() in {
        "feishu",
        "dingtalk",
        "wecom",
        "google_workspace",
        "microsoft_teams",
        "google",
        "github",
    }


@router.post(
    "/{tenant_id}/ownership-transfer",
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/{tenant_id}/ownership-transfers",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_tenant_ownership_transfer(
    tenant_id: uuid.UUID,
    body: OwnershipTransferRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a target-confirmed ownership transfer request.

    The current owner proves the high-risk action with their password. The
    proposed owner remains unchanged until they explicitly accept while signed
    in to their own verified account.
    """
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    owner_result = await db.execute(
        select(User)
        .where(User.id == current_user.id)
        .options(selectinload(User.identity))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = owner_result.scalar_one_or_none()
    if (
        owner is None
        or owner.tenant_id != tenant.id
        or tenant.owner_user_id != owner.id
        or owner.role != "org_owner"
        or not owner.is_active
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "company_owner_required", "message": "Only the current company owner can transfer ownership"},
        )
    await _require_password_proof(owner, body.current_password)

    if body.new_owner_user_id == owner.id:
        return {"status": "unchanged", "owner_user_id": str(owner.id)}

    pending_result = await db.execute(
        select(TenantOwnershipTransfer)
        .where(
            TenantOwnershipTransfer.tenant_id == tenant.id,
            TenantOwnershipTransfer.status == "pending",
        )
        .with_for_update()
    )
    pending = pending_result.scalar_one_or_none()
    now = datetime.now(UTC)
    if pending and pending.expires_at <= now:
        pending.status = "expired"
        await db.flush()
        pending = None
    if pending:
        if pending.proposed_owner_user_id != body.new_owner_user_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ownership_transfer_already_pending",
                    "message": "Cancel the existing ownership transfer before selecting another owner",
                },
            )
        return _ownership_transfer_payload(pending)

    target_result = await db.execute(
        select(User)
        .where(
            User.id == body.new_owner_user_id,
            User.tenant_id == tenant.id,
            User.is_active.is_(True),
        )
        .options(selectinload(User.identity))
        .with_for_update()
    )
    target = target_result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=400, detail="New owner must be an active member of this company")
    if not _owner_candidate_has_verified_login(target):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "new_owner_login_not_verified",
                "message": "The proposed owner must verify their email or sign in through an approved SSO provider first",
            },
        )

    transfer = TenantOwnershipTransfer(
        tenant_id=tenant.id,
        current_owner_user_id=owner.id,
        proposed_owner_user_id=target.id,
        status="pending",
        expires_at=now + timedelta(hours=24),
    )
    db.add(transfer)
    await db.flush()
    db.add(
        AuditLog(
            user_id=owner.id,
            action="tenant_ownership_transfer_requested",
            details={
                "tenant_id": str(tenant.id),
                "transfer_id": str(transfer.id),
                "current_owner_user_id": str(owner.id),
                "proposed_owner_user_id": str(target.id),
                "expires_at": transfer.expires_at.isoformat(),
            },
        )
    )
    await db.commit()
    return _ownership_transfer_payload(transfer)


@router.get("/{tenant_id}/ownership-transfers/pending")
async def get_pending_tenant_ownership_transfer(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the pending transfer visible to its current or proposed owner."""

    if current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Can only inspect your own company")
    result = await db.execute(
        select(TenantOwnershipTransfer).where(
            TenantOwnershipTransfer.tenant_id == tenant_id,
            TenantOwnershipTransfer.status == "pending",
        )
    )
    transfer = result.scalar_one_or_none()
    if transfer is None:
        return {"item": None}
    if current_user.id not in {
        transfer.current_owner_user_id,
        transfer.proposed_owner_user_id,
    }:
        raise HTTPException(status_code=403, detail="Ownership transfer is not visible to this member")
    if transfer.expires_at <= datetime.now(UTC):
        transfer.status = "expired"
        await db.commit()
        return {"item": None}
    return {"item": _ownership_transfer_payload(transfer)}


@router.post("/{tenant_id}/ownership-transfers/{transfer_id}/accept")
async def accept_tenant_ownership_transfer(
    tenant_id: uuid.UUID,
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply the role swap only after the proposed owner confirms it."""

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    transfer_result = await db.execute(
        select(TenantOwnershipTransfer)
        .where(
            TenantOwnershipTransfer.id == transfer_id,
            TenantOwnershipTransfer.tenant_id == tenant.id,
        )
        .with_for_update()
    )
    transfer = transfer_result.scalar_one_or_none()
    if not transfer:
        raise HTTPException(status_code=404, detail="Ownership transfer not found")
    if transfer.proposed_owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail={"code": "proposed_owner_required", "message": "Only the proposed owner can accept this transfer"},
        )
    if transfer.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "ownership_transfer_not_pending", "message": f"Ownership transfer is {transfer.status}"},
        )
    now = datetime.now(UTC)
    if transfer.expires_at <= now:
        transfer.status = "expired"
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"code": "ownership_transfer_expired", "message": "Ownership transfer has expired"},
        )
    if tenant.owner_user_id != transfer.current_owner_user_id:
        transfer.status = "cancelled"
        transfer.cancelled_at = now
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"code": "ownership_transfer_stale", "message": "Company ownership changed before this transfer was accepted"},
        )

    owner_result = await db.execute(
        select(User)
        .where(User.id == transfer.current_owner_user_id)
        .options(selectinload(User.identity))
        .with_for_update()
    )
    owner = owner_result.scalar_one_or_none()
    target_result = await db.execute(
        select(User)
        .where(User.id == transfer.proposed_owner_user_id)
        .options(selectinload(User.identity))
        .with_for_update()
    )
    target = target_result.scalar_one_or_none()
    if (
        owner is None
        or target is None
        or owner.tenant_id != tenant.id
        or target.tenant_id != tenant.id
        or owner.role != "org_owner"
        or not owner.is_active
        or not target.is_active
        or not _owner_candidate_has_verified_login(target)
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ownership_transfer_invalid_membership", "message": "Ownership transfer members are no longer eligible"},
        )

    # Flush the demotion before the promotion so the database-level unique
    # owner index remains valid throughout the transaction.
    owner.role = "org_admin"
    await db.flush()
    target.role = "org_owner"
    tenant.owner_user_id = target.id
    tenant.owner_resolution_required = False
    transfer.status = "accepted"
    transfer.accepted_at = now
    db.add(
        AuditLog(
            user_id=target.id,
            action="tenant_ownership_transfer_accepted",
            details={
                "tenant_id": str(tenant.id),
                "transfer_id": str(transfer.id),
                "previous_owner_user_id": str(owner.id),
                "new_owner_user_id": str(target.id),
            },
        )
    )
    await db.commit()
    return {"status": "transferred", "owner_user_id": str(target.id), "transfer": _ownership_transfer_payload(transfer)}


@router.delete("/{tenant_id}/ownership-transfers/{transfer_id}")
async def cancel_tenant_ownership_transfer(
    tenant_id: uuid.UUID,
    transfer_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Allow only the current owner to cancel a pending transfer."""

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if tenant.owner_user_id != current_user.id or current_user.role != "org_owner":
        raise HTTPException(
            status_code=403,
            detail={"code": "company_owner_required", "message": "Only the current company owner can cancel this transfer"},
        )
    transfer_result = await db.execute(
        select(TenantOwnershipTransfer)
        .where(
            TenantOwnershipTransfer.id == transfer_id,
            TenantOwnershipTransfer.tenant_id == tenant.id,
        )
        .with_for_update()
    )
    transfer = transfer_result.scalar_one_or_none()
    if not transfer:
        raise HTTPException(status_code=404, detail="Ownership transfer not found")
    if transfer.status == "cancelled":
        return _ownership_transfer_payload(transfer)
    if transfer.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "ownership_transfer_not_pending", "message": f"Ownership transfer is {transfer.status}"},
        )
    transfer.status = "cancelled"
    transfer.cancelled_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="tenant_ownership_transfer_cancelled",
            details={"tenant_id": str(tenant.id), "transfer_id": str(transfer.id)},
        )
    )
    await db.commit()
    return _ownership_transfer_payload(transfer)


async def _fallback_membership_context(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID | None,
    excluded_tenant_id: uuid.UUID,
    mfa_verified: bool,
) -> tuple[str | None, str | None]:
    """Return a deterministic active fallback membership and scoped token."""
    if identity_id is None:
        return None, None
    fallback_result = await db.execute(
        select(User)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(
            User.identity_id == identity_id,
            User.tenant_id != excluded_tenant_id,
            User.is_active.is_(True),
            Tenant.is_active.is_(True),
        )
        .options(selectinload(User.identity))
        .order_by(User.created_at.asc(), User.id.asc())
        .limit(1)
    )
    fallback = fallback_result.scalar_one_or_none()
    if fallback is None:
        return None, None
    return (
        str(fallback.tenant_id),
        create_access_token(
            str(fallback.id),
            fallback.role,
            auth_version=identity_auth_version(fallback),
            mfa_verified=mfa_verified,
        ),
    )


async def _tenant_leave_preflight(
    db: AsyncSession,
    *,
    membership: User,
    tenant: Tenant,
    lock_owned_agents: bool = False,
) -> dict:
    """Build the authoritative responsibility review before one membership leaves.

    Agent creator ownership is a hard boundary: leaving while an active Agent
    still points at the departing membership would create an orphaned owner and
    can make a private Agent unreachable. Other work remains tenant-owned and
    is surfaced as an acknowledgement warning rather than trapping the member.
    """
    from app.models.agent_credential import AgentCredential
    from app.models.audit import ApprovalRequest
    from app.models.deliverable import DeliverableRequest
    from app.models.identity_governance import TenantOwnershipTransfer
    from app.models.onboarding import UserTenantOnboarding
    from app.models.task import Task

    owned_agent_filters = (
        Agent.tenant_id == tenant.id,
        Agent.creator_id == membership.id,
        Agent.deleted_at.is_(None),
    )
    owned_agent_count = int(
        (
            await db.execute(
                select(sqla_func.count(Agent.id)).where(*owned_agent_filters)
            )
        ).scalar_one()
        or 0
    )
    owned_agent_query = (
        select(Agent)
        .where(*owned_agent_filters)
        .order_by(Agent.created_at.asc(), Agent.id.asc())
        .limit(50)
    )
    if lock_owned_agents:
        owned_agent_query = owned_agent_query.with_for_update()
    owned_agents = (await db.execute(owned_agent_query)).scalars().all()

    onboarding_result = await db.execute(
        select(UserTenantOnboarding.personal_assistant_agent_id).where(
            UserTenantOnboarding.user_id == membership.id,
            UserTenantOnboarding.tenant_id == tenant.id,
        )
    )
    personal_assistant_id = onboarding_result.scalar_one_or_none()

    open_task_filters = (
        Task.tenant_id == tenant.id,
        Task.created_by == membership.id,
        Task.status.in_(("pending", "doing")),
    )
    open_task_count = int(
        (
            await db.execute(
                select(sqla_func.count(Task.id)).where(*open_task_filters)
            )
        ).scalar_one()
        or 0
    )
    open_tasks = (
        await db.execute(
            select(Task)
            .where(*open_task_filters)
            .order_by(Task.updated_at.desc(), Task.id.asc())
            .limit(20)
        )
    ).scalars().all()

    requested_by = ApprovalRequest.details["requested_by"].as_string()
    pending_approval_filters = (
        Agent.tenant_id == tenant.id,
        ApprovalRequest.status == "pending",
        requested_by == str(membership.id),
    )
    pending_approval_count = int(
        (
            await db.execute(
                select(sqla_func.count(ApprovalRequest.id))
                .join(Agent, Agent.id == ApprovalRequest.agent_id)
                .where(*pending_approval_filters)
            )
        ).scalar_one()
        or 0
    )
    pending_approvals = (
        await db.execute(
            select(ApprovalRequest)
            .join(Agent, Agent.id == ApprovalRequest.agent_id)
            .where(*pending_approval_filters)
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.asc())
            .limit(20)
        )
    ).scalars().all()

    open_deliverable_filters = (
        DeliverableRequest.tenant_id == tenant.id,
        DeliverableRequest.created_by_user_id == membership.id,
        DeliverableRequest.status.in_(
            ("draft", "ready", "running", "waiting_approval")
        ),
    )
    open_deliverable_count = int(
        (
            await db.execute(
                select(sqla_func.count(DeliverableRequest.id)).where(
                    *open_deliverable_filters
                )
            )
        ).scalar_one()
        or 0
    )
    open_deliverables = (
        await db.execute(
            select(DeliverableRequest)
            .where(*open_deliverable_filters)
            .order_by(DeliverableRequest.updated_at.desc(), DeliverableRequest.id.asc())
            .limit(20)
        )
    ).scalars().all()

    delegated_agent_filters = (
        Agent.tenant_id == tenant.id,
        Agent.deleted_at.is_(None),
        Agent.creator_id != membership.id,
        AgentPermission.scope_type == "user",
        AgentPermission.scope_id == membership.id,
        AgentPermission.access_level == "manage",
    )
    delegated_agent_count = int(
        (
            await db.execute(
                select(sqla_func.count(AgentPermission.id))
                .join(Agent, Agent.id == AgentPermission.agent_id)
                .where(*delegated_agent_filters)
            )
        ).scalar_one()
        or 0
    )
    delegated_agents = (
        await db.execute(
            select(Agent)
            .join(AgentPermission, AgentPermission.agent_id == Agent.id)
            .where(*delegated_agent_filters)
            .order_by(Agent.name.asc(), Agent.id.asc())
            .limit(20)
        )
    ).scalars().all()

    credential_count = int(
        (
            await db.execute(
                select(sqla_func.count(AgentCredential.id))
                .join(Agent, Agent.id == AgentCredential.agent_id)
                .where(
                    Agent.tenant_id == tenant.id,
                    AgentCredential.owner_user_id == membership.id,
                    AgentCredential.status != "expired",
                )
            )
        ).scalar_one()
        or 0
    )

    pending_transfer_count = int(
        (
            await db.execute(
                select(sqla_func.count(TenantOwnershipTransfer.id)).where(
                    TenantOwnershipTransfer.tenant_id == tenant.id,
                    TenantOwnershipTransfer.status == "pending",
                    (
                        (TenantOwnershipTransfer.current_owner_user_id == membership.id)
                        | (TenantOwnershipTransfer.proposed_owner_user_id == membership.id)
                    ),
                )
            )
        ).scalar_one()
        or 0
    )

    blockers: list[dict] = []
    if tenant.owner_user_id == membership.id or membership.role == "org_owner":
        blockers.append(
            {
                "code": "owner_must_transfer_before_leaving",
                "count": 1,
                "message": "Transfer company ownership before leaving",
            }
        )
    if owned_agent_count:
        blockers.append(
            {
                "code": "agent_ownership_handoff_required",
                "count": owned_agent_count,
                "message": "Handover or delete every owned Agent before leaving",
            }
        )

    summary = {
        "owned_agents": owned_agent_count,
        "open_tasks": open_task_count,
        "pending_approvals": pending_approval_count,
        "open_deliverables": open_deliverable_count,
        "delegated_agents": delegated_agent_count,
        "personal_credentials": credential_count,
        "pending_ownership_transfers": pending_transfer_count,
    }
    warning_count = sum(
        summary[key]
        for key in (
            "open_tasks",
            "pending_approvals",
            "open_deliverables",
            "delegated_agents",
            "personal_credentials",
            "pending_ownership_transfers",
        )
    )
    return {
        "version": 1,
        "tenant_id": str(tenant.id),
        "membership_id": str(membership.id),
        "can_leave": not blockers,
        "requires_acknowledgement": warning_count > 0,
        "blockers": blockers,
        "summary": summary,
        "owned_agents": [
            {
                "id": str(agent.id),
                "name": agent.name,
                "status": agent.status,
                "access_mode": agent.access_mode or "company",
                "is_personal_assistant": agent.id == personal_assistant_id,
                "required_action": (
                    "delete" if agent.id == personal_assistant_id else "handover_or_delete"
                ),
            }
            for agent in owned_agents
        ],
        "open_tasks": [
            {
                "id": str(task.id),
                "title": task.title,
                "status": task.status,
                "agent_id": str(task.agent_id),
            }
            for task in open_tasks
        ],
        "pending_approvals": [
            {
                "id": str(approval.id),
                "action_type": approval.action_type,
                "agent_id": str(approval.agent_id) if approval.agent_id else None,
            }
            for approval in pending_approvals
        ],
        "open_deliverables": [
            {
                "id": str(item.id),
                "status": item.status,
                "work_type": item.work_type,
                "agent_id": str(item.agent_id),
            }
            for item in open_deliverables
        ],
        "delegated_agents": [
            {"id": str(agent.id), "name": agent.name}
            for agent in delegated_agents
        ],
        "effects_on_leave": {
            "membership": "deactivated",
            "global_identity": "preserved",
            "explicit_agent_grants": "revoked",
            "personal_credentials": "expired",
            "pending_approvals": "retained_but_execution_fails_closed",
            "historical_tasks_and_artifacts": "preserved",
        },
    }


async def _revoke_departed_membership_scope(
    db: AsyncSession,
    *,
    membership: User,
    tenant: Tenant,
) -> dict[str, int]:
    """Revoke membership-scoped grants without deleting historical work."""
    from app.models.agent_credential import AgentCredential
    from app.models.org import OrgMember

    permission_result = await db.execute(
        select(AgentPermission.id).where(
            AgentPermission.scope_type == "user",
            AgentPermission.scope_id == membership.id,
            AgentPermission.agent_id.in_(
                select(Agent.id).where(Agent.tenant_id == tenant.id)
            ),
        )
    )
    permission_ids = list(permission_result.scalars().all())
    if permission_ids:
        await db.execute(
            sql_delete(AgentPermission).where(AgentPermission.id.in_(permission_ids))
        )

    credential_result = await db.execute(
        select(AgentCredential.id)
        .join(Agent, Agent.id == AgentCredential.agent_id)
        .where(
            Agent.tenant_id == tenant.id,
            AgentCredential.owner_user_id == membership.id,
            AgentCredential.status != "expired",
        )
    )
    credential_ids = list(credential_result.scalars().all())
    if credential_ids:
        await db.execute(
            sql_update(AgentCredential)
            .where(AgentCredential.id.in_(credential_ids))
            .values(status="expired")
        )

    org_member_result = await db.execute(
        select(OrgMember.id).where(
            OrgMember.tenant_id == tenant.id,
            OrgMember.user_id == membership.id,
            OrgMember.status == "active",
        )
    )
    org_member_ids = list(org_member_result.scalars().all())
    if org_member_ids:
        await db.execute(
            sql_update(OrgMember)
            .where(OrgMember.id.in_(org_member_ids))
            .values(status="inactive")
        )

    return {
        "revoked_agent_grants": len(permission_ids),
        "expired_personal_credentials": len(credential_ids),
        "deactivated_directory_members": len(org_member_ids),
    }


@router.get("/{tenant_id}/leave-preflight")
async def get_tenant_leave_preflight(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the blockers, handoff items and exact effects of leaving."""
    if current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Company membership not found")
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return await _tenant_leave_preflight(
        db,
        membership=current_user,
        tenant=tenant,
    )


@router.post("/{tenant_id}/leave")
async def leave_tenant(
    tenant_id: uuid.UUID,
    body: TenantLeaveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate only the caller's membership; never delete the Identity."""
    if body.confirmation.strip().upper() != "LEAVE":
        raise HTTPException(status_code=400, detail="Type LEAVE to confirm")
    membership_result = await db.execute(
        select(User).where(User.id == current_user.id).with_for_update()
    )
    membership = membership_result.scalar_one_or_none()
    if not membership or membership.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Company membership not found")
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if tenant.owner_user_id == membership.id or membership.role == "org_owner":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "owner_must_transfer_before_leaving",
                "message": "Transfer company ownership before leaving",
            },
        )
    if not membership.is_active:
        return {"status": "already_left"}
    preflight = await _tenant_leave_preflight(
        db,
        membership=membership,
        tenant=tenant,
        lock_owned_agents=True,
    )
    if preflight["blockers"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": preflight["blockers"][0]["code"],
                "message": preflight["blockers"][0]["message"],
                "preflight": preflight,
            },
        )
    if preflight["requires_acknowledgement"] and not body.acknowledge_responsibilities:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "leave_responsibilities_acknowledgement_required",
                "message": "Review and acknowledge the remaining responsibilities before leaving",
                "preflight": preflight,
            },
        )
    membership.is_active = False
    revocation_summary = await _revoke_departed_membership_scope(
        db,
        membership=membership,
        tenant=tenant,
    )
    fallback_tenant_id, fallback_access_token = await _fallback_membership_context(
        db,
        identity_id=membership.identity_id,
        excluded_tenant_id=tenant_id,
        mfa_verified=access_context_mfa_verified(current_user),
    )
    db.add(
        AuditLog(
            tenant_id=tenant.id,
            user_id=membership.id,
            action="organization_membership_left",
            details={
                "tenant_id": str(tenant.id),
                "responsibility_summary": preflight["summary"],
                **revocation_summary,
            },
        )
    )
    await db.commit()
    return {
        "status": "left",
        "fallback_tenant_id": fallback_tenant_id,
        "access_token": fallback_access_token,
    }


# ─── Recoverable Company Deletion ─────────────────────

@router.delete("/{tenant_id}")
async def delete_tenant(
    tenant_id: uuid.UUID,
    body: TenantDeletionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Schedule an owner-confirmed, recoverable company deletion."""
    t_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = t_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if tenant.owner_user_id != current_user.id or current_user.role != "org_owner":
        raise HTTPException(
            status_code=403,
            detail={"code": "company_owner_required", "message": "Only the company owner can delete the company"},
        )
    await _require_password_proof(current_user, body.current_password)
    if body.company_name.strip() != tenant.name:
        raise HTTPException(
            status_code=400,
            detail={"code": "company_name_confirmation_mismatch", "message": "Company name confirmation does not match"},
        )

    identity_id = current_user.identity_id
    now = datetime.now(UTC)
    if tenant.deletion_requested_at is None:
        tenant.deletion_requested_at = now
        tenant.deletion_scheduled_for = now + timedelta(days=30)
        tenant.deletion_requested_by_user_id = current_user.id
        tenant.is_active = False
        db.add(
            AuditLog(
                user_id=current_user.id,
                action="tenant_deletion_scheduled",
                details={
                    "tenant_id": str(tenant.id),
                    "deletion_scheduled_for": tenant.deletion_scheduled_for.isoformat(),
                },
            )
        )

    job_result = await db.execute(
        select(TenantDeletionJob)
        .where(TenantDeletionJob.tenant_id == tenant.id)
        .with_for_update()
    )
    job = job_result.scalar_one_or_none()
    if job is None:
        job = TenantDeletionJob(
            tenant_id=tenant.id,
            status="scheduled",
            eligible_at=tenant.deletion_scheduled_for,
        )
        db.add(job)
    else:
        job.eligible_at = tenant.deletion_scheduled_for

    # Return a still-active membership and its membership-scoped token so the
    # browser can atomically leave the suspended company context. No tenant
    # data is destroyed here.
    fallback_tenant_id, fallback_access_token = await _fallback_membership_context(
        db,
        identity_id=identity_id,
        excluded_tenant_id=tenant_id,
        mfa_verified=access_context_mfa_verified(current_user),
    )
    await db.commit()
    return {
        "status": "scheduled",
        "deletion_scheduled_for": tenant.deletion_scheduled_for,
        "fallback_tenant_id": fallback_tenant_id,
        "access_token": fallback_access_token,
    }


@router.post("/{tenant_id}/restore")
async def restore_tenant(
    tenant_id: uuid.UUID,
    body: TenantRestoreRequest,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore a company during its deletion grace period."""
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    is_owner = tenant.owner_user_id == current_user.id and current_user.role == "org_owner"
    is_platform_operator = bool(getattr(getattr(current_user, "identity", None), "is_platform_admin", False))
    if not is_owner and not is_platform_operator:
        raise HTTPException(status_code=403, detail="Company owner or platform operator required")
    if is_owner:
        if not body.current_password:
            raise HTTPException(status_code=400, detail="current_password is required")
        await _require_password_proof(current_user, body.current_password)
    if tenant.deletion_requested_at is None:
        return {"status": "active" if tenant.is_active else "suspended"}
    if tenant.deletion_scheduled_for and tenant.deletion_scheduled_for <= datetime.now(UTC):
        raise HTTPException(
            status_code=409,
            detail={"code": "tenant_restore_window_elapsed", "message": "The restore window has elapsed"},
        )
    tenant.is_active = True
    tenant.deletion_requested_at = None
    tenant.deletion_scheduled_for = None
    tenant.deletion_requested_by_user_id = None
    # A restored company starts a fresh lifecycle.  Historical operator
    # actions remain in AuditLog, while purge plans and active holds tied to
    # the cancelled schedule must not leak into a future deletion request.
    await db.execute(
        sql_delete(TenantDeletionHold).where(TenantDeletionHold.tenant_id == tenant.id)
    )
    await db.execute(
        sql_delete(TenantDeletionJob).where(TenantDeletionJob.tenant_id == tenant.id)
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="tenant_deletion_restored",
            details={"tenant_id": str(tenant.id)},
        )
    )
    await db.commit()
    return {"status": "restored"}
