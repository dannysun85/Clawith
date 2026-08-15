"""Build a bounded, viewer-scoped read model for the workforce topology."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    build_manageable_agents_query,
    build_visible_agents_query,
)
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableRequest,
)
from app.models.gateway_message import GatewayMessage
from app.models.org import AgentAgentRelationship
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.workforce_topology import (
    WorkforceTopologyActivityEdgeOut,
    WorkforceTopologyActivityOut,
    WorkforceTopologyNodeOut,
    WorkforceTopologyOut,
    WorkforceTopologyRelationshipEdgeOut,
    WorkforceTopologyWorkOut,
)
from app.services.product_roles import resolve_agent_product_roles
from app.services.work_projection import (
    TERMINAL_RUN_EVENTS,
    project_execution_status,
    project_user_stage,
)


SAFE_MEMBER_ACTIVITY_SUMMARIES = {
    "heartbeat": "Heartbeat completed",
    "oneshot_task": "One-time task completed",
    "schedule_run": "Scheduled work completed",
    "task_updated": "Task updated",
    "tool_call": "Tool executed",
    "file_written": "Generated file",
}
# Company topology audit is membership authority. A global platform operator
# does not inherit tenant visibility; the sole owner and company admins do.
COMPANY_AUDIT_ROLES = frozenset({"org_owner", "org_admin"})
TERMINAL_DELIVERABLE_STATUSES = frozenset({"succeeded", "cancelled"})
TOPOLOGY_WORK_STAGE_PRIORITY = {
    "blocked": 0,
    "approval": 1,
    "review": 2,
    "artifact": 3,
    "execution": 4,
    "task": 5,
    "delivery": 6,
    "completed": 7,
}


@dataclass(frozen=True, slots=True)
class _WorkCandidate:
    id: uuid.UUID
    agent_id: uuid.UUID
    title: str
    summary: str
    user_stage: str
    deep_link: str
    updated_at: datetime


def _topology_work_stage(user_stage: str) -> str | None:
    if user_stage in {"task", "execution"}:
        return "executing"
    if user_stage in {"artifact", "review"}:
        return "review"
    if user_stage == "approval":
        return "approval"
    if user_stage == "blocked":
        return "blocked"
    if user_stage in {"delivery", "completed"}:
        return "completed"
    return None


def _project_topology_work_summaries(
    candidates: Iterable[_WorkCandidate],
    *,
    since: datetime,
) -> dict[uuid.UUID, WorkforceTopologyWorkOut]:
    grouped: dict[uuid.UUID, list[tuple[_WorkCandidate, str]]] = {}
    for candidate in candidates:
        stage = _topology_work_stage(candidate.user_stage)
        if stage is None or (stage == "completed" and candidate.updated_at < since):
            continue
        grouped.setdefault(candidate.agent_id, []).append((candidate, stage))

    projected: dict[uuid.UUID, WorkforceTopologyWorkOut] = {}
    for agent_id, items in grouped.items():
        items.sort(
            key=lambda entry: (
                TOPOLOGY_WORK_STAGE_PRIORITY.get(entry[0].user_stage, 99),
                -entry[0].updated_at.timestamp(),
                entry[0].id.int,
            )
        )
        selected, stage = items[0]
        projected[agent_id] = WorkforceTopologyWorkOut(
            id=selected.id,
            title=selected.title,
            summary=selected.summary,
            stage=stage,
            active_count=sum(item_stage != "completed" for _, item_stage in items),
            recently_completed_count=sum(item_stage == "completed" for _, item_stage in items),
            deep_link=selected.deep_link,
            updated_at=selected.updated_at,
        )
    return projected


async def _load_topology_work_summaries(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    employee_ids: set[uuid.UUID],
    since: datetime,
) -> dict[uuid.UUID, WorkforceTopologyWorkOut]:
    """Project all active and window-recent viewer-owned work without a global cap."""

    if not employee_ids:
        return {}

    tasks = list(
        (
            await db.execute(
                select(Task)
                .where(
                    Task.tenant_id == tenant_id,
                    Task.created_by == user_id,
                    Task.agent_id.in_(employee_ids),
                    or_(
                        Task.status != "done",
                        Task.updated_at >= since,
                    ),
                )
                .order_by(Task.updated_at.desc(), Task.id.desc())
            )
        )
        .scalars()
        .all()
    )
    task_ids = {task.id for task in tasks}
    deliverable_recency = or_(
        ~DeliverableRequest.status.in_(TERMINAL_DELIVERABLE_STATUSES),
        DeliverableRequest.updated_at >= since,
    )
    if task_ids:
        deliverable_recency = or_(
            deliverable_recency,
            DeliverableRequest.task_id.in_(task_ids),
        )
    deliverables = list(
        (
            await db.execute(
                select(DeliverableRequest)
                .where(
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.created_by_user_id == user_id,
                    DeliverableRequest.agent_id.in_(employee_ids),
                    deliverable_recency,
                )
                .order_by(
                    DeliverableRequest.updated_at.desc(),
                    DeliverableRequest.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    missing_task_ids = {
        request.task_id for request in deliverables if request.task_id is not None and request.task_id not in task_ids
    }
    if missing_task_ids:
        tasks.extend(
            list(
                (
                    await db.execute(
                        select(Task).where(
                            Task.tenant_id == tenant_id,
                            Task.created_by == user_id,
                            Task.agent_id.in_(employee_ids),
                            Task.id.in_(missing_task_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
        )

    task_ids = [task.id for task in tasks]
    run_by_task: dict[uuid.UUID, AgentRun] = {}
    if task_ids:
        source_ids = [str(task_id) for task_id in task_ids]
        correlation_ids = [f"work-task:{task_id}" for task_id in task_ids]
        runs = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.tenant_id == tenant_id,
                        or_(
                            and_(
                                AgentRun.source_type == "task",
                                AgentRun.source_id.in_(source_ids),
                            ),
                            AgentRun.correlation_id.in_(correlation_ids),
                        ),
                    )
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            try:
                task_id = (
                    uuid.UUID(run.correlation_id.removeprefix("work-task:"))
                    if run.correlation_id and run.correlation_id.startswith("work-task:")
                    else uuid.UUID(run.source_id or "")
                )
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
                    .order_by(
                        AgentRunEvent.created_at.desc(),
                        AgentRunEvent.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            terminal_event_by_run.setdefault(event.run_id, event.event_type)

    request_ids = [request.id for request in deliverables]
    artifact_status_by_request: dict[uuid.UUID, str] = {}
    review_status_by_request: dict[uuid.UUID, str] = {}
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
            )
            .scalars()
            .all()
        )
        for artifact in artifacts:
            artifact_status_by_request.setdefault(
                artifact.request_id,
                artifact.status,
            )
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
            )
            .scalars()
            .all()
        )
        for review in reviews:
            review_status_by_request.setdefault(review.request_id, review.status)

    deliverable_by_task: dict[uuid.UUID, DeliverableRequest] = {}
    standalone_deliverables: list[DeliverableRequest] = []
    for request in deliverables:
        if request.task_id is None:
            standalone_deliverables.append(request)
        else:
            deliverable_by_task.setdefault(request.task_id, request)

    candidates: list[_WorkCandidate] = []
    for task in tasks:
        run = run_by_task.get(task.id)
        request = deliverable_by_task.get(task.id)
        execution_status = project_execution_status(
            task_status=task.status,
            terminal_run_event=terminal_event_by_run.get(run.id) if run else None,
        )
        artifact_status = artifact_status_by_request.get(request.id) if request else None
        review_status = review_status_by_request.get(request.id) if request else None
        updated_at = max(
            task.updated_at,
            request.updated_at if request else task.updated_at,
        )
        candidates.append(
            _WorkCandidate(
                id=task.id,
                agent_id=task.agent_id,
                title=task.title,
                summary=task.intent,
                user_stage=project_user_stage(
                    task_status=task.status,
                    execution_status=execution_status,
                    deliverable_status=request.status if request else None,
                    artifact_status=artifact_status,
                    review_status=review_status,
                ),
                deep_link=(
                    f"/groups/{task.group_id}/{task.executor_snapshot.get('group_session_id')}"
                    if task.executor_kind == "group" and task.group_id is not None
                    else (
                        f"/agents/{task.agent_id}/chat?session_id={request.session_id}&task_id={task.id}"
                        if request
                        else f"/agents/{task.agent_id}/chat?task_id={task.id}"
                    )
                ),
                updated_at=updated_at,
            )
        )

    for request in standalone_deliverables:
        execution_status = (
            "failed"
            if request.status == "failed"
            else "completed"
            if request.status in {"succeeded", "waiting_approval"}
            else "running"
            if request.status == "running"
            else "queued"
        )
        candidates.append(
            _WorkCandidate(
                id=request.id,
                agent_id=request.agent_id,
                title=request.goal[:500],
                summary=request.goal,
                user_stage=project_user_stage(
                    task_status=None,
                    execution_status=execution_status,
                    deliverable_status=request.status,
                    artifact_status=artifact_status_by_request.get(request.id),
                    review_status=review_status_by_request.get(request.id),
                ),
                deep_link=f"/agents/{request.agent_id}/chat?session_id={request.session_id}",
                updated_at=request.updated_at,
            )
        )

    return _project_topology_work_summaries(candidates, since=since)


def _canonical_pair(
    first: uuid.UUID,
    second: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    return (first, second) if first.int < second.int else (second, first)


def merge_topology_activity_edges(
    rows: Iterable[tuple[uuid.UUID | None, uuid.UUID | None, int, datetime | None]],
    *,
    employee_ids: set[uuid.UUID],
) -> list[WorkforceTopologyActivityEdgeOut]:
    """Merge native-chat and gateway aggregates into undirected recent edges."""

    merged: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, datetime]] = {}
    for first, second, count, last_activity_at in rows:
        if (
            first is None
            or second is None
            or first == second
            or first not in employee_ids
            or second not in employee_ids
            or last_activity_at is None
            or int(count or 0) <= 0
        ):
            continue
        key = _canonical_pair(first, second)
        previous_count, previous_at = merged.get(key, (0, last_activity_at))
        merged[key] = (
            previous_count + int(count),
            max(previous_at, last_activity_at),
        )

    return sorted(
        (
            WorkforceTopologyActivityEdgeOut(
                agent_a_id=first,
                agent_b_id=second,
                interaction_count=count,
                last_activity_at=last_activity_at,
            )
            for (first, second), (count, last_activity_at) in merged.items()
        ),
        key=lambda edge: (edge.last_activity_at, edge.agent_a_id.int, edge.agent_b_id.int),
        reverse=True,
    )


def _project_recent_activities(
    logs: Iterable[AgentActivityLog],
    *,
    auditable_agent_ids: set[uuid.UUID],
    employee_ids: set[uuid.UUID],
) -> list[WorkforceTopologyActivityOut]:
    projected: list[WorkforceTopologyActivityOut] = []
    for log in logs:
        if log.agent_id not in employee_ids or log.created_at is None:
            continue
        can_audit = log.agent_id in auditable_agent_ids
        if not can_audit and log.action_type not in SAFE_MEMBER_ACTIVITY_SUMMARIES:
            continue
        projected.append(
            WorkforceTopologyActivityOut(
                id=log.id,
                agent_id=log.agent_id,
                summary=(log.summary if can_audit else SAFE_MEMBER_ACTIVITY_SUMMARIES[log.action_type]),
                created_at=log.created_at,
            )
        )
    return projected


async def build_workforce_topology(
    db: AsyncSession,
    *,
    user: User,
    window_hours: int,
) -> WorkforceTopologyOut:
    """Return one constant-query topology projection for the current viewer."""

    tenant_id = user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")

    tenant = (
        await db.execute(
            select(Tenant).where(
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Company not found")

    visible_agents = list(
        (
            await db.execute(
                build_visible_agents_query(user, tenant_id=tenant_id).order_by(
                    Agent.created_at.asc(),
                    Agent.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    product_roles = await resolve_agent_product_roles(
        db,
        viewer_id=user.id,
        tenant_id=tenant_id,
        agents=visible_agents,
    )
    employees = [agent for agent in visible_agents if product_roles.get(agent.id, "agent_employee") == "agent_employee"]
    employee_ids = {agent.id for agent in employees}

    if not employee_ids:
        return WorkforceTopologyOut(
            company_id=tenant.id,
            company_name=tenant.name,
            window_hours=window_hours,
            generated_at=datetime.now(timezone.utc),
        )

    manageable_agents = list(
        (await db.execute(build_manageable_agents_query(user, tenant_id=tenant_id).where(Agent.id.in_(employee_ids))))
        .scalars()
        .all()
    )
    manageable_ids = {agent.id for agent in manageable_agents}
    company_auditor = user.role in COMPANY_AUDIT_ROLES
    auditable_ids = employee_ids if company_auditor else manageable_ids
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    work_by_agent = await _load_topology_work_summaries(
        db,
        tenant_id=tenant_id,
        user_id=user.id,
        employee_ids=employee_ids,
        since=since,
    )

    activity_query = select(AgentActivityLog).where(
        AgentActivityLog.agent_id.in_(employee_ids),
        AgentActivityLog.created_at >= since,
    )
    if auditable_ids != employee_ids:
        activity_query = activity_query.where(
            or_(
                AgentActivityLog.agent_id.in_(auditable_ids),
                AgentActivityLog.action_type.in_(SAFE_MEMBER_ACTIVITY_SUMMARIES),
            )
        )
    activity_logs = list(
        (
            await db.execute(
                activity_query.order_by(
                    AgentActivityLog.created_at.desc(),
                    AgentActivityLog.id.desc(),
                ).limit(100)
            )
        )
        .scalars()
        .all()
    )

    relationship_query = select(AgentAgentRelationship).where(
        AgentAgentRelationship.agent_id.in_(employee_ids),
        AgentAgentRelationship.target_agent_id.in_(employee_ids),
    )
    if not company_auditor:
        relationship_query = relationship_query.where(
            AgentAgentRelationship.agent_id.in_(manageable_ids),
            AgentAgentRelationship.target_agent_id.in_(manageable_ids),
        )
    relationships = list(
        (
            await db.execute(
                relationship_query.order_by(
                    func.coalesce(
                        AgentAgentRelationship.updated_at,
                        AgentAgentRelationship.created_at,
                    ).desc(),
                    AgentAgentRelationship.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    chat_edge_query = (
        select(
            ChatSession.agent_id,
            ChatSession.peer_agent_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.created_at),
        )
        .join(
            ChatMessage,
            ChatMessage.conversation_id == cast(ChatSession.id, String),
        )
        .where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.source_channel == "agent",
            ChatSession.deleted_at.is_(None),
            ChatSession.agent_id.in_(employee_ids),
            ChatSession.peer_agent_id.in_(employee_ids),
            ChatMessage.created_at >= since,
        )
    )
    if not company_auditor:
        chat_edge_query = chat_edge_query.where(
            or_(
                ChatSession.user_id == user.id,
                and_(
                    ChatSession.agent_id.in_(manageable_ids),
                    ChatSession.peer_agent_id.in_(manageable_ids),
                ),
            )
        )
    chat_rows = list(
        (
            await db.execute(
                chat_edge_query.group_by(
                    ChatSession.agent_id,
                    ChatSession.peer_agent_id,
                )
            )
        ).all()
    )

    gateway_edge_query = select(
        GatewayMessage.sender_agent_id,
        GatewayMessage.agent_id,
        func.count(GatewayMessage.id),
        func.max(GatewayMessage.created_at),
    ).where(
        GatewayMessage.sender_agent_id.is_not(None),
        GatewayMessage.sender_agent_id.in_(employee_ids),
        GatewayMessage.agent_id.in_(employee_ids),
        GatewayMessage.created_at >= since,
        GatewayMessage.status != "expired",
    )
    if not company_auditor:
        gateway_edge_query = gateway_edge_query.where(
            or_(
                GatewayMessage.sender_user_id == user.id,
                and_(
                    GatewayMessage.sender_agent_id.in_(manageable_ids),
                    GatewayMessage.agent_id.in_(manageable_ids),
                ),
            )
        )
    gateway_rows = list(
        (
            await db.execute(
                gateway_edge_query.group_by(
                    GatewayMessage.sender_agent_id,
                    GatewayMessage.agent_id,
                )
            )
        ).all()
    )

    return WorkforceTopologyOut(
        company_id=tenant.id,
        company_name=tenant.name,
        window_hours=window_hours,
        generated_at=datetime.now(timezone.utc),
        nodes=[
            WorkforceTopologyNodeOut(
                id=agent.id,
                name=agent.name,
                avatar_url=agent.avatar_url,
                role_description=agent.role_description or "",
                status=agent.status,
                last_active_at=agent.last_active_at,
                tokens_used_today=agent.tokens_used_today or 0,
                cache_read_tokens_today=agent.cache_read_tokens_today or 0,
                max_tokens_per_day=agent.max_tokens_per_day,
                is_expired=bool(agent.is_expired),
                is_system=bool(agent.is_system),
                visibility=(
                    agent.access_mode
                    if getattr(agent, "access_mode", None) in {"company", "private", "custom"}
                    else "company"
                ),
                can_manage=agent.id in manageable_ids,
                work=work_by_agent.get(agent.id),
            )
            for agent in employees
        ],
        relationship_edges=[
            WorkforceTopologyRelationshipEdgeOut(
                id=relationship.id,
                source_agent_id=relationship.agent_id,
                target_agent_id=relationship.target_agent_id,
                relation=relationship.relation,
                updated_at=relationship.updated_at or relationship.created_at,
            )
            for relationship in relationships
            if relationship.agent_id in employee_ids
            and relationship.target_agent_id in employee_ids
            and (
                company_auditor
                or (relationship.agent_id in manageable_ids and relationship.target_agent_id in manageable_ids)
            )
        ],
        activity_edges=merge_topology_activity_edges(
            [*chat_rows, *gateway_rows],
            employee_ids=employee_ids,
        ),
        recent_activities=_project_recent_activities(
            activity_logs,
            auditable_agent_ids=auditable_ids,
            employee_ids=employee_ids,
        )[:20],
    )


__all__ = [
    "SAFE_MEMBER_ACTIVITY_SUMMARIES",
    "build_workforce_topology",
    "merge_topology_activity_edges",
]
