from typing import Any, Sequence

from sqlalchemy import func, select

from app.dao.base import BaseDAO
from app.models.tenant import Tenant


class TenantDAO(BaseDAO[Tenant]):
    """DAO for Tenant model handling organization-scoped records."""

    def __init__(self) -> None:
        super().__init__(Tenant)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        """Find a tenant by its unique slug identifier."""
        async with self.session() as db:
            query = select(Tenant).where(Tenant.slug == slug)
            result = await db.execute(query)
            return result.scalar_one_or_none()

    async def get_by_ids(self, ids: Sequence[Any]) -> Sequence[Tenant]:
        """Find multiple tenants by a list of their IDs."""
        if not ids:
            return []
        async with self.session() as db:
            query = select(Tenant).where(Tenant.id.in_(ids))
            result = await db.execute(query)
            return result.scalars().all()

    async def get_by_sso_domain(self, domain: str) -> Tenant | None:
        """Find one active tenant whose exact SSO origin host is the email domain."""
        from app.services.platform_service import platform_service

        candidates = platform_service.sso_origin_candidates_for_email_domain(domain)
        if not candidates:
            return None
        async with self.session() as db:
            result = await db.execute(
                select(Tenant).where(
                    func.lower(Tenant.sso_domain).in_(candidates),
                    Tenant.is_active.is_(True),
                )
            )
            tenants = list(result.scalars().all())
            return tenants[0] if len(tenants) == 1 else None


tenant_dao = TenantDAO()
