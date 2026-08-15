"""Platform Admin company management API.

Provides endpoints for platform admins to manage companies, view stats,
and control platform-level settings.
"""

import asyncio
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func as sqla_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_platform_operator
from app.core.secret_detection import looks_like_secret
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.identity_governance import RegistrationGrant
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.user import User, Identity
from app.services.subscription_lifecycle import ensure_free_subscription_for_tenant
from app.services.system_setting_security import strict_system_setting_enabled
from app.services.tenant_purge import (
    TenantPurgeError,
    create_tenant_purge_hold,
    dry_run_tenant_purge,
    list_tenant_purge_states,
    release_tenant_purge_hold,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Schemas ────────────────────────────────────────────

class CompanyStats(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    created_at: datetime | None = None
    user_count: int = 0
    agent_count: int = 0
    agent_running_count: int = 0
    total_tokens: int = 0
    cache_read_tokens_total: int = 0
    org_admin_email: str | None = None


class CompanyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    owner_email: EmailStr


class AgentBayCleanupReconcileRequest(BaseModel):
    provider_session_id: str = Field(min_length=1, max_length=200)
    provider_deleted_out_of_band: bool = False
    verification_note: str = Field(default="", max_length=500)


class CompanyCreateResponse(BaseModel):
    company: CompanyStats
    admin_invitation_code: str


class PlatformSettingsOut(BaseModel):
    allow_self_create_company: bool = True
    invitation_code_enabled: bool = True
    sso_custom_domain_redirect_enabled: bool = False


class PlatformSettingsUpdate(BaseModel):
    allow_self_create_company: bool | None = None
    invitation_code_enabled: bool | None = None
    sso_custom_domain_redirect_enabled: bool | None = None


class RegistrationCodeCreateRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=100)
    max_uses: int = Field(default=1, ge=1, le=10000)


class RegistrationCodeOut(BaseModel):
    id: uuid.UUID
    code: str
    max_uses: int
    used_count: int
    is_active: bool
    created_at: datetime | None = None


class RegistrationCodeListOut(BaseModel):
    items: list[RegistrationCodeOut]
    total: int
    page: int
    page_size: int


class RegistrationCodeCreateResponse(BaseModel):
    created: int
    codes: list[str]


class TenantPurgeHoldRequest(BaseModel):
    hold_type: Literal["legal", "operations"]
    reason_code: str = Field(min_length=3, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]+$")


class TenantPurgeHoldReleaseRequest(BaseModel):
    reason_code: str = Field(min_length=3, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]+$")


def _raise_tenant_purge_http(exc: TenantPurgeError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


# ─── Expired Tenant Purge Operations ──────────────────

@router.get("/tenant-deletions")
async def get_tenant_deletions(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Return scheduled purge work and non-sensitive completion receipts."""
    return await list_tenant_purge_states(db)


@router.post("/tenant-deletions/{tenant_id}/dry-run")
async def run_tenant_deletion_dry_run(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Plan and audit a due tenant purge without deleting any data."""
    try:
        return await dry_run_tenant_purge(
            db,
            tenant_id,
            actor_user_id=current_user.id,
        )
    except TenantPurgeError as exc:
        _raise_tenant_purge_http(exc)


@router.post("/tenant-deletions/{tenant_id}/holds", status_code=201)
async def add_tenant_deletion_hold(
    tenant_id: uuid.UUID,
    body: TenantPurgeHoldRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Create one idempotent legal or operational purge hold."""
    try:
        return await create_tenant_purge_hold(
            db,
            tenant_id,
            hold_type=body.hold_type,
            reason_code=body.reason_code,
            actor_user_id=current_user.id,
            actor_identity_id=current_user.identity_id,
        )
    except TenantPurgeError as exc:
        _raise_tenant_purge_http(exc)


@router.post("/tenant-deletions/{tenant_id}/holds/{hold_id}/release")
async def remove_tenant_deletion_hold(
    tenant_id: uuid.UUID,
    hold_id: uuid.UUID,
    body: TenantPurgeHoldReleaseRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Release a purge hold while retaining its audit trail."""
    try:
        return await release_tenant_purge_hold(
            db,
            tenant_id,
            hold_id,
            reason_code=body.reason_code,
            actor_user_id=current_user.id,
            actor_identity_id=current_user.identity_id,
        )
    except TenantPurgeError as exc:
        _raise_tenant_purge_http(exc)


# ─── Company Management ────────────────────────────────

@router.get("/companies", response_model=list[CompanyStats])
async def list_companies(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """List all companies with stats."""
    tenants = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    result = []

    for tenant in tenants.scalars().all():
        tid = tenant.id

        # User count
        uc = await db.execute(
            select(sqla_func.count()).select_from(User).where(User.tenant_id == tid)
        )
        user_count = uc.scalar() or 0

        # Agent count
        ac = await db.execute(
            select(sqla_func.count()).select_from(Agent).where(Agent.tenant_id == tid)
        )
        agent_count = ac.scalar() or 0

        # Running agents
        rc = await db.execute(
            select(sqla_func.count()).select_from(Agent).where(
                Agent.tenant_id == tid, Agent.status == "running"
            )
        )
        agent_running = rc.scalar() or 0

        # Total tokens
        tc = await db.execute(
            select(
                sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0),
                sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_total), 0),
            ).where(
                Agent.tenant_id == tid
            )
        )
        total_tokens, cache_read_tokens_total = tc.one()

        # Org Admin Email (first found if multiple)
        admin_q = await db.execute(
            select(Identity.email)
            .join(User, Identity.id == User.identity_id)
            .where(User.tenant_id == tid, User.role == "org_admin")
            .order_by(User.created_at.asc())
            .limit(1)
        )
        org_admin_email = admin_q.scalar()

        result.append(CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            sso_enabled=tenant.sso_enabled,
            sso_domain=tenant.sso_domain,
            created_at=tenant.created_at,
            user_count=user_count,
            agent_count=agent_count,
            agent_running_count=agent_running,
            total_tokens=total_tokens,
            cache_read_tokens_total=cache_read_tokens_total,
            org_admin_email=org_admin_email,
        ))

    return result


@router.post("/companies", response_model=CompanyCreateResponse, status_code=201)
async def create_company(
    data: CompanyCreateRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Create an ownerless tenant plus an email-bound owner invitation."""
    import re

    company_name = data.name.strip()
    if looks_like_secret(company_name):
        raise HTTPException(
            status_code=400,
            detail="Company name looks like a credential. Rotate the credential if it was pasted here, then enter a public company name.",
        )

    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")[:40]
    if not slug:
        slug = "company"
    slug = f"{slug}-{secrets.token_hex(3)}"

    tenant = Tenant(
        name=company_name,
        slug=slug,
        im_provider="web_only",
        owner_resolution_required=True,
    )
    db.add(tenant)
    await db.flush()
    await ensure_free_subscription_for_tenant(
        db,
        tenant.id,
        granted_by=current_user.id,
    )

    from app.services.identity_governance import issue_organization_invitation

    issued = await issue_organization_invitation(
        db,
        tenant_id=tenant.id,
        target_email=str(data.owner_email),
        invited_role="org_owner",
        invited_by_user_id=current_user.id,
    )
    code_str = issued.raw_token
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="tenant_created",
            details={
                "tenant_id": str(tenant.id),
                "source": "platform_operator",
                "owner_invitation_id": str(issued.record.id),
                "owner_email": issued.record.target_email,
            },
        )
    )

    return CompanyCreateResponse(
        company=CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
        ),
        admin_invitation_code=code_str,
    )


@router.put("/companies/{company_id}/toggle")
async def toggle_company(
    company_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable a company."""
    # Authorization fences acquire Agent rows before Tenant.  Mutations must
    # use the same global order so a disable operation cannot deadlock an
    # in-flight chat, trigger, A2A, or AgentBay side effect.
    agents_result = await db.execute(
        select(Agent)
        .where(Agent.tenant_id == company_id)
        .order_by(Agent.id)
        .with_for_update()
    )
    company_agents = list(agents_result.scalars().all())
    result = await db.execute(
        select(Tenant).where(Tenant.id == company_id).with_for_update()
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    new_state = not tenant.is_active
    tenant.is_active = new_state

    # When disabling: pause all running agents
    if not new_state:
        for agent in company_agents:
            if agent.status in {"running", "idle"}:
                agent.status = "paused"

    await db.flush()
    return {"ok": True, "is_active": new_state}


@router.get("/agentbay/cleanup-required")
async def list_agentbay_cleanup_required(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Release preflight and operator queue for unconfirmed provider cleanup."""

    from app.models.agentbay_session import (
        AGENTBAY_PROVIDER_COLLISION_STATUS,
        AgentBaySessionLedger,
    )

    result = await db.execute(
        select(AgentBaySessionLedger)
        .where(
            AgentBaySessionLedger.status.in_(
                ["cleanup_required", AGENTBAY_PROVIDER_COLLISION_STATUS]
            )
        )
        .order_by(
            AgentBaySessionLedger.agent_id,
            AgentBaySessionLedger.image_type,
            AgentBaySessionLedger.started_at,
            AgentBaySessionLedger.id,
        )
    )
    rows = list(result.scalars().all())
    return {
        "count": len(rows),
        "release_blocked": bool(rows),
        "items": [
            {
                "id": str(row.id),
                "tenant_id": str(row.tenant_id) if row.tenant_id else None,
                "agent_id": str(row.agent_id) if row.agent_id else None,
                "user_id": str(row.user_id) if row.user_id else None,
                "chat_session_id": row.chat_session_id,
                "provider_session_id": row.provider_session_id,
                "provider_identity_collision_ledger_id": (
                    (row.context or {}).get(
                        "provider_identity_collision_ledger_id"
                    )
                    if isinstance(row.context, dict)
                    else None
                ),
                "image_type": row.image_type,
                "status": row.status,
                "reason": row.close_reason,
                "started_at": row.started_at,
            }
            for row in rows
        ],
    }


async def _reconcile_agentbay_provider_collision_group(
    ledger,
    body: AgentBayCleanupReconcileRequest,
    *,
    current_user: User,
    db: AsyncSession,
):
    """Close one collision group only after the provider UUID is absent."""

    from app.models.agentbay_session import (
        AGENTBAY_PROVIDER_COLLISION_STATUS,
        AgentBaySessionLedger,
    )
    from app.models.audit import AuditLog
    from app.services.agentbay_client import _lock_agentbay_provider_identity

    context = ledger.context if isinstance(ledger.context, dict) else {}
    raw_group_id = context.get("provider_identity_collision_ledger_id") or str(
        ledger.id
    )
    try:
        group_id = uuid.UUID(str(raw_group_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Provider collision group identity is invalid",
        ) from exc

    if not body.provider_deleted_out_of_band:
        raise HTTPException(
            status_code=409,
            detail=(
                "A provider identity collision cannot be attached automatically; "
                "verify the exact provider session out of band"
            ),
        )
    verification_note = body.verification_note.strip()
    if len(verification_note) < 20:
        raise HTTPException(
            status_code=422,
            detail=(
                "An out-of-band provider verification note of at least 20 "
                "characters is required"
            ),
        )

    requested_ledger_id = ledger.id
    provider_session_id = body.provider_session_id
    await db.rollback()

    # Lock the provider identity before any row lookup, then reload both the
    # canonical keeper and the complete JSON-linked group in this transaction.
    # Quarantine uses the same advisory-lock namespace, so no new group member
    # can be inserted between these reads and the closing commit.
    await _lock_agentbay_provider_identity(db, provider_session_id)
    keeper = (
        await db.execute(
            select(AgentBaySessionLedger)
            .where(AgentBaySessionLedger.id == group_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        keeper is None
        or keeper.status != AGENTBAY_PROVIDER_COLLISION_STATUS
        or not keeper.provider_session_id
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider collision group is incomplete",
        )
    if provider_session_id != keeper.provider_session_id:
        raise HTTPException(
            status_code=409,
            detail="Provider session confirmation mismatch",
        )

    group_pointer = AgentBaySessionLedger.context[
        "provider_identity_collision_ledger_id"
    ].as_string()
    locked_rows = list(
        (
            await db.execute(
                select(AgentBaySessionLedger)
                .where(
                    AgentBaySessionLedger.status
                    == AGENTBAY_PROVIDER_COLLISION_STATUS,
                    or_(
                        AgentBaySessionLedger.id == group_id,
                        group_pointer == str(group_id),
                    ),
                )
                .order_by(AgentBaySessionLedger.id)
                .with_for_update()
            )
        ).scalars().all()
    )
    matching_keepers = [
        row
        for row in locked_rows
        if row.id == group_id and row.provider_session_id == provider_session_id
    ]
    if (
        len(matching_keepers) != 1
        or requested_ledger_id not in {row.id for row in locked_rows}
        or any(
        row.status != AGENTBAY_PROVIDER_COLLISION_STATUS
        or str(
            (
                row.context
                if isinstance(row.context, dict)
                else {}
            ).get("provider_identity_collision_ledger_id")
            or row.id
        )
        != str(group_id)
        for row in locked_rows
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider collision group changed during reconciliation",
        )

    now = datetime.now(timezone.utc)
    for row in locked_rows:
        row.status = "closed"
        row.close_reason = "provider_identity_collision_verified_absent"
        row.error_message = None
        row.closed_at = now
        row.context = {
            **(row.context if isinstance(row.context, dict) else {}),
            "reconciled_at": now.isoformat(),
            "reconciled_by_user_id": str(current_user.id),
            "reconciliation_mode": "operator_confirmed_absent",
            "verification_note": verification_note,
        }
    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=keeper.agent_id,
            action="agentbay:provider_collision_reconciled",
            details={
                "collision_group_id": str(group_id),
                "claim_count": len(locked_rows),
                "mode": "operator_confirmed_absent",
            },
        )
    )
    await db.commit()
    return {
        "status": "closed",
        "mode": "operator_confirmed_absent",
        "collision_group_id": str(group_id),
        "claim_count": len(locked_rows),
        "provider_session_id": provider_session_id,
    }


@router.post("/agentbay/cleanup-required/{ledger_id}/reconcile")
async def reconcile_agentbay_cleanup_required(
    ledger_id: uuid.UUID,
    body: AgentBayCleanupReconcileRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Close a poison row only after provider deletion is proven explicitly."""

    from app.models.agentbay_session import (
        AGENTBAY_PROVIDER_COLLISION_STATUS,
        AgentBaySessionLedger,
    )
    from app.models.audit import AuditLog
    from app.services.agentbay_client import _configured_agentbay_client

    # Snapshot the immutable provider identity, then release the transaction
    # before any provider call. A final locked compare-and-set below prevents a
    # stale operator request from closing a changed row.
    result = await db.execute(
        select(AgentBaySessionLedger).where(AgentBaySessionLedger.id == ledger_id)
    )
    ledger = result.scalar_one_or_none()
    if ledger is None:
        raise HTTPException(status_code=404, detail="Cleanup record not found")
    if ledger.status == AGENTBAY_PROVIDER_COLLISION_STATUS:
        return await _reconcile_agentbay_provider_collision_group(
            ledger,
            body,
            current_user=current_user,
            db=db,
        )
    if ledger.status != "cleanup_required":
        raise HTTPException(status_code=409, detail="Cleanup record is not pending")
    if not ledger.provider_session_id:
        raise HTTPException(
            status_code=409,
            detail="Cleanup record lacks a verifiable provider identity",
        )
    if body.provider_session_id != ledger.provider_session_id:
        raise HTTPException(status_code=409, detail="Provider session confirmation mismatch")
    snapshot_tenant_id = ledger.tenant_id
    snapshot_agent_id = ledger.agent_id
    snapshot_user_id = ledger.user_id
    snapshot_chat_session_id = ledger.chat_session_id
    snapshot_provider_session_id = ledger.provider_session_id
    snapshot_image_type = ledger.image_type
    snapshot_context = ledger.context if isinstance(ledger.context, dict) else {}
    trusted_v2_binding = bool(
        snapshot_context.get("binding_version") == 2
        and snapshot_tenant_id
        and snapshot_agent_id
        and snapshot_user_id
        and snapshot_chat_session_id
        and snapshot_provider_session_id
    )
    await db.rollback()

    mode = "operator_confirmed_absent"
    if not body.provider_deleted_out_of_band:
        if not trusted_v2_binding:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This cleanup row lacks a trusted v2 owner binding and cannot "
                    "be attached automatically; verify the exact provider session "
                    "out of band and submit an operator note"
                ),
            )
        mode = "provider_delete_confirmed"
        try:
            client, _tool_config = await _configured_agentbay_client(
                snapshot_agent_id
            )
            await client.attach_session(
                snapshot_provider_session_id,
                snapshot_image_type,
            )
            await client.delete_session_strict()
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Provider deletion was not confirmed; verify the exact "
                    "session in the AgentBay console before using the explicit "
                    "out-of-band confirmation"
                ),
            ) from exc
    elif len(body.verification_note.strip()) < 20:
        raise HTTPException(
            status_code=422,
            detail=(
                "An out-of-band provider verification note of at least 20 "
                "characters is required"
            ),
        )

    result = await db.execute(
        select(AgentBaySessionLedger)
        .where(AgentBaySessionLedger.id == ledger_id)
        .with_for_update()
    )
    ledger = result.scalar_one_or_none()
    if ledger is None:
        raise HTTPException(status_code=409, detail="Cleanup record changed during reconciliation")
    if ledger.status != "cleanup_required":
        raise HTTPException(status_code=409, detail="Cleanup record changed during reconciliation")
    if ledger.provider_session_id != snapshot_provider_session_id:
        raise HTTPException(status_code=409, detail="Provider identity changed during reconciliation")
    if (
        ledger.tenant_id != snapshot_tenant_id
        or ledger.agent_id != snapshot_agent_id
        or ledger.user_id != snapshot_user_id
        or ledger.chat_session_id != snapshot_chat_session_id
        or ledger.image_type != snapshot_image_type
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider ownership changed during reconciliation",
        )

    now = datetime.now(timezone.utc)
    ledger.status = "closed"
    ledger.close_reason = "operator_cleanup_verified"
    ledger.error_message = None
    ledger.closed_at = now
    ledger.context = {
        **(ledger.context if isinstance(ledger.context, dict) else {}),
        "reconciled_at": now.isoformat(),
        "reconciled_by_user_id": str(current_user.id),
        "reconciliation_mode": mode,
        "verification_note": body.verification_note.strip(),
    }
    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=ledger.agent_id,
            action="agentbay:provider_cleanup_reconciled",
            details={
                "ledger_id": str(ledger.id),
                "image_type": ledger.image_type,
                "mode": mode,
            },
        )
    )
    await db.commit()
    return {"status": "closed", "ledger_id": str(ledger.id), "mode": mode}


