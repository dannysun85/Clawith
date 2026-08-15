"""Explicit product APIs for identity and organization governance."""

from datetime import UTC, datetime, timedelta
import hashlib
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import (
    get_authenticated_user,
    get_company_governor,
    get_company_or_platform_admin,
    get_platform_operator,
    verify_password_async,
)
from app.core.auth_rate_limit import (
    enforce_auth_rate_limit,
    password_reauth_rate_limit_policy,
)
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.identity_governance import (
    OrganizationInvitation,
    OrganizationJoinLink,
    PlatformSupportSession,
    RegistrationGrant,
    TenantOwnershipResolution,
)
from app.models.outbound_email import OutboundEmailDelivery
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.identity_governance import (
    COMPANY_CREATE_CAPABILITY,
    GovernanceCredentialError,
    grant_identity_capability,
    issue_organization_invitation,
    issue_organization_join_link,
    issue_registration_grant,
    revoke_identity_capability,
)
from app.services.outbound_email_service import (
    cancel_invitation_deliveries,
    delivery_public_payload,
    dispatch_outbound_email,
    enqueue_template_email,
)
from app.services.platform_service import platform_service


router = APIRouter(prefix="/governance", tags=["identity-governance"])

_SUPPORT_SCOPES = {
    "tenant.metadata.read",
    "tenant.lifecycle.manage",
    "tenant.diagnostics.read",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _require_current_tenant(user: User, tenant_id: uuid.UUID) -> None:
    if user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "tenant_scope_mismatch", "message": "Current company membership is required"},
        )


def _credential_error(exc: GovernanceCredentialError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


class RegistrationGrantCreate(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_days: int | None = Field(default=30, ge=1, le=365)


class IdentityCapabilityMutation(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class OrganizationInvitationCreate(BaseModel):
    email: EmailStr
    role: str = "member"
    expires_in_days: int = Field(default=7, ge=1, le=30)


class OrganizationInvitationManualLink(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)


class OrganizationJoinLinkCreate(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_days: int | None = Field(default=7, ge=1, le=90)


class PlatformSupportSessionCreate(BaseModel):
    tenant_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=1000)
    scopes: list[str] = Field(min_length=1, max_length=3)
    duration_minutes: int = Field(default=30, ge=5, le=60)


class OwnershipResolutionRequest(BaseModel):
    owner_user_id: uuid.UUID
    reason: str = Field(min_length=5, max_length=1000)


@router.post("/platform/registration-grants", status_code=status.HTTP_201_CREATED)
async def create_registration_grants(
    body: RegistrationGrantCreate,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    expires_at = _now() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    issued = []
    for _ in range(body.count):
        credential = await issue_registration_grant(
            db,
            max_uses=body.max_uses,
            created_by_identity_id=current_user.identity_id,
            expires_at=expires_at,
        )
        issued.append(
            {
                "id": str(credential.record.id),
                "token": credential.raw_token,
                "token_prefix": credential.record.token_prefix,
                "max_uses": credential.record.max_uses,
                "expires_at": credential.record.expires_at,
            }
        )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="registration_grants_issued",
            details={"count": len(issued), "max_uses": body.max_uses},
        )
    )
    await db.commit()
    return {"items": issued}


@router.get("/platform/registration-grants")
async def list_registration_grants(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(RegistrationGrant).order_by(RegistrationGrant.created_at.desc()))
    return {
        "items": [
            {
                "id": str(item.id),
                "token_prefix": item.token_prefix,
                "max_uses": item.max_uses,
                "used_count": item.used_count,
                "status": item.status,
                "expires_at": item.expires_at,
                "created_at": item.created_at,
                "legacy": item.legacy_invitation_code_id is not None,
            }
            for item in result.scalars().all()
        ]
    }


@router.delete("/platform/registration-grants/{grant_id}")
async def revoke_registration_grant(
    grant_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(RegistrationGrant).where(RegistrationGrant.id == grant_id).with_for_update()
    )
    grant = result.scalar_one_or_none()
    if not grant:
        raise HTTPException(status_code=404, detail="Registration grant not found")
    if grant.status == "active":
        grant.status = "revoked"
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="registration_grant_revoked",
            details={"registration_grant_id": str(grant.id)},
        )
    )
    await db.commit()
    return {"status": grant.status}


