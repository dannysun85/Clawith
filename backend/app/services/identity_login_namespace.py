"""One transaction-wide policy for global login identifier mutations."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_canonicalization import (
    normalize_username,
    username_looks_like_contact,
)
from app.dao import identity_dao


_LOGIN_NAMESPACE_ADVISORY_LOCK_KEY = 0x4153545241524547


async def acquire_identity_login_namespace_lock(db: AsyncSession) -> None:
    """Serialize cross-column login-key writes across production workers."""

    bind = db.get_bind() if hasattr(db, "get_bind") else None
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _LOGIN_NAMESPACE_ADVISORY_LOCK_KEY},
        )


def normalize_safe_username(username: object | None) -> str:
    """Normalize a username while keeping email/phone ownership claims distinct."""

    normalized = normalize_username(username)
    if not normalized:
        raise HTTPException(status_code=400, detail="Username is required")
    if len(normalized) > 100:
        raise HTTPException(status_code=400, detail="Username is too long")
    if username_looks_like_contact(normalized):
        raise HTTPException(
            status_code=400,
            detail="Username cannot be an email address or phone number",
        )
    return normalized


async def validate_identity_login_namespace(
    *,
    username: str | None,
    email: str | None = None,
    phone: str | None = None,
    owned_identity_id: uuid.UUID | None = None,
) -> None:
    """Reject aliases that shadow another identity's ownership claim."""

    def belongs_to_other(identity) -> bool:
        return bool(identity and identity.id != owned_identity_id)

    if username:
        for identity in (
            await identity_dao.get_by_username(username),
            await identity_dao.get_by_email(username),
            await identity_dao.get_by_phone(username),
        ):
            if belongs_to_other(identity):
                raise HTTPException(status_code=409, detail="Username already taken")

    if email and belongs_to_other(await identity_dao.get_by_username(email)):
        raise HTTPException(
            status_code=409,
            detail="Email conflicts with an existing username",
        )
    if email and belongs_to_other(await identity_dao.get_by_email(email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    if phone and belongs_to_other(await identity_dao.get_by_username(phone)):
        raise HTTPException(
            status_code=409,
            detail="Mobile conflicts with an existing username",
        )
    if phone and belongs_to_other(await identity_dao.get_by_phone(phone)):
        raise HTTPException(status_code=409, detail="Mobile already registered")
