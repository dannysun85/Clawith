"""Organization management API routes (users only)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_company_governor
from app.core.identity_canonicalization import canonicalize_email, canonicalize_phone
from app.database import get_db
from app.models.user import User, Identity
from app.schemas.schemas import UserOut, UserUpdate
from app.services.identity_login_namespace import (
    acquire_identity_login_namespace_lock,
    normalize_safe_username,
    validate_identity_login_namespace,
)

from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/org", tags=["organization"])


# ─── Users Management ──────────────────────────────────

@router.get("/users", response_model=list[UserOut])
async def list_users(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """List users, optionally filtered by tenant."""
    await get_company_governor(current_user)
    query = (
        select(User)
        .options(selectinload(User.identity))
        .where(User.is_active.is_(True))
    )

    target_tenant_id = current_user.tenant_id
    if tenant_id is not None and tenant_id != target_tenant_id:
        raise HTTPException(status_code=403, detail="Cannot list users outside your organization")
    query = query.where(User.tenant_id == target_tenant_id)

    query = query.order_by(User.display_name)
    result = await db.execute(query)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.patch("/users/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    current_user: User = Depends(get_company_governor),
    db: AsyncSession = Depends(get_db),
):
    """Admin update user profile."""
    await get_company_governor(current_user)
    result = await db.execute(
        select(User)
        .options(selectinload(User.identity))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    update_data = data.model_dump(exclude_unset=True)
    if "email" in update_data and update_data["email"] is not None:
        update_data["email"] = canonicalize_email(update_data["email"])
    if "username" in update_data and update_data["username"] is not None:
        update_data["username"] = normalize_safe_username(update_data["username"])
    if "primary_mobile" in update_data and update_data["primary_mobile"]:
        update_data["primary_mobile"] = canonicalize_phone(
            update_data["primary_mobile"]
        )

    global_identity_fields = {"email", "username", "primary_mobile"} & set(update_data)
    locked_identity = None
    changed_global_fields: set[str] = set()
    if global_identity_fields:
        await acquire_identity_login_namespace_lock(db)
        locked_result = await db.execute(
            select(Identity)
            .where(Identity.id == user.identity_id)
            .with_for_update()
        )
        locked_identity = locked_result.scalar_one_or_none()
        if not locked_identity:
            raise HTTPException(status_code=404, detail="Identity not found")
        current_values = {
            "email": locked_identity.email,
            "username": locked_identity.username,
            "primary_mobile": locked_identity.phone,
        }
        changed_global_fields = {
            field
            for field in global_identity_fields
            if update_data[field] != current_values[field]
        }

    if changed_global_fields:
        raise HTTPException(
            status_code=403,
            detail="Company administrators cannot modify global login identity fields",
        )

    email_changed = "email" in changed_global_fields
    username_changed = "username" in changed_global_fields
    phone_changed = "primary_mobile" in changed_global_fields
    if email_changed and update_data["email"] is None:
        raise HTTPException(status_code=400, detail="Email cannot be cleared")
    if username_changed and update_data["username"] is None:
        raise HTTPException(status_code=400, detail="Username cannot be cleared")

    if changed_global_fields:
        await validate_identity_login_namespace(
            username=update_data.get("username", locked_identity.username),
            email=update_data.get("email", locked_identity.email),
            phone=update_data.get("primary_mobile", locked_identity.phone),
            owned_identity_id=user.identity_id,
        )

    for field, value in update_data.items():
        if field in {"email", "username", "primary_mobile"}:
            continue
        setattr(user, field, value)

    if email_changed:
        from app.services.email_verification_service import email_verification_service
        from app.services.password_reset_service import invalidate_password_reset_tokens

        await email_verification_service.invalidate_email_verification_tokens(
            user.identity_id
        )
        await invalidate_password_reset_tokens(user.identity_id)
        locked_identity.email = update_data["email"]
        locked_identity.email_verified = False
        user.identity = locked_identity
    if locked_identity is not None:
        try:
            if username_changed:
                locked_identity.username = update_data["username"]
            if phone_changed:
                locked_identity.phone = update_data["primary_mobile"]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if changed_global_fields:
            locked_identity.auth_version = int(locked_identity.auth_version or 0) + 1
        user.identity = locked_identity
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Login or recovery field is already in use",
        ) from exc

    # Sync email/phone to OrgMember if changed
    if email_changed or phone_changed:
        from app.services.registration_service import registration_service
        await registration_service.sync_org_member_contact_from_user(
            user,
            sync_email=email_changed,
            sync_phone=phone_changed,
        )

    return UserOut.model_validate(user)