@router.put("/platform/identities/{identity_id}/capabilities/company-create")
async def grant_company_create(
    identity_id: uuid.UUID,
    body: IdentityCapabilityMutation,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(Identity, identity_id)
    if not target or not target.is_active:
        raise HTTPException(status_code=404, detail="Active identity not found")
    grant = await grant_identity_capability(
        db,
        identity_id=identity_id,
        capability=COMPANY_CREATE_CAPABILITY,
        granted_by_identity_id=current_user.identity_id,
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="identity_capability_granted",
            details={
                "identity_id": str(identity_id),
                "capability": COMPANY_CREATE_CAPABILITY,
                "reason": body.reason,
            },
        )
    )
    await db.commit()
    return {"id": str(grant.id), "capability": grant.capability, "status": "active"}


@router.delete("/platform/identities/{identity_id}/capabilities/company-create")
async def revoke_company_create(
    identity_id: uuid.UUID,
    body: IdentityCapabilityMutation,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    changed = await revoke_identity_capability(
        db,
        identity_id=identity_id,
        capability=COMPANY_CREATE_CAPABILITY,
        revoked_by_identity_id=current_user.identity_id,
        reason=body.reason,
    )
    if changed:
        db.add(
            AuditLog(
                user_id=current_user.id,
                action="identity_capability_revoked",
                details={
                    "identity_id": str(identity_id),
                    "capability": COMPANY_CREATE_CAPABILITY,
                    "reason": body.reason,
                },
            )
        )
    await db.commit()
    return {"status": "revoked" if changed else "not_granted"}


def _invitation_payload(
    invitation: OrganizationInvitation,
    delivery: OutboundEmailDelivery | None,
) -> dict:
    delivery_payload = delivery_public_payload(delivery)
    delivery_status = (
        "manual_link_issued"
        if invitation.delivery_mode == "manual_link" and delivery is None
        else delivery.status if delivery is not None else "not_queued"
    )
    return {
        "id": str(invitation.id),
        "token_prefix": invitation.token_prefix,
        "target_email": invitation.target_email,
        "role": invitation.invited_role,
        "status": invitation.status,
        "delivery_mode": invitation.delivery_mode,
        "delivery_status": delivery_status,
        "delivery": delivery_payload,
        "expires_at": invitation.expires_at,
        "accepted_at": invitation.accepted_at,
        "created_at": invitation.created_at,
    }


def _invitation_idempotency_key(tenant_id: uuid.UUID, raw: str | None, operation: str) -> str | None:
    cleaned = raw.strip() if isinstance(raw, str) else ""
    if not cleaned:
        return None
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return f"organization-invitation:{tenant_id}:{operation}:{digest}"


async def _lock_invitation_idempotency_key(db: AsyncSession, key: str | None) -> None:
    if key:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )


async def _existing_idempotent_invitation(
    db: AsyncSession,
    key: str | None,
) -> tuple[OrganizationInvitation, OutboundEmailDelivery] | None:
    if not key:
        return None
    result = await db.execute(
        select(OrganizationInvitation, OutboundEmailDelivery)
        .join(
            OutboundEmailDelivery,
            OutboundEmailDelivery.invitation_id == OrganizationInvitation.id,
        )
        .where(OutboundEmailDelivery.idempotency_key == key)
    )
    return result.first()


