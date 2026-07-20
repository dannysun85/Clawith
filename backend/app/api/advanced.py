"""Agent collaboration and template market API routes."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import check_agent_access
from app.core.security import get_current_user, get_current_admin
from app.database import get_db
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.org import AgentRelationship, OrgMember
from app.models.user import User
from app.services.collaboration import collaboration_service

router = APIRouter(tags=["advanced"])


# ─── Collaboration ──────────────────────────────────────

class DelegateRequest(BaseModel):
    to_agent_id: uuid.UUID
    task_title: str = Field(min_length=1, max_length=200)
    task_description: str = Field(default="", max_length=10000)


class InterAgentMessage(BaseModel):
    to_agent_id: uuid.UUID
    message: str = Field(min_length=1, max_length=20000)
    msg_type: Literal["notify", "consult"] = "notify"


@router.get("/agents/{agent_id}/collaborators")
async def list_collaborators(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List agents that can collaborate with this agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    return await collaboration_service.list_collaborators(
        db,
        agent,
        requester=current_user,
    )


@router.post("/agents/{agent_id}/collaborate/delegate")
async def delegate_task(
    agent_id: uuid.UUID,
    data: DelegateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delegate a task from one agent to another."""
    await check_agent_access(db, current_user, agent_id)
    try:
        result = await collaboration_service.delegate_task(
            db,
            agent_id,
            data.to_agent_id,
            data.task_title,
            data.task_description,
            requester=current_user,
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/agents/{agent_id}/collaborate/message")
async def send_inter_agent_message(
    agent_id: uuid.UUID,
    data: InterAgentMessage,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message between agents."""
    await check_agent_access(db, current_user, agent_id)
    try:
        return await collaboration_service.send_message_between_agents(
            db,
            agent_id,
            data.to_agent_id,
            data.message,
            data.msg_type,
            requester=current_user,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ─── Template Market ────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    category: str = "general"
    soul_template: str = ""
    default_skills: list[str] = Field(default_factory=list)
    default_tools: list[str] = Field(default_factory=list)
    default_autonomy_policy: dict = Field(default_factory=dict)


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    icon: str
    category: str
    soul_template: str
    default_skills: list
    default_tools: list
    default_autonomy_policy: dict
    is_builtin: bool
    created_at: str | None = None

    model_config = {"from_attributes": True}


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List available agent templates."""
    query = select(AgentTemplate).order_by(AgentTemplate.name)
    if category:
        query = query.where(AgentTemplate.category == category)
    result = await db.execute(query)
    return [TemplateOut.model_validate(t) for t in result.scalars().all()]


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get template details."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateOut.model_validate(template)


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    data: TemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new agent template (share to template market)."""
    if data.default_tools or data.default_autonomy_policy:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Executable default Tools and autonomy policies are reserved for "
                "reviewed builtin templates. Users can configure them explicitly "
                "after creating an Agent."
            ),
        )
    template = AgentTemplate(
        name=data.name,
        description=data.description,
        icon=data.icon,
        category=data.category,
        soul_template=data.soul_template,
        default_skills=data.default_skills,
        default_tools=[],
        default_autonomy_policy={},
        created_by=current_user.id,
    )
    db.add(template)
    await db.flush()
    return TemplateOut.model_validate(template)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a template (admin or creator)."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)


# ─── Agent Handover ─────────────────────────────────────

class HandoverRequest(BaseModel):
    new_creator_id: uuid.UUID


@router.post("/agents/{agent_id}/handover")
async def handover_agent(
    agent_id: uuid.UUID,
    data: HandoverRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Transfer ownership of a digital employee to another user."""
    from app.core.permissions import is_agent_creator
    from app.models.audit import AuditLog

    agent, _access = await check_agent_access(db, current_user, agent_id)
    locked_agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked_agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not is_agent_creator(current_user, locked_agent):
        raise HTTPException(status_code=403, detail="Only creator can handover agent")

    # A handover is an in-tenant ownership change. Cross-tenant moves require a
    # separate platform-admin migration workflow because billing, automation,
    # relationships and storage are tenant-bound.
    new_creator_result = await db.execute(
        select(User)
        .where(
            User.id == data.new_creator_id,
            User.tenant_id == locked_agent.tenant_id,
            User.is_active == True,  # noqa: E712
        )
        .options(selectinload(User.identity))
        .with_for_update()
    )
    new_creator = new_creator_result.scalar_one_or_none()
    if (
        not new_creator
        or new_creator.identity is None
        or not new_creator.identity.is_active
    ):
        raise HTTPException(
            status_code=403,
            detail="Target user must be an active member of this tenant",
        )

    old_creator_id = locked_agent.creator_id
    if old_creator_id != data.new_creator_id:
        permission_result = await db.execute(
            select(AgentPermission)
            .where(
                AgentPermission.agent_id == agent_id,
                AgentPermission.scope_type == "user",
                AgentPermission.scope_id.in_([old_creator_id, data.new_creator_id]),
            )
            .with_for_update()
        )
        permissions = permission_result.scalars().all()
        new_creator_permission = None
        for permission in permissions:
            if permission.scope_id == old_creator_id:
                await db.delete(permission)
            elif permission.scope_id == data.new_creator_id:
                new_creator_permission = permission
        if new_creator_permission is None:
            db.add(
                AgentPermission(
                    agent_id=agent_id,
                    scope_type="user",
                    scope_id=data.new_creator_id,
                    access_level="manage",
                )
            )
        else:
            new_creator_permission.access_level = "manage"

        if (locked_agent.access_mode or "company") in {"private", "custom"}:
            await db.execute(
                delete(AgentRelationship).where(
                    AgentRelationship.agent_id == agent_id,
                    AgentRelationship.member_id.in_(
                        select(OrgMember.id).where(
                            OrgMember.tenant_id == locked_agent.tenant_id,
                            OrgMember.user_id == old_creator_id,
                        )
                    ),
                )
            )

        locked_agent.creator_id = data.new_creator_id
        await db.flush()
        from app.services.access_relationships import (
            ensure_access_granted_platform_relationships,
        )

        await ensure_access_granted_platform_relationships(
            db,
            locked_agent,
            created_by_user_id=current_user.id,
        )

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent_id,
        action="agent:handover",
        details={
            "from_creator": str(old_creator_id),
            "to_creator": str(data.new_creator_id),
        },
    ))
    await db.flush()

    return {
        "status": "transferred",
        "agent_name": locked_agent.name,
        "new_creator": new_creator.display_name,
    }


