"""Domain operations for account and organization governance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_canonicalization import canonicalize_email
from app.models.identity_governance import (
    IdentityCapabilityGrant,
    OrganizationInvitation,
    OrganizationJoinLink,
    RegistrationGrant,
)


COMPANY_CREATE_CAPABILITY = "company.create"
ORGANIZATION_INVITATION_TTL_DAYS = 7


class GovernanceCredentialError(ValueError):
    """A stable product error for an invalid or unusable credential."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(slots=True)
class IssuedCredential:
    raw_token: str
    record: RegistrationGrant | OrganizationInvitation | OrganizationJoinLink
    replaced_record_id: uuid.UUID | None = None


@dataclass(slots=True)
class ResolvedOrganizationCredential:
    kind: str
    tenant_id: uuid.UUID
    role: str
    record: OrganizationInvitation | OrganizationJoinLink


def normalize_governance_token(raw_token: str | None) -> str:
    return (raw_token or "").strip().upper()


def governance_token_hash(raw_token: str | None) -> str:
    normalized = normalize_governance_token(raw_token)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _new_token(prefix: str) -> str:
    # 120 random bits. Grouping is for human transcription only; matching uses
    # the normalized digest and never relies on the display prefix.
    body = secrets.token_hex(15).upper()
    return f"{prefix}-{body[:10]}-{body[10:20]}-{body[20:]}"


def _display_prefix(raw_token: str) -> str:
    return normalize_governance_token(raw_token)[:12]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    comparison = now or _utc_now()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= comparison


