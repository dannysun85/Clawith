"""Agent (Digital Employee) API routes."""

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from loguru import logger
from sqlalchemy import String, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.permissions import build_visible_agents_query, check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import async_session, get_db
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.org import OrgMember
from app.models.audit import ApprovalRequest, ChatMessage
from app.models.chat_session import ChatSession
from app.models.douyin import DouyinOperation, DouyinPublishJob
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditReservation
from app.models.user import User
from app.schemas.schemas import AgentCreate, AgentOut, AgentUpdate, ApprovalAction, ApprovalRequestOut
from app.services.media_generation import UNRESOLVED_MEDIA_STATUSES
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.quota_guard import check_agent_creation_quota, QuotaExceeded, quota_error_payload
from app.services.entitlements import get_tenant_entitlements
from app.services.agent_plan_selection import (
    InvalidAgentPlanSelection,
    resolve_agent_plan_selection,
)
from app.models.tenant import Tenant
from app.models.participant import Participant
from app.services.okr_agent_hook import hook_new_agent
from app.services.agent_manager import agent_manager
from app.models.skill import Skill
from app.services.resource_discovery import import_mcp_from_smithery
from app.services.skill_scope import resolve_agent_skills, scope_skill_query

router = APIRouter(prefix="/agents", tags=["agents"])
settings = get_settings()

def _resolve_agent_plan_selection(ent, requested_tier: str | None, requested_modality: str | None) -> tuple[str | None, str | None]:
    """Resolve and validate the Agent's SaaS tier/modality selection."""
    try:
        return resolve_agent_plan_selection(ent, requested_tier, requested_modality)
    except InvalidAgentPlanSelection as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


def _is_platform_admin(current_user: User) -> bool:
    identity = getattr(current_user, "identity", None)
    return current_user.role == "platform_admin" or bool(
        getattr(identity, "is_platform_admin", False)
    )


async def _validate_agent_skill_selection(
    db: AsyncSession,
    skill_ids: list[uuid.UUID],
    tenant_id: uuid.UUID | None,
) -> None:
    """Reject selected skills that are outside the target tenant's view."""
    requested = set(skill_ids)
    if not requested:
        return
    result = await db.execute(
        scope_skill_query(
            select(Skill.id).where(Skill.id.in_(requested)),
            tenant_id,
        )
    )
    visible = set(result.scalars().all())
    if visible != requested:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="One or more selected skills are unavailable to the target tenant",
        )


async def _get_active_admin_users(db: AsyncSession, tenant_id: uuid.UUID | None) -> list[User]:
    if not tenant_id:
        return []
    result = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.is_active == True,  # noqa: E712
            User.role.in_(["platform_admin", "org_admin"]),
        )
    )
    return result.scalars().all()


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _archive_agent_task_history(db: AsyncSession, agent_id: uuid.UUID, archive_dir: Path) -> Path | None:
    """Persist task and task-log history into the agent archive directory before DB cleanup."""
    from app.models.task import Task, TaskLog

    task_result = await db.execute(select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.asc()))
    tasks = task_result.scalars().all()
    if not tasks:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "agent_id": str(agent_id),
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "tasks": [],
    }

    for task in tasks:
        log_result = await db.execute(
            select(TaskLog).where(TaskLog.task_id == task.id).order_by(TaskLog.created_at.asc())
        )
        logs = log_result.scalars().all()
        payload["tasks"].append(
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "type": task.type,
                "status": task.status,
                "priority": task.priority,
                "assignee": task.assignee,
                "created_by": str(task.created_by),
                "due_date": _serialize_dt(task.due_date),
                "supervision_target_user_id": (
                    str(task.supervision_target_user_id) if task.supervision_target_user_id else None
                ),
                "supervision_target_name": task.supervision_target_name,
                "supervision_channel": task.supervision_channel,
                "remind_schedule": task.remind_schedule,
                "created_at": _serialize_dt(task.created_at),
                "updated_at": _serialize_dt(task.updated_at),
                "completed_at": _serialize_dt(task.completed_at),
                "logs": [
                    {
                        "id": str(log.id),
                        "content": log.content,
                        "created_at": _serialize_dt(log.created_at),
                    }
                    for log in logs
                ],
            }
        )

    archive_path = archive_dir / "task_history.json"
    archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return archive_path


async def _agent_deletion_blockers(
    db: AsyncSession,
    agent_id: uuid.UUID,
) -> list[str]:
    """Return in-flight work that makes physical Agent deletion unsafe."""

    checks = (
        (
            "Credits reservation",
            select(CreditReservation.id)
            .where(
                CreditReservation.agent_id == agent_id,
                CreditReservation.status.in_(
                    ("reserved", "provider_inflight", "settlement_ready")
                ),
            )
            .limit(1),
        ),
        (
            "media generation",
            select(MediaGenerationTask.id)
            .where(
                MediaGenerationTask.agent_id == agent_id,
                MediaGenerationTask.status.in_(UNRESOLVED_MEDIA_STATUSES),
            )
            .limit(1),
        ),
        (
            "approval execution",
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.agent_id == agent_id,
                ApprovalRequest.status == "approved",
                ApprovalRequest.execution_status == "executing",
            )
            .limit(1),
        ),
        (
            "Douyin publish",
            select(DouyinPublishJob.id)
            .where(
                DouyinPublishJob.agent_id == agent_id,
                DouyinPublishJob.status.in_(
                    ("preparing_share_package", "creating")
                ),
            )
            .limit(1),
        ),
        (
            "Douyin operation",
            select(DouyinOperation.id)
            .where(
                DouyinOperation.agent_id == agent_id,
                DouyinOperation.status == "running",
            )
            .limit(1),
        ),
    )
    blockers: list[str] = []
    for label, statement in checks:
        if (await db.execute(statement)).scalar_one_or_none() is not None:
            blockers.append(label)
    return blockers


async def _lazy_reset_token_counters(agent: Agent, db: AsyncSession) -> bool:
    """Reset daily/monthly token counters if the day or month has changed.

    Returns True if any counter was reset (caller should commit/flush).
    """
    from datetime import datetime, timezone as tz

    now = datetime.now(tz.utc)
    changed = False

    last_daily = agent.last_daily_reset
    if last_daily is None or last_daily.date() < now.date():
        agent.tokens_used_today = 0
        agent.cache_read_tokens_today = 0
        agent.cache_creation_tokens_today = 0
        agent.last_daily_reset = now
        changed = True

    last_monthly = agent.last_monthly_reset
    if last_monthly is None or (last_monthly.year, last_monthly.month) < (now.year, now.month):
        agent.tokens_used_month = 0
        agent.cache_read_tokens_month = 0
        agent.cache_creation_tokens_month = 0
        agent.last_monthly_reset = now
        changed = True

    return changed


