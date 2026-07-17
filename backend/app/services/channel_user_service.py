"""Channel user resolution service for messaging platforms.

This service provides unified user resolution for incoming messages from
external channels (DingTalk, WeCom, Feishu, etc.). It reuses the SSO service
and OrgMember-based identity management.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.external_identity_policy import (
    acquire_external_subject_lock,
    create_isolated_external_identity,
    external_user_can_authenticate,
    require_stable_external_subject,
)


class ChannelUserResolutionError(ValueError):
    """Raised when a channel message cannot be safely attributed to a user."""


class ChannelUserService:
    """Service for resolving channel users via OrgMember and SSO patterns."""

    CHANNEL_TYPE_ALIASES = {
        "microsoft_teams": "teams",
    }

    def _normalize_channel_type(self, channel_type: str) -> str:
        raw = (channel_type or "").strip().lower()
        return self.CHANNEL_TYPE_ALIASES.get(raw, raw)

    def _legacy_provider_types_for_channel(self, channel_type: str) -> list[str]:
        normalized = self._normalize_channel_type(channel_type)
        legacy = [normalized]
        if normalized == "teams":
            legacy.append("microsoft_teams")
        elif normalized == "microsoft_teams":
            legacy.append("teams")
        return legacy

    def _get_channel_ids(
        self,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        normalized_channel = self._normalize_channel_type(channel_type)
        unionid = (extra_info.get("unionid") or extra_info.get("union_id") or "").strip() or None
        open_id = (extra_info.get("open_id") or "").strip() or None
        external_id = (extra_info.get("external_id") or external_user_id or "").strip() or None

        if normalized_channel == "feishu":
            # Feishu external_id must remain tenant-stable user_id only.
            # Never backfill it from open_id.
            external_id = (extra_info.get("external_id") or "").strip() or None
        elif normalized_channel == "dingtalk":
            open_id = open_id or None
        elif normalized_channel == "wecom":
            unionid = None
            open_id = open_id or None
        else:
            unionid = None
            open_id = None

        return unionid, open_id, external_id

    async def provision_provider_for_config(
        self,
        db: AsyncSession,
        *,
        channel_type: str,
        tenant_id: uuid.UUID | None,
    ) -> IdentityProvider:
        """Provision channel authorization from an authenticated admin flow."""
        tenant = await db.get(Tenant, tenant_id) if tenant_id else None
        if not tenant or not tenant.is_active:
            raise ChannelUserResolutionError("Channel tenant is disabled or unavailable")
        canonical_type = self._normalize_channel_type(channel_type)
        provider_types = self._legacy_provider_types_for_channel(channel_type)
        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.tenant_id == tenant_id,
                IdentityProvider.provider_type.in_(provider_types),
            )
        )
        providers = list(result.scalars().all())
        if len(providers) > 1:
            raise ChannelUserResolutionError(
                "Multiple channel providers require administrator repair"
            )
        if providers:
            if not providers[0].is_active:
                raise ChannelUserResolutionError(
                    "Channel provider is disabled; re-enable it explicitly first"
                )
            return providers[0]

        provider = IdentityProvider(
            provider_type=canonical_type,
            name=canonical_type.replace("_", " ").title(),
            is_active=True,
            sso_login_enabled=False,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()
        return provider

    async def resolve_channel_user(
        self,
        db: AsyncSession,
        agent: Agent,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> User:
        """Resolve channel user identity, find or create platform User.

        Priority order:
        1. OrgMember already linked to an active User → return existing User
        2. OrgMember exists but not linked → create an isolated User and link
        3. No match → create an isolated User and OrgMember (lazy registration)

        Provider email/mobile fields are directory metadata only.  They never
        select an existing global Identity.

        Args:
            db: Database session
            agent: Agent receiving the message (for tenant_id)
            channel_type: "dingtalk" | "wecom" | "wechat" | "feishu"
            external_user_id: User ID from external platform. For Feishu this must be user_id, not open_id.
            extra_info: Optional name/avatar/mobile/email from platform API

        Returns:
            Resolved User instance
        """
        tenant_id = agent.tenant_id
        extra_info = extra_info or {}

        tenant = await db.get(Tenant, tenant_id) if tenant_id else None
        if not tenant or not tenant.is_active:
            raise ChannelUserResolutionError("Channel tenant is disabled or unavailable")

        _unionid, _open_id, stable_subject = self._get_channel_ids(
            channel_type, external_user_id, extra_info
        )
        try:
            stable_subject = require_stable_external_subject(
                channel_type,
                _unionid or stable_subject,
            )
        except ValueError as exc:
            raise ChannelUserResolutionError(
                "Channel authentication did not provide a stable user identifier"
            ) from exc
        await acquire_external_subject_lock(
            db,
            provider_type=channel_type,
            tenant_id=tenant_id,
            provider_subject=stable_subject,
        )

        # Step 1: require an explicitly provisioned, active provider. Incoming
        # traffic must never create or re-enable a channel authorization.
        provider = await self._ensure_provider(db, channel_type, tenant_id)

        # Step 2: Try to find OrgMember by external identity
        org_member = await self._find_org_member(
            db,
            provider.id,
            tenant_id,
            channel_type,
            external_user_id,
            extra_info,
        )

        # Step 3: Resolve User from OrgMember or other means
        user = None

        if org_member and org_member.user_id:
            # Case 1: OrgMember already linked to User
            user_result = await db.execute(
                select(User)
                .where(
                    User.id == org_member.user_id,
                    User.tenant_id == tenant_id,
                )
                .options(selectinload(User.identity))
            )
            user = user_result.scalar_one_or_none()
            if external_user_can_authenticate(user):
                logger.debug(
                    f"[{channel_type}] Found user via linked OrgMember: {user.id}"
                )
                return user
            raise ChannelUserResolutionError(
                "Linked channel account is disabled or unavailable"
            )

        should_persist_member = True

        unionid, open_id, external_id = self._get_channel_ids(
            channel_type, external_user_id, extra_info
        )

        if channel_type == "feishu" and not org_member and not (unionid or external_id):
            raise ChannelUserResolutionError(
                "Feishu sender could not be resolved to a stable user_id/union_id; "
                "refusing to lazily create a duplicate user from open_id only."
            )

        # Step 5: Create new User (lazy registration)
        user = await self._create_channel_user(
            db, channel_type, external_user_id, extra_info, tenant_id
        )

        # Step 6: Link or create OrgMember
        if should_persist_member:
            if org_member:
                org_member.user_id = user.id
            else:
                await self._create_org_member_shell(
                    db, provider, channel_type, external_user_id, extra_info,
                    linked_user_id=user.id
                )
            await db.flush()
        logger.info(f"[{channel_type}] Created new user {user.id}")

        return user

    async def _ensure_provider(
        self, db: AsyncSession, provider_type: str, tenant_id: uuid.UUID | None
    ) -> IdentityProvider:
        """Return an explicitly configured active IdentityProvider."""
        canonical_type = self._normalize_channel_type(provider_type)

        query = select(IdentityProvider).where(
            IdentityProvider.provider_type == canonical_type
        )
        if tenant_id:
            query = query.where(IdentityProvider.tenant_id == tenant_id)
        else:
            query = query.where(IdentityProvider.tenant_id.is_(None))

        result = await db.execute(query)
        provider = result.scalar_one_or_none()
        if provider and provider.is_active:
            return provider
        if provider:
            raise ChannelUserResolutionError("Channel provider is disabled")

        for legacy_type in self._legacy_provider_types_for_channel(provider_type):
            if legacy_type == canonical_type:
                continue
            legacy_query = select(IdentityProvider).where(
                IdentityProvider.provider_type == legacy_type
            )
            if tenant_id:
                legacy_query = legacy_query.where(IdentityProvider.tenant_id == tenant_id)
            else:
                legacy_query = legacy_query.where(IdentityProvider.tenant_id.is_(None))
            legacy_result = await db.execute(legacy_query)
            legacy_provider = legacy_result.scalar_one_or_none()
            if legacy_provider and legacy_provider.is_active:
                return legacy_provider
            if legacy_provider:
                raise ChannelUserResolutionError("Channel provider is disabled")

        raise ChannelUserResolutionError(
            "Channel provider has not been provisioned by an administrator"
        )

    async def _find_org_member(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID,
        tenant_id: uuid.UUID,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any] | None = None,
    ) -> OrgMember | None:
        """Find OrgMember by external identity.

        For Feishu: try unionid first, then open_id, then external_id
        For DingTalk: try unionid first, then external_id
        For WeCom: try external_id (userid)
        For WeChat: try external_id (from_user_id)

        Returns None if OrgMember not found or org sync is not enabled for this channel.
        """
        extra_info = extra_info or {}
        unionid, open_id, external_id = self._get_channel_ids(
            channel_type, external_user_id, extra_info
        )

        # Build OR conditions for matching
        conditions = [
            OrgMember.provider_id == provider_id,
            OrgMember.tenant_id == tenant_id,
            OrgMember.status == "active",
        ]

        # Channel-specific matching priority
        normalized_channel = self._normalize_channel_type(channel_type)
        if normalized_channel == "feishu":
            # Feishu identifiers have distinct semantics:
            # unionid/open_id come from extra_info; external_id is user_id only.
            lookup_conditions = []
            if unionid:
                lookup_conditions.append(OrgMember.unionid == unionid)
            if open_id:
                lookup_conditions.append(OrgMember.open_id == open_id)
            if external_id:
                lookup_conditions.append(OrgMember.external_id == external_id)
            if not lookup_conditions:
                return None
            conditions.append(lookup_conditions[0])
            for cond in lookup_conditions[1:]:
                conditions[-1] = conditions[-1] | cond
        elif normalized_channel == "dingtalk":
            # DingTalk: unionid is stable across apps, then external_id
            lookup_conditions = []
            if unionid:
                lookup_conditions.append(OrgMember.unionid == unionid)
            if external_id:
                lookup_conditions.append(OrgMember.external_id == external_id)
            if not lookup_conditions:
                return None
            conditions.append(lookup_conditions[0])
            for cond in lookup_conditions[1:]:
                conditions[-1] = conditions[-1] | cond
        elif normalized_channel == "wecom":
            # WeCom: external_id (userid) is the primary identifier
            if not external_id:
                return None
            conditions.append(OrgMember.external_id == external_id)
        else:
            # Generic channels: provider is already channel-scoped, so external_id
            # can be used directly without namespacing.
            if not external_id:
                return None
            conditions.append(OrgMember.external_id == external_id)

        # Load two rows so historical duplicate stable IDs cannot be silently
        # attributed to whichever row the database happens to return first.
        # Database failures must propagate; treating them as "not found" would
        # turn an outage into duplicate account creation.
        query = (
            select(OrgMember)
            .where(*conditions)
            .order_by(
                OrgMember.user_id.isnot(None).desc(),
                OrgMember.synced_at.asc(),
            )
            .limit(2)
        )
        result = await db.execute(query)
        members = list(result.scalars().all())
        if len(members) > 1:
            logger.error(
                "Ambiguous channel identity provider_id={} member_ids={}",
                provider_id,
                [str(member.id) for member in members],
            )
            raise ChannelUserResolutionError(
                "Channel identity is ambiguous and requires administrator repair"
            )
        return members[0] if members else None

    async def _create_org_member_shell(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        linked_user_id: uuid.UUID | None = None,
    ) -> OrgMember:
        """Create a shell OrgMember record for this identity."""
        identity_seed = (
            external_user_id
            or (extra_info.get("open_id") or "").strip()
            or uuid.uuid4().hex
        )
        name = extra_info.get("name") or f"{channel_type.capitalize()} User {identity_seed[:8]}"
        unionid, open_id, external_id = self._get_channel_ids(channel_type, external_user_id, extra_info)

        member = OrgMember(
            name=name,
            email=extra_info.get("email"),
            provider_id=provider.id,
            user_id=linked_user_id,
            tenant_id=provider.tenant_id,
            external_id=external_id,
            unionid=unionid,
            open_id=open_id,
            avatar_url=extra_info.get("avatar_url"),
            phone=extra_info.get("mobile"),
            title=extra_info.get("title", ""),
            status="active",
        )
        db.add(member)
        await db.flush()
        return member

    async def _find_existing_org_member_for_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        provider_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> OrgMember | None:
        """Find an existing OrgMember already linked to the given platform User.

        Used before creating a shell record to avoid duplicate OrgMember entries
        when an org-sync-sourced record already exists for the same user.
        """
        query = select(OrgMember).where(
            OrgMember.user_id == user_id,
            OrgMember.provider_id == provider_id,
            OrgMember.status == "active",
        )
        if tenant_id:
            query = query.where(OrgMember.tenant_id == tenant_id)
        result = await db.execute(query.limit(1))
        return result.scalar_one_or_none()

    async def _create_channel_user(
        self,
        db: AsyncSession,
        channel_type: str,
        external_user_id: str | None,
        extra_info: dict[str, Any],
        tenant_id: uuid.UUID | None,
    ) -> User:
        """Create an isolated passwordless Identity and tenant User."""
        identity_seed = (
            external_user_id
            or (extra_info.get("unionid") or extra_info.get("union_id") or "").strip()
            or (extra_info.get("external_id") or "").strip()
            or (extra_info.get("open_id") or "").strip()
        )
        identity = await create_isolated_external_identity(
            db,
            provider_type=channel_type,
            provider_subject=identity_seed,
        )
        name = extra_info.get("name") or f"{channel_type.capitalize()} {identity_seed[:8]}"

        # ── Step 2: Create tenant-scoped User linked to Identity ─────────────
        user = User(
            identity=identity,
            display_name=name,
            avatar_url=extra_info.get("avatar_url"),
            role="member",
            registration_source=channel_type,
            tenant_id=tenant_id,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        return user



# Global service instance
channel_user_service = ChannelUserService()


async def get_platform_user_by_org_member(
    db: AsyncSession,
    org_member: OrgMember,
    agent_tenant_id: uuid.UUID | None = None,
) -> User:
    """Get or create platform User from an existing OrgMember.

    This is used by agent_tools.py when sending proactive messages:
    - OrgMember already exists (from AgentRelationship)
    - But user_id may be NULL (not yet linked to platform User)
    - We need to get or create the User and link it

    Args:
        db: Database session
        org_member: Existing OrgMember instance
        agent_tenant_id: Optional tenant ID for scoping

    Returns:
        Linked/created User instance
    """
    if (
        agent_tenant_id is None
        or org_member.tenant_id is None
        or agent_tenant_id != org_member.tenant_id
    ):
        raise ChannelUserResolutionError(
            "Organization member requires one exact tenant scope"
        )
    effective_tenant_id = agent_tenant_id

    provider_result = await db.execute(
        select(IdentityProvider)
        .where(
            IdentityProvider.id == org_member.provider_id,
            IdentityProvider.tenant_id == effective_tenant_id,
            IdentityProvider.is_active.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    provider = provider_result.scalar_one_or_none()
    if not provider:
        raise ChannelUserResolutionError("Organization member provider is unavailable")

    tenant_result = await db.execute(
        select(Tenant)
        .where(
            Tenant.id == effective_tenant_id,
            Tenant.is_active.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise ChannelUserResolutionError("Organization member tenant is unavailable")

    member_result = await db.execute(
        select(OrgMember)
        .where(
            OrgMember.id == org_member.id,
            OrgMember.provider_id == provider.id,
            OrgMember.tenant_id == effective_tenant_id,
            OrgMember.status == "active",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    org_member = member_result.scalar_one_or_none()
    if not org_member:
        raise ChannelUserResolutionError(
            "Organization member scope is disabled or unavailable"
        )

    provider_subject = require_stable_external_subject(
        str(provider.provider_type),
        org_member.unionid or org_member.external_id or org_member.open_id,
    )
    await acquire_external_subject_lock(
        db,
        provider_type=str(provider.provider_type),
        tenant_id=effective_tenant_id,
        provider_subject=provider_subject,
    )

    # Case 1: OrgMember already linked to User
    if org_member.user_id:
        query = (
            select(User)
            .where(User.id == org_member.user_id)
            .options(selectinload(User.identity))
        )
        if effective_tenant_id:
            query = query.where(User.tenant_id == effective_tenant_id)
        user_res = await db.execute(query)
        user = user_res.scalar_one_or_none()
        if external_user_can_authenticate(user):
            return user
        raise ChannelUserResolutionError(
            "Linked organization member account is disabled or unavailable"
        )

    # Case 2: Create an isolated User and link it to the stable OrgMember.
    # Determine channel type from provider
    channel_type = provider.provider_type if provider else "unknown"
    external_seed = org_member.external_id

    seed_for_name = external_seed or org_member.id.hex
    name = org_member.name or f"{channel_type.capitalize()} User {seed_for_name[:8]}"
    identity = await create_isolated_external_identity(
        db,
        provider_type=str(channel_type),
        provider_subject=(
            org_member.unionid
            or org_member.external_id
            or org_member.open_id
            or org_member.id
        ),
    )

    user = User(
        identity=identity,
        display_name=name,
        avatar_url=org_member.avatar_url,
        role="member",
        registration_source=channel_type,
        tenant_id=effective_tenant_id,
        is_active=True,
    )

    db.add(user)
    await db.flush()

    # Link OrgMember to new User
    org_member.user_id = user.id
    await db.flush()

    logger.info(f"[channel_user_service] Created User {user.id} for OrgMember {org_member.id}")
    
    return user