@router.post("/organizations/{tenant_id}/invitations", status_code=status.HTTP_201_CREATED)
async def create_organization_invitation(
    tenant_id: uuid.UUID,
    body: OrganizationInvitationCreate,
    current_user: User = Depends(get_company_or_platform_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
    background_tasks: BackgroundTasks = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    from app.services.access_control import is_company_governor, is_platform_operator

    company_context = current_user.tenant_id == tenant_id and is_company_governor(current_user)
    platform_operator = is_platform_operator(current_user) and not company_context
    if platform_operator:
        if body.role != "org_owner":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "platform_invitation_scope_denied",
                    "message": "Platform operators may only invite an owner to an ownerless company",
                },
            )
    elif company_context:
        _require_current_tenant(current_user, tenant_id)
    else:
        raise HTTPException(status_code=403, detail="Company governance access required")

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    tenant = tenant_result.scalar_one_or_none()
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Active company not found")

    if body.role == "org_owner":
        if not platform_operator or tenant.owner_user_id is not None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "owner_invitation_not_allowed",
                    "message": "Only a platform operator may resolve an ownerless company with an owner invitation",
                },
            )
    elif body.role == "org_admin":
        if current_user.role != "org_owner":
            raise HTTPException(
                status_code=403,
                detail={"code": "owner_required_for_admin_invitation", "message": "Only the company owner can invite an admin"},
            )
    elif body.role != "member":
        raise HTTPException(status_code=400, detail="Invalid organization invitation role")

    scoped_idempotency_key = _invitation_idempotency_key(tenant_id, idempotency_key, "create")
    await _lock_invitation_idempotency_key(db, scoped_idempotency_key)
    existing = await _existing_idempotent_invitation(db, scoped_idempotency_key)
    if existing:
        invitation, delivery = existing
        return _invitation_payload(invitation, delivery)

    try:
        credential = await issue_organization_invitation(
            db,
            tenant_id=tenant_id,
            target_email=str(body.email),
            invited_role=body.role,
            invited_by_user_id=current_user.id,
            expires_at=_now() + timedelta(days=body.expires_in_days),
        )
    except GovernanceCredentialError as exc:
        raise _credential_error(exc) from exc
    replaced_record_id = getattr(credential, "replaced_record_id", None)
    if replaced_record_id:
        await cancel_invitation_deliveries(db, replaced_record_id)
    credential.record.delivery_mode = "email"
    base_url = await platform_service.get_public_base_url(db, request=request)
    query = urlencode({"code": credential.raw_token, "email": credential.record.target_email})
    delivery = await enqueue_template_email(
        db,
        purpose="company_invitation",
        to=credential.record.target_email,
        scenario_key="company_invitation",
        variables={
            "inviter_name": current_user.display_name or current_user.username,
            "company_name": tenant.name,
            "invite_url": f"{base_url}/login?{query}",
        },
        tenant_id=tenant_id,
        invitation_id=credential.record.id,
        requested_by_user_id=current_user.id,
        idempotency_key=scoped_idempotency_key,
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_invitation_issued",
            details={
                "tenant_id": str(tenant_id),
                "invitation_id": str(credential.record.id),
                "target_email": credential.record.target_email,
                "role": credential.record.invited_role,
            },
        )
    )
    await db.commit()
    if background_tasks is not None and delivery.status in {"queued", "retry_wait"}:
        background_tasks.add_task(dispatch_outbound_email, delivery.id)
    return _invitation_payload(credential.record, delivery)


@router.get("/organizations/{tenant_id}/invitations")
async def list_organization_invitations(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    _require_current_tenant(current_user, tenant_id)
    result = await db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.tenant_id == tenant_id)
        .order_by(OrganizationInvitation.created_at.desc())
    )
    invitations = result.scalars().all()
    invitation_ids = [item.id for item in invitations]
    deliveries_by_invitation: dict[uuid.UUID, OutboundEmailDelivery] = {}
    if invitation_ids:
        delivery_result = await db.execute(
            select(OutboundEmailDelivery)
            .where(OutboundEmailDelivery.invitation_id.in_(invitation_ids))
            .order_by(OutboundEmailDelivery.created_at.desc())
        )
        for delivery in delivery_result.scalars().all():
            deliveries_by_invitation.setdefault(delivery.invitation_id, delivery)
    return {
        "items": [
            _invitation_payload(item, deliveries_by_invitation.get(item.id))
            for item in invitations
        ]
    }