async def _build_unread_count_by_agent(
    db: AsyncSession,
    agents: list[Agent],
    current_user: User,
) -> dict[str, int]:
    """Return unread assistant/system/tool message counts for the current user per agent.

    The sidebar only needs user-facing unread state, so we scope strictly to sessions owned by
    the current platform user and ignore agent-to-agent / trigger-only threads.
    """

    if not agents:
        return {}

    agent_ids = [agent.id for agent in agents]
    result = await db.execute(
        select(ChatSession.agent_id, func.count(ChatMessage.id))
        .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
        .where(
            ChatSession.agent_id.in_(agent_ids),
            ChatSession.user_id == current_user.id,
            ChatSession.is_group.is_(False),
            ChatSession.source_channel.notin_(["agent", "trigger"]),
            ChatMessage.role.in_(["assistant", "system", "tool_call"]),
            ChatMessage.created_at
            > func.coalesce(
                ChatSession.last_read_at_by_user,
                datetime(1970, 1, 1, tzinfo=timezone.utc),
            ),
        )
        .group_by(ChatSession.agent_id)
    )
    return {str(row[0]): int(row[1] or 0) for row in result.all()}


def _serialize_agent_out(agent: Agent, unread_count: int = 0) -> AgentOut:
    payload = AgentOut.model_validate(agent).model_dump()
    payload["unread_count"] = unread_count
    model = AgentOut.model_validate(payload)
    _apply_release_capabilities(model)
    return model