# ─── Observability ──────────────────────────────────────

@router.get("/agents/{agent_id}/metrics")
async def get_agent_metrics(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get observability metrics for an agent."""
    from sqlalchemy import func
    from app.models.task import Task
    from app.models.audit import AuditLog, ApprovalRequest

    agent, _access = await check_agent_access(db, current_user, agent_id)

    # Task stats
    total_tasks = await db.execute(select(func.count(Task.id)).where(Task.agent_id == agent_id))
    done_tasks = await db.execute(
        select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "done")
    )
    pending_tasks = await db.execute(
        select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "pending")
    )

    # Approval stats
    total_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(ApprovalRequest.agent_id == agent_id)
    )
    pending_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.agent_id == agent_id, ApprovalRequest.status == "pending"
        )
    )

    # Recent activity count (last 24h)
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_actions = await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.agent_id == agent_id, AuditLog.created_at >= cutoff
        )
    )

    # Container status
    from app.services.agent_manager import agent_manager
    container_status = agent_manager.get_container_status(agent)

    # Extract scalar values (each result can only be consumed once)
    _total_tasks = total_tasks.scalar() or 0
    _done_tasks = done_tasks.scalar() or 0
    _pending_tasks = pending_tasks.scalar() or 0
    _total_approvals = total_approvals.scalar() or 0
    _pending_approvals = pending_approvals.scalar() or 0
    _recent_actions = recent_actions.scalar() or 0

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "status": agent.status,
        "container": container_status,
        "tokens": {
            "used_today": agent.tokens_used_today,
            "used_month": agent.tokens_used_month,
            "used_total": agent.tokens_used_total,
            "cache_read_today": agent.cache_read_tokens_today,
            "cache_read_month": agent.cache_read_tokens_month,
            "cache_read_total": agent.cache_read_tokens_total,
            "cache_creation_today": agent.cache_creation_tokens_today,
            "cache_creation_month": agent.cache_creation_tokens_month,
            "cache_creation_total": agent.cache_creation_tokens_total,
            "cache_hit_rate_today": round((agent.cache_read_tokens_today or 0) / max(agent.tokens_used_today or 0, 1), 4),
            "cache_hit_rate_month": round((agent.cache_read_tokens_month or 0) / max(agent.tokens_used_month or 0, 1), 4),
            "cache_hit_rate_total": round((agent.cache_read_tokens_total or 0) / max(agent.tokens_used_total or 0, 1), 4),
            "limit_day": agent.max_tokens_per_day,
            "limit_month": agent.max_tokens_per_month,
        },
        "tasks": {
            "total": _total_tasks,
            "done": _done_tasks,
            "pending": _pending_tasks,
            "completion_rate": round(
                _done_tasks / max(_total_tasks, 1) * 100, 1
            ),
        },
        "approvals": {
            "total": _total_approvals,
            "pending": _pending_approvals,
        },
        "activity": {
            "actions_last_24h": _recent_actions,
        },
    }