@router.post("/organizations/{tenant_id}/invitations/{invitation_id}/resend")
async def resend_organization_invitation(
    request: Request,
    background_tasks: BackgroundTasks,
    tenant_id: uuid.UUID,
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Rotate the invitation credential and queue a new email exactly once per key."""

    _require_current_tenant(current_user, tenant_id)
    scoped_key = _invitation_idempotency_key(
        tenant_id,
        idempotency_key,
        f"resend:{invitation_id}",
    )
    await _lock_invitation_idempotency_key(db, scoped_key)
    existing = await _existing_idempotent_invitation(db, scoped_key)
    if existing:
        invitation, delivery = existing
        return _invitation_payload(invitation, delivery)
    result = await db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Organization invitation not found")
    if invitation.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "invitation_not_pending", "message": "Only a pending invitation can be resent"},
        )
    if invitation.invited_role == "org_admin" and current_user.role != "org_owner":
        raise HTTPException(status_code=403, detail="Only the company owner can resend an admin invitation")
    tenant = await db.get(Tenant, tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Active company not found")
    await cancel_invitation_deliveries(db, invitation.id)
    credential = await issue_organization_invitation(
        db,
        tenant_id=tenant_id,
        target_email=invitation.target_email,
        invited_role=invitation.invited_role,
        invited_by_user_id=current_user.id,
        expires_at=_now() + timedelta(days=7),
    )
    credential.record.delivery_mode = "email"
    base_url = await platform_service.get_public_base_url(db, request=request)
    query = urlencode({"code": credential.raw_token, "email": credential.record.target_email})
    delivery = await enqueue_template_email(
        db,
        purpose="company_invitation",
        to=credential.record.target_email,
        scenario_key="company_invitation",
        variables={
            "inviter_name": current_user.display_name or current_user.username,
            "company_name": tenant.name,
            "invite_url": f"{base_url}/login?{query}",
        },
        tenant_id=tenant_id,
        invitation_id=credential.record.id,
        requested_by_user_id=current_user.id,
        idempotency_key=scoped_key,
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_invitation_resent",
            details={
                "tenant_id": str(tenant_id),
                "previous_invitation_id": str(invitation_id),
                "invitation_id": str(credential.record.id),
            },
        )
    )
    await db.commit()
    if delivery.status in {"queued", "retry_wait"}:
        background_tasks.add_task(dispatch_outbound_email, delivery.id)
    return _invitation_payload(credential.record, delivery)


@router.post("/organizations/{tenant_id}/invitations/{invitation_id}/manual-link")
async def issue_organization_invitation_manual_link(
    request: Request,
    tenant_id: uuid.UUID,
    invitation_id: uuid.UUID,
    body: OrganizationInvitationManualLink,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Rotate and reveal one manually shared link after password reauthentication."""

    _require_current_tenant(current_user, tenant_id)
    identity = getattr(current_user, "identity", None)
    if not identity or not identity.password_login_enabled or not identity.password_hash:
        raise HTTPException(status_code=403, detail="Password reauthentication is unavailable")
    await enforce_auth_rate_limit(
        request,
        identity=f"identity:{identity.id}",
        policy=password_reauth_rate_limit_policy(),
    )
    if not await verify_password_async(body.current_password, identity.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    result = await db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Organization invitation not found")
    if invitation.status != "pending":
        raise HTTPException(status_code=409, detail="Only a pending invitation can be rotated")
    if invitation.invited_role == "org_admin" and current_user.role != "org_owner":
        raise HTTPException(status_code=403, detail="Only the company owner can share an admin invitation")
    tenant = await db.get(Tenant, tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=404, detail="Active company not found")
    await cancel_invitation_deliveries(db, invitation.id, error_code="manual_link_issued")
    credential = await issue_organization_invitation(
        db,
        tenant_id=tenant_id,
        target_email=invitation.target_email,
        invited_role=invitation.invited_role,
        invited_by_user_id=current_user.id,
        expires_at=_now() + timedelta(days=7),
    )
    credential.record.delivery_mode = "manual_link"
    base_url = await platform_service.get_public_base_url(db, request=request)
    query = urlencode({"code": credential.raw_token, "email": credential.record.target_email})
    manual_url = f"{base_url}/login?{query}"
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_invitation_manual_link_issued",
            details={
                "tenant_id": str(tenant_id),
                "previous_invitation_id": str(invitation_id),
                "invitation_id": str(credential.record.id),
                "target_email": credential.record.target_email,
            },
        )
    )
    await db.commit()
    return {
        **_invitation_payload(credential.record, None),
        "manual_url": manual_url,
        "one_time_display": True,
    }


