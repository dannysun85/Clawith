"""Company onboarding APIs."""

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.audit import AuditLog
from app.models.llm import LLMModel
from app.models.onboarding import UserTenantOnboarding
from app.models.participant import Participant
from app.models.skill import Skill
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.access_control import is_company_governor
from app.services.agent_plan_selection import (
    InvalidAgentPlanSelection,
    resolve_agent_plan_selection,
)
from app.services.entitlements import get_tenant_entitlements
from app.services.skill_scope import resolve_agent_skills, scope_skill_query
from app.services.company_product_policy import default_agent_autonomy_policy

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingStartRequest(BaseModel):
    entry_mode: str = Field(default="create", pattern="^(create|join)$")


class PersonalAssistantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    personality: str = Field(default="warm", max_length=64)
    work_style: str = Field(default="concise", max_length=64)
    proactivity: str = Field(default="balanced", pattern="^(reactive|balanced|proactive)$")
    boundaries: str = Field(default="", max_length=1000)


class CompanyInitializationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=50)
    country_region: str = Field(min_length=2, max_length=10)
    company_size: str = Field(
        default="unspecified",
        pattern=r"^(unspecified|1-10|11-50|51-200|201-1000|1000\+)$",
    )
    allow_member_private_agents: bool = False
    default_approval_policy: str = Field(
        default="high_risk",
        pattern="^(high_risk|external_actions|all_writes)$",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class MemberProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=100)
    timezone: str = Field(min_length=1, max_length=50)
    work_hours_start: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    work_hours_end: str = Field(default="18:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


def _status_payload(
    row: UserTenantOnboarding | None,
    *,
    tenant: Tenant | None = None,
    user: User | None = None,
) -> dict:
    return {
        "exists": row is not None,
        "status": row.status if row else "not_started",
        "current_step": row.current_step if row else "company",
        "entry_mode": row.entry_mode if row else None,
        "personal_assistant_agent_id": str(row.personal_assistant_agent_id) if row and row.personal_assistant_agent_id else None,
        "completed_at": row.completed_at.isoformat() if row and row.completed_at else None,
        "company_initialization_required": bool(
            tenant
            and tenant.initialization_completed_at is None
            and user
            and is_company_governor(user)
        ),
        "company": (
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "timezone": tenant.timezone,
                "country_region": tenant.country_region,
                "company_size": getattr(tenant, "company_size", "unspecified"),
                "allow_member_private_agents": bool(
                    getattr(tenant, "allow_member_private_agents", False)
                ),
                "default_approval_policy": getattr(
                    tenant, "default_approval_policy", "high_risk"
                ),
                "initialization_completed_at": (
                    tenant.initialization_completed_at.isoformat()
                    if tenant.initialization_completed_at
                    else None
                ),
            }
            if tenant
            else None
        ),
        "member_profile": (
            {
                "display_name": getattr(user, "display_name", ""),
                "title": getattr(user, "title", None) or "",
                "timezone": getattr(user, "timezone", None)
                or (tenant.timezone if tenant else "UTC"),
                "work_hours_start": getattr(user, "work_hours_start", None) or "09:00",
                "work_hours_end": getattr(user, "work_hours_end", None) or "18:00",
            }
            if user
            else None
        ),
        "private_assistant_owner_only": True,
    }


async def _get_tenant(db: AsyncSession, user: User, *, for_update: bool = False) -> Tenant | None:
    if not user.tenant_id:
        return None
    statement = select(Tenant).where(Tenant.id == user.tenant_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def _get_row(
    db: AsyncSession,
    user: User,
    *,
    for_update: bool = False,
) -> UserTenantOnboarding | None:
    if not user.tenant_id:
        return None
    statement = select(UserTenantOnboarding).where(
        UserTenantOnboarding.user_id == user.id,
        UserTenantOnboarding.tenant_id == user.tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalar_one_or_none()


def _authoritative_entry_mode(user: User, tenant: Tenant | None) -> str:
    """Classify creation from persisted ownership provenance, never URL state."""
    if (
        tenant is not None
        and getattr(user, "role", None) == "org_owner"
        and getattr(user, "identity_id", None) is not None
        and getattr(tenant, "created_by_identity_id", None) == user.identity_id
    ):
        return "create"
    return "join"


async def _ensure_row(
    db: AsyncSession,
    user: User,
    entry_mode: str | None,
    *,
    lock: bool = False,
) -> UserTenantOnboarding:
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="Company is required before onboarding")
    row = await _get_row(db, user, for_update=lock)
    if row:
        # Entry provenance is immutable after creation. A client-controlled
        # ?mode query parameter must never rewrite create into join or vice versa.
        return row

    if entry_mode is None:
        entry_mode = _authoritative_entry_mode(user, await _get_tenant(db, user))

    await db.execute(
        pg_insert(UserTenantOnboarding)
        .values(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=user.tenant_id,
            entry_mode=entry_mode,
            current_step="profile",
            status="in_progress",
        )
        .on_conflict_do_nothing(constraint="uq_user_tenant_onboarding")
    )

    row = await _get_row(db, user, for_update=lock)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to start onboarding")
    return row


async def _tenant_default_model_id(db: AsyncSession, tenant_id: uuid.UUID | None) -> uuid.UUID | None:
    if not tenant_id:
        return None
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if tenant and tenant.default_model_id:
        return tenant.default_model_id
    model_result = await db.execute(
        select(LLMModel.id).where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled == True,  # noqa: E712
        ).order_by(LLMModel.created_at.asc())
    )
    return model_result.scalar_one_or_none()