async def identity_has_capability(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
    capability: str,
) -> bool:
    result = await db.execute(
        select(IdentityCapabilityGrant.id).where(
            IdentityCapabilityGrant.identity_id == identity_id,
            IdentityCapabilityGrant.capability == capability,
            IdentityCapabilityGrant.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is not None


async def grant_identity_capability(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
    capability: str,
    granted_by_identity_id: uuid.UUID | None,
) -> IdentityCapabilityGrant:
    existing = await db.execute(
        select(IdentityCapabilityGrant).where(
            IdentityCapabilityGrant.identity_id == identity_id,
            IdentityCapabilityGrant.capability == capability,
            IdentityCapabilityGrant.revoked_at.is_(None),
        )
    )
    active = existing.scalar_one_or_none()
    if active:
        return active
    grant = IdentityCapabilityGrant(
        identity_id=identity_id,
        capability=capability,
        granted_by_identity_id=granted_by_identity_id,
    )
    db.add(grant)
    await db.flush()
    return grant


async def revoke_identity_capability(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
    capability: str,
    revoked_by_identity_id: uuid.UUID,
    reason: str,
) -> bool:
    result = await db.execute(
        select(IdentityCapabilityGrant)
        .where(
            IdentityCapabilityGrant.identity_id == identity_id,
            IdentityCapabilityGrant.capability == capability,
            IdentityCapabilityGrant.revoked_at.is_(None),
        )
        .with_for_update()
    )
    grant = result.scalar_one_or_none()
    if not grant:
        return False
    grant.revoked_at = _utc_now()
    grant.revoked_by_identity_id = revoked_by_identity_id
    grant.revocation_reason = reason.strip()
    await db.flush()
    return True


async def issue_registration_grant(
    db: AsyncSession,
    *,
    max_uses: int,
    created_by_identity_id: uuid.UUID,
    expires_at: datetime | None,
) -> IssuedCredential:
    raw_token = _new_token("REG")
    record = RegistrationGrant(
        token_hash=governance_token_hash(raw_token),
        token_prefix=_display_prefix(raw_token),
        max_uses=max_uses,
        used_count=0,
        status="active",
        expires_at=expires_at,
        created_by_identity_id=created_by_identity_id,
    )
    db.add(record)
    await db.flush()
    return IssuedCredential(raw_token=raw_token, record=record)


async def resolve_registration_grant(
    db: AsyncSession,
    raw_token: str | None,
    *,
    for_update: bool = False,
) -> RegistrationGrant | None:
    normalized = normalize_governance_token(raw_token)
    if not normalized:
        return None
    statement = select(RegistrationGrant).where(
        RegistrationGrant.token_hash == governance_token_hash(normalized)
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    grant = result.scalar_one_or_none()
    if not grant:
        return None
    if grant.status != "active":
        raise GovernanceCredentialError("registration_grant_inactive", "Registration grant is not active")
    if _is_expired(grant.expires_at):
        grant.status = "revoked"
        raise GovernanceCredentialError("registration_grant_expired", "Registration grant has expired")
    if grant.used_count >= grant.max_uses:
        grant.status = "exhausted"
        raise GovernanceCredentialError("registration_grant_exhausted", "Registration grant has reached its usage limit")
    return grant


def consume_registration_grant(grant: RegistrationGrant) -> None:
    if grant.status != "active" or grant.used_count >= grant.max_uses:
        raise GovernanceCredentialError("registration_grant_unavailable", "Registration grant is no longer available")
    grant.used_count += 1
    if grant.used_count >= grant.max_uses:
        grant.status = "exhausted"


async def issue_organization_invitation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_email: str,
    invited_role: str,
    invited_by_user_id: uuid.UUID,
    expires_at: datetime | None = None,
) -> IssuedCredential:
    email = canonicalize_email(target_email)
    if not email:
        raise GovernanceCredentialError("invitation_email_required", "A valid invitation email is required")
    if invited_role not in {"member", "org_admin", "org_owner"}:
        raise GovernanceCredentialError("invitation_role_invalid", "Invitation role is invalid")

    existing_result = await db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.tenant_id == tenant_id,
            OrganizationInvitation.target_email == email,
            OrganizationInvitation.status == "pending",
        )
        .with_for_update()
    )
    existing = existing_result.scalar_one_or_none()
    replaced_record_id = None
    if existing:
        replaced_record_id = existing.id
        existing.status = "revoked"
        existing.revoked_at = _utc_now()
        # Retire the old pending row before inserting the replacement so the
        # partial unique index remains valid regardless of ORM flush ordering.
        await db.flush()

    raw_token = _new_token("ORG")
    record = OrganizationInvitation(
        tenant_id=tenant_id,
        target_email=email,
        invited_role=invited_role,
        token_hash=governance_token_hash(raw_token),
        token_prefix=_display_prefix(raw_token),
        status="pending",
        expires_at=expires_at or (_utc_now() + timedelta(days=ORGANIZATION_INVITATION_TTL_DAYS)),
        invited_by_user_id=invited_by_user_id,
    )
    db.add(record)
    await db.flush()
    return IssuedCredential(
        raw_token=raw_token,
        record=record,
        replaced_record_id=replaced_record_id,
    )