@router.delete("/organizations/{tenant_id}/invitations/{invitation_id}")
async def revoke_organization_invitation(
    tenant_id: uuid.UUID,
    invitation_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    _require_current_tenant(current_user, tenant_id)
    result = await db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Organization invitation not found")
    if invitation.status == "pending":
        invitation.status = "revoked"
        invitation.revoked_at = _now()
        await cancel_invitation_deliveries(db, invitation.id, error_code="invitation_revoked")
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_invitation_revoked",
            details={"tenant_id": str(tenant_id), "invitation_id": str(invitation.id)},
        )
    )
    await db.commit()
    return {"status": invitation.status}


@router.get("/me/pending-invitations")
async def list_my_pending_invitations(
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    email = getattr(getattr(current_user, "identity", None), "email", None)
    if not email:
        return {"items": []}
    result = await db.execute(
        select(OrganizationInvitation, Tenant)
        .join(Tenant, Tenant.id == OrganizationInvitation.tenant_id)
        .where(
            OrganizationInvitation.target_email == email,
            OrganizationInvitation.status == "pending",
            OrganizationInvitation.expires_at > _now(),
            Tenant.is_active.is_(True),
        )
        .order_by(OrganizationInvitation.created_at.desc())
    )
    return {
        "items": [
            {
                "id": str(invitation.id),
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "role": invitation.invited_role,
                "expires_at": invitation.expires_at,
                "created_at": invitation.created_at,
            }
            for invitation, tenant in result.all()
        ]
    }


@router.post("/organizations/{tenant_id}/join-links", status_code=status.HTTP_201_CREATED)
async def create_organization_join_link(
    tenant_id: uuid.UUID,
    body: OrganizationJoinLinkCreate,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    _require_current_tenant(current_user, tenant_id)
    expires_at = _now() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    try:
        credential = await issue_organization_join_link(
            db,
            tenant_id=tenant_id,
            max_uses=body.max_uses,
            created_by_user_id=current_user.id,
            expires_at=expires_at,
        )
    except GovernanceCredentialError as exc:
        raise _credential_error(exc) from exc
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_join_link_issued",
            details={"tenant_id": str(tenant_id), "join_link_id": str(credential.record.id)},
        )
    )
    await db.commit()
    return {
        "id": str(credential.record.id),
        "token": credential.raw_token,
        "token_prefix": credential.record.token_prefix,
        "max_uses": credential.record.max_uses,
        "expires_at": credential.record.expires_at,
    }