async def _tenant_plan_selection(tenant_id: uuid.UUID) -> tuple[str, str]:
    entitlements = await get_tenant_entitlements(tenant_id)
    try:
        return resolve_agent_plan_selection(entitlements, None, "text")
    except InvalidAgentPlanSelection as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _create_personal_assistant(
    db: AsyncSession,
    user: User,
    data: PersonalAssistantRequest,
) -> Agent:
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="Company is required before creating a personal assistant")

    template_result = await db.execute(
        select(AgentTemplate).where(
            AgentTemplate.name == "Private Assistant",
            AgentTemplate.is_builtin.is_(True),
        )
    )
    template = template_result.scalar_one_or_none()
    primary_model_id = await _tenant_default_model_id(db, user.tenant_id)
    preferred_tier, preferred_modality = await _tenant_plan_selection(user.tenant_id)
    personality_note = (
        f"Personality: {data.personality}. Work style: {data.work_style}. "
        f"Proactivity: {data.proactivity}."
    )
    boundaries = data.boundaries.strip()
    bio = (
        "A private assistant for daily coordination, notes, follow-ups, drafts, and light planning. "
        f"{personality_note}"
        + (f" Boundaries: {boundaries}" if boundaries else "")
    )

    agent = Agent(
        name=data.name.strip(),
        role_description="Private Assistant",
        bio=bio,
        creator_id=user.id,
        tenant_id=user.tenant_id,
        agent_type="native",
        primary_model_id=primary_model_id,
        preferred_tier=preferred_tier,
        preferred_modality=preferred_modality,
        template_id=template.id if template else None,
        status="creating",
        access_mode="private",
        company_access_level="use",
    )
    if template and template.default_autonomy_policy:
        agent.autonomy_policy = template.default_autonomy_policy
    else:
        tenant = await _get_tenant(db, user)
        agent.autonomy_policy = default_agent_autonomy_policy(
            tenant.default_approval_policy if tenant else None
        )

    db.add(agent)
    await db.flush()

    db.add(Participant(type="agent", ref_id=agent.id, display_name=agent.name, avatar_url=agent.avatar_url))
    db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=user.id, access_level="manage"))
    await db.flush()
    await ensure_access_granted_platform_relationships(db, agent, created_by_user_id=user.id)

    from app.services.agent_manager import agent_manager
    await agent_manager.initialize_agent_files(
        db,
        agent,
        personality=personality_note,
        boundaries=boundaries,
    )

    skills_result = await db.execute(
        scope_skill_query(
            select(Skill).options(selectinload(Skill.files)),
            user.tenant_id,
        )
    )
    resolved_skills = resolve_agent_skills(
        skills_result.scalars().all(),
        user.tenant_id,
        template_folders=list(template.default_skills or []) if template else [],
    )
    if resolved_skills:
        from app.services.skill_workspace import deploy_skills_to_agent_workspace

        await deploy_skills_to_agent_workspace(agent.id, resolved_skills)

    if template and template.default_tools:
        from app.services.template_capabilities import grant_template_tools

        _, unresolved = await grant_template_tools(
            db,
            agent_id=agent.id,
            tool_names=list(template.default_tools),
        )
        if unresolved:
            raise RuntimeError(
                "Template Tool registry is incomplete: " + ", ".join(unresolved)
            )

    from app.api.relationships import _regenerate_relationships_file
    await _regenerate_relationships_file(db, agent.id)

    try:
        await agent_manager.start_container(db, agent)
    except Exception:
        agent.status = "error"
        raise

    await db.flush()
    return agent


@router.get("/status")
async def get_onboarding_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return onboarding state for the current user/company."""
    tenant = await _get_tenant(db, current_user)
    return _status_payload(
        await _get_row(db, current_user),
        tenant=tenant,
        user=current_user,
    )


@router.post("/start")
async def start_onboarding(
    data: OnboardingStartRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start or resume onboarding for the current user/company."""
    tenant = await _get_tenant(db, current_user)
    row = await _ensure_row(
        db,
        current_user,
        _authoritative_entry_mode(current_user, tenant),
    )
    if row.status != "completed":
        company_required = bool(
            tenant
            and tenant.initialization_completed_at is None
            and is_company_governor(current_user)
        )
        if company_required:
            row.current_step = "company"
        elif row.current_step == "company":
            row.current_step = "profile"
    await db.commit()
    return _status_payload(row, tenant=tenant, user=current_user)


