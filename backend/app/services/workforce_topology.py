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
    DeliverableExecution,
    DeliverableQualityReview,
    DeliverableRequest,
)
from app.models.gateway_message import GatewayMessage
from app.models.media_generation import MediaGenerationTask
from app.models.org import AgentAgentRelationship
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.workforce_topology import (
    WorkforceTopologyActivityEdgeOut,
    WorkforceTopologyActivityOut,
    WorkforceTopologyExecutionOut,
    WorkforceTopologyNodeOut,
    WorkforceTopologyOut,
    WorkforceTopologyRelationshipEdgeOut,
    WorkforceTopologyWorkOut,
)
from app.services.product_roles import resolve_agent_product_roles
from app.services.work_detail_projection import latest_runtime_lifecycle_event_by_run
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
TERMINAL_DELIVERABLE_EXECUTION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
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
RUNTIME_LIFECYCLE_EVENT_TYPES = (
    "run_created",
    "status_changed",
    "waiting_started",
    "resumed",
    "run_completed",
    "run_failed",
    "run_cancelled",
)
TERMINAL_EXECUTION_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_EXECUTION_STATUSES = frozenset(
    {"queued", "running", "waiting_user", "waiting_agent", "waiting_external"}
)
TERMINAL_MEDIA_STATUSES = frozenset(
    {"succeeded", "failed", "compensated", "closed_nonrefundable"}
)
EXECUTION_STATUS_PRIORITY = {
    "waiting_user": 0,
    "waiting_agent": 1,
    "waiting_external": 2,
    "running": 3,
    "queued": 4,
    "failed": 5,
    "cancelled": 6,
    "completed": 7,
}
SAFE_EXECUTION_TITLES = {
    "direct_chat": "Direct conversation",
    "group": "Group collaboration",
    "a2a": "Agent delegation",
    "task": "Workbench task",
    "trigger": "Scheduled trigger",
    "heartbeat": "Heartbeat",
    "deliverable": "Formal deliverable",
    "media": "Media generation",
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


@dataclass(frozen=True, slots=True)
class _ExecutionCandidate:
    id: uuid.UUID
    agent_id: uuid.UUID
    run_id: uuid.UUID | None
    source_type: str
    status: str
    phase: str | None
    title: str
    summary: str
    details_visible: bool
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


def _bounded_execution_text(value: object, *, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:limit]


def _run_execution_status(event: AgentRunEvent | None) -> str:
    """Project only lifecycle events; Tool/delivery UI events never finish a Run."""

    if event is None or event.event_type == "run_created":
        return "queued"
    if event.event_type in {"status_changed", "resumed"}:
        return "running"
    if event.event_type == "waiting_started":
        payload = event.payload if isinstance(event.payload, dict) else {}
        stored = payload.get("status")
        if stored in {"waiting_user", "waiting_agent", "waiting_external"}:
            return str(stored)
        waiting_type = payload.get("waiting_type")
        return {
            "user": "waiting_user",
            "agent": "waiting_agent",
            "external": "waiting_external",
        }.get(str(waiting_type), "waiting_external")
    return {
        "run_completed": "completed",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
    }.get(event.event_type, "running")


def _run_execution_phase(event: AgentRunEvent | None) -> str | None:
    if event is None:
        return "queued"
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "status_changed":
        activity_type = payload.get("activity_type")
        if activity_type in {"thinking", "assistant_progress", "tool_call"}:
            return str(activity_type)
    if event.event_type == "waiting_started":
        return _run_execution_status(event)
    return event.event_type


def _run_execution_source(run: AgentRun) -> str:
    delivery_target = run.delivery_target if isinstance(run.delivery_target, dict) else {}
    if delivery_target.get("kind") == "group":
        return "group"
    if run.source_type == "chat":
        return "direct_chat"
    if run.source_type in {"a2a", "task", "trigger", "heartbeat"}:
        return run.source_type
    return "direct_chat"


def _run_execution_deep_link(
    run: AgentRun,
    *,
    source_type: str,
    viewer_id: uuid.UUID,
) -> str:
    assert run.agent_id is not None
    base = f"/agents/{run.agent_id}/chat"
    owns_origin = run.origin_user_id == viewer_id
    delivery_target = run.delivery_target if isinstance(run.delivery_target, dict) else {}
    if source_type == "task" and run.source_id:
        try:
            return f"/work/{uuid.UUID(run.source_id)}"
        except ValueError:
            pass
    if source_type == "group" and owns_origin:
        try:
            group_id = uuid.UUID(str(delivery_target.get("group_id")))
            session_id = uuid.UUID(str(delivery_target.get("session_id")))
        except (TypeError, ValueError):
            pass
        else:
            return f"/groups/{group_id}/{session_id}"
    if owns_origin and run.session_id is not None:
        return f"{base}?session_id={run.session_id}"
    return base


def _media_execution_status(status: str) -> str:
    if status == "succeeded":
        return "completed"
    if status in {"failed", "closed_nonrefundable", "asset_delivery_failed", "backfill_attention"}:
        return "failed"
    if status == "compensated":
        return "cancelled"
    if status in {"submitting", "submitted", "provider_accepted"}:
        return "queued"
    if status in {"submission_ambiguous", "backfill_scanning"}:
        return "waiting_external"
    return "running"


def _deliverable_execution_status(
    request: DeliverableRequest,
    execution: DeliverableExecution | None,
) -> str:
    status = execution.status if execution is not None else request.status
    return {
        "draft": "queued",
        "ready": "queued",
        "running": "running",
        "reconciling": "running",
        "blocked": "waiting_external",
        "waiting_approval": "waiting_user",
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status, "running")


def _project_topology_execution_summaries(
    candidates: Iterable[_ExecutionCandidate],
    *,
    since: datetime,
) -> dict[uuid.UUID, WorkforceTopologyExecutionOut]:
    grouped: dict[uuid.UUID, list[_ExecutionCandidate]] = {}
    for candidate in candidates:
        if candidate.status in TERMINAL_EXECUTION_STATUSES and candidate.updated_at < since:
            continue
        grouped.setdefault(candidate.agent_id, []).append(candidate)

    projected: dict[uuid.UUID, WorkforceTopologyExecutionOut] = {}
    for agent_id, items in grouped.items():
        items.sort(
            key=lambda item: (
                EXECUTION_STATUS_PRIORITY.get(item.status, 99),
                -item.updated_at.timestamp(),
                item.id.int,
            )
        )
        selected = items[0]
        projected[agent_id] = WorkforceTopologyExecutionOut(
            id=selected.id,
            run_id=selected.run_id,
            source_type=selected.source_type,
            status=selected.status,
            phase=selected.phase,
            title=selected.title,
            summary=selected.summary,
            details_visible=selected.details_visible,
            active_count=sum(item.status in ACTIVE_EXECUTION_STATUSES for item in items),
            recently_finished_count=sum(item.status in TERMINAL_EXECUTION_STATUSES for item in items),
            deep_link=selected.deep_link,
            updated_at=selected.updated_at,
        )
    return projected


def _project_topology_agent_status(
    agent_status: str,
    execution: WorkforceTopologyExecutionOut | None,
) -> str:
    """Overlay live Run activity without hiding lifecycle/health failures."""

    if agent_status in {"creating", "stopped", "error"}:
        return agent_status
    if execution is not None and execution.status in ACTIVE_EXECUTION_STATUSES:
        return "running"
    return agent_status


async def _load_topology_execution_summaries(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    employee_ids: set[uuid.UUID],
    auditable_agent_ids: set[uuid.UUID],
    since: datetime,
) -> dict[uuid.UUID, WorkforceTopologyExecutionOut]:
    """Rebuild company-visible live execution from authoritative domain facts."""

    if not employee_ids:
        return {}

    terminal_event_exists = (
        select(AgentRunEvent.id)
        .where(
            AgentRunEvent.tenant_id == tenant_id,
            AgentRunEvent.run_id == AgentRun.id,
            AgentRunEvent.event_type.in_(tuple(TERMINAL_RUN_EVENTS)),
        )
        .correlate(AgentRun)
        .exists()
    )
    runs = list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.agent_id.in_(employee_ids),
                    or_(AgentRun.created_at >= since, ~terminal_event_exists),
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            )
        )
        .scalars()
        .all()
    )
    run_ids = [run.id for run in runs]
    events: list[AgentRunEvent] = []
    if run_ids:
        events = list(
            (
                await db.execute(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.tenant_id == tenant_id,
                        AgentRunEvent.run_id.in_(run_ids),
                        AgentRunEvent.event_type.in_(RUNTIME_LIFECYCLE_EVENT_TYPES),
                    )
                    .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )
    latest_event_by_run = latest_runtime_lifecycle_event_by_run(events)

    deliverable_rows = list(
        (
            await db.execute(
                select(DeliverableRequest, DeliverableExecution)
                .outerjoin(
                    DeliverableExecution,
                    and_(
                        DeliverableExecution.tenant_id == tenant_id,
                        DeliverableExecution.id == DeliverableRequest.current_execution_id,
                    ),
                )
                .where(
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.agent_id.in_(employee_ids),
                    or_(
                        ~DeliverableRequest.status.in_(TERMINAL_DELIVERABLE_EXECUTION_STATUSES),
                        DeliverableRequest.updated_at >= since,
                    ),
                )
                .order_by(DeliverableRequest.updated_at.desc(), DeliverableRequest.id.desc())
            )
        ).all()
    )
    media_tasks = list(
        (
            await db.execute(
                select(MediaGenerationTask)
                .where(
                    MediaGenerationTask.tenant_id == tenant_id,
                    MediaGenerationTask.agent_id.in_(employee_ids),
                    or_(
                        ~MediaGenerationTask.status.in_(TERMINAL_MEDIA_STATUSES),
                        MediaGenerationTask.updated_at >= since,
                    ),
                )
                .order_by(MediaGenerationTask.updated_at.desc(), MediaGenerationTask.id.desc())
            )
        )
        .scalars()
        .all()
    )

    media_by_execution: dict[uuid.UUID, list[MediaGenerationTask]] = {}
    for task in media_tasks:
        if task.deliverable_execution_id is not None:
            media_by_execution.setdefault(task.deliverable_execution_id, []).append(task)

    candidates: list[_ExecutionCandidate] = []
    deliverable_run_ids = {
        request.agent_run_id
        for request, _execution in deliverable_rows
        if request.agent_run_id is not None
    }
    for run in runs:
        if run.agent_id is None or run.id in deliverable_run_ids:
            continue
        event = latest_event_by_run.get(run.id)
        source_type = _run_execution_source(run)
        status = _run_execution_status(event)
        owns_origin = run.origin_user_id == user_id
        details_visible = owns_origin or (
            source_type in {"trigger", "heartbeat"} and run.agent_id in auditable_agent_ids
        )
        generic_title = SAFE_EXECUTION_TITLES[source_type]
        detail = _bounded_execution_text(run.goal, limit=500)
        candidates.append(
            _ExecutionCandidate(
                id=run.id,
                agent_id=run.agent_id,
                run_id=run.id,
                source_type=source_type,
                status=status,
                phase=_run_execution_phase(event),
                title=(
                    _bounded_execution_text(run.goal, limit=160) or generic_title
                    if details_visible
                    else generic_title
                ),
                summary=(
                    detail or generic_title
                    if details_visible
                    else f"{generic_title} status: {status}"
                ),
                details_visible=details_visible,
                deep_link=_run_execution_deep_link(
                    run,
                    source_type=source_type,
                    viewer_id=user_id,
                ),
                updated_at=event.created_at if event is not None else run.updated_at,
            )
        )

    for request, execution in deliverable_rows:
        status = _deliverable_execution_status(request, execution)
        phase = execution.current_stage if execution is not None else request.current_stage
        if execution is not None:
            linked_media = media_by_execution.get(execution.id, [])
            if linked_media:
                linked_media.sort(
                    key=lambda item: (
                        EXECUTION_STATUS_PRIORITY.get(_media_execution_status(item.status), 99),
                        -item.updated_at.timestamp(),
                        item.id.int,
                    )
                )
                media = linked_media[0]
                media_status = _media_execution_status(media.status)
                if status in {"queued", "running"} and media_status != "completed":
                    status = media_status
                phase = f"{media.modality}:{media.status}"
        details_visible = request.created_by_user_id == user_id
        generic_title = f"{request.work_type.title()} deliverable"
        detail = _bounded_execution_text(request.goal, limit=500)
        candidates.append(
            _ExecutionCandidate(
                id=request.id,
                agent_id=request.agent_id,
                run_id=request.agent_run_id,
                source_type="deliverable",
                status=status,
                phase=phase,
                title=(
                    _bounded_execution_text(request.goal, limit=160) or generic_title
                    if details_visible
                    else generic_title
                ),
                summary=(
                    detail or generic_title
                    if details_visible
                    else f"{generic_title} status: {status}"
                ),
                details_visible=details_visible,
                deep_link=(
                    f"/agents/{request.agent_id}/chat?session_id={request.session_id}"
                    if details_visible
                    else f"/agents/{request.agent_id}/chat"
                ),
                updated_at=max(
                    request.updated_at,
                    execution.updated_at if execution is not None else request.updated_at,
                ),
            )
        )

    for task in media_tasks:
        if task.agent_id is None or task.deliverable_execution_id is not None:
            continue
        status = _media_execution_status(task.status)
        details_visible = task.user_id == user_id
        generic_title = f"{task.modality.title()} generation"
        candidates.append(
            _ExecutionCandidate(
                id=task.id,
                agent_id=task.agent_id,
                run_id=None,
                source_type="media",
                status=status,
                phase=task.status,
                title=generic_title,
                summary=f"{generic_title} status: {status}",
                details_visible=details_visible,
                deep_link=(
                    f"/agents/{task.agent_id}/chat?session_id={task.origin_session_id}"
                    if details_visible and task.origin_session_id is not None
                    else f"/agents/{task.agent_id}/chat"
                ),
                updated_at=task.updated_at,
            )
        )

    return _project_topology_execution_summaries(candidates, since=since)


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
    can_view_company_analytics = company_auditor
    auditable_ids = employee_ids if company_auditor else manageable_ids
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    work_by_agent = await _load_topology_work_summaries(
        db,
        tenant_id=tenant_id,
        user_id=user.id,
        employee_ids=employee_ids,
        since=since,
    )
    execution_by_agent = await _load_topology_execution_summaries(
        db,
        tenant_id=tenant_id,
        user_id=user.id,
        employee_ids=employee_ids,
        auditable_agent_ids=auditable_ids,
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
                status=_project_topology_agent_status(
                    agent.status,
                    execution_by_agent.get(agent.id),
                ),
                last_active_at=agent.last_active_at,
                tokens_used_today=(agent.tokens_used_today or 0) if can_view_company_analytics else None,
                cache_read_tokens_today=(agent.cache_read_tokens_today or 0) if can_view_company_analytics else None,
                max_tokens_per_day=agent.max_tokens_per_day if can_view_company_analytics else None,
                is_expired=bool(agent.is_expired),
                is_system=bool(agent.is_system),
                visibility=(
                    agent.access_mode
                    if getattr(agent, "access_mode", None) in {"company", "private", "custom"}
                    else "company"
                ),
                can_manage=agent.id in manageable_ids,
                execution=execution_by_agent.get(agent.id),
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