@router.get("/organizations/{tenant_id}/join-links")
async def list_organization_join_links(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    _require_current_tenant(current_user, tenant_id)
    result = await db.execute(
        select(OrganizationJoinLink)
        .where(OrganizationJoinLink.tenant_id == tenant_id)
        .order_by(OrganizationJoinLink.created_at.desc())
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "token_prefix": item.token_prefix,
                "max_uses": item.max_uses,
                "used_count": item.used_count,
                "status": item.status,
                "expires_at": item.expires_at,
                "legacy": item.legacy_invitation_code_id is not None,
            }
            for item in result.scalars().all()
        ]
    }


@router.delete("/organizations/{tenant_id}/join-links/{link_id}")
async def revoke_organization_join_link(
    tenant_id: uuid.UUID,
    link_id: uuid.UUID,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    _require_current_tenant(current_user, tenant_id)
    result = await db.execute(
        select(OrganizationJoinLink)
        .where(OrganizationJoinLink.id == link_id, OrganizationJoinLink.tenant_id == tenant_id)
        .with_for_update()
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Organization join link not found")
    if link.status == "active":
        link.status = "revoked"
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_join_link_revoked",
            details={"tenant_id": str(tenant_id), "join_link_id": str(link.id)},
        )
    )
    await db.commit()
    return {"status": link.status}


@router.post("/platform/support-sessions", status_code=status.HTTP_201_CREATED)
async def create_platform_support_session(
    body: PlatformSupportSessionCreate,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    invalid_scopes = sorted(set(body.scopes) - _SUPPORT_SCOPES)
    if invalid_scopes:
        raise HTTPException(
            status_code=400,
            detail={"code": "support_scope_invalid", "message": f"Unsupported scopes: {', '.join(invalid_scopes)}"},
        )
    tenant = await db.get(Tenant, body.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    session = PlatformSupportSession(
        platform_identity_id=current_user.identity_id,
        tenant_id=body.tenant_id,
        reason=body.reason.strip(),
        scopes=sorted(set(body.scopes)),
        expires_at=_now() + timedelta(minutes=body.duration_minutes),
    )
    db.add(session)
    await db.flush()
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="platform_support_session_started",
            details={
                "support_session_id": str(session.id),
                "tenant_id": str(session.tenant_id),
                "scopes": session.scopes,
                "reason": session.reason,
            },
        )
    )
    await db.commit()
    return {
        "id": str(session.id),
        "tenant_id": str(session.tenant_id),
        "scopes": session.scopes,
        "expires_at": session.expires_at,
    }


@router.delete("/platform/support-sessions/{session_id}")
async def end_platform_support_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PlatformSupportSession)
        .where(
            PlatformSupportSession.id == session_id,
            PlatformSupportSession.platform_identity_id == current_user.identity_id,
        )
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Support session not found")
    if session.ended_at is None:
        session.ended_at = _now()
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="platform_support_session_ended",
            details={"support_session_id": str(session.id), "tenant_id": str(session.tenant_id)},
        )
    )
    await db.commit()
    return {"status": "ended"}