@router.get("/templates")
async def list_templates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all available agent templates."""
    from app.models.agent import AgentTemplate

    result = await db.execute(
        select(AgentTemplate).order_by(AgentTemplate.is_builtin.desc(), AgentTemplate.created_at.asc())
    )
    templates = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "category": t.category,
            "is_builtin": t.is_builtin,
            "soul_template": t.soul_template,
            "default_skills": t.default_skills,
            "default_tools": t.default_tools,
            "default_autonomy_policy": t.default_autonomy_policy,
            "capability_bullets": t.capability_bullets or [],
        }
        for t in templates
    ]


async def _agent_to_out(
    db: AsyncSession,
    agent: Agent,
    viewer_id: uuid.UUID,
) -> AgentOut:
    """Serialize one agent with ``onboarded_for_me`` for the given viewer."""
    from app.services.onboarding import is_onboarded

    model = AgentOut.model_validate(agent)
    model.onboarded_for_me = await is_onboarded(db, agent.id, viewer_id)
    _apply_release_capabilities(model)
    return model


async def _agents_to_out(
    db: AsyncSession,
    agents: list[Agent],
    viewer_id: uuid.UUID,
) -> list[AgentOut]:
    """List variant that fetches all junction rows in one query."""
    from app.services.onboarding import onboarded_agent_ids

    onboarded = await onboarded_agent_ids(db, viewer_id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        model = AgentOut.model_validate(a)
        model.onboarded_for_me = a.id in onboarded
        _apply_release_capabilities(model)
        out.append(model)
    return out


def _apply_release_capabilities(model: AgentOut) -> None:
    """Expose fail-closed RC5 execution gates to product surfaces."""

    from app.services.autonomy_service import (
        APPROVAL_AUTOMATIC_EXECUTION_ENABLED,
    )
    from app.services.scheduler import AUTOMATIC_SCHEDULE_EXECUTION_ENABLED
    from app.services.supervision_reminder import SUPERVISION_EXECUTION_ENABLED
    from app.services.task_executor import AUTOMATIC_TASK_EXECUTION_ENABLED
    from app.services.trigger_runtime.config import (
        AUTOMATIC_TRIGGER_EXECUTION_ENABLED,
    )

    model.automation_execution_enabled = all(
        (
            AUTOMATIC_SCHEDULE_EXECUTION_ENABLED,
            AUTOMATIC_TASK_EXECUTION_ENABLED,
            AUTOMATIC_TRIGGER_EXECUTION_ENABLED,
        )
    )
    model.approval_execution_enabled = (
        APPROVAL_AUTOMATIC_EXECUTION_ENABLED
    )
    model.execution_capabilities = {
        "schedule_execution": AUTOMATIC_SCHEDULE_EXECUTION_ENABLED,
        "task_execution": AUTOMATIC_TASK_EXECUTION_ENABLED,
        "supervision_execution": SUPERVISION_EXECUTION_ENABLED,
        "trigger_execution": AUTOMATIC_TRIGGER_EXECUTION_ENABLED,
        "approval_dispatch": APPROVAL_AUTOMATIC_EXECUTION_ENABLED,
        # Gateway-to-human delivery is fail-closed until a durable provider
        # outbox identity makes timeout ambiguity non-replayable.
        "gateway_human_delivery": False,
    }
    model.deletion_state = (
        "cleanup_pending"
        if model.deletion_requested_at is not None
        else "active"
    )


def _format_agent_setup_error(stage: str, exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    message = re.sub(
        r"(?i)(password|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2***",
        message,
    )
    message = re.sub(r"://([^:/\s]+):([^@\s]+)@", r"://\1:***@", message)
    return f"{stage}: {type(exc).__name__}: {message[:1500]}"


async def _record_agent_setup_error(
    agent_id: uuid.UUID,
    stage: str,
    exc: Exception,
) -> None:
    """Persist a manager-visible failure reason without leaking credentials."""
    try:
        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if agent:
                agent_manager.mark_error(agent, _format_agent_setup_error(stage, exc))
                await db.commit()
    except Exception as persist_exc:
        logger.exception(
            f"Failed to persist setup error for agent {agent_id}: {persist_exc}"
        )


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all agents the current user has access to."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )

    requested_tenant_id = current_user.tenant_id

    stmt = build_visible_agents_query(
        current_user,
        tenant_id=requested_tenant_id,
    ).order_by(Agent.created_at.desc())

    result = await db.execute(stmt)
    agents = result.scalars().all()
    # Lazy reset token counters
    needs_flush = False
    for a in agents:
        if await _lazy_reset_token_counters(a, db):
            needs_flush = True
    if needs_flush:
        await db.commit()
    unread_by_agent = await _build_unread_count_by_agent(db, agents, current_user)
    from app.services.onboarding import onboarded_agent_ids

    onboarded = await onboarded_agent_ids(db, current_user.id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        model = _serialize_agent_out(a, unread_by_agent.get(str(a.id), 0))
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


async def _background_agent_setup(
    agent_id: uuid.UUID,
    personality: str,
    boundaries: str,
    skill_ids: list[uuid.UUID],
    template_skill_folder_names: list[str],
    template_tool_names: list[str],
    template_mcp_servers: list[str],
) -> None:
    """Run all creation tasks asynchronously with small, short-lived transactions."""
    agent_tenant_id: uuid.UUID | None = None
    # 1. Initialize agent file system from template
    try:
        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                logger.error(f"[background_agent_setup] Agent {agent_id} not found")
                return
            agent_tenant_id = agent.tenant_id
            await agent_manager.initialize_agent_files(
                db,
                agent,
                personality=personality,
                boundaries=boundaries,
            )
            await db.commit()
    except Exception as e:
        logger.exception(f"Error during agent file initialization for {agent_id}: {e}")
        await _record_agent_setup_error(agent_id, "file_initialization", e)
        return

    # 2. Skill resolution (reads from DB)
    automatic_skills = []
    user_selected_skills = []
    try:
        async with async_session() as db:
            skills_result = await db.execute(
                scope_skill_query(
                    select(Skill).options(selectinload(Skill.files)),
                    agent_tenant_id,
                )
            )
            visible_skills = skills_result.scalars().all()
            skills = resolve_agent_skills(
                visible_skills,
                agent_tenant_id,
                selected_ids=skill_ids,
                template_folders=template_skill_folder_names,
            )
            automatic_folders = set(template_skill_folder_names)
            automatic_folders.update(
                skill.folder_name for skill in visible_skills if skill.is_default
            )
            automatic_skills = [
                skill for skill in skills if skill.folder_name in automatic_folders
            ]
            user_selected_skills = [
                skill for skill in skills if skill.folder_name not in automatic_folders
            ]
    except Exception as e:
        logger.exception(f"Error resolving skills for agent {agent_id}: {e}")
        await _record_agent_setup_error(agent_id, "skill_resolution", e)
        return

    # 3. Skills Copying (I/O only, NO db connection held!)
    if automatic_skills or user_selected_skills:
        try:
            from app.services.skill_workspace import deploy_skills_to_agent_workspace

            stats = {"files": 0, "conflicts": 0}
            for skills, provisioning in (
                (automatic_skills, "automatic"),
                (user_selected_skills, "user_selected"),
            ):
                if not skills:
                    continue
                batch_stats = await deploy_skills_to_agent_workspace(
                    agent_id,
                    skills,
                    provisioning=provisioning,
                )
                stats["files"] += batch_stats["files"]
                stats["conflicts"] += batch_stats["conflicts"]
            logger.info(
                "[_skills_copy] background agent={} files={} conflicts={}",
                agent_id,
                stats["files"],
                stats["conflicts"],
            )
        except Exception as e:
            logger.exception(f"Error copying skills files for agent {agent_id}: {e}")
            await _record_agent_setup_error(agent_id, "skill_copy", e)
            return

    # 4. Grant executable role capabilities. Skill copying above only adds
    # instructions and must never be treated as execution permission.
    if template_tool_names:
        try:
            from app.services.template_capabilities import grant_template_tools

            async with async_session() as db:
                _, unresolved = await grant_template_tools(
                    db,
                    agent_id=agent_id,
                    tool_names=template_tool_names,
                )
                await db.commit()
            if unresolved:
                raise RuntimeError(
                    "Template Tool registry is incomplete: "
                    + ", ".join(unresolved)
                )
        except Exception as e:
            logger.exception(
                "Error granting template tools agent={} error_type={}",
                agent_id,
                type(e).__name__,
            )
            await _record_agent_setup_error(agent_id, "template_tools", e)
            return

    # 5. Install template MCP servers
    if template_mcp_servers:
        for server_id in template_mcp_servers:
            try:
                result_msg = await import_mcp_from_smithery(
                    server_id=server_id,
                    agent_id=agent_id,
                    config={},
                )
                if result_msg.startswith("❌"):
                    logger.warning(
                        "[create_agent] background MCP pre-install server={} agent={} "
                        "reported error result_chars={}",
                        server_id,
                        agent_id,
                        len(result_msg),
                    )
                else:
                    logger.info(
                        f"[create_agent] background MCP pre-install '{server_id}' succeeded for agent {agent_id}"
                    )
            except Exception as e:
                logger.warning(
                    "[create_agent] background MCP pre-install failed "
                    "server_id={} agent={} error_type={}",
                    server_id,
                    agent_id,
                    type(e).__name__,
                )

    # 6. Start container and Hook OKR Agent
    try:
        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                logger.error(f"[background_agent_setup] Agent {agent_id} not found before starting container")
                return
            if agent.deletion_requested_at is not None:
                logger.info(
                    "[background_agent_setup] Agent deletion is pending; startup skipped"
                )
                return

            await agent_manager.start_container(db, agent)

            if agent.status == "error":
                await db.commit()
                return

            if agent.tenant_id:
                await hook_new_agent(db, agent.id, agent.tenant_id)

            await db.commit()
    except Exception as e:
        logger.exception(f"Error starting container for agent {agent_id}: {e}")
        await _record_agent_setup_error(agent_id, "container_start", e)


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new digital employee (any authenticated user)."""
    # A TTL of 0 or less means the agent never expires.
    ttl_hours = current_user.quota_agent_ttl_hours

    # Determine target tenant: only platform admins may cross tenant boundaries.
    target_tenant_id = current_user.tenant_id
    if data.tenant_id:
        if data.tenant_id != current_user.tenant_id and not _is_platform_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot create an agent in another tenant",
            )
        target_tenant_id = data.tenant_id

    await _validate_agent_skill_selection(
        db,
        list(data.skill_ids or []),
        target_tenant_id,
    )

    selected_template = None
    if data.template_id:
        template_result = await db.execute(
            select(AgentTemplate).where(AgentTemplate.id == data.template_id)
        )
        selected_template = template_result.scalar_one_or_none()
        if selected_template is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent template not found",
            )

    # Plan max_agents is a tenant-level commercial limit. Enforce it after the
    # final target tenant is known so org/platform admins cannot bypass it.
    try:
        await check_agent_creation_quota(current_user.id, tenant_id=target_tenant_id, db=db)
    except QuotaExceeded as e:
        if e.quota_type == "max_agents":
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=quota_error_payload(e),
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=quota_error_payload(e))

    # Get default limits from subscription entitlements, falling back to tenant defaults
    max_llm_calls = 1000
    default_max_triggers = 20
    default_min_poll = 5
    default_webhook_rate = 5
    default_heartbeat_interval = 240  # model default
    tenant_default_model_id = None
    tenant = None
    if target_tenant_id:
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == target_tenant_id))
        tenant = tenant_result.scalar_one_or_none()
        if tenant:
            ttl_hours = tenant.default_agent_ttl_hours
            default_min_poll = tenant.min_poll_interval_floor or 5
            default_webhook_rate = tenant.max_webhook_rate_ceiling or 5
            tenant_default_model_id = tenant.default_model_id
            # Enforce heartbeat floor: new agents must respect company minimum
            if (
                tenant.min_heartbeat_interval_minutes
                and tenant.min_heartbeat_interval_minutes > default_heartbeat_interval
            ):
                default_heartbeat_interval = tenant.min_heartbeat_interval_minutes

        ent = await get_tenant_entitlements(target_tenant_id)
        if ent:
            max_llm_calls = ent.max_llm_calls_per_day
            default_max_triggers = ent.max_triggers
        elif tenant:
            max_llm_calls = tenant.default_max_llm_calls_per_day or 1000
            default_max_triggers = tenant.default_max_triggers or 20

    effective_preferred_tier = data.preferred_tier
    effective_preferred_modality = data.preferred_modality or "text"
    if (data.agent_type or "native") == "native":
        effective_preferred_tier, effective_preferred_modality = _resolve_agent_plan_selection(
            ent if target_tenant_id else None,
            data.preferred_tier,
            data.preferred_modality,
        )

    # If the caller didn't pick a model, fall back to the tenant's default.
    effective_primary_model_id = data.primary_model_id or tenant_default_model_id
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None

    agent = Agent(
        name=data.name,
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        creator_id=current_user.id,
        tenant_id=target_tenant_id,
        agent_type=data.agent_type or "native",
        primary_model_id=effective_primary_model_id,
        fallback_model_id=data.fallback_model_id,
        preferred_tier=effective_preferred_tier,
        preferred_modality=effective_preferred_modality,
        max_tokens_per_day=data.max_tokens_per_day,
        max_tokens_per_month=data.max_tokens_per_month,
        template_id=data.template_id,
        status="creating" if data.agent_type != "openclaw" else "idle",
        expires_at=expires_at,
        max_llm_calls_per_day=max_llm_calls,
        max_triggers=default_max_triggers,
        min_poll_interval_min=default_min_poll,
        webhook_rate_limit=default_webhook_rate,
        heartbeat_interval_minutes=default_heartbeat_interval,
    )
    if data.autonomy_policy:
        agent.autonomy_policy = data.autonomy_policy
    elif (
        selected_template
        and selected_template.is_builtin
        and selected_template.default_autonomy_policy
    ):
        agent.autonomy_policy = dict(selected_template.default_autonomy_policy)

    db.add(agent)
    await db.flush()

    # Auto-create Participant identity for the new agent
    db.add(
        Participant(
            type="agent",
            ref_id=agent.id,
            display_name=agent.name,
            avatar_url=agent.avatar_url,
        )
    )
    await db.flush()

    # Set permissions
    access_level = data.permission_access_level if data.permission_access_level in ("use", "manage") else "use"
    if data.permission_scope_type not in ("company", "user", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported permission_scope_type")
    if data.permission_scope_type == "company":
        agent.access_mode = "company"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent.id, scope_type="company", access_level=access_level))
    elif data.permission_scope_type == "user":
        agent.access_mode = "private"
        agent.company_access_level = access_level
        if data.permission_scope_ids:
            for scope_id in data.permission_scope_ids:
                db.add(
                    AgentPermission(agent_id=agent.id, scope_type="user", scope_id=scope_id, access_level=access_level)
                )
        else:
            # "仅自己" — insert creator as the only permitted user
            db.add(
                AgentPermission(agent_id=agent.id, scope_type="user", scope_id=current_user.id, access_level="manage")
            )
    elif data.permission_scope_type == "custom":
        agent.access_mode = "custom"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent.id, scope_type="user", scope_id=current_user.id, access_level="manage"))

    await db.flush()
    await ensure_access_granted_platform_relationships(db, agent, created_by_user_id=current_user.id)

    # For OpenClaw agents: skip file system and container setup, generate API key
    if agent.agent_type == "openclaw":
        raw_key = f"oc-{secrets.token_urlsafe(32)}"
        agent.api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        agent.status = "idle"
        await db.commit()

        if agent.tenant_id:
            await hook_new_agent(db, agent.id, agent.tenant_id)
            await db.commit()

        out_model = await _agent_to_out(db, agent, current_user.id)
        out = out_model.model_dump()
        out["api_key"] = raw_key  # Return once on creation
        return out

    # Resolve template settings
    folder_names = []
    template_tool_names = []
    template_mcp_servers = []
    if selected_template:
        folder_names = list(selected_template.default_skills or [])
        if selected_template.is_builtin:
            template_tool_names = list(selected_template.default_tools or [])
            template_mcp_servers = list(selected_template.default_mcp_servers or [])

    # Prepare return response before transaction is committed
    out = await _agent_to_out(db, agent, current_user.id)

    # Commit initial state to DB so background task can read the agent row
    await db.commit()

    # Dispatch heavy setup to background task
    background_tasks.add_task(
        _background_agent_setup,
        agent_id=agent.id,
        personality=data.personality or "",
        boundaries=data.boundaries or "",
        skill_ids=list(data.skill_ids or []),
        template_skill_folder_names=folder_names,
        template_tool_names=template_tool_names,
        template_mcp_servers=template_mcp_servers,
    )

    return out


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent details."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if agent_manager.reconcile_error_status(agent):
        await db.flush()
    # Lazy reset token counters
    if await _lazy_reset_token_counters(agent, db):
        await db.commit()
    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    out["access_level"] = access_level
    if access_level == "manage":
        out["last_error"] = agent.last_error
        out["last_error_at"] = agent.last_error_at

    # Resolve creator username (one extra query, only on detail page).
    # IMPORTANT: User.username is an association_proxy to User.identity.username.
    # We must eagerly load the identity relationship (selectinload) to avoid
    # async lazy-loading errors (SQLAlchemy raises MissingGreenlet in async context).
    if agent.creator_id:
        from sqlalchemy.orm import selectinload
        from app.models.user import Identity  # noqa: F401

        creator_result = await db.execute(
            select(User).where(User.id == agent.creator_id).options(selectinload(User.identity))
        )
        creator = creator_result.scalar_one_or_none()
        out["creator_username"] = creator.username if creator else None

    # Resolve effective timezone (agent → tenant → UTC)
    effective_tz = agent.timezone
    if not effective_tz and agent.tenant_id:
        from app.models.tenant import Tenant

        t_result = await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            effective_tz = tenant.timezone or "UTC"
    out["effective_timezone"] = effective_tz or "UTC"

    return out


