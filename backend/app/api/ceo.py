"""CEO orchestrator (P1 observer) HTTP boundary.

All endpoints are tenant-scoped and rollout-gated: when the CEO canary is
closed for the tenant, mutations return 403 and reads report
``feature_available=false``. Mutations require a company governor; the
company-brief read additionally accepts the enabling administrator.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.ceo import CeoOrchestratorSettings
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import is_company_governor
from app.services.ceo_briefing import (
    CeoBriefingError,
    build_company_brief_snapshot,
    ceo_orchestrator_allowed,
)
from app.services.ceo_orchestrator import (
    CEO_MEETING_KINDS,
    CeoOrchestratorError,
    disable_ceo_orchestrator,
    enable_ceo_orchestrator,
    get_ceo_settings,
    is_enabled_ceo_agent,
    start_ceo_meeting,
    update_ceo_settings,
)

router = APIRouter(prefix="/api", tags=["ceo"])


class CeoEnableIn(BaseModel):
    member_agent_ids: list[uuid.UUID] = Field(default_factory=list, max_length=12)
    briefing_enabled: bool = False
    morning_meeting_enabled: bool = False
    daily_credit_cap: int = Field(default=20, ge=0)
    monthly_credit_cap: int = Field(default=300, ge=0)


class CeoSettingsPatchIn(BaseModel):
    briefing_enabled: bool | None = None
    morning_meeting_enabled: bool | None = None
    daily_credit_cap: int | None = Field(default=None, ge=0)
    monthly_credit_cap: int | None = Field(default=None, ge=0)
    member_agent_ids: list[uuid.UUID] | None = Field(default=None, max_length=12)


def _settings_out(
    row: CeoOrchestratorSettings | None,
    *,
    feature_available: bool,
) -> dict:
    base = {
        "feature_available": feature_available,
        "configured": row is not None,
        "ceo_agent_id": None,
        "enabled": False,
        "enabled_by_user_id": None,
        "enabled_at": None,
        "briefing_enabled": False,
        "morning_meeting_enabled": False,
        "meeting_group_id": None,
        "daily_credit_cap": 20,
        "monthly_credit_cap": 300,
        "meeting_member_agent_ids": [],
    }
    if row is None:
        return base
    base.update(
        {
            "ceo_agent_id": str(row.ceo_agent_id),
            "enabled": bool(row.enabled),
            "enabled_by_user_id": (
                str(row.enabled_by_user_id) if row.enabled_by_user_id else None
            ),
            "enabled_at": row.enabled_at.isoformat() if row.enabled_at else None,
            "briefing_enabled": bool(row.briefing_enabled),
            "morning_meeting_enabled": bool(row.morning_meeting_enabled),
            "meeting_group_id": (
                str(row.meeting_group_id) if row.meeting_group_id else None
            ),
            "daily_credit_cap": row.daily_credit_cap,
            "monthly_credit_cap": row.monthly_credit_cap,
            "meeting_member_agent_ids": [
                str(value) for value in (row.meeting_member_agent_ids or [])
            ],
        }
    )
    return base


def _stage_audit(
    db: AsyncSession,
    *,
    current_user: User,
    action: str,
    tenant_id: uuid.UUID,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=current_user.id,
            action=action,
            details={
                "tenant_id": str(tenant_id),
                **(details or {}),
            },
        )
    )


def _tenant_id(current_user: User) -> uuid.UUID:
    if current_user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")
    return current_user.tenant_id


def _require_governor(current_user: User) -> None:
    if not is_company_governor(current_user):
        raise HTTPException(
            status_code=403,
            detail="Only company admins can manage the CEO orchestrator",
        )


async def _load_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return tenant


def _translate_ceo_error(exc: CeoOrchestratorError) -> HTTPException:
    status_by_code = {
        "ceo_orchestrator_not_available": status.HTTP_403_FORBIDDEN,
        "ceo_orchestrator_disabled": status.HTTP_403_FORBIDDEN,
        "ceo_budget_cap_exceeded": status.HTTP_402_PAYMENT_REQUIRED,
        "ceo_meeting_kind_invalid": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "ceo_meeting_member_invalid": status.HTTP_400_BAD_REQUEST,
        "ceo_meeting_member_limit": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(
        status_code=status_by_code.get(exc.code, status.HTTP_409_CONFLICT),
        detail={"code": exc.code, "message": str(exc)},
    )


@router.get("/companies/current/ceo/settings")
async def get_ceo_orchestrator_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    row = await get_ceo_settings(db, tenant_id)
    return _settings_out(
        row,
        feature_available=ceo_orchestrator_allowed(
            tenant_id=tenant_id,
            agent_id=row.ceo_agent_id if row else None,
        ),
    )


@router.post("/companies/current/ceo/enable")
async def enable_ceo(
    body: CeoEnableIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_governor(current_user)
    tenant = await _load_tenant(db, _tenant_id(current_user))
    try:
        row = await enable_ceo_orchestrator(
            db,
            tenant=tenant,
            admin=current_user,
            member_agent_ids=body.member_agent_ids,
            briefing_enabled=body.briefing_enabled,
            morning_meeting_enabled=body.morning_meeting_enabled,
            daily_credit_cap=body.daily_credit_cap,
            monthly_credit_cap=body.monthly_credit_cap,
        )
    except CeoOrchestratorError as exc:
        raise _translate_ceo_error(exc) from exc
    except IntegrityError:
        # Concurrent enable lost the ceo_agent_id uniqueness race; idempotent retry.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ceo_already_exists", "message": "CEO is being enabled concurrently; retry"},
        ) from None
    _stage_audit(
        db,
        current_user=current_user,
        action="ceo:enable",
        tenant_id=tenant.id,
        details={
            "ceo_agent_id": str(row.ceo_agent_id),
            "briefing_enabled": row.briefing_enabled,
            "morning_meeting_enabled": row.morning_meeting_enabled,
            "daily_credit_cap": row.daily_credit_cap,
            "monthly_credit_cap": row.monthly_credit_cap,
            "member_agent_ids": [str(value) for value in (row.meeting_member_agent_ids or [])],
        },
    )
    return _settings_out(row, feature_available=True)


@router.post("/companies/current/ceo/disable")
async def disable_ceo(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_governor(current_user)
    tenant_id = _tenant_id(current_user)
    row = await get_ceo_settings(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CEO orchestrator is not configured")
    await disable_ceo_orchestrator(db, settings=row)
    _stage_audit(
        db,
        current_user=current_user,
        action="ceo:disable",
        tenant_id=tenant_id,
        details={"ceo_agent_id": str(row.ceo_agent_id)},
    )
    return _settings_out(
        row,
        feature_available=ceo_orchestrator_allowed(
            tenant_id=tenant_id,
            agent_id=row.ceo_agent_id,
        ),
    )


@router.patch("/companies/current/ceo/settings")
async def patch_ceo_settings(
    body: CeoSettingsPatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_governor(current_user)
    tenant_id = _tenant_id(current_user)
    row = await get_ceo_settings(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CEO orchestrator is not configured")
    if not ceo_orchestrator_allowed(tenant_id=tenant_id, agent_id=row.ceo_agent_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "ceo_orchestrator_not_available", "message": "CEO rollout gate is closed"},
        )
    try:
        row = await update_ceo_settings(
            db,
            settings=row,
            actor=current_user,
            briefing_enabled=body.briefing_enabled,
            morning_meeting_enabled=body.morning_meeting_enabled,
            daily_credit_cap=body.daily_credit_cap,
            monthly_credit_cap=body.monthly_credit_cap,
            member_agent_ids=body.member_agent_ids,
        )
    except CeoOrchestratorError as exc:
        raise _translate_ceo_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="ceo:settings_update",
        tenant_id=tenant_id,
        details={
            "briefing_enabled": row.briefing_enabled,
            "morning_meeting_enabled": row.morning_meeting_enabled,
            "daily_credit_cap": row.daily_credit_cap,
            "monthly_credit_cap": row.monthly_credit_cap,
            "member_agent_ids": [str(value) for value in (row.meeting_member_agent_ids or [])],
        },
    )
    return _settings_out(row, feature_available=True)


async def _load_enabled_ceo_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Agent:
    result = await db.execute(
        select(Agent).where(
            Agent.id == agent_id,
            Agent.tenant_id == tenant_id,
            Agent.deleted_at.is_(None),
        )
    )
    agent = result.scalar_one_or_none()
    if agent is None or not await is_enabled_ceo_agent(db, agent):
        raise HTTPException(status_code=404, detail="Enabled CEO Agent not found for this company")
    return agent


def _require_governor_or_enabler(
    current_user: User,
    row: CeoOrchestratorSettings,
) -> None:
    if is_company_governor(current_user):
        return
    if row.enabled_by_user_id is not None and row.enabled_by_user_id == current_user.id:
        return
    raise HTTPException(status_code=403, detail="Admin or CEO enabler access is required")


@router.get("/agents/{agent_id}/company-brief")
async def get_company_brief(
    agent_id: uuid.UUID,
    window_hours: int = Query(default=168, ge=1, le=168),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    agent = await _load_enabled_ceo_agent(db, agent_id=agent_id, tenant_id=tenant_id)
    row = await get_ceo_settings(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CEO orchestrator is not configured")
    _require_governor_or_enabler(current_user, row)
    if not ceo_orchestrator_allowed(tenant_id=tenant_id, agent_id=agent.id):
        raise HTTPException(
            status_code=403,
            detail={"code": "ceo_orchestrator_not_available", "message": "CEO rollout gate is closed"},
        )
    try:
        snapshot = await build_company_brief_snapshot(
            db,
            tenant_id=tenant_id,
            viewer_user_id=current_user.id,
            window_hours=window_hours,
        )
    except CeoBriefingError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "markdown": snapshot.render_markdown(
            max_chars=get_settings().CEO_BRIEF_SNAPSHOT_MAX_CHARS
        ),
    }


@router.post("/agents/{agent_id}/meetings/{kind}/start")
async def start_meeting(
    agent_id: uuid.UUID,
    kind: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if kind not in CEO_MEETING_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"code": "ceo_meeting_kind_invalid", "message": f"kind must be one of {sorted(CEO_MEETING_KINDS)}"},
        )
    tenant_id = _tenant_id(current_user)
    await _load_enabled_ceo_agent(db, agent_id=agent_id, tenant_id=tenant_id)
    row = await get_ceo_settings(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CEO orchestrator is not configured")
    _require_governor_or_enabler(current_user, row)
    try:
        execution = await start_ceo_meeting(db, settings=row, actor=current_user, kind=kind)
    except CeoOrchestratorError as exc:
        raise _translate_ceo_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="ceo:meeting_start",
        tenant_id=tenant_id,
        details={
            "ceo_agent_id": str(row.ceo_agent_id),
            "kind": kind,
            "trigger_execution_id": str(execution.id),
            "meeting_group_id": str(row.meeting_group_id) if row.meeting_group_id else None,
        },
    )
    return {
        "trigger_execution_id": str(execution.id),
        "status": execution.status,
        "meeting_group_id": str(row.meeting_group_id) if row.meeting_group_id else None,
        "kind": kind,
    }