@router.get("/platform/support-sessions/{session_id}/tenants/{tenant_id}/summary")
async def get_platform_support_tenant_summary(
    session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Return only the non-private tenant facts allowed by one support session."""
    result = await db.execute(
        select(PlatformSupportSession)
        .where(
            PlatformSupportSession.id == session_id,
            PlatformSupportSession.platform_identity_id == current_user.identity_id,
        )
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Support session not found")
    if session.ended_at is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "support_session_inactive", "message": "Support session has ended"},
        )
    if session.expires_at <= _now():
        raise HTTPException(
            status_code=403,
            detail={"code": "support_session_expired", "message": "Support session has expired"},
        )
    if session.tenant_id != tenant_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "support_session_tenant_mismatch", "message": "Support session is bound to another company"},
        )

    readable_scopes = sorted(
        set(session.scopes or {})
        & {"tenant.metadata.read", "tenant.diagnostics.read"}
    )
    if not readable_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "support_scope_required", "message": "Support session does not permit diagnostic reads"},
        )

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    payload: dict[str, object] = {
        "support_session_id": str(session.id),
        "tenant_id": str(tenant.id),
        "scopes_applied": readable_scopes,
    }
    if "tenant.metadata.read" in readable_scopes:
        payload["metadata"] = {
            "name": tenant.name,
            "slug": tenant.slug,
            "is_active": tenant.is_active,
            "timezone": tenant.timezone,
            "country_region": tenant.country_region,
            "sso_enabled": tenant.sso_enabled,
            "created_at": tenant.created_at,
        }
    if "tenant.diagnostics.read" in readable_scopes:
        membership_counts = (
            await db.execute(
                select(
                    func.count(User.id),
                    func.count(User.id).filter(User.is_active.is_(True)),
                ).where(User.tenant_id == tenant_id)
            )
        ).one()
        agent_counts = (
            await db.execute(
                select(
                    func.count(Agent.id),
                    func.count(Agent.id).filter(Agent.deleted_at.is_(None)),
                ).where(Agent.tenant_id == tenant_id)
            )
        ).one()
        payload["diagnostics"] = {
            "memberships_total": int(membership_counts[0] or 0),
            "memberships_active": int(membership_counts[1] or 0),
            "agents_total": int(agent_counts[0] or 0),
            "agents_active": int(agent_counts[1] or 0),
        }

    session.last_used_at = _now()
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="platform_support_tenant_summary_read",
            details={
                "support_session_id": str(session.id),
                "tenant_id": str(tenant.id),
                "scopes_applied": readable_scopes,
            },
        )
    )
    await db.commit()
    return payload


@router.get("/platform/ownership-resolutions")
async def list_ownership_resolutions(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TenantOwnershipResolution, Tenant)
        .join(Tenant, Tenant.id == TenantOwnershipResolution.tenant_id)
        .where(TenantOwnershipResolution.status == "open")
        .order_by(TenantOwnershipResolution.created_at)
    )
    return {
        "items": [
            {
                "id": str(resolution.id),
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "reason": resolution.reason,
                "candidate_user_ids": resolution.candidate_user_ids,
                "created_at": resolution.created_at,
            }
            for resolution, tenant in result.all()
        ]
    }


@router.post("/platform/ownership-resolutions/{resolution_id}/resolve")
async def resolve_tenant_ownership(
    resolution_id: uuid.UUID,
    body: OwnershipResolutionRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TenantOwnershipResolution)
        .where(TenantOwnershipResolution.id == resolution_id)
        .with_for_update()
    )
    resolution = result.scalar_one_or_none()
    if not resolution or resolution.status != "open":
        raise HTTPException(status_code=404, detail="Open ownership resolution not found")
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == resolution.tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one()
    owner_result = await db.execute(
        select(User)
        .options(selectinload(User.identity))
        .where(
            User.id == body.owner_user_id,
            User.tenant_id == tenant.id,
            User.is_active.is_(True),
        )
        .with_for_update()
    )
    owner = owner_result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=400, detail="Owner must be an active member of this company")
    other_owners = await db.execute(
        select(User).where(
            User.tenant_id == tenant.id,
            User.role == "org_owner",
            User.id != owner.id,
        )
    )
    for previous in other_owners.scalars().all():
        previous.role = "org_admin"
    # The database enforces one org_owner per tenant. Persist every demotion
    # before promoting the selected owner so the invariant holds throughout
    # the transaction.
    await db.flush()
    owner.role = "org_owner"
    tenant.owner_user_id = owner.id
    tenant.owner_resolution_required = False
    resolution.status = "resolved"
    resolution.resolved_by_identity_id = current_user.identity_id
    resolution.resolved_at = _now()
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="tenant_ownership_resolved",
            details={
                "tenant_id": str(tenant.id),
                "owner_user_id": str(owner.id),
                "reason": body.reason,
            },
        )
    )
    await db.commit()
    return {"status": "resolved", "tenant_id": str(tenant.id), "owner_user_id": str(owner.id)}