@router.get("/{agent_id}/media-capabilities")
async def get_media_capabilities(
    agent_id: uuid.UUID,
    tier: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return safe, user-facing media generation readiness for an Agent."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    entitlements = await get_tenant_entitlements(agent.tenant_id) if agent.tenant_id else None
    selected_tier = str(tier or agent.preferred_tier or "lite").strip().lower()
    if selected_tier not in {"lite", "pro", "ultra"}:
        selected_tier = "lite"

    from app.services.media_capabilities import get_agent_media_capabilities

    capabilities = await get_agent_media_capabilities(
        db,
        agent_id=agent.id,
        entitlements=entitlements,
        tier=selected_tier,
    )
    return {"tier": selected_tier, "capabilities": capabilities}


@router.get("/{agent_id}/permissions")
async def get_agent_permissions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent permission scope."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    perms = result.scalars().all()
    can_manage = access_level == "manage"
    is_owner = is_agent_creator(current_user, agent)
    access_mode = getattr(agent, "access_mode", None) or "company"

    if not perms:
        return {
            "scope_type": access_mode,
            "scope_ids": [],
            "user_access": [],
            "access_level": "manage" if is_owner else "use",
            "effective_access_level": access_level,
            "can_manage": can_manage,
            "is_owner": is_owner,
            "creator_id": str(agent.creator_id) if agent.creator_id else None,
        }

    scope_type = access_mode
    scope_ids = [str(p.scope_id) for p in perms if p.scope_type == "user" and p.scope_id]
    perm_access_level = getattr(agent, "company_access_level", None) or next(
        (p.access_level for p in perms if p.scope_type == "company"),
        "use",
    )

    # Resolve names for display
    scope_names = []
    user_access = []
    display_user_ids = {uuid.UUID(sid) for sid in scope_ids}
    if access_mode == "custom":
        if agent.creator_id:
            display_user_ids.add(agent.creator_id)
        display_user_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))

    if display_user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(display_user_ids)))
        users_by_id = {str(u.id): u for u in users_result.scalars().all()}
        access_by_user_id = {
            str(perm.scope_id): (perm.access_level or "use")
            for perm in perms
            if perm.scope_type == "user" and perm.scope_id
        }
        ordered_user_ids = [str(uid) for uid in display_user_ids]
        ordered_user_ids.sort(
            key=lambda sid: (
                (users_by_id.get(sid).display_name or users_by_id.get(sid).username or "")
                if users_by_id.get(sid)
                else ""
            )
        )
        for perm in perms:
            if perm.scope_type != "user" or not perm.scope_id:
                continue
            sid = str(perm.scope_id)
            if sid not in ordered_user_ids:
                ordered_user_ids.append(sid)

        for sid in ordered_user_ids:
            u = users_by_id.get(sid)
            if not u:
                continue
            is_creator = agent.creator_id == u.id
            is_admin = u.role in ("platform_admin", "org_admin")
            is_required = access_mode == "custom" and (is_creator or is_admin)
            item = {
                "id": sid,
                "name": u.display_name or u.username,
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "access_level": "manage" if is_required else access_by_user_id.get(sid, "use"),
                "is_required": is_required,
                "required_reason": "creator" if is_creator else "company_admin" if is_admin else None,
            }
            scope_names.append({"id": sid, "name": item["name"]})
            user_access.append(item)

    return {
        "scope_type": scope_type,
        "scope_ids": scope_ids,
        "scope_names": scope_names,
        "user_access": user_access,
        "access_level": perm_access_level,
        "effective_access_level": access_level,
        "can_manage": can_manage,
        "is_owner": is_owner,
        "creator_id": str(agent.creator_id) if agent.creator_id else None,
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent permission scope (owner or platform_admin only)."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    scope_type = data.get("scope_type", "company")
    scope_ids = data.get("scope_ids", [])
    user_access = data.get("user_access", [])
    access_level = data.get("access_level", "use")
    if access_level not in ("use", "manage"):
        access_level = "use"
    if scope_type not in ("company", "user", "private", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope_type")
    if scope_type == "user":
        scope_type = "private"

    # Serialize every ACL replacement with final-delivery authorization fences.
    # Deleting/reinserting AgentPermission rows alone does not conflict with the
    # Agent FOR SHARE locks used by A2A delivery and can otherwise cross a revoke.
    locked_agent_result = await db.execute(
        select(Agent).where(Agent.id == agent_id).with_for_update()
    )
    locked_agent = locked_agent_result.scalar_one_or_none()
    if locked_agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    agent = locked_agent

    # Delete existing permissions
    from sqlalchemy import delete as sql_delete

    await db.execute(sql_delete(AgentPermission).where(AgentPermission.agent_id == agent_id))

    # Insert new permissions
    if scope_type == "company":
        agent.access_mode = "company"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent_id, scope_type="company", access_level=access_level))
    elif scope_type == "private":
        agent.access_mode = "private"
        agent.company_access_level = access_level
        # "Only me" means private to the agent creator, even when an org admin
        # is managing a company-visible agent created by someone else.
        db.add(
            AgentPermission(
                agent_id=agent_id,
                scope_type="user",
                scope_id=agent.creator_id or current_user.id,
                access_level="manage",
            )
        )
    elif scope_type == "custom":
        agent.access_mode = "custom"
        agent.company_access_level = access_level
        seen_user_ids: set[uuid.UUID] = set()
        creator_id = agent.creator_id or current_user.id
        required_manager_ids = {creator_id}
        required_manager_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))
        for item in user_access:
            sid = item.get("id") or item.get("user_id")
            if not sid:
                continue
            uid = uuid.UUID(str(sid))
            if uid in seen_user_ids:
                continue
            lvl = item.get("access_level", "use")
            if lvl not in ("use", "manage"):
                lvl = "use"
            if uid in required_manager_ids:
                lvl = "manage"
            seen_user_ids.add(uid)
            db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level=lvl))
        for sid in scope_ids:
            uid = uuid.UUID(str(sid))
            if uid not in seen_user_ids:
                seen_user_ids.add(uid)
                db.add(
                    AgentPermission(
                        agent_id=agent_id,
                        scope_type="user",
                        scope_id=uid,
                        access_level="manage" if uid in required_manager_ids else access_level,
                    )
                )
        for uid in required_manager_ids:
            if uid not in seen_user_ids:
                db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level="manage"))

    await db.flush()
    relationships_changed = await ensure_access_granted_platform_relationships(
        db,
        agent,
        created_by_user_id=current_user.id,
    )
    if relationships_changed:
        from app.api.relationships import _regenerate_relationships_file

        await _regenerate_relationships_file(db, agent_id)

    await db.commit()
    return {"status": "ok"}


