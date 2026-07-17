"""Helpers for resolving identity providers safely."""

from __future__ import annotations

import uuid
from typing import Iterable

from loguru import logger
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import AuthProviderType, IdentityProvider
from app.models.tenant import Tenant


def build_identity_provider_query(
    provider_type: AuthProviderType | str,
    tenant_id: str | None = None,
    *,
    is_active: bool | None = None,
) -> Select[tuple[IdentityProvider]]:
    """Build a deterministic provider lookup query."""
    query = select(IdentityProvider).where(IdentityProvider.provider_type == provider_type)
    if tenant_id is not None:
        query = query.where(IdentityProvider.tenant_id == tenant_id)
    else:
        query = query.where(IdentityProvider.tenant_id.is_(None))
    if is_active is not None:
        query = query.where(IdentityProvider.is_active == is_active)
    return query.order_by(
        IdentityProvider.updated_at.desc(),
        IdentityProvider.created_at.desc(),
        IdentityProvider.id.desc(),
    )


def choose_preferred_identity_provider(
    providers: Iterable[IdentityProvider],
    *,
    provider_type: AuthProviderType | str,
    tenant_id: str | None = None,
) -> IdentityProvider | None:
    """Pick the preferred provider and warn when duplicates are present."""
    items = list(providers)
    if not items:
        return None

    if len(items) > 1:
        logger.error(
            "Ambiguous identity providers type=%s tenant_id=%s count=%s; refusing selection",
            provider_type,
            tenant_id,
            len(items),
        )
        return None
    return items[0]


async def get_preferred_identity_provider(
    db: AsyncSession,
    provider_type: AuthProviderType | str,
    tenant_id: str | None = None,
    *,
    is_active: bool | None = None,
    allow_global_fallback: bool = True,
) -> IdentityProvider | None:
    """Fetch the preferred provider without raising on duplicate rows."""
    result = await db.execute(
        build_identity_provider_query(provider_type, tenant_id, is_active=is_active)
    )
    providers = list(result.scalars().all())
    provider = choose_preferred_identity_provider(
        providers,
        provider_type=provider_type,
        tenant_id=tenant_id,
    )

    # Ambiguity is a security failure, not a reason to silently fall back to a
    # different provider scope.
    if len(providers) > 1:
        return None

    # Fallback to global provider if tenant-scoped provider is not found and a tenant_id was specified
    if not provider and tenant_id is not None and allow_global_fallback:
        result = await db.execute(
            build_identity_provider_query(provider_type, None, is_active=is_active)
        )
        global_providers = list(result.scalars().all())
        provider = choose_preferred_identity_provider(
            global_providers,
            provider_type=provider_type,
            tenant_id=None,
        )
        if len(global_providers) > 1:
            return None

    return provider


async def get_login_identity_provider(
    db: AsyncSession,
    provider_type: AuthProviderType | str,
    tenant_id: str | None = None,
    *,
    allow_global_fallback: bool = False,
) -> IdentityProvider | None:
    """Return a provider that is explicitly enabled for authentication.

    Directory-only or disabled providers must never become login credentials by
    calling callback URLs directly. Tenant providers additionally require an
    active tenant whose SSO switch remains enabled.
    """
    provider = await get_preferred_identity_provider(
        db,
        provider_type,
        tenant_id,
        is_active=True,
        allow_global_fallback=allow_global_fallback,
    )
    if not provider or not provider.sso_login_enabled:
        return None

    if provider.tenant_id is not None:
        tenant = await db.get(Tenant, provider.tenant_id)
        if not tenant or not tenant.is_active or not tenant.sso_enabled:
            return None

    return provider


async def get_login_identity_provider_by_id(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID,
    provider_type: AuthProviderType | str,
    tenant_id: uuid.UUID | str | None,
    for_update: bool = False,
) -> IdentityProvider | None:
    """Revalidate the exact provider immediately before issuing a login."""
    try:
        expected_tenant_id = uuid.UUID(str(tenant_id)) if tenant_id is not None else None
    except (TypeError, ValueError):
        return None

    query = select(IdentityProvider).where(
            IdentityProvider.id == provider_id,
            IdentityProvider.provider_type == provider_type,
            IdentityProvider.is_active.is_(True),
            IdentityProvider.sso_login_enabled.is_(True),
        )
    if for_update:
        query = query.with_for_update()
    query = query.execution_options(populate_existing=True)
    result = await db.execute(query)
    provider = result.scalar_one_or_none()
    if not provider or provider.tenant_id != expected_tenant_id:
        return None

    if expected_tenant_id is not None:
        tenant_query = select(Tenant).where(Tenant.id == expected_tenant_id)
        if for_update:
            tenant_query = tenant_query.with_for_update()
        tenant_result = await db.execute(
            tenant_query.execution_options(populate_existing=True)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant or not tenant.is_active or not tenant.sso_enabled:
            return None
    return provider
