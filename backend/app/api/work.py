"""Tenant-scoped task workbench without a second Runtime state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
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
from app.models.group import Group, GroupMember
from app.models.participant import Participant
from app.models.task import Task, TaskLog
from app.models.user import User
from app.schemas.work import (
    WorkArtifactSummary,
    WorkIndexOut,
    WorkItemOut,
    WorkTaskCreate,
    WorkTaskCreateOut,
    WorkTaskDraft,
    WorkTaskPreflight,
    WorkTaskPreflightOut,
)
from app.services.agent_runtime.model_route import (
    RuntimeModelRouteError,
    resolve_runtime_model_route,
)
from app.services.agent_runtime.model_capabilities import (
    PlatformModelConfigurationError,
    resolve_multi_agent_planning_model,
)
from app.services.group_chat_service import (
    GroupChatServiceError,
    authorize_group_session,
)
from app.services.task_executor import (
    TaskRuntimeIntakeError,
    enqueue_group_task_runtime,
    enqueue_task_runtime,
)
from app.services.work_projection import (
    TERMINAL_RUN_EVENTS,
    project_execution_status,
    project_user_stage,
)
from app.services.work_deliverable_contract import work_task_deliverable_contract


router = APIRouter(prefix="/api/work", tags=["work"])


@dataclass(frozen=True, slots=True)
class _ResolvedExecutor:
    primary_agent: Agent
    agents: tuple[Agent, ...]
    snapshot: dict


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")
    return user.tenant_id


def _fingerprint(data: WorkTaskDraft) -> str:
    payload = data.model_dump(
        mode="json",
        exclude={"client_request_id", "confirmation_fingerprint"},
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _confirmation_fingerprint(data: WorkTaskDraft, *, agent_id: uuid.UUID) -> str:
    evidence = f"{_fingerprint(data)}:{agent_id}"
    return hashlib.sha256(evidence.encode("utf-8")).hexdigest()


_EXPECTED_OUTPUT_BY_WORK_TYPE = {
    "general": "task_result",
    "image": "confirmed_image_brief",
    "video": "confirmed_video_brief",
    "presentation": "confirmed_presentation_brief",
    "document": "confirmed_document_brief",
}


def _build_work_statement(
    data: WorkTaskDraft,
    *,
    agent: Agent,
    executor_snapshot: dict,
    capability_status: str = "available",
) -> dict:
    expected_output = _EXPECTED_OUTPUT_BY_WORK_TYPE[data.work_type]
    completion_criteria = [
        "Return the concrete execution result to the task workbench.",
        "Preserve the confirmed objective, executor and output boundary.",
    ]
    if data.work_type in {"image", "video", "presentation"}:
        completion_criteria.append(
            "Do not claim a formal creative artifact until a linked Deliverable passes its own preflight, review and approval gates."
        )
    return {
        "version": 1,
        "objective": data.intent.strip(),
        "title": data.title.strip(),
        "work_type": data.work_type,
        "expected_output": expected_output,
        "delivery_mode": "task_only",
        "priority": data.priority,
        "executor": {
            "kind": data.executor_kind,
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "expert_role": executor_snapshot.get("expert_role"),
            "group_id": executor_snapshot.get("group_id"),
            "group_name": executor_snapshot.get("group_name"),
            "group_session_id": executor_snapshot.get("group_session_id"),
            "group_session_title": executor_snapshot.get("group_session_title"),
            "participants": list(executor_snapshot.get("participants") or []),
        },
        "capability_preflight": {
            "status": capability_status,
            "scope": "task_execution",
            "provider_selection": "platform_managed",
        },
        "cost": {
            "estimated_credits": None,
            "basis": "usage_based_task_execution",
            "formal_media_requires_separate_preflight": data.work_type
            in {"image", "video", "presentation"},
        },
        "approval": {
            "required_to_start": False,
            "runtime_actions_checked_separately": True,
        },
        "completion_criteria": completion_criteria,
    }


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
    data: WorkTaskDraft,
    user: User,
) -> _ResolvedExecutor:
    tenant_id = _tenant_id(user)
    if data.executor_kind == "group":
        assert data.group_id is not None
        assert data.group_session_id is not None
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "user",
                    Participant.ref_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if participant is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "group_access_denied", "message": "Group membership is required"},
            )
        try:
            session = await authorize_group_session(
                db,
                tenant_id=tenant_id,
                group_id=data.group_id,
                session_id=data.group_session_id,
                participant_id=participant.id,
                human_only=True,
            )
        except GroupChatServiceError as exc:
            response_status = 404 if exc.code.endswith("not_found") else 403
            raise HTTPException(
                status_code=response_status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        group = (
            await db.execute(
                select(Group).where(
                    Group.id == data.group_id,
                    Group.tenant_id == tenant_id,
                    Group.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found")

        memberships = list(
            (
                await db.execute(
                    select(GroupMember, Participant)
                    .join(Participant, Participant.id == GroupMember.participant_id)
                    .where(
                        GroupMember.group_id == group.id,
                        GroupMember.participant_id.in_(data.group_agent_participant_ids),
                        GroupMember.removed_at.is_(None),
                    )
                )
            ).all()
        )
        participant_by_id = {
            member.participant_id: member_participant
            for member, member_participant in memberships
        }
        if set(participant_by_id) != set(data.group_agent_participant_ids):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "group_agent_membership_changed",
                    "message": "One or more selected Agents are no longer Group members",
                },
            )
        ordered_participants = [
            participant_by_id[participant_id]
            for participant_id in data.group_agent_participant_ids
        ]
        if any(member_participant.type != "agent" for member_participant in ordered_participants):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "group_agent_participant_required",
                    "message": "Group task collaborators must be Agent participants",
                },
            )
        agent_ids = [member_participant.ref_id for member_participant in ordered_participants]
        agent_by_id = {
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
        if set(agent_by_id) != set(agent_ids):
            raise HTTPException(status_code=409, detail="A selected Group Agent is unavailable")
        agents = tuple(agent_by_id[agent_id] for agent_id in agent_ids)
        if any(not is_agent_executable(agent) for agent in agents):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "group_agent_unavailable",
                    "message": "A selected Group Agent cannot currently execute tasks",
                },
            )
        snapshot = {
            "agent_id": str(agents[0].id),
            "agent_name": agents[0].name,
            "group_id": str(group.id),
            "group_name": group.name,
            "group_session_id": str(session.id),
            "group_session_title": session.title,
            "sender_participant_id": str(participant.id),
            "participants": [
                {
                    "participant_id": str(member_participant.id),
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "role_description": agent.role_description or "",
                    "responsibility": (
                        "primary_owner" if index == 0 else "collaborator"
                    ),
                }
                for index, (member_participant, agent) in enumerate(
                    zip(ordered_participants, agents, strict=True)
                )
            ],
        }
        return _ResolvedExecutor(
            primary_agent=agents[0],
            agents=agents,
            snapshot=snapshot,
        )

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
    return _ResolvedExecutor(primary_agent=agent, agents=(agent,), snapshot=snapshot)


async def _executor_capability(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    executor: _ResolvedExecutor,
) -> tuple[str, list[str], str | None]:
    reasons: list[str] = []
    for agent in executor.agents:
        try:
            await resolve_runtime_model_route(agent)
        except RuntimeModelRouteError:
            reasons.append(f"text_route_unavailable:{agent.id}")
    if len(executor.agents) > 1:
        try:
            await resolve_multi_agent_planning_model(db, tenant_id=tenant_id)
        except PlatformModelConfigurationError:
            reasons.append("group_planning_route_unavailable")
    if reasons:
        return (
            "unavailable",
            reasons,
            "ask_company_admin_to_configure_available_execution_routes",
        )
    return "available", [], None


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
        task_source_ids = [str(candidate_id) for candidate_id in task_ids]
        task_correlation_ids = [f"work-task:{candidate_id}" for candidate_id in task_ids]
        runs = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.tenant_id == tenant_id,
                        or_(
                            (
                                (AgentRun.source_type == "task")
                                & AgentRun.source_id.in_(task_source_ids)
                            ),
                            AgentRun.correlation_id.in_(task_correlation_ids),
                        ),
                    )
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            ).scalars().all()
        )
    run_by_task: dict[uuid.UUID, AgentRun] = {}
    for run in runs:
        try:
            projected_task_id = (
                uuid.UUID(run.correlation_id.removeprefix("work-task:"))
                if run.correlation_id and run.correlation_id.startswith("work-task:")
                else uuid.UUID(run.source_id or "")
            )
        except ValueError:
            continue
        run_by_task.setdefault(projected_task_id, run)

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

    latest_log_by_task: dict[uuid.UUID, TaskLog] = {}
    if task_ids:
        logs = list(
            (
                await db.execute(
                    select(TaskLog)
                    .where(TaskLog.task_id.in_(task_ids))
                    .order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
                )
            ).scalars().all()
        )
        for log in logs:
            latest_log_by_task.setdefault(log.task_id, log)

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
        latest_log = latest_log_by_task.get(task.id)
        artifact_status, review_status, delivery_status = deliverable_facts(request)
        formal_delivery_contract = work_task_deliverable_contract(task)
        items.append(
            WorkItemOut(
                id=task.id,
                kind="task",
                title=task.title,
                intent=task.intent,
                origin_type=task.origin_type,
                executor_kind=task.executor_kind,
                executor_snapshot=dict(task.executor_snapshot or {}),
                work_statement=dict(task.work_statement or {}),
                formal_delivery_spec=(
                    dict(formal_delivery_contract.spec)
                    if formal_delivery_contract is not None
                    else {}
                ),
                confirmed_at=task.confirmed_at,
                agent_id=agent.id,
                agent_name=agent.name,
                task_id=task.id,
                task_status=task.status,
                priority=task.priority,
                run_id=run.id if run else None,
                execution_status=execution_status,
                deliverable_id=request.id if request else None,
                work_type=request.work_type if request else task.work_type,
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
                latest_update=latest_log.content if latest_log else None,
                latest_update_at=latest_log.created_at if latest_log else None,
                deep_link=(
                    f"/groups/{task.group_id}/{task.executor_snapshot.get('group_session_id')}"
                    if task.executor_kind == "group" and task.group_id is not None
                    else (
                        f"/agents/{agent.id}/chat?session_id={request.session_id}&task_id={task.id}"
                        if request
                        else f"/agents/{agent.id}/chat?task_id={task.id}"
                    )
                ),
                formal_delivery_link=(
                    f"/agents/{agent.id}/chat?task_id={task.id}"
                    if task.executor_kind == "group"
                    else (
                        f"/agents/{agent.id}/chat?session_id={request.session_id}&task_id={task.id}"
                        if request
                        else f"/agents/{agent.id}/chat?task_id={task.id}"
                    )
                ),
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
                work_statement={},
                formal_delivery_spec={},
                confirmed_at=None,
                agent_id=agent.id,
                agent_name=agent.name,
                priority=None,
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
                formal_delivery_link=None,
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


@router.post("/tasks/preflight", response_model=WorkTaskPreflightOut)
async def preflight_work_task(
    data: WorkTaskPreflight,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    resolved = await _resolve_executor(db, data=data, user=current_user)
    capability_status, reasons, next_action = await _executor_capability(
        db,
        tenant_id=_tenant_id(current_user),
        executor=resolved,
    )
    return WorkTaskPreflightOut(
        confirmation_fingerprint=_confirmation_fingerprint(
            data,
            agent_id=resolved.primary_agent.id,
        ),
        capability_status=capability_status,
        estimated_credits=None,
        cost_note=(
            "Usage-based task execution; formal media generation requires a separate Deliverable preflight."
        ),
        approval_required=False,
        reasons=reasons,
        next_action=next_action,
        work_statement=_build_work_statement(
            data,
            agent=resolved.primary_agent,
            executor_snapshot=resolved.snapshot,
            capability_status=capability_status,
        ),
    )


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


@router.get("/tasks/{task_id}", response_model=WorkItemOut)
async def get_work_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore one tenant- and creator-scoped Work contract for formal handoff."""

    return await _work_item_for_task(db, user=current_user, task_id=task_id)


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

    resolved = await _resolve_executor(db, data=data, user=current_user)
    capability_status, reasons, next_action = await _executor_capability(
        db,
        tenant_id=tenant_id,
        executor=resolved,
    )
    if capability_status == "unavailable":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_capability_changed",
                "message": "The confirmed executor route is no longer available; run preflight again.",
                "reasons": reasons,
                "next_action": next_action,
            },
        )
    expected_confirmation = _confirmation_fingerprint(
        data,
        agent_id=resolved.primary_agent.id,
    )
    if not hmac.compare_digest(data.confirmation_fingerprint, expected_confirmation):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_confirmation_stale",
                "message": "The work statement changed after preflight; review and confirm it again.",
            },
        )
    work_statement = _build_work_statement(
        data,
        agent=resolved.primary_agent,
        executor_snapshot=resolved.snapshot,
    )
    task = Task(
        tenant_id=tenant_id,
        agent_id=resolved.primary_agent.id,
        title=data.title.strip(),
        description=data.intent.strip(),
        intent=data.intent.strip(),
        origin_type="workbench",
        executor_kind=data.executor_kind,
        executor_snapshot=resolved.snapshot,
        work_type=data.work_type,
        work_statement=work_statement,
        confirmation_fingerprint=expected_confirmation,
        confirmed_at=datetime.now(UTC),
        client_request_id=data.client_request_id,
        request_fingerprint=fingerprint,
        group_id=data.group_id if data.executor_kind == "group" else None,
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
        try:
            if data.executor_kind == "group":
                runtime_handle = await enqueue_group_task_runtime(
                    db,
                    task=task,
                    primary_agent=resolved.primary_agent,
                )
            else:
                runtime_handle = await enqueue_task_runtime(
                    db,
                    task=task,
                    agent=resolved.primary_agent,
                )
        except TaskRuntimeIntakeError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        if runtime_handle is None:
            raise HTTPException(
                status_code=503,
                detail="Unified Agent Runtime is not enabled for tasks",
            )
        created = True
    await db.commit()

    item = await _work_item_for_task(db, user=current_user, task_id=task.id)
    return WorkTaskCreateOut(item=item, created=created)