@router.get("/{agent_id}/permissions/candidates")
async def get_agent_permission_candidates(
    agent_id: uuid.UUID,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return org members that can be granted custom access.

    For members without a linked platform account (user_id is None), we call
    get_platform_user_by_org_member which will find-or-create a User using the
    member's email/phone, then link it back to the OrgMember row.
    """
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    member_query = select(OrgMember).where(
        OrgMember.tenant_id == agent.tenant_id,
        OrgMember.status == "active",
    )
    if search:
        pattern = f"%{search}%"
        member_query = member_query.where(
            OrgMember.name.ilike(pattern)
            | OrgMember.email.ilike(pattern)
            | OrgMember.name_translit_full.ilike(pattern)
            | OrgMember.name_translit_initial.ilike(pattern)
        )

    members_result = await db.execute(member_query.order_by(OrgMember.name.asc()).limit(50))
    members = members_result.scalars().all()

    # For members already linked, batch-load User rows for display info.
    linked_user_ids = [m.user_id for m in members if m.user_id]
    users_by_id: dict[uuid.UUID, User] = {}
    if linked_user_ids:
        users_result = await db.execute(
            select(User)
            .where(User.id.in_(linked_user_ids), User.tenant_id == agent.tenant_id)
            .options(selectinload(User.identity))
        )
        users_by_id = {u.id: u for u in users_result.scalars().all()}

    from app.services.channel_user_service import get_platform_user_by_org_member

    candidates = []
    for m in members:
        if m.user_id:
            u = users_by_id.get(m.user_id)
        else:
            # No platform account yet — find-or-create one from OrgMember info
            # and link it back so future lookups hit Case 1.
            try:
                u = await get_platform_user_by_org_member(db, m, agent_tenant_id=agent.tenant_id)
            except Exception:
                # If user creation fails for any reason, skip this member
                continue

        if u is None:
            continue

        candidates.append(
            {
                "id": str(u.id),  # always a valid User.id
                "name": m.name,
                "username": u.username if u else None,
                "email": m.email or (u.email if u else None),
                "title": m.title or None,
                "avatar_url": m.avatar_url or None,
            }
        )

    await db.commit()

    return {
        "users": candidates,
        "agents": [],
    }


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent settings (creator or admin)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    is_admin = current_user.role in ("platform_admin", "org_admin")

    if not is_agent_creator(current_user, agent) and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can update agent settings"
        )

    update_data = data.model_dump(exclude_unset=True)

    # These fields select a subscription tier in the shared model pool; they
    # never grant direct access to a tenant-owned model object. Validate any
    # explicit change against the Agent's tenant and repair a stale counterpart
    # left by an earlier plan before persisting the pair.
    plan_fields = {"preferred_tier", "preferred_modality"}
    if agent.agent_type == "native" and plan_fields & set(update_data):
        ent = await get_tenant_entitlements(agent.tenant_id) if agent.tenant_id else None
        try:
            current_tier, current_modality = resolve_agent_plan_selection(
                ent,
                agent.preferred_tier,
                agent.preferred_modality,
                strict=False,
            )
            preferred_tier, preferred_modality = resolve_agent_plan_selection(
                ent,
                update_data.get("preferred_tier", current_tier),
                update_data.get("preferred_modality", current_modality),
            )
        except InvalidAgentPlanSelection as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        update_data["preferred_tier"] = preferred_tier
        update_data["preferred_modality"] = preferred_modality

    # expires_at: admin only
    if "expires_at" in update_data:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin can modify agent expiry time")
        from datetime import datetime, timezone as tz

        new_expires = update_data["expires_at"]
        # Allow any value: extend, shorten, or null (permanent).
        # Re-activate the agent if new expiry is in the future or cleared.
        if new_expires is None or new_expires > datetime.now(tz.utc):
            if agent.is_expired:
                agent.is_expired = False
                agent.status = "idle"

    # Enforce heartbeat floor from tenant
    clamped_fields = []  # track fields adjusted by tenant floor
    if "heartbeat_interval_minutes" in update_data and current_user.tenant_id:
        from app.models.tenant import Tenant

        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and update_data["heartbeat_interval_minutes"] < tenant.min_heartbeat_interval_minutes:
            update_data["heartbeat_interval_minutes"] = tenant.min_heartbeat_interval_minutes
            clamped_fields.append(
                {
                    "field": "heartbeat_interval_minutes",
                    "requested": update_data["heartbeat_interval_minutes"],
                    "applied": tenant.min_heartbeat_interval_minutes,
                    "reason": "company_floor",
                }
            )

    # Enforce trigger limit floors from tenant
    trigger_fields = {"min_poll_interval_min", "webhook_rate_limit", "max_triggers"}
    if trigger_fields & set(update_data.keys()) and current_user.tenant_id:
        from app.models.tenant import Tenant

        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            if "min_poll_interval_min" in update_data:
                original = update_data["min_poll_interval_min"]
                update_data["min_poll_interval_min"] = max(original, tenant.min_poll_interval_floor)
                if update_data["min_poll_interval_min"] != original:
                    clamped_fields.append(
                        {
                            "field": "min_poll_interval_min",
                            "requested": original,
                            "applied": update_data["min_poll_interval_min"],
                            "reason": "company_floor",
                        }
                    )
            if "webhook_rate_limit" in update_data:
                original = update_data["webhook_rate_limit"]
                update_data["webhook_rate_limit"] = min(original, tenant.max_webhook_rate_ceiling)
                if update_data["webhook_rate_limit"] != original:
                    clamped_fields.append(
                        {
                            "field": "webhook_rate_limit",
                            "requested": original,
                            "applied": update_data["webhook_rate_limit"],
                            "reason": "company_ceiling",
                        }
                    )

    for field, value in update_data.items():
        setattr(agent, field, value)
    await db.flush()

    # Sync Participant display_name / avatar if changed
    if "name" in update_data or "avatar_url" in update_data:
        from app.models.participant import Participant

        p_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id))
        p = p_r.scalar_one_or_none()
        if p:
            if "name" in update_data:
                p.display_name = agent.name
            if "avatar_url" in update_data:
                p.avatar_url = agent.avatar_url
            await db.flush()

    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an Agent only after runtime and provider cleanup is proven."""

    # Phase 1 is the global execution fence. Acquire Agent first (the same
    # order used by Gateway/chat/trigger authority paths), then persist a
    # stopped + deletion-requested state before any slow external cleanup.
    await check_agent_access(db, current_user, agent_id)
    agent = (
        await db.execute(
            select(Agent)
            .where(Agent.id == agent_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    fresh_user = (
        await db.execute(
            select(User)
            .where(User.id == current_user.id)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if fresh_user is None or not fresh_user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")
    if not is_agent_creator(fresh_user, agent) and fresh_user.role not in (
        "super_admin",
        "org_admin",
        "platform_admin",
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can delete agent")

    # System agents (OKR Agent, etc.) cannot be deleted — they are seeded by the
    # platform and required for core features. Disable them via settings instead.
    if agent.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System agents cannot be deleted. Disable the related feature (e.g. OKR) in Company Settings instead.",
        )

    blockers = await _agent_deletion_blockers(db, agent.id)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail=(
                "Agent has in-flight work that must be reconciled before "
                f"deletion: {', '.join(blockers)}"
            ),
        )

    agent.status = "stopped"
    agent.heartbeat_enabled = False
    agent.deletion_requested_at = (
        agent.deletion_requested_at or datetime.now(timezone.utc)
    )
    await db.commit()

    # Phase 2 performs strict external cleanup while every execution validator
    # sees the durable stopped/deletion fence.
    archive_dir: Path | None = None
    try:
        container_removed = await agent_manager.remove_container(agent)
    except Exception as exc:
        logger.error(
            "Agent container cleanup failed error_type={}",
            type(exc).__name__,
        )
        container_removed = False
    if not container_removed:
        raise HTTPException(
            status_code=409,
            detail="Agent runtime cleanup could not be verified; deletion remains fenced",
        )
    try:
        archive_dir = await agent_manager.archive_agent_files(agent.id)
    except Exception as exc:
        logger.error(
            "Agent workspace archive failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Agent workspace archive failed; deletion remains fenced",
        ) from exc
    if archive_dir is not None:
        try:
            await _archive_agent_task_history(db, agent.id, archive_dir)
        except Exception as exc:
            logger.error(
                "Agent task-history archive failed error_type={}",
                type(exc).__name__,
            )
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Agent task history archive failed; deletion remains fenced",
            ) from exc
    await db.rollback()

    from app.services.agentbay_client import agentbay_agent_deletion_fence

    # Delete related records only after every provider sandbox has reached a
    # confirmed terminal state. Any ambiguity leaves both Agent and ledger in
    # place for the platform-admin reconcile workflow.
    cleanup_tables = [
        "agent_activity_logs",
        "agent_schedules",
        "agent_triggers",
        "channel_configs",
        "agent_permissions",
        "agent_tools",
        "agent_relationships",
        "published_pages",
        "notifications",
        "daily_token_usage",
    ]

    secondary_fk_cleanups = [
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = :aid)",
        "DELETE FROM tasks WHERE agent_id = :aid",
    ]
    try:
        async with agentbay_agent_deletion_fence(agent_id=agent_id):
            from app.services.media_generation import (
                delete_private_media_recovery_assets_for_agent,
            )

            # Object storage is not transactional. The helper first commits a
            # durable deletion intent in its own short transaction, performs
            # idempotent object deletion without SQL locks, and then records
            # the acknowledgement. A later Agent-row failure cannot erase the
            # evidence or turn the deletion into an unexplained asset loss.
            await delete_private_media_recovery_assets_for_agent(agent_id)

            # Re-lock the parent before child deletes. The durable phase-1
            # fence must still exist; start/recover cannot clear it.
            locked_agent = (
                await db.execute(
                    select(Agent)
                    .where(Agent.id == agent_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_agent is None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            if (
                locked_agent.deletion_requested_at is None
                or locked_agent.status != "stopped"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Agent deletion fence is no longer valid",
                )

            blockers = await _agent_deletion_blockers(db, locked_agent.id)
            if blockers:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Agent acquired new in-flight work while deletion was "
                        f"being fenced: {', '.join(blockers)}"
                    ),
                )

            # Keep financial and external-side-effect ledgers.  Non-started
            # Douyin work is explicitly cancelled by the delete request;
            # completed/ambiguous rows retain their status for reconciliation.
            await db.execute(
                text("UPDATE audit_logs SET agent_id = NULL WHERE agent_id = :aid"),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE credit_transactions SET agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE credit_reservations SET agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE media_generation_tasks SET agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE douyin_accounts SET primary_agent_id = NULL "
                    "WHERE primary_agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE douyin_publish_jobs "
                    "SET status = CASE WHEN status IN "
                    "('approval_required', 'awaiting_user_publish', "
                    "'user_confirmed_waiting_verification') "
                    "THEN 'cancelled_agent_deleted' ELSE status END, "
                    "approval_status = CASE WHEN status IN "
                    "('approval_required', 'awaiting_user_publish', "
                    "'user_confirmed_waiting_verification') "
                    "THEN 'cancelled' ELSE approval_status END, "
                    "agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE douyin_operations "
                    "SET status = CASE WHEN status = 'pending_approval' "
                    "THEN 'cancelled_agent_deleted' ELSE status END, "
                    "approval_status = CASE WHEN status = 'pending_approval' "
                    "THEN 'cancelled' ELSE approval_status END, "
                    "agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE approval_requests "
                    "SET status = 'rejected', resolved_at = now(), "
                    "execution_status = 'not_required', "
                    "execution_claim_token = NULL, "
                    "execution_claimed_at = NULL, "
                    "execution_finished_at = NULL, "
                    "execution_attempts = 0, "
                    "execution_result_summary = "
                    "'{\"reason\":\"agent_deleted_before_approval\"}'::json, "
                    "execution_error_code = NULL "
                    "WHERE agent_id = :aid AND status = 'pending'"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE approval_requests "
                    "SET execution_status = 'legacy', "
                    "execution_claim_token = NULL, "
                    "execution_claimed_at = NULL, "
                    "execution_finished_at = NULL, "
                    "execution_attempts = 0, "
                    "execution_result_summary = "
                    "'{\"reason\":\"agent_deleted_execution_cancelled\"}'::json, "
                    "execution_error_code = NULL "
                    "WHERE agent_id = :aid AND status = 'approved' "
                    "AND execution_status = 'pending'"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE approval_requests SET agent_id = NULL "
                    "WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )

            # Preserve conversation and Group history as tombstones.  Agent
            # deletion removes future routing authority, not the user's
            # historical messages or collaboration audit trail.
            await db.execute(
                text("UPDATE chat_messages SET agent_id = NULL WHERE agent_id = :aid"),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE chat_sessions SET agent_id = NULL, is_primary = false, "
                    "deleted_at = COALESCE(deleted_at, now()) WHERE agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE chat_sessions SET peer_agent_id = NULL, is_primary = false, "
                    "deleted_at = COALESCE(deleted_at, now()) WHERE peer_agent_id = :aid"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "UPDATE group_members SET removed_at = COALESCE(removed_at, now()) "
                    "WHERE participant_id IN (SELECT id FROM participants "
                    "WHERE type = 'agent' AND ref_id = :aid)"
                ),
                {"aid": agent_id},
            )

            # A2A sessions are canonicalized on the lower Agent UUID.  Deleting
            # the peer must retire the entire conversation and every queued
            # Gateway delivery before either session or participant is removed.
            await db.execute(
                text(
                    "DELETE FROM gateway_messages "
                    "WHERE agent_id = :aid OR sender_agent_id = :aid "
                    "OR authorization_source_agent_id = :aid "
                    "OR conversation_id IN ("
                    "SELECT id::text FROM chat_sessions "
                    "WHERE agent_id = :aid OR peer_agent_id = :aid)"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "DELETE FROM chat_messages "
                    "WHERE agent_id = :aid OR conversation_id IN ("
                    "SELECT id::text FROM chat_sessions "
                    "WHERE agent_id = :aid OR peer_agent_id = :aid)"
                ),
                {"aid": agent_id},
            )
            await db.execute(
                text(
                    "DELETE FROM chat_sessions "
                    "WHERE agent_id = :aid OR peer_agent_id = :aid"
                ),
                {"aid": agent_id},
            )

            for table in cleanup_tables:
                await db.execute(
                    text(f"DELETE FROM {table} WHERE agent_id = :aid"),
                    {"aid": agent_id},
                )
            for sql in secondary_fk_cleanups:
                await db.execute(text(sql), {"aid": agent_id})

            await db.execute(
                text("DELETE FROM agent_agent_relationships WHERE agent_id = :aid OR target_agent_id = :aid"),
                {"aid": agent_id},
            )
            await db.execute(text("DELETE FROM plaza_posts WHERE author_id = :aid"), {"aid": str(agent_id)})
            await db.delete(locked_agent)
            await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "Agent deletion cleanup could not be verified error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Agent provider or database cleanup is incomplete; deletion remains fenced",
        ) from exc


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can start agent")
    if agent.deletion_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent deletion is pending provider cleanup and cannot be started",
        )

    from app.services.agent_manager import agent_manager

    await agent_manager.start_container(db, agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


@router.post("/{agent_id}/recover", response_model=AgentOut)
async def recover_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Repair an incomplete workspace and restart an agent after an error."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only manager can recover agent",
        )
    if agent.deletion_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent deletion is pending provider cleanup and cannot be recovered",
        )

    try:
        if agent.container_id:
            container_removed = await agent_manager.remove_container(agent)
            if not container_removed:
                raise RuntimeError(
                    "Existing Agent runtime cleanup could not be verified"
                )
        await agent_manager.initialize_agent_files(db, agent)
        await agent_manager.start_container(db, agent)
        if agent.status == "error":
            raise RuntimeError(agent.last_error or "Agent recovery failed")
        await db.flush()
    except Exception as exc:
        agent_manager.mark_error(agent, _format_agent_setup_error("recovery", exc))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=agent.last_error,
        ) from exc

    return await _agent_to_out(db, agent, current_user.id)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can stop agent")

    from app.services.agent_manager import agent_manager

    await agent_manager.stop_container(agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


# ─── Agent-Level Approvals ──────────────────────────────


@router.get("/{agent_id}/approvals", response_model=list[ApprovalRequestOut])
async def list_agent_approvals(
    agent_id: uuid.UUID,
    response: Response,
    status_filter: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests for a specific agent. Only creator or admin can view."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only agent creator or admin can view approvals"
        )

    from app.models.audit import ApprovalRequest
    from app.services.autonomy_service import approval_to_public_dict

    response.headers["Cache-Control"] = "no-store"

    query = select(ApprovalRequest).where(ApprovalRequest.agent_id == agent_id)
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter)
    query = query.order_by(ApprovalRequest.created_at.desc()).limit(limit)
    result = await db.execute(query)
    approvals = result.scalars().all()

    return [approval_to_public_dict(approval) for approval in approvals]


