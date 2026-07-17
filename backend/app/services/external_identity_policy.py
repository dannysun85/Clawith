"""Fail-closed ownership policy for SSO, directory, and channel identities.

Provider profile email and phone fields are useful directory metadata, but they
are not automatically proof that the caller owns an existing global
``Identity``.  External entry points therefore resolve accounts only through a
provider-scoped stable subject (stored on ``OrgMember``).  When no explicit
link exists, they create an isolated passwordless Identity; merging with an
existing account requires the authenticated identity-bind flow.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Identity


def require_stable_external_subject(
    provider_type: str,
    provider_subject: object,
) -> str:
    """Return a normalized provider subject or reject unsafe lazy creation."""
    subject = str(provider_subject or "").strip()
    if not subject:
        raise ValueError(
            f"{provider_type or 'external'} authentication did not return a stable subject"
        )
    return subject


def external_user_can_authenticate(user: object | None) -> bool:
    """Require both tenant membership and global Identity to remain active."""
    identity = getattr(user, "identity", None)
    return bool(
        user
        and getattr(user, "is_active", False)
        and identity
        and getattr(identity, "is_active", False)
    )


async def acquire_external_subject_lock(
    db: AsyncSession,
    *,
    provider_type: str,
    tenant_id: object | None,
    provider_subject: object,
) -> None:
    """Serialize provider-subject find/create operations in PostgreSQL.

    OrgMember predates a database uniqueness constraint across its three legacy
    provider-ID columns. A transaction-scoped advisory lock closes the
    check-then-create race across application workers without guessing how to
    merge historical ambiguous rows. Non-PostgreSQL engines skip the
    PostgreSQL-specific primitive.
    """
    subject = require_stable_external_subject(provider_type, provider_subject)
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        return

    scope = f"external-subject:{provider_type}:{tenant_id or 'global'}:{subject}"
    digest = hashlib.sha256(scope.encode("utf-8")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


async def create_isolated_external_identity(
    db: AsyncSession,
    *,
    provider_type: str,
    provider_subject: object,
) -> Identity:
    """Create a passwordless Identity without trusting provider contact claims.

    A random suffix deliberately prevents username collision from becoming an
    implicit account-link operation.  Claimed email/phone values stay on the
    provider-scoped ``OrgMember`` until an authenticated user explicitly binds
    the provider or sets and verifies their own global contact information.
    """
    subject = require_stable_external_subject(provider_type, provider_subject)
    provider_slug = re.sub(r"[^a-z0-9]+", "_", provider_type.lower()).strip("_")
    subject_slug = re.sub(r"[^a-zA-Z0-9]+", "", subject)[:24].lower()
    username = (
        f"{provider_slug or 'external'}_{subject_slug or 'user'}_"
        f"{uuid.uuid4().hex[:12]}"
    )[:100]
    identity = Identity(
        email=None,
        phone=None,
        username=username,
        password_hash=None,
        password_login_enabled=False,
        is_active=True,
        is_platform_admin=False,
        email_verified=False,
    )
    db.add(identity)
    await db.flush()
    return identity