async def issue_organization_join_link(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    max_uses: int,
    created_by_user_id: uuid.UUID,
    expires_at: datetime | None,
) -> IssuedCredential:
    if max_uses < 1 or max_uses > 1000:
        raise GovernanceCredentialError("join_link_max_uses_invalid", "Join-link max_uses must be between 1 and 1000")
    raw_token = _new_token("JOIN")
    record = OrganizationJoinLink(
        tenant_id=tenant_id,
        token_hash=governance_token_hash(raw_token),
        token_prefix=_display_prefix(raw_token),
        max_uses=max_uses,
        used_count=0,
        status="active",
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    db.add(record)
    await db.flush()
    return IssuedCredential(raw_token=raw_token, record=record)


def _resolve_invitation_record(
    invitation: OrganizationInvitation,
    *,
    identity_email: str | None,
    target_tenant_id: uuid.UUID | None,
) -> ResolvedOrganizationCredential:
    if invitation.status != "pending":
        raise GovernanceCredentialError("organization_invitation_inactive", "Organization invitation is not pending")
    if _is_expired(invitation.expires_at):
        invitation.status = "expired"
        raise GovernanceCredentialError("organization_invitation_expired", "Organization invitation has expired")
    email = canonicalize_email(identity_email)
    if not email or email != invitation.target_email:
        raise GovernanceCredentialError(
            "organization_invitation_email_mismatch",
            "This invitation belongs to a different email address",
            status_code=403,
        )
    if target_tenant_id is not None and invitation.tenant_id != target_tenant_id:
        raise GovernanceCredentialError(
            "organization_invitation_tenant_mismatch",
            "This invitation does not belong to the required organization",
            status_code=403,
        )
    return ResolvedOrganizationCredential(
        kind="organization_invitation",
        tenant_id=invitation.tenant_id,
        role=invitation.invited_role,
        record=invitation,
    )


async def resolve_organization_invitation_by_id(
    db: AsyncSession,
    invitation_id: uuid.UUID,
    *,
    identity_email: str | None,
    target_tenant_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> ResolvedOrganizationCredential | None:
    """Resolve an authenticated user's pending invitation without exposing its token."""

    statement = select(OrganizationInvitation).where(OrganizationInvitation.id == invitation_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    invitation = result.scalar_one_or_none()
    if invitation is None:
        return None
    return _resolve_invitation_record(
        invitation,
        identity_email=identity_email,
        target_tenant_id=target_tenant_id,
    )


async def resolve_organization_credential(
    db: AsyncSession,
    raw_token: str | None,
    *,
    identity_email: str | None,
    target_tenant_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> ResolvedOrganizationCredential | None:
    normalized = normalize_governance_token(raw_token)
    if not normalized:
        return None
    digest = governance_token_hash(normalized)

    invitation_statement = select(OrganizationInvitation).where(
        OrganizationInvitation.token_hash == digest
    )
    if for_update:
        invitation_statement = invitation_statement.with_for_update()
    invitation_result = await db.execute(invitation_statement)
    invitation = invitation_result.scalar_one_or_none()
    if invitation:
        return _resolve_invitation_record(
            invitation,
            identity_email=identity_email,
            target_tenant_id=target_tenant_id,
        )

    link_statement = select(OrganizationJoinLink).where(
        OrganizationJoinLink.token_hash == digest
    )
    if for_update:
        link_statement = link_statement.with_for_update()
    link_result = await db.execute(link_statement)
    link = link_result.scalar_one_or_none()
    if not link:
        return None
    if link.status != "active":
        raise GovernanceCredentialError("organization_join_link_inactive", "Organization join link is not active")
    if _is_expired(link.expires_at):
        link.status = "revoked"
        raise GovernanceCredentialError("organization_join_link_expired", "Organization join link has expired")
    if link.used_count >= link.max_uses:
        link.status = "exhausted"
        raise GovernanceCredentialError("organization_join_link_exhausted", "Organization join link is exhausted")
    if target_tenant_id is not None and link.tenant_id != target_tenant_id:
        raise GovernanceCredentialError(
            "organization_join_link_tenant_mismatch",
            "This join link does not belong to the required organization",
            status_code=403,
        )
    return ResolvedOrganizationCredential(
        kind="organization_join_link",
        tenant_id=link.tenant_id,
        role="member",
        record=link,
    )


def consume_organization_credential(
    credential: ResolvedOrganizationCredential,
    *,
    accepted_by_user_id: uuid.UUID,
) -> None:
    if credential.kind == "organization_invitation":
        invitation = credential.record
        if not isinstance(invitation, OrganizationInvitation) or invitation.status != "pending":
            raise GovernanceCredentialError("organization_invitation_unavailable", "Organization invitation is unavailable")
        invitation.status = "accepted"
        invitation.accepted_by_user_id = accepted_by_user_id
        invitation.accepted_at = _utc_now()
        return

    link = credential.record
    if not isinstance(link, OrganizationJoinLink) or link.status != "active":
        raise GovernanceCredentialError("organization_join_link_unavailable", "Organization join link is unavailable")
    link.used_count += 1
    if link.used_count >= link.max_uses:
        link.status = "exhausted"