@router.post(
    "/{agent_id}/approvals/{approval_id}/resolve",
    response_model=ApprovalRequestOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resolve_agent_approval(
    agent_id: uuid.UUID,
    approval_id: uuid.UUID,
    data: ApprovalAction,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a pending approval for a specific agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.services.autonomy_service import autonomy_service

    try:
        approval = await autonomy_service.resolve_approval(
            db,
            approval_id,
            current_user,
            data.action,
            expected_agent_id=agent.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.services.autonomy_service import approval_to_public_dict

    return approval_to_public_dict(approval)


# ─── OpenClaw API Key Management ────────────────────────


@router.post("/{agent_id}/api-key")
async def generate_or_reset_api_key(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate or regenerate API key for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can manage API keys")
    if getattr(agent, "agent_type", "native") != "openclaw":
        raise HTTPException(status_code=400, detail="API keys are only available for OpenClaw agents")

    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    agent.api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await db.commit()

    return {"api_key": raw_key, "message": "Key configured successfully."}


@router.get("/{agent_id}/gateway-messages")
async def list_gateway_messages(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recent gateway messages for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.models.gateway_message import GatewayMessage
    from app.services.chat_session_access import can_audit_agent_chat_sessions

    query = select(GatewayMessage).where(GatewayMessage.agent_id == agent_id)
    if not can_audit_agent_chat_sessions(current_user):
        query = query.where(GatewayMessage.sender_user_id == current_user.id)
    result = await db.execute(
        query
        .order_by(GatewayMessage.created_at.desc())
        .limit(50)
    )
    messages = result.scalars().all()

    out = []
    for m in messages:
        sender_name = None
        if m.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == m.sender_agent_id))
            sender_name = r.scalar_one_or_none()
        out.append(
            {
                "id": str(m.id),
                "sender_agent_name": sender_name,
                "content": m.content,
                "status": m.status,
                "result": m.result,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
                "completed_at": m.completed_at.isoformat() if m.completed_at else None,
            }
        )
    return out
