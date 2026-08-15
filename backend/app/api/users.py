import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class UserQuotaUpdate(BaseModel):
    quota_message_limit: int | None = None
    quota_message_period: str | None = None
    quota_max_agents: int | None = None
    quota_agent_ttl_hours: int | None = None


class MembershipDeactivateRequest(BaseModel):
    acknowledge_responsibilities: bool = False


class UserOut(BaseModel):
    id: uuid.UUID
    # username/email/display_name can be None for SSO-created users whose Identity
    # was created without explicit values (e.g., DingTalk/Feishu OAuth flow).
    # The frontend should handle None gracefully.
    username: str | None = None
    email: str | None = None
    display_name: str | None = None
    role: str
    is_active: bool
    # Quota fields
    quota_message_limit: int
    quota_message_period: str
    quota_messages_used: int
    quota_max_agents: int
    quota_agent_ttl_hours: int
    # Computed
    agents_count: int = 0
    mfa_enabled: bool = False
    mfa_required: bool = False
    # Source info
    created_at: str | None = None
    source: str = 'registered'  # 'registered' | 'feishu' | 'dingtalk' | 'wecom' | etc.

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[UserOut])
async def list_users(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the specified tenant (admin only)."""
    if current_user.role not in ("org_owner", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    tid = str(current_user.tenant_id)

    # Filter users by tenant — platform_admins only shown in their own tenant
    result = await db.execute(
        select(User).options(selectinload(User.identity)).where(
            User.tenant_id == tid
        ).order_by(User.created_at.asc())
    )
    users = result.scalars().all()

    out = []
    for u in users:
        # Count non-expired agents
        count_result = await db.execute(
            select(func.count()).select_from(Agent).where(
                Agent.creator_id == u.id,
                Agent.is_expired.is_(False),
            )
        )
        agents_count = count_result.scalar() or 0

        user_dict = {
            "id": u.id,
            # Fallback to empty string if username/email/display_name is None to prevent
            # serialization errors for SSO-created users with incomplete Identity records.
            "username": u.username or u.email or f"{u.registration_source or 'user'}_{str(u.id)[:8]}",
            "email": u.email or "",
            "display_name": u.display_name or u.username or "",
            "role": u.role,
            "is_active": u.is_active,
            "quota_message_limit": u.quota_message_limit,
            "quota_message_period": u.quota_message_period,
            "quota_messages_used": u.quota_messages_used,
            "quota_max_agents": u.quota_max_agents,
            "quota_agent_ttl_hours": u.quota_agent_ttl_hours,
            "agents_count": agents_count,
            "mfa_enabled": bool(getattr(u.identity, "mfa_enabled", False)),
            "mfa_required": bool(
                getattr(u.identity, "is_platform_admin", False)
                or u.role in {"platform_admin", "org_owner", "org_admin"}
            ),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "source": (u.registration_source or 'registered'),
        }
        out.append(UserOut(**user_dict))
    return out


@router.patch("/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: uuid.UUID,
    data: UserQuotaUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's quota settings (admin only)."""
    if current_user.role not in ("org_owner", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    result = await db.execute(
        select(User).options(selectinload(User.identity)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    if data.quota_message_limit is not None:
        user.quota_message_limit = data.quota_message_limit
    if data.quota_message_period is not None:
        if data.quota_message_period not in ("permanent", "daily", "weekly", "monthly"):
            raise HTTPException(status_code=400, detail="Invalid period. Use: permanent, daily, weekly, monthly")
        user.quota_message_period = data.quota_message_period
    if data.quota_max_agents is not None:
        user.quota_max_agents = data.quota_max_agents
    if data.quota_agent_ttl_hours is not None:
        user.quota_agent_ttl_hours = data.quota_agent_ttl_hours

    await db.commit()
    await db.refresh(user)

    # Count agents
    count_result = await db.execute(
        select(func.count()).select_from(Agent).where(
            Agent.creator_id == user.id,
            Agent.is_expired.is_(False),
        )
    )
    agents_count = count_result.scalar() or 0

    return UserOut(
        id=user.id, username=user.username, email=user.email,
        display_name=user.display_name, role=user.role, is_active=user.is_active,
        quota_message_limit=user.quota_message_limit,
        quota_message_period=user.quota_message_period,
        quota_messages_used=user.quota_messages_used,
        quota_max_agents=user.quota_max_agents,
        quota_agent_ttl_hours=user.quota_agent_ttl_hours,
        agents_count=agents_count,
    )


# ─── Role Management ───────────────────────────────────

class RoleUpdate(BaseModel):
    role: str


@router.patch("/{user_id}/role")
async def update_user_role(
    user_id: uuid.UUID,
    data: RoleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a user's role within the same company.

    Permissions:
    - org_owner: may appoint/revoke org_admin within the same company.
    - org_admin: may normalize legacy member roles, but cannot appoint admins.
    - platform operator authority is global and never assigned through this API.

    Safety:
    - If the target is the ONLY remaining org_admin in the company,
      demoting them is blocked to prevent orphaned companies.
    """
    if current_user.role not in ("org_owner", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    # ``agent_admin`` is a migration-only legacy role. Agent delegation is an
    # object grant and no new tenant role may be assigned here.
    allowed_roles = ("member",)
    if current_user.role == "org_owner":
        allowed_roles = ("org_admin", "member")
    if data.role not in allowed_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Allowed: {', '.join(allowed_roles)}")

    # Find target user
    result = await db.execute(
        select(User).options(selectinload(User.identity)).where(User.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    if target_user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if target_user.id == tenant.owner_user_id or target_user.role == "org_owner":
        raise HTTPException(
            status_code=409,
            detail={"code": "owner_role_immutable", "message": "Use ownership transfer to change the company owner"},
        )

    # No-op shortcut
    if target_user.role == data.role:
        return {"status": "ok", "user_id": str(user_id), "role": data.role}

    target_user.role = data.role
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="organization_membership_role_changed",
            details={
                "tenant_id": str(current_user.tenant_id),
                "target_user_id": str(target_user.id),
                "role": data.role,
            },
        )
    )
    await db.commit()
    return {"status": "ok", "user_id": str(user_id), "role": data.role}


@router.get("/{user_id}/deactivation-preflight")
async def membership_deactivation_preflight(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Review a target membership without exposing its private Agent metadata."""
    if current_user.role not in {"org_owner", "org_admin"}:
        raise HTTPException(status_code=403, detail="Company governance access required")
    target = await db.get(User, user_id)
    if not target or target.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Company membership not found")
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if target.id == tenant.owner_user_id or target.role == "org_owner":
        raise HTTPException(
            status_code=409,
            detail={"code": "owner_cannot_be_deactivated", "message": "Transfer ownership before deactivation"},
        )
    if current_user.role == "org_admin" and target.role == "org_admin":
        raise HTTPException(status_code=403, detail="Only the company owner can deactivate an administrator")
    if target.id == current_user.id:
        raise HTTPException(status_code=409, detail="Use the leave-company flow for your own membership")

    from app.api.tenants import _tenant_leave_preflight

    preflight = await _tenant_leave_preflight(
        db,
        membership=target,
        tenant=tenant,
    )
    # Governance may see that a responsibility exists, but not the name or
    # stable object ID of a private Agent. Company/custom Agent metadata is a
    # company-governance surface and remains available for reassignment.
    preflight["owned_agents"] = [
        (
            {
                **item,
                "id": None,
                "name": "Private Agent",
            }
            if item["access_mode"] == "private"
            else item
        )
        for item in preflight["owned_agents"]
    ]
    # Task titles, approval actions and deliverable types may originate in the
    # member's private assistant. Governance receives counts for handoff
    # planning, never those private work details.
    preflight["open_tasks"] = []
    preflight["pending_approvals"] = []
    preflight["open_deliverables"] = []
    preflight["private_work_details_redacted"] = True
    preflight["action"] = "deactivate_membership"
    preflight["can_deactivate"] = True
    preflight["can_leave"] = False
    preflight["effects_on_deactivation"] = {
        "membership": "deactivated_immediately",
        "global_identity": "preserved",
        "private_agents": "preserved_and_inaccessible_to_governance",
        "existing_object_grants": "dormant_until_explicit_reactivation",
        "historical_tasks_and_artifacts": "preserved",
    }
    return preflight


@router.post("/{user_id}/deactivate")
async def deactivate_membership(
    user_id: uuid.UUID,
    body: MembershipDeactivateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate one tenant membership without disabling the global account."""
    if current_user.role not in {"org_owner", "org_admin"}:
        raise HTTPException(status_code=403, detail="Company governance access required")
    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    target = result.scalar_one_or_none()
    if not target or target.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Company membership not found")
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")
    if target.id == tenant.owner_user_id or target.role == "org_owner":
        raise HTTPException(
            status_code=409,
            detail={"code": "owner_cannot_be_deactivated", "message": "Transfer ownership before deactivation"},
        )
    if current_user.role == "org_admin" and target.role == "org_admin":
        raise HTTPException(status_code=403, detail="Only the company owner can deactivate an administrator")
    if target.id == current_user.id:
        raise HTTPException(status_code=409, detail="Use the leave-company flow for your own membership")
    if target.is_active:
        from app.api.tenants import _tenant_leave_preflight

        preflight = await _tenant_leave_preflight(
            db,
            membership=target,
            tenant=tenant,
            lock_owned_agents=True,
        )
        acknowledgement_required = bool(
            preflight["blockers"] or preflight["requires_acknowledgement"]
        )
        if acknowledgement_required and not (
            body and body.acknowledge_responsibilities
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "deactivation_responsibilities_acknowledgement_required",
                    "message": "Review and acknowledge the target membership responsibilities before deactivation",
                },
            )
        target.is_active = False
        db.add(
            AuditLog(
                tenant_id=tenant.id,
                user_id=current_user.id,
                action="organization_membership_deactivated",
                details={
                    "tenant_id": str(target.tenant_id),
                    "target_user_id": str(target.id),
                    "responsibility_summary": preflight["summary"],
                    "private_agent_metadata_redacted_from_governor": True,
                },
            )
        )
    await db.commit()
    return {"status": "deactivated"}


@router.post("/{user_id}/reactivate")
async def reactivate_membership(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reactivate a membership only through an explicit governance action."""
    if current_user.role not in {"org_owner", "org_admin"}:
        raise HTTPException(status_code=403, detail="Company governance access required")
    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    target = result.scalar_one_or_none()
    if not target or target.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Company membership not found")
    if target.role == "org_admin" and current_user.role != "org_owner":
        raise HTTPException(status_code=403, detail="Only the company owner can reactivate an administrator")
    if not target.is_active:
        target.is_active = True
        target.activation_pending_email_verification = False
        db.add(
            AuditLog(
                user_id=current_user.id,
                action="organization_membership_reactivated",
                details={"tenant_id": str(target.tenant_id), "target_user_id": str(target.id)},
            )
        )
    await db.commit()
    return {"status": "active"}