@router.post("/company")
async def complete_company_initialization(
    data: CompanyInitializationRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm first-company product policy without exposing operator settings."""

    if not is_company_governor(current_user):
        raise HTTPException(status_code=403, detail="Company administrator access required")
    row = await _ensure_row(db, current_user, None, lock=True)
    tenant = await _get_tenant(db, current_user, for_update=True)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Company not found")

    tenant.name = data.name.strip()
    tenant.timezone = data.timezone
    tenant.country_region = data.country_region.strip().upper()
    tenant.company_size = data.company_size
    tenant.allow_member_private_agents = data.allow_member_private_agents
    tenant.default_approval_policy = data.default_approval_policy
    if tenant.initialization_completed_at is None:
        tenant.initialization_completed_at = datetime.now(timezone.utc)
        tenant.initialized_by_user_id = current_user.id
        db.add(
            AuditLog(
                tenant_id=tenant.id,
                user_id=current_user.id,
                action="company_initialization_completed",
                details={
                    "company_size": data.company_size,
                    "allow_member_private_agents": data.allow_member_private_agents,
                    "default_approval_policy": data.default_approval_policy,
                },
            )
        )
    row.current_step = "profile"
    row.status = "in_progress"
    await db.commit()
    return _status_payload(row, tenant=tenant, user=current_user)


@router.post("/profile")
async def complete_member_profile(
    data: MemberProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist the current membership's profile and working context."""

    row = await _ensure_row(db, current_user, None, lock=True)
    tenant = await _get_tenant(db, current_user)
    if (
        tenant
        and tenant.initialization_completed_at is None
        and is_company_governor(current_user)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "company_initialization_required",
                "message": "Complete company initialization before personal onboarding",
            },
        )
    if data.work_hours_start == data.work_hours_end:
        raise HTTPException(status_code=400, detail="Work hours must have a non-zero duration")

    current_user.display_name = data.display_name.strip()
    current_user.title = data.title.strip() or None
    current_user.timezone = data.timezone
    current_user.work_hours_start = data.work_hours_start
    current_user.work_hours_end = data.work_hours_end
    await db.execute(
        update(Participant)
        .where(Participant.type == "user", Participant.ref_id == current_user.id)
        .values(display_name=current_user.display_name)
    )
    row.current_step = "assistant"
    row.status = "in_progress"
    await db.commit()
    return _status_payload(row, tenant=tenant, user=current_user)


@router.post("/personal-assistant", status_code=status.HTTP_201_CREATED)
async def create_personal_assistant(
    data: PersonalAssistantRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create the user's private assistant and advance onboarding."""
    # The onboarding row is the serialization boundary for one user's companion.
    # It also preserves the entry mode recorded by /start instead of rewriting a
    # company creator as a joining member.
    row = await _ensure_row(db, current_user, None, lock=True)
    if row.current_step == "company":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "company_initialization_required",
                "message": "Complete company initialization before creating a private assistant",
            },
        )
    if row.current_step == "profile":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "member_profile_required",
                "message": "Complete your company profile before creating a private assistant",
            },
        )
    tenant = await _get_tenant(db, current_user)
    if row.personal_assistant_agent_id:
        result = await db.execute(
            select(Agent).where(
                Agent.id == row.personal_assistant_agent_id,
                Agent.deleted_at.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            row.current_step = "opening"
            await db.commit()
            return {
                "agent": {"id": str(existing.id), "name": existing.name},
                "onboarding": _status_payload(row, tenant=tenant, user=current_user),
            }

    # A user's one onboarding-linked private assistant is a companion, not a
    # purchased long-term Agent employee seat.  The onboarding row is the
    # authoritative identity boundary; ordinary Agent creation continues to
    # enforce max_agents in app.api.agents.
    agent = await _create_personal_assistant(db, current_user, data)
    row.personal_assistant_agent_id = agent.id
    row.current_step = "opening"
    row.status = "in_progress"
    await db.commit()
    return {
        "agent": {"id": str(agent.id), "name": agent.name},
        "onboarding": _status_payload(row, tenant=tenant, user=current_user),
    }


@router.post("/complete")
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark the current user/company onboarding as completed."""
    row = await _get_row(db, current_user)
    if not row:
        row = await _ensure_row(db, current_user, None)
    if row.status != "completed" and not row.personal_assistant_agent_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "personal_assistant_required",
                "message": "Create or restore the membership's private assistant before completing onboarding",
            },
        )
    row.status = "completed"
    row.current_step = "completed"
    row.completed_at = datetime.now(timezone.utc)
    await db.commit()
    tenant = await _get_tenant(db, current_user)
    return _status_payload(row, tenant=tenant, user=current_user)