# ─── Platform Metrics Dashboard ─────────────────────────

@router.get("/metrics/timeseries", response_model=list[dict[str, Any]])
async def get_platform_timeseries(
    start_date: datetime,
    end_date: datetime,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Get daily platform metrics within a date range.

    Returns per-day: companies, users, tokens (existing) +
    sessions, DAU, WAU, MAU (new).
    """
    from app.models.activity_log import DailyTokenUsage
    from app.models.chat_session import ChatSession
    from sqlalchemy import cast, Date, text
    from datetime import timedelta

    # 1. New Companies per day
    companies_q = await db.execute(
        select(
            cast(Tenant.created_at, Date).label('d'),
            sqla_func.count().label('c')
        ).where(
            Tenant.created_at >= start_date,
            Tenant.created_at <= end_date
        ).group_by('d')
    )
    companies_by_day = {row.d: row.c for row in companies_q.all()}

    # 2. New Users per day
    users_q = await db.execute(
        select(
            cast(User.created_at, Date).label('d'),
            sqla_func.count().label('c')
        ).where(
            User.created_at >= start_date,
            User.created_at <= end_date
        ).group_by('d')
    )
    users_by_day = {row.d: row.c for row in users_q.all()}

    # 3. Tokens consumed per day
    tokens_q = await db.execute(
        select(
            cast(DailyTokenUsage.date, Date).label('d'),
            sqla_func.sum(DailyTokenUsage.tokens_used).label('c'),
            sqla_func.sum(DailyTokenUsage.cache_read_tokens).label('cache_read'),
        ).where(
            DailyTokenUsage.date >= start_date,
            DailyTokenUsage.date <= end_date
        ).group_by('d')
    )
    tokens_by_day = {row.d: row.c for row in tokens_q.all()}
    tokens_q = await db.execute(
        select(
            cast(DailyTokenUsage.date, Date).label('d'),
            sqla_func.sum(DailyTokenUsage.cache_read_tokens).label('cache_read'),
        ).where(
            DailyTokenUsage.date >= start_date,
            DailyTokenUsage.date <= end_date
        ).group_by('d')
    )
    cache_by_day = {row.d: row.cache_read for row in tokens_q.all()}

    # 4. New Sessions per day (DAU = distinct users with sessions that day)
    sessions_q = await db.execute(
        select(
            cast(ChatSession.created_at, Date).label('d'),
            sqla_func.count().label('sessions'),
            sqla_func.count(sqla_func.distinct(ChatSession.user_id)).label('dau'),
        ).where(
            ChatSession.created_at >= start_date,
            ChatSession.created_at <= end_date
        ).group_by('d')
    )
    sessions_by_day = {}
    dau_by_day = {}
    for row in sessions_q.all():
        sessions_by_day[row.d] = row.sessions
        dau_by_day[row.d] = row.dau

    # 5. WAU/MAU: for each day, count distinct users in rolling 7/30-day window.
    #    Use a single SQL query with window functions for efficiency.
    wau_mau_q = await db.execute(text("""
        WITH daily_users AS (
            SELECT DISTINCT
                DATE(created_at) AS d,
                user_id
            FROM chat_sessions
            WHERE created_at >= CAST(:range_start AS timestamptz)
              AND created_at <= CAST(:range_end AS timestamptz)
        ),
        day_series AS (
            SELECT CAST(generate_series(
                CAST(:series_start AS date),
                CAST(:series_end AS date),
                CAST('1 day' AS interval)
            ) AS date) AS d
        )
        SELECT
            ds.d,
            (SELECT COUNT(DISTINCT du.user_id) FROM daily_users du
             WHERE du.d BETWEEN ds.d - 6 AND ds.d) AS wau,
            (SELECT COUNT(DISTINCT du.user_id) FROM daily_users du
             WHERE du.d BETWEEN ds.d - 29 AND ds.d) AS mau
        FROM day_series ds
        ORDER BY ds.d
    """), {
        "range_start": start_date - timedelta(days=30),
        "range_end": end_date,
        "series_start": start_date.date(),
        "series_end": end_date.date(),
    })
    wau_by_day = {}
    mau_by_day = {}
    for row in wau_mau_q.all():
        wau_by_day[row[0]] = row[1]
        mau_by_day[row[0]] = row[2]

    # Generate date range list with cumulative totals
    result = []
    current_d = start_date.date()
    end_d = end_date.date()

    # Cumulative totals up to start_date
    total_companies = (await db.execute(select(sqla_func.count()).select_from(Tenant).where(Tenant.created_at < start_date))).scalar() or 0
    total_users = (await db.execute(select(sqla_func.count()).select_from(User).where(User.created_at < start_date))).scalar() or 0
    total_tokens = (await db.execute(select(sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0)).where(Agent.created_at < start_date))).scalar() or 0
    total_cache_read = (await db.execute(select(sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_total), 0)).where(Agent.created_at < start_date))).scalar() or 0
    total_sessions = (await db.execute(select(sqla_func.count()).select_from(ChatSession).where(ChatSession.created_at < start_date))).scalar() or 0

    while current_d <= end_d:
        nc = companies_by_day.get(current_d, 0)
        nu = users_by_day.get(current_d, 0)
        nt = tokens_by_day.get(current_d, 0)
        ncache = cache_by_day.get(current_d, 0)
        ns = sessions_by_day.get(current_d, 0)

        total_companies += nc
        total_users += nu
        total_tokens += nt
        total_cache_read += ncache
        total_sessions += ns

        result.append({
            "date": current_d.isoformat(),
            "new_companies": nc,
            "total_companies": total_companies,
            "new_users": nu,
            "total_users": total_users,
            "new_tokens": nt,
            "total_tokens": total_tokens,
            "new_cache_read_tokens": ncache,
            "total_cache_read_tokens": total_cache_read,
            "cache_hit_rate": round((ncache or 0) / max(nt or 0, 1), 4),
            # New metrics
            "new_sessions": ns,
            "total_sessions": total_sessions,
            "dau": dau_by_day.get(current_d, 0),
            "wau": wau_by_day.get(current_d, 0),
            "mau": mau_by_day.get(current_d, 0),
        })
        current_d += timedelta(days=1)

    return result


@router.get("/metrics/leaderboards")
async def get_platform_leaderboards(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Get Top 20 token consuming companies and agents."""
    # Top 20 Companies by total tokens
    top_companies_q = await db.execute(
        select(
            Tenant.name,
            sqla_func.coalesce(sqla_func.sum(Agent.tokens_used_total), 0).label('total'),
            sqla_func.coalesce(sqla_func.sum(Agent.cache_read_tokens_total), 0).label('cache_read'),
        )
        .join(Agent, Agent.tenant_id == Tenant.id)
        .group_by(Tenant.id)
        .order_by(sqla_func.sum(Agent.tokens_used_total).desc())
        .limit(20)
    )
    top_companies = [
        {
            "name": row.name,
            "tokens": row.total,
            "cache_read_tokens": row.cache_read,
            "cache_hit_rate": round((row.cache_read or 0) / max(row.total or 0, 1), 4),
        }
        for row in top_companies_q.all()
    ]

    # Top 20 Agents by total tokens
    top_agents_q = await db.execute(
        select(Agent.name, Tenant.name.label('tenant_name'), Agent.tokens_used_total, Agent.cache_read_tokens_total)
        .join(Tenant, Tenant.id == Agent.tenant_id)
        .order_by(Agent.tokens_used_total.desc())
        .limit(20)
    )
    top_agents = [
        {
            "name": row.name,
            "company": row.tenant_name,
            "tokens": row.tokens_used_total,
            "cache_read_tokens": row.cache_read_tokens_total,
            "cache_hit_rate": round((row.cache_read_tokens_total or 0) / max(row.tokens_used_total or 0, 1), 4),
        }
        for row in top_agents_q.all()
    ]

    return {
        "top_companies": top_companies,
        "top_agents": top_agents
    }


@router.get("/metrics/enhanced")
async def get_enhanced_metrics(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Enhanced platform metrics: retention, avg tokens/session,
    channel distribution, tool categories, and churn warnings.
    """
    from app.models.chat_session import ChatSession
    from app.models.tool import Tool, AgentTool
    from sqlalchemy import text
    from datetime import timedelta

    now = datetime.utcnow()

    # ── 1. Average tokens per session (last 30 days) ──
    # Sum of daily_token_usage / count of chat_sessions in last 30 days
    thirty_days_ago = now - timedelta(days=30)
    from app.models.activity_log import DailyTokenUsage
    total_tok_30d = (await db.execute(
        select(sqla_func.coalesce(sqla_func.sum(DailyTokenUsage.tokens_used), 0))
        .where(DailyTokenUsage.date >= thirty_days_ago)
    )).scalar() or 0
    total_sess_30d = (await db.execute(
        select(sqla_func.count())
        .select_from(ChatSession)
        .where(ChatSession.created_at >= thirty_days_ago)
    )).scalar() or 1  # avoid div by zero
    avg_tokens_per_session = round(total_tok_30d / max(total_sess_30d, 1))

    # ── 2. 7-Day Retention Rate (excluding companies <14 days old) ──
    # Last week = 14..7 days ago, This week = 7..0 days ago
    retention_q = await db.execute(text("""
        WITH established AS (
            SELECT id FROM tenants WHERE created_at < NOW() - INTERVAL '14 days'
        ),
        last_week_active AS (
            SELECT DISTINCT a.tenant_id
            FROM chat_sessions cs
            JOIN agents a ON a.id = cs.agent_id
            WHERE cs.created_at BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '7 days'
            AND a.tenant_id IN (SELECT id FROM established)
        ),
        this_week_active AS (
            SELECT DISTINCT a.tenant_id
            FROM chat_sessions cs
            JOIN agents a ON a.id = cs.agent_id
            WHERE cs.created_at > NOW() - INTERVAL '7 days'
            AND a.tenant_id IN (SELECT id FROM established)
        )
        SELECT
            COUNT(DISTINCT lw.tenant_id) AS last_week_total,
            COUNT(DISTINCT lw.tenant_id) FILTER (
                WHERE lw.tenant_id IN (SELECT tenant_id FROM this_week_active)
            ) AS retained
        FROM last_week_active lw
    """))
    ret_row = retention_q.first()
    last_week_total = ret_row[0] if ret_row else 0
    retained = ret_row[1] if ret_row else 0
    retention_rate = round(retained * 100.0 / max(last_week_total, 1), 1)

    # ── 3. Channel Distribution (last 30 days) ──
    channel_q = await db.execute(
        select(
            ChatSession.source_channel,
            sqla_func.count().label('count')
        ).where(
            ChatSession.created_at >= thirty_days_ago
        ).group_by(ChatSession.source_channel)
        .order_by(sqla_func.count().desc())
    )
    channel_distribution = [
        {"channel": row.source_channel, "count": row.count}
        for row in channel_q.all()
    ]

    # ── 4. Top 10 Tool Categories ──
    # Count enabled agent_tools grouped by tool category
    tool_q = await db.execute(
        select(
            Tool.category,
            sqla_func.count().label('count')
        ).join(AgentTool, AgentTool.tool_id == Tool.id)
        .where(AgentTool.enabled == True)  # noqa: E712
        .group_by(Tool.category)
        .order_by(sqla_func.count().desc())
        .limit(10)
    )
    tool_category_top10 = [
        {"category": row.category or "uncategorized", "count": row.count}
        for row in tool_q.all()
    ]

    # ── 5. Churn Warnings (>10M tokens, 14+ days inactive) ──
    churn_q = await db.execute(text("""
        WITH tenant_token_totals AS (
            SELECT
                tenant_id,
                SUM(tokens_used_total) AS total_tokens
            FROM agents
            GROUP BY tenant_id
        ),
        tenant_last_active AS (
            SELECT
                a.tenant_id,
                MAX(cs.created_at) AS last_active
            FROM agents a
            LEFT JOIN chat_sessions cs ON cs.agent_id = a.id
            GROUP BY a.tenant_id
        )
        SELECT
            t.name,
            tt.total_tokens,
            tla.last_active,
            CASE
                WHEN tla.last_active IS NULL THEN NULL
                ELSE EXTRACT(DAY FROM NOW() - tla.last_active)::int
            END AS days_inactive
        FROM tenants t
        JOIN tenant_token_totals tt ON tt.tenant_id = t.id
        LEFT JOIN tenant_last_active tla ON tla.tenant_id = t.id
        WHERE tt.total_tokens > 10000000
            AND (
                tla.last_active IS NULL
                OR tla.last_active < NOW() - INTERVAL '14 days'
            )
        ORDER BY tt.total_tokens DESC
    """))
    churn_warnings = []
    for row in churn_q.all():
        churn_warnings.append({
            "name": row[0],
            "total_tokens": row[1],
            "last_active": row[2].isoformat() if row[2] else None,
            "days_inactive": row[3] if row[3] else None,
        })

    return {
        "avg_tokens_per_session_30d": avg_tokens_per_session,
        "retention_rate_7d": retention_rate,
        "last_week_active_companies": last_week_total,
        "retained_companies": retained,
        "channel_distribution": channel_distribution,
        "tool_category_top10": tool_category_top10,
        "churn_warnings": churn_warnings,
    }


# ─── Platform Settings ─────────────────────────────────

@router.get("/platform-settings", response_model=PlatformSettingsOut)
async def get_platform_settings(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Get platform-level settings."""
    settings: dict[str, bool] = {}

    for key, default in [
        ("allow_self_create_company", True),
        ("invitation_code_enabled", True),
        ("sso_custom_domain_redirect_enabled", False),
    ]:
        r = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        s = r.scalar_one_or_none()
        if key == "sso_custom_domain_redirect_enabled":
            settings[key] = strict_system_setting_enabled(
                getattr(s, "value", None),
                default=False,
            )
        else:
            settings[key] = (
                s.value.get("enabled", default)
                if s and isinstance(s.value, dict)
                else default
            )

    return PlatformSettingsOut(**settings)


@router.put("/platform-settings", response_model=PlatformSettingsOut)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Update platform-level settings."""
    updates = data.model_dump(exclude_unset=True)

    for key, value in updates.items():
        r = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        s = r.scalar_one_or_none()
        if s:
            s.value = {"enabled": value}
        else:
            db.add(SystemSetting(key=key, value={"enabled": value}))

    await db.flush()
    return await get_platform_settings(current_user=current_user, db=db)


# ─── Platform Registration Codes ───────────────────────

@router.get("/registration-codes", response_model=RegistrationCodeListOut)
async def list_registration_codes(
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Legacy-shaped list backed by separated RegistrationGrant records."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    stmt = select(RegistrationGrant)
    count_stmt = select(sqla_func.count()).select_from(RegistrationGrant)

    if search:
        normalized = search.strip().upper()
        stmt = stmt.where(RegistrationGrant.token_prefix.ilike(f"%{normalized}%"))
        count_stmt = count_stmt.where(RegistrationGrant.token_prefix.ilike(f"%{normalized}%"))

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0
    result = await db.execute(
        stmt.order_by(RegistrationGrant.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return RegistrationCodeListOut(
        items=[
            RegistrationCodeOut(
                id=grant.id,
                code=f"{grant.token_prefix}…",
                max_uses=grant.max_uses,
                used_count=grant.used_count,
                is_active=grant.status == "active",
                created_at=grant.created_at,
            )
            for grant in result.scalars().all()
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/registration-codes", response_model=RegistrationCodeCreateResponse, status_code=201)
async def create_registration_codes(
    data: RegistrationCodeCreateRequest,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Batch-create registration grants; raw tokens are returned once."""
    from app.services.identity_governance import issue_registration_grant

    created: list[str] = []
    for _ in range(data.count):
        issued = await issue_registration_grant(
            db,
            max_uses=data.max_uses,
            created_by_identity_id=current_user.identity_id,
            expires_at=None,
        )
        created.append(issued.raw_token)

    await db.flush()
    return RegistrationCodeCreateResponse(created=len(created), codes=created)


@router.get("/registration-codes/export")
async def export_registration_codes_csv(
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Export non-secret registration grant metadata as CSV."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    result = await db.execute(
        select(RegistrationGrant).order_by(RegistrationGrant.created_at.asc())
    )
    codes = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Token Prefix", "Max Uses", "Used Count", "Status", "Created At"])
    for code in codes:
        writer.writerow([
            code.token_prefix,
            code.max_uses,
            code.used_count,
            code.status,
            code.created_at.strftime("%Y-%m-%d %H:%M:%S") if code.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=registration_codes.csv"},
    )


@router.delete("/registration-codes/{code_id}")
async def deactivate_registration_code(
    code_id: uuid.UUID,
    current_user: User = Depends(get_platform_operator),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a RegistrationGrant without affecting organization invitations."""
    result = await db.execute(
        select(RegistrationGrant).where(RegistrationGrant.id == code_id).with_for_update()
    )
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Registration code not found")
    if code.status == "active":
        code.status = "revoked"
    await db.flush()
    return {"status": "deactivated"}
