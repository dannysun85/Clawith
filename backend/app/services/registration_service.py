"""Registration service for user account creation with SSO support.

This module handles user registration including:
- Email domain-based tenant detection
- SSO-based registration flow
- Duplicate identity detection
"""

import re
import uuid
from typing import Any

from app.config import unverified_local_signup_allowed
from app.core.security import hash_password_async
from app.core.identity_canonicalization import canonicalize_email
from app.dao import (
    identity_dao,
    identity_provider_dao,
    invitation_code_dao,
    org_member_dao,
    participant_dao,
    tenant_dao,
    user_dao,
)
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.models.user import User, Identity
from app.services.external_identity_policy import (
    create_isolated_external_identity,
    require_stable_external_subject,
)
from app.services.identity_provider_lookup import (
    get_login_identity_provider,
    get_login_identity_provider_by_id,
)
from app.services.sso_service import ExternalIdentityProvisioningDeniedError, sso_service
from app.services.system_email_service import resolve_email_config_async
from loguru import logger


_EMAIL_CONFIG_UNRESOLVED = object()


class RegistrationService:
    """Service for handling user registration flows."""

    # ── Identity provider ────────────────────────────────────────────────────

    async def ensure_identity_provider(
        self,
        provider_type: str,
        tenant_id: uuid.UUID | None,
        *,
        name: str | None = None,
        sso_login_enabled: bool = False,
    ) -> IdentityProvider:
        """Get or create an identity provider record for a tenant."""
        return await identity_provider_dao.get_or_create(
            provider_type,
            tenant_id,
            name=name,
            sso_login_enabled=sso_login_enabled,
        )

    # ── Tenant detection ─────────────────────────────────────────────────────

    async def detect_tenant_by_email(self, email: str) -> Tenant | None:
        """Detect tenant based on email domain."""
        email = canonicalize_email(email) or ""
        if not email or "@" not in email:
            return None
        domain = email.split("@")[1].lower()
        return await tenant_dao.get_by_sso_domain(domain)

    # ── Duplicate check ──────────────────────────────────────────────────────

    async def check_duplicate_identity(
        self,
        email: str | None = None,
        mobile: str | None = None,
    ) -> dict[str, Any]:
        """Check for existing identities that might conflict.

        Returns:
            Dict with ``has_conflict`` bool and ``conflicts`` list.
        """
        conflicts = []

        email = canonicalize_email(email)
        if email and await identity_dao.get_by_email(email):
            conflicts.append({
                "type": "email",
                "scope": "global",
                "message": "Email already registered",
            })

        if mobile:
            normalized = re.sub(r"[\s\-\+]", "", mobile)
            if await identity_dao.get_by_phone(normalized):
                conflicts.append({
                    "type": "mobile",
                    "scope": "global",
                    "message": "Mobile already registered",
                })

        return {"has_conflict": len(conflicts) > 0, "conflicts": conflicts}

    # ── Identity find / create ───────────────────────────────────────────────

    async def find_or_create_identity(
        self,
        email: str | None = None,
        phone: str | None = None,
        username: str | None = None,
        password: str | None = None,
        is_platform_admin: bool = False,
        email_config: Any = _EMAIL_CONFIG_UNRESOLVED,
        password_hash: str | None = None,
    ) -> Identity:
        """Find an existing identity or create a new one.

        Security note: only email and phone are authoritative identity claims.
        """
        identity: Identity | None = None
        email = canonicalize_email(email)

        # Match by email (primary ownership claim)
        if email:
            identity = await identity_dao.get_by_email(email)

        # Match by phone (secondary ownership claim)
        if not identity and phone:
            identity = await identity_dao.get_by_phone(phone)

        if identity:
            # Missing SMTP is not proof of mailbox ownership.  Only an
            # explicit local development/test escape hatch may preserve the
            # historical auto-verify behavior.
            if email_config is _EMAIL_CONFIG_UNRESOLVED:
                email_config = await resolve_email_config_async()
            if (
                not email_config
                and not identity.email_verified
                and unverified_local_signup_allowed()
            ):
                await identity_dao.update(db_obj=identity, obj_in={"email_verified": True})
            return identity

        # Determine verified status
        if email_config is _EMAIL_CONFIG_UNRESOLVED:
            email_config = await resolve_email_config_async()
        is_verified = bool(
            not email_config and unverified_local_signup_allowed()
        )

        # Resolve a safe, unique username
        final_username = username
        if username and await identity_dao.is_username_taken(username):
            final_username = f"{username}_{uuid.uuid4().hex[:6]}"
            logger.info("Registration assigned a generated unique username")

        # Hash password if not pre-hashed
        if not password_hash and password:
            password_hash = await hash_password_async(password)

        return await identity_dao.create_identity(
            email=email,
            phone=phone,
            username=final_username,
            password_hash=password_hash,
            is_platform_admin=is_platform_admin,
            email_verified=is_verified,
        )

    # ── User create ──────────────────────────────────────────────────────────

    async def create_user_with_identity(
        self,
        identity: Identity,
        display_name: str | None = None,
        role: str = "member",
        tenant_id: uuid.UUID | None = None,
        registration_source: str = "web",
        email_config: Any = _EMAIL_CONFIG_UNRESOLVED,
        require_email_verification_for_activation: bool = True,
    ) -> User:
        """Create a new tenant-specific user linked to an identity."""
        name = display_name or identity.username or "User"

        if (
            require_email_verification_for_activation
            and email_config is _EMAIL_CONFIG_UNRESOLVED
        ):
            email_config = await resolve_email_config_async()

        activation_pending_email_verification = bool(
            require_email_verification_for_activation
            and not identity.email_verified
            and not identity.is_platform_admin
        )

        user = await user_dao.create(obj_in={
            "identity_id": identity.id,
            "tenant_id": tenant_id,
            "display_name": name,
            "role": role,
            "registration_source": registration_source,
            "is_active": not activation_pending_email_verification,
            "activation_pending_email_verification": activation_pending_email_verification,
        })
        user.identity = identity

        # Link to OrgMember if exists
        await self.bind_org_member(user)

        # Create Participant record
        await participant_dao.create_for_user(
            user.id,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        )

        return user

    # ── SSO flows ────────────────────────────────────────────────────────────

    async def handle_sso_registration(
        self,
        provider_type: str,
        provider_user_id: str,
        user_info: dict,
        existing_user: User | None = None,
    ) -> tuple[User, bool]:
        """Compatibility SSO creation path using stable provider identity only."""
        # Tenant membership must come from an explicitly tenant-scoped
        # provider/callback context, never from an email-domain guess.
        tenant_id = user_info.get("tenant_id")
        if tenant_id is None:
            raise ExternalIdentityProvisioningDeniedError(
                "Compatibility SSO registration requires an exact tenant"
            )

        lookup_provider_user_id = require_stable_external_subject(
            provider_type,
            user_info.get("union_id") or user_info.get("unionId") or provider_user_id,
        )
        async with identity_dao.session() as db:
            provider = await get_login_identity_provider(
                db,
                provider_type,
                str(tenant_id),
                allow_global_fallback=False,
            )
            if not provider:
                raise ExternalIdentityProvisioningDeniedError(
                    "SSO provider is disabled"
                )
            existing = await sso_service.resolve_user_identity(
                db,
                lookup_provider_user_id,
                provider_type,
                tenant_id=tenant_id,
                identity_data=user_info,
                provider_id=provider.id,
            )
            if existing:
                return existing, False

            if existing_user:
                raise ValueError(
                    "Implicit SSO account merging is disabled; use authenticated identity bind"
                )

            provisioned_member = await sso_service.find_identity_member(
                db,
                provider.id,
                provider_type,
                lookup_provider_user_id,
                user_info,
                active_only=False,
            )
            if not provisioned_member or provisioned_member.status != "active":
                raise ExternalIdentityProvisioningDeniedError(
                    "The external subject is not provisioned for this tenant"
                )

            identity = await create_isolated_external_identity(
                db,
                provider_type=provider_type,
                provider_subject=lookup_provider_user_id,
            )
            user = await self.create_user_with_identity(
                identity=identity,
                display_name=user_info.get("name") or identity.username,
                registration_source=provider_type,
                tenant_id=tenant_id,
                # Provider authentication activates the membership; it does
                # not promote provider contact claims to global ownership.
                require_email_verification_for_activation=False,
            )
            await sso_service.link_identity(
                db,
                str(user.id),
                provider_type,
                lookup_provider_user_id,
                user_info,
                tenant_id=str(tenant_id) if tenant_id else None,
                provider_id=provider.id,
            )
            return user, True

    async def register_with_sso(
        self,
        db,
        provider_type: str,
        auth_provider,
        user_info_obj,
        *,
        membership_tenant_id: uuid.UUID | None,
        membership_role: str,
        signup_capacity_available: bool,
    ) -> tuple[User, bool, str | None]:
        """Persist provider-authenticated user info in one short transaction."""
        provider_record = getattr(auth_provider, "provider", None)
        tenant_id = getattr(provider_record, "tenant_id", None)
        if not provider_record:
            return None, False, "SSO provider is disabled"
        current_provider = await get_login_identity_provider_by_id(
            db,
            provider_id=provider_record.id,
            provider_type=provider_type,
            tenant_id=tenant_id,
            for_update=True,
        )
        if not current_provider:
            return None, False, "SSO provider is disabled"
        current_auth_provider = type(auth_provider)(
            provider=current_provider,
            config=current_provider.config or {},
        )
        provider_subject = require_stable_external_subject(
            provider_type,
            user_info_obj.provider_user_id or user_info_obj.provider_union_id,
        )
        identity_data = current_auth_provider._identity_payload(user_info_obj)
        linked_user = await sso_service.resolve_user_identity(
            db,
            provider_subject,
            provider_type,
            tenant_id=str(tenant_id) if tenant_id else None,
            identity_data=identity_data,
            provider_id=current_provider.id,
        )
        if linked_user is not None:
            if membership_tenant_id is None:
                return linked_user, False, None
            existing_membership = await user_dao.get_by_identity_and_tenant(
                linked_user.identity_id,
                membership_tenant_id,
            )
            if existing_membership is not None:
                existing_membership.identity = linked_user.identity
                return existing_membership, False, None
            if not signup_capacity_available:
                return None, False, "Registration code has reached its usage limit"
            user = await self.create_user_with_identity(
                identity=linked_user.identity,
                display_name=linked_user.display_name,
                role=membership_role,
                tenant_id=membership_tenant_id,
                registration_source=provider_type,
                require_email_verification_for_activation=False,
            )
            return user, True, None

        if not signup_capacity_available:
            return None, False, "Registration code has reached its usage limit"
        user, is_new = await current_auth_provider.find_or_create_user(
            db,
            user_info_obj,
            tenant_id=str(tenant_id) if tenant_id else None,
            membership_tenant_id=(
                str(membership_tenant_id)
                if membership_tenant_id is not None
                else None
            ),
        )
        user.role = membership_role
        return user, is_new, None

    # ── Tenant for registration ──────────────────────────────────────────────

    async def get_tenant_for_registration(
        self,
        email: str | None = None,
        invitation_code: str | None = None,
    ) -> tuple[Tenant | None, str]:
        """Determine tenant for new user registration."""
        if invitation_code:
            inv = await invitation_code_dao.get_active_by_code(invitation_code)
            if inv and inv.used_count < inv.max_uses:
                t = await tenant_dao.get(inv.tenant_id)
                if t and t.is_active:
                    return t, None
                return None, "Invitation code tenant is inactive"

        if email:
            tenant = await self.detect_tenant_by_email(email)
            if tenant:
                return tenant, None

        return None, None

    # ── OrgMember binding ────────────────────────────────────────────────────

    async def bind_org_member(self, user: User) -> None:
        """Ensure the platform-owned Web membership only.

        Directory and SSO contact fields are untrusted profile metadata.  An
        unbound provider member may be linked only by an authenticated provider
        bind/login flow using its stable provider subject, never implicitly by
        matching email or phone during Web registration or tenant joining.
        """
        await self.ensure_web_org_member(user)

    async def ensure_web_org_member(self, user: User):
        """Ensure the user has a dedicated platform OrgMember record in their tenant."""
        if not user.tenant_id:
            return None

        from app.models.org import OrgMember

        web_provider = await self.ensure_identity_provider("web", user.tenant_id, name="Platform")
        if web_provider.name == "Web":
            web_provider.name = "Platform"

        # Look up existing OrgMember
        member = await org_member_dao.get_by_user_and_provider(
            user.id, user.tenant_id, web_provider.id
        )
        if not member and user.email:
            member = await org_member_dao.find_unbound_by_email_and_provider(
                user.email, user.tenant_id, web_provider.id
            )
        if not member and user.primary_mobile:
            member = await org_member_dao.find_unbound_by_phone_and_provider(
                user.primary_mobile, user.tenant_id, web_provider.id
            )

        created = False
        linked_existing = False
        async with org_member_dao.session() as db:
            if member:
                linked_existing = member.user_id is None
                member.user_id = user.id
            else:
                member = OrgMember(
                    name=user.display_name or "User",
                    email=user.email,
                    phone=user.primary_mobile,
                    provider_id=web_provider.id,
                    title="Platform User",
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    status="active",
                )
                db.add(member)
                created = True

            desired_name = user.display_name or member.name or "User"
            if desired_name and member.name != desired_name:
                member.name = desired_name
            if member.email != user.email:
                member.email = user.email
            if member.phone != user.primary_mobile:
                member.phone = user.primary_mobile
            if member.title in (None, "", "Web User"):
                member.title = "Platform User"

            await db.flush()

        if created or linked_existing:
            from app.services.okr_agent_hook import hook_new_org_member
            async with org_member_dao.session() as db:
                await hook_new_org_member(db, member.id, user.tenant_id)

        return member

    async def sync_org_member_contact_from_user(
        self,
        user: User,
        *,
        sync_email: bool = False,
        sync_phone: bool = False,
    ) -> None:
        """Sync email/phone from User to linked OrgMember (user is source of truth)."""
        if not user.tenant_id or not (sync_email or sync_phone):
            return

        web_provider = await self.ensure_identity_provider("web", user.tenant_id, name="Platform")
        if web_provider.name == "Web":
            web_provider.name = "Platform"

        members = await org_member_dao.get_by_user_and_tenant_and_provider(
            user.id, user.tenant_id, web_provider.id
        )
        if not members:
            return

        async with org_member_dao.session() as db:
            for member in members:
                if sync_email and member.email != user.email:
                    member.email = user.email
                if sync_phone and member.phone != user.primary_mobile:
                    member.phone = user.primary_mobile
            await db.flush()


# Global registration service
registration_service = RegistrationService()
