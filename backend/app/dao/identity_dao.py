from sqlalchemy import func, select

from app.core.identity_canonicalization import (
    canonicalize_email,
    canonicalize_phone,
    normalize_username,
)
from app.dao.base import BaseDAO
from app.models.user import Identity


class IdentityDAO(BaseDAO[Identity]):
    """DAO for Identity model handling authentication credentials."""

    def __init__(self) -> None:
        super().__init__(Identity)

    async def get_by_login_identifier(self, identifier: str) -> Identity | None:
        """Resolve a login key deterministically without cross-column fan-out.

        Historical data can contain one row whose username equals another
        row's email or phone. Email and phone are ownership claims and therefore
        take precedence over the display/login alias. Separate bounded queries
        avoid ``MultipleResultsFound`` and keep the authoritative account
        reachable while new cross-namespace conflicts are rejected on writes.
        """
        normalized_identifier = str(identifier or "").strip()
        if not normalized_identifier:
            return None

        canonical_email = canonicalize_email(normalized_identifier)
        async with self.session() as db:
            if canonical_email is not None:
                result = await db.execute(
                    select(Identity)
                    .where(func.lower(Identity.email) == canonical_email)
                    .limit(1)
                )
                identity = result.scalar_one_or_none()
                if identity is not None:
                    return identity

            normalized_phone = canonicalize_phone(normalized_identifier)
            if normalized_phone:
                result = await db.execute(
                    select(Identity)
                    .where(Identity.phone == normalized_phone)
                    .limit(1)
                )
                identity = result.scalar_one_or_none()
                if identity is not None:
                    return identity

            result = await db.execute(
                select(Identity)
                .where(Identity.username == normalize_username(normalized_identifier))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Identity | None:
        """Find identity by email address."""
        canonical_email = canonicalize_email(email)
        if canonical_email is None:
            return None
        async with self.session() as db:
            query = select(Identity).where(func.lower(Identity.email) == canonical_email)
            result = await db.execute(query)
            return result.scalar_one_or_none()

    async def get_for_update(self, identity_id) -> Identity | None:
        """Lock one Identity row for ownership-sensitive state changes."""
        async with self.session() as db:
            query = (
                select(Identity)
                .where(Identity.id == identity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Identity | None:
        """Find identity by username."""
        normalized = normalize_username(username)
        if normalized is None:
            return None
        async with self.session() as db:
            query = select(Identity).where(Identity.username == normalized)
            result = await db.execute(query)
            return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Identity | None:
        """Find identity by normalized phone number."""
        normalized = canonicalize_phone(phone)
        if normalized is None:
            return None
        async with self.session() as db:
            query = select(Identity).where(Identity.phone == normalized)
            result = await db.execute(query)
            return result.scalar_one_or_none()

    async def is_username_taken(self, username: str) -> bool:
        """Return True if the username is already used by another identity."""
        normalized = normalize_username(username)
        if normalized is None:
            return False
        async with self.session() as db:
            result = await db.execute(
                select(Identity.id).where(Identity.username == normalized).limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def create_identity(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        username: str | None = None,
        password_hash: str | None = None,
        is_platform_admin: bool = False,
        email_verified: bool = False,
    ) -> Identity:
        """Create and flush a new Identity row."""
        normalized_email = canonicalize_email(email)
        normalized_phone = canonicalize_phone(phone)
        async with self.session() as db:
            identity = Identity(
                email=normalized_email,
                phone=normalized_phone,
                username=normalize_username(username),
                password_hash=password_hash,
                password_login_enabled=bool(password_hash),
                is_platform_admin=is_platform_admin,
                email_verified=email_verified,
            )
            db.add(identity)
            await db.flush()
            return identity


identity_dao = IdentityDAO()
