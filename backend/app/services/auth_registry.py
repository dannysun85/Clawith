"""Authentication provider registry and factory.

This module provides a centralized way to manage and instantiate auth providers.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dao import identity_provider_dao
from app.models.identity import IdentityProvider
from app.services.auth_provider import (
    PROVIDER_CLASSES,
    BaseAuthProvider,
)
from app.services.identity_provider_lookup import (
    get_login_identity_provider,
    get_preferred_identity_provider,
)


class AuthProviderRegistry:
    """Registry for managing authentication provider instances.

    This class provides a factory method to create provider instances
    and caches them for reuse.
    """

    def __init__(self):
        self._cache: dict[str, BaseAuthProvider] = {}

    async def get_provider(
        self,
        provider_type: str,
        tenant_id: str | None = None,
        *,
        require_sso_login: bool = False,
        allow_global_fallback: bool = True,
    ) -> BaseAuthProvider | None:
        """Get or create an authentication provider instance.

        Args:
            provider_type: The type of provider (feishu, dingtalk, etc.)
            tenant_id: Optional tenant ID for tenant-specific providers

        Returns:
            Provider instance or None if provider type is not supported
        """
        # Login authorization is a revocation boundary. Never reuse a cached
        # allow decision: provider and tenant switches must take effect across
        # every worker on the very next request.
        cache_key = (
            f"{provider_type}:{tenant_id or 'global'}:"
            f"login={int(require_sso_login)}:fallback={int(allow_global_fallback)}"
        )
        if not require_sso_login and cache_key in self._cache:
            return self._cache[cache_key]

        # Try to get provider config from database
        async with identity_provider_dao.session() as db:
            if require_sso_login:
                provider_model = await get_login_identity_provider(
                    db,
                    provider_type,
                    tenant_id,
                    allow_global_fallback=allow_global_fallback,
                )
            else:
                provider_model = await get_preferred_identity_provider(
                    db,
                    provider_type,
                    tenant_id,
                    is_active=True,
                    allow_global_fallback=allow_global_fallback,
                )

        if require_sso_login and provider_model is None:
            return None

        # Create provider instance
        provider = self._create_provider(provider_type, provider_model)
        if provider and not require_sso_login:
            self._cache[cache_key] = provider

        return provider

    def _create_provider(
        self, provider_type: str, provider_model: IdentityProvider | None
    ) -> BaseAuthProvider | None:
        """Create a provider instance based on type.

        Args:
            provider_type: The type of provider
            provider_model: Optional IdentityProvider model from database

        Returns:
            Provider instance or None
        """
        provider_class = PROVIDER_CLASSES.get(provider_type)
        if not provider_class:
            return None

        config = provider_model.config if provider_model else {}
        return provider_class(provider=provider_model, config=config)

    async def list_providers(
        self, tenant_id: str | None = None
    ) -> list[IdentityProvider]:
        """List all available identity providers.

        Args:
            tenant_id: Optional tenant ID to filter by

        Returns:
            List of IdentityProvider records
        """
        async with identity_provider_dao.session() as db:
            query = select(IdentityProvider).where(IdentityProvider.is_active.is_(True))
            query = query.where(IdentityProvider.sso_login_enabled.is_(True))

            if tenant_id:
                # Only include tenant-specific ones
                query = query.where(IdentityProvider.tenant_id == tenant_id)
            else:
                # Public OAuth login should only expose global providers.
                query = query.where(IdentityProvider.tenant_id.is_(None))

            result = await db.execute(query)
            return list(result.scalars().all())

    async def create_provider(
        self,
        db: AsyncSession,
        provider_type: str,
        name: str,
        config: dict[str, Any],
        tenant_id: str | None = None,
    ) -> IdentityProvider:
        """Create a new identity provider.

        Args:
            db: Database session
            provider_type: Type of provider
            name: Display name
            config: Provider configuration
            tenant_id: Optional tenant ID for tenant-specific provider

        Returns:
            Created IdentityProvider record
        """
        provider = IdentityProvider(
            provider_type=provider_type,
            name=name,
            is_active=True,
            config=config,
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()

        # Clear cache for this provider type
        self._clear_cache(provider_type)

        return provider

    async def update_provider(
        self,
        db: AsyncSession,
        provider_id: str,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> IdentityProvider | None:
        """Update an existing identity provider.

        Args:
            db: Database session
            provider_id: Provider ID
            name: New display name
            config: New configuration
            is_active: New active status

        Returns:
            Updated IdentityProvider or None if not found
        """
        result = await db.execute(
            select(IdentityProvider).where(IdentityProvider.id == provider_id)
        )
        provider = result.scalar_one_or_none()

        if not provider:
            return None

        if name is not None:
            provider.name = name
        if config is not None:
            provider.config = config
        if is_active is not None:
            provider.is_active = is_active

        await db.flush()

        # Clear cache
        self._clear_cache(provider.provider_type)

        return provider

    async def delete_provider(self, db: AsyncSession, provider_id: str) -> bool:
        """Delete an identity provider.

        Args:
            db: Database session
            provider_id: Provider ID

        Returns:
            True if deleted, False if not found
        """
        result = await db.execute(
            select(IdentityProvider).where(IdentityProvider.id == provider_id)
        )
        provider = result.scalar_one_or_none()

        if not provider:
            return False

        provider_type = provider.provider_type
        await db.delete(provider)
        await db.flush()

        # Clear cache
        self._clear_cache(provider_type)

        return True

    def _clear_cache(self, provider_type: str):
        """Clear cached provider instances for a type."""
        keys_to_delete = [k for k in self._cache if k.startswith(f"{provider_type}:")]
        for key in keys_to_delete:
            del self._cache[key]

    def clear_all_cache(self):
        """Clear all cached provider instances."""
        self._cache.clear()


# Global registry instance
auth_provider_registry = AuthProviderRegistry()
