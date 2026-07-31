"""Tenant-scoped task workbench without a second Runtime state machine."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_executable
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableRequest,
)
from app.models.onboarding import UserTenantOnboarding
from app.models.task import Task
from app.models.user import User
from app.schemas.work import (
    WorkArtifactSummary,
    WorkIndexOut,
    WorkItemOut,
    WorkTaskCreate,
    WorkTaskCreateOut,
)
from app.services.task_executor import enqueue_task_runtime
from app.services.work_projection import (
    TERMINAL_RUN_EVENTS,
    project_execution_status,
    project_user_stage,
)


router = APIRouter(prefix="/api/work", tags=["work"])


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")
    return user.tenant_id


def _fingerprint(data: WorkTaskCreate) -> str:
    payload = data.model_dump(mode="json", exclude={"client_request_id"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _personal_assistant_id(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID | None:
    return (
        await db.execute(
            select(UserTenantOnboarding.personal_assistant_agent_id).where(
                UserTenantOnboarding.tenant_id == tenant_id,
                UserTenantOnboarding.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def _resolve_executor(
    db: AsyncSession,
    *,
    data: WorkTaskCreate,
    user: User,
) -> tuple[Agent, dict]:
    tenant_id = _tenant_id(user)
    if data.executor_kind == "agent_employee":
        assert data.agent_id is not None
        agent, _ = await check_agent_access(db, user, data.agent_id)
        if agent.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        assistant_id = await _personal_assistant_id(
            db,
            tenant_id=tenant_id,
            user_id=user.id,
        )
        if assistant_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "personal_assistant_required", "onboarding_url": "/onboarding?mode=join"},
            )
        agent, _ = await check_agent_access(db, user, assistant_id)
    if not is_agent_executable(agent):
        raise HTTPException(status_code=409, detail="Selected executor is not available")

    snapshot = {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "role_description": agent.role_description or "",
    }
    if data.executor_kind == "temporary_expert":
        snapshot.update(
            {
                "expert_role": data.expert_role,
                "scope": "task_run_only",
                "inherits_long_term_memory": False,
                "visible_as_employee": False,
            }
        )
    return agent, snapshot


async def _work_items(
    db: AsyncSession,
    *,
    user: User,
    limit: int,
    task_id: uuid.UUID | None = None,
) -> WorkIndexOut:
    tenant_id = _tenant_id(user)
    task_query = select(Task).where(
        Task.tenant_id == tenant_id,
        Task.created_by == user.id,
    )
    if task_id is not None:
        task_query = task_query.where(Task.id == task_id)
    tasks = list(
        (
            await db.execute(
                task_query.order_by(Task.updated_at.desc(), Task.id.desc()).limit(limit)
            )
        ).scalars().all()
    )
    task_ids = [task.id for task in tasks]
    runs = []
    if task_ids:
        runs = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.tenant_id == tenant_id,
                        AgentRun.source_type == "task",
                        AgentRun.source_id.in_([str(task_id) for task_id in task_ids]),
                    )
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            ).scalars().all()
        )
    run_by_task: dict[uuid.UUID, AgentRun] = {}
    for run in runs:
        try:
            task_id = uuid.UUID(run.source_id or "")
        except ValueError:
            continue
        run_by_task.setdefault(task_id, run)

    terminal_event_by_run: dict[uuid.UUID, str] = {}
    run_ids = [run.id for run in run_by_task.values()]
    if run_ids:
        events = list(
            (
                await db.execute(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.tenant_id == tenant_id,
                        AgentRunEvent.run_id.in_(run_ids),
                        AgentRunEvent.event_type.in_(tuple(TERMINAL_RUN_EVENTS)),
                    )
                    .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
                )
            ).scalars().all()
        )
        for event in events:
            terminal_event_by_run.setdefault(event.run_id, event.event_type)

    deliverable_query = select(DeliverableRequest).where(
        DeliverableRequest.tenant_id == tenant_id,
        DeliverableRequest.created_by_user_id == user.id,
    )
    if task_id is not None:
        deliverable_query = deliverable_query.where(DeliverableRequest.task_id == task_id)
    deliverables = list(
        (
            await db.execute(
                deliverable_query.order_by(
                    DeliverableRequest.updated_at.desc(),
                    DeliverableRequest.id.desc(),
                ).limit(limit)
            )
        ).scalars().all()
    )
    deliverable_by_task: dict[uuid.UUID, DeliverableRequest] = {}
    standalone_deliverables: list[DeliverableRequest] = []
    for request in deliverables:
        if request.task_id is not None and request.task_id in task_ids:
            deliverable_by_task.setdefault(request.task_id, request)
        else:
            standalone_deliverables.append(request)

    request_ids = [request.id for request in deliverables]
    artifacts_by_request: dict[uuid.UUID, list[DeliverableArtifactRevision]] = {}
    review_by_request: dict[uuid.UUID, DeliverableQualityReview] = {}
    if request_ids:
        artifacts = list(
            (
                await db.execute(
                    select(DeliverableArtifactRevision)
                    .where(
                        DeliverableArtifactRevision.tenant_id == tenant_id,
                        DeliverableArtifactRevision.request_id.in_(request_ids),
                    )
                    .order_by(
                        DeliverableArtifactRevision.created_at.desc(),
                        DeliverableArtifactRevision.id.desc(),
                    )
                )
            ).scalars().all()
        )
        for artifact in artifacts:
            artifacts_by_request.setdefault(artifact.request_id, []).append(artifact)
        reviews = list(
            (
                await db.execute(
                    select(DeliverableQualityReview)
                    .where(
                        DeliverableQualityReview.tenant_id == tenant_id,
                        DeliverableQualityReview.request_id.in_(request_ids),
                    )
                    .order_by(
                        DeliverableQualityReview.created_at.desc(),
                        DeliverableQualityReview.id.desc(),
                    )
                )
            ).scalars().all()
        )
        for review in reviews:
            review_by_request.setdefault(review.request_id, review)

    agent_ids = {task.agent_id for task in tasks} | {
        request.agent_id for request in deliverables
    }
    agents = {}
    if agent_ids:
        agents = {
            agent.id: agent
            for agent in (
                await db.execute(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).scalars().all()
        }

    def artifact_summaries(request: DeliverableRequest | None) -> list[WorkArtifactSummary]:
        if request is None:
            return []
        return [
            WorkArtifactSummary.model_validate(artifact)
            for artifact in artifacts_by_request.get(request.id, [])
        ]

    def deliverable_facts(request: DeliverableRequest | None) -> tuple[str | None, str | None, str]:
        if request is None:
            return None, None, "not_requested"
        summaries = artifacts_by_request.get(request.id, [])
        artifact_status = summaries[0].status if summaries else None
        review = review_by_request.get(request.id)
        delivery_status = (
            "delivered"
            if request.status == "succeeded" and artifact_status == "approved"
            else "pending"
        )
        return artifact_status, review.status if review else None, delivery_status

    items: list[WorkItemOut] = []
    for task in tasks:
        agent = agents.get(task.agent_id)
        if agent is None:
            continue
        run = run_by_task.get(task.id)
        execution_status = project_execution_status(
            task_status=task.status,
            terminal_run_event=terminal_event_by_run.get(run.id) if run else None,
        )
        request = deliverable_by_task.get(task.id)
        artifact_status, review_status, delivery_status = deliverable_facts(request)
        items.append(
            WorkItemOut(
                id=task.id,
                kind="task",
                title=task.title,
                intent=task.intent,
                origin_type=task.origin_type,
                executor_kind=task.executor_kind,
                executor_snapshot=dict(task.executor_snapshot or {}),
                agent_id=agent.id,
                agent_name=agent.name,
                task_id=task.id,
                task_status=task.status,
                run_id=run.id if run else None,
                execution_status=execution_status,
                deliverable_id=request.id if request else None,
                work_type=request.work_type if request else None,
                deliverable_status=request.status if request else None,
                artifact_status=artifact_status,
                review_status=review_status,
                approval_status=(
                    "pending" if request and request.status == "waiting_approval" else None
                ),
                delivery_status=delivery_status,
                delivery_mode="formal_deliverable" if request else "task_only",
                user_stage=project_user_stage(
                    task_status=task.status,
                    execution_status=execution_status,
                    deliverable_status=request.status if request else None,
                    artifact_status=artifact_status,
                    review_status=review_status,
                ),
                artifacts=artifact_summaries(request),
                deep_link=f"/agents/{agent.id}/chat",
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )

    for request in standalone_deliverables:
        agent = agents.get(request.agent_id)
        if agent is None:
            continue
        artifact_status, review_status, delivery_status = deliverable_facts(request)
        execution_status = (
            "failed"
            if request.status == "failed"
            else "completed"
            if request.status in {"succeeded", "waiting_approval"}
            else "running"
            if request.status == "running"
            else "queued"
        )
        items.append(
            WorkItemOut(
                id=request.id,
                kind="deliverable",
                title=request.goal[:500],
                intent=request.goal,
                origin_type="agent_chat",
                executor_kind="agent_employee",
                executor_snapshot={"agent_id": str(agent.id), "agent_name": agent.name},
                agent_id=agent.id,
                agent_name=agent.name,
                run_id=request.agent_run_id,
                execution_status=execution_status,
                deliverable_id=request.id,
                work_type=request.work_type,
                deliverable_status=request.status,
                artifact_status=artifact_status,
                review_status=review_status,
                approval_status="pending" if request.status == "waiting_approval" else None,
                delivery_status=delivery_status,
                delivery_mode="formal_deliverable",
                user_stage=project_user_stage(
                    task_status=None,
                    execution_status=execution_status,
                    deliverable_status=request.status,
                    artifact_status=artifact_status,
                    review_status=review_status,
                ),
                artifacts=artifact_summaries(request),
                deep_link=f"/agents/{agent.id}/chat?session_id={request.session_id}",
                created_at=request.created_at,
                updated_at=request.updated_at,
            )
        )

    items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
    assistant_id = await _personal_assistant_id(
        db,
        tenant_id=tenant_id,
        user_id=user.id,
    )
    return WorkIndexOut(
        items=items[:limit],
        personal_assistant_agent_id=assistant_id,
    )


@router.get("", response_model=WorkIndexOut)
async def list_work(
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _work_items(db, user=current_user, limit=limit)


async def _work_item_for_task(
    db: AsyncSession,
    *,
    user: User,
    task_id: uuid.UUID,
) -> WorkItemOut:
    index = await _work_items(db, user=user, limit=1, task_id=task_id)
    item = next((candidate for candidate in index.items if candidate.task_id == task_id), None)
    if item is None:
        raise HTTPException(
            status_code=409,
            detail="Task exists but cannot be projected in the current company context",
        )
    return item


@router.post(
    "/tasks",
    response_model=WorkTaskCreateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_work_task(
    data: WorkTaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    fingerprint = _fingerprint(data)
    existing = (
        await db.execute(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.created_by == current_user.id,
                Task.client_request_id == data.client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another task",
            )
        item = await _work_item_for_task(db, user=current_user, task_id=existing.id)
        return WorkTaskCreateOut(item=item, created=False)

    agent, executor_snapshot = await _resolve_executor(db, data=data, user=current_user)
    task = Task(
        tenant_id=tenant_id,
        agent_id=agent.id,
        title=data.title.strip(),
        description=data.intent.strip(),
        intent=data.intent.strip(),
        origin_type="workbench",
        executor_kind=data.executor_kind,
        executor_snapshot=executor_snapshot,
        client_request_id=data.client_request_id,
        request_fingerprint=fingerprint,
        type="todo",
        priority=data.priority,
        created_by=current_user.id,
    )
    try:
        async with db.begin_nested():
            db.add(task)
            await db.flush()
    except IntegrityError:
        concurrent = (
            await db.execute(
                select(Task).where(
                    Task.tenant_id == tenant_id,
                    Task.created_by == current_user.id,
                    Task.client_request_id == data.client_request_id,
                )
            )
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        if concurrent.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another task",
            )
        task = concurrent
        created = False
    else:
        runtime_handle = await enqueue_task_runtime(db, task=task, agent=agent)
        if runtime_handle is None:
            raise HTTPException(
                status_code=503,
                detail="Unified Agent Runtime is not enabled for tasks",
            )
        created = True
    await db.commit()

    item = await _work_item_for_task(db, user=current_user, task_id=task.id)
    return WorkTaskCreateOut(item=item, created=created)
