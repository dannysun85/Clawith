"""Read-only Group projection of formal Work tasks.

Group remains a collaboration surface. This module batches authoritative Task,
Runtime, Deliverable, Review and Approval facts and never owns workflow state.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ApprovalRequest
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableQualityReview,
    DeliverableRequest,
)
from app.models.task import Task, TaskResultReviewReceipt
from app.models.user import User
from app.schemas.work import (
    GroupTaskParticipantOut,
    GroupTaskRunSummaryOut,
    GroupTaskSummaryOut,
)
from app.services.work_detail_projection import (
    latest_runtime_lifecycle_event_by_run,
    load_work_inbox_actions,
    project_status_axes,
)
from app.services.work_projection import (
    project_task_result_review_status,
    project_user_stage,
    work_requires_owner_review,
)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _task_id_for_run(run: AgentRun) -> uuid.UUID | None:
    candidates: set[uuid.UUID] = set()
    if run.correlation_id and run.correlation_id.startswith("work-task:"):
        if value := _uuid(run.correlation_id.removeprefix("work-task:")):
            candidates.add(value)
    if run.source_type == "task":
        if value := _uuid(run.source_id):
            candidates.add(value)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _runtime_run_id(approval: ApprovalRequest) -> uuid.UUID | None:
    details = approval.details if isinstance(approval.details, dict) else {}
    scope = details.get("runtime_scope")
    return _uuid(scope.get("run_id")) if isinstance(scope, dict) else None


def _origin(task: Task) -> dict:
    snapshot = task.executor_snapshot if isinstance(task.executor_snapshot, dict) else {}
    value = snapshot.get("origin")
    return value if isinstance(value, dict) else {}


def _session_id(task: Task) -> uuid.UUID | None:
    snapshot = task.executor_snapshot if isinstance(task.executor_snapshot, dict) else {}
    origin = _origin(task)
    return _uuid(origin.get("session_id") or snapshot.get("group_session_id"))


async def load_group_task_summaries(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    session_id: uuid.UUID | None,
    limit: int,
    user: User | None = None,
) -> list[GroupTaskSummaryOut]:
    tasks = list(
        (
            await db.execute(
                select(Task)
                .where(
                    Task.tenant_id == tenant_id,
                    Task.group_id == group_id,
                    Task.executor_kind == "group",
                )
                .order_by(Task.updated_at.desc(), Task.id.desc())
            )
        ).scalars().all()
    )
    if session_id is not None:
        tasks = [task for task in tasks if _session_id(task) == session_id]
    tasks = tasks[:limit]
    if not tasks:
        return []

    task_ids = [task.id for task in tasks]
    task_id_strings = [str(task_id) for task_id in task_ids]
    correlation_ids = [f"work-task:{task_id}" for task_id in task_ids]
    runs = list(
        (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.tenant_id == tenant_id,
                    or_(
                        (AgentRun.source_type == "task")
                        & AgentRun.source_id.in_(task_id_strings),
                        AgentRun.correlation_id.in_(correlation_ids),
                    ),
                )
            )
        ).scalars().all()
    )
    agent_ids = {
        agent_id
        for agent_id in [
            *(task.agent_id for task in tasks),
            *(run.agent_id for run in runs),
        ]
        if agent_id is not None
    }
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
    run_ids = [run.id for run in runs]
    events = (
        list(
            (
                await db.execute(
                    select(AgentRunEvent).where(
                        AgentRunEvent.tenant_id == tenant_id,
                        AgentRunEvent.run_id.in_(run_ids),
                    )
                )
            ).scalars().all()
        )
        if run_ids
        else []
    )
    reviewable_task_ids = [
        task.id
        for task in tasks
        if task.status == "done"
        and work_requires_owner_review(getattr(task, "work_statement", {}))
    ]
    task_result_reviews = (
        list(
            (
                await db.execute(
                    select(TaskResultReviewReceipt).where(
                        TaskResultReviewReceipt.tenant_id == tenant_id,
                        TaskResultReviewReceipt.task_id.in_(reviewable_task_ids),
                    )
                )
            ).scalars().all()
        )
        if reviewable_task_ids
        else []
    )
    requests = list(
        (
            await db.execute(
                select(DeliverableRequest).where(
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.task_id.in_(task_ids),
                )
            )
        ).scalars().all()
    )
    request_ids = [request.id for request in requests]
    executions: list[DeliverableExecution] = []
    artifacts: list[DeliverableArtifactRevision] = []
    reviews: list[DeliverableQualityReview] = []
    receipts: list[DeliverableApprovalReceipt] = []
    if request_ids:
        executions = list(
            (
                await db.execute(
                    select(DeliverableExecution).where(
                        DeliverableExecution.tenant_id == tenant_id,
                        DeliverableExecution.request_id.in_(request_ids),
                    )
                )
            ).scalars().all()
        )
        artifacts = list(
            (
                await db.execute(
                    select(DeliverableArtifactRevision).where(
                        DeliverableArtifactRevision.tenant_id == tenant_id,
                        DeliverableArtifactRevision.request_id.in_(request_ids),
                    )
                )
            ).scalars().all()
        )
        reviews = list(
            (
                await db.execute(
                    select(DeliverableQualityReview).where(
                        DeliverableQualityReview.tenant_id == tenant_id,
                        DeliverableQualityReview.request_id.in_(request_ids),
                    )
                )
            ).scalars().all()
        )
        receipts = list(
            (
                await db.execute(
                    select(DeliverableApprovalReceipt).where(
                        DeliverableApprovalReceipt.tenant_id == tenant_id,
                        DeliverableApprovalReceipt.request_id.in_(request_ids),
                    )
                )
            ).scalars().all()
        )
    approvals = (
        list(
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.agent_id.in_(agent_ids),
                    )
                )
            ).scalars().all()
        )
        if agent_ids and run_ids
        else []
    )
    runs_by_task: dict[uuid.UUID, list[AgentRun]] = defaultdict(list)
    for run in runs:
        if (task_id := _task_id_for_run(run)) is not None:
            runs_by_task[task_id].append(run)
    events_by_run: dict[uuid.UUID, list[AgentRunEvent]] = defaultdict(list)
    for event in events:
        events_by_run[event.run_id].append(event)
    latest_event_by_run = latest_runtime_lifecycle_event_by_run(events)
    requests_by_task: dict[uuid.UUID, list[DeliverableRequest]] = defaultdict(list)
    for request in requests:
        if request.task_id is not None:
            requests_by_task[request.task_id].append(request)
    executions_by_request: dict[uuid.UUID, list[DeliverableExecution]] = defaultdict(list)
    artifacts_by_request: dict[uuid.UUID, list[DeliverableArtifactRevision]] = defaultdict(list)
    reviews_by_request: dict[uuid.UUID, list[DeliverableQualityReview]] = defaultdict(list)
    receipts_by_request: dict[uuid.UUID, list[DeliverableApprovalReceipt]] = defaultdict(list)
    result_reviews_by_task: dict[uuid.UUID, list[TaskResultReviewReceipt]] = defaultdict(list)
    for value in executions:
        executions_by_request[value.request_id].append(value)
    for value in artifacts:
        artifacts_by_request[value.request_id].append(value)
    for value in reviews:
        reviews_by_request[value.request_id].append(value)
    for value in receipts:
        receipts_by_request[value.request_id].append(value)
    for value in task_result_reviews:
        result_reviews_by_task[value.task_id].append(value)
    approvals_by_run: dict[uuid.UUID, list[ApprovalRequest]] = defaultdict(list)
    for approval in approvals:
        if (run_id := _runtime_run_id(approval)) in run_ids:
            approvals_by_run[run_id].append(approval)
    viewer_actions = await load_work_inbox_actions(db, user=user) if user is not None else []
    actions_by_task = defaultdict(list)
    for action in viewer_actions:
        if action.task_id in task_ids:
            actions_by_task[action.task_id].append(action)

    output: list[GroupTaskSummaryOut] = []
    for task in tasks:
        task_runs = runs_by_task.get(task.id, [])
        task_run_ids = {run.id for run in task_runs}
        task_events = [
            event for run_id in task_run_ids for event in events_by_run.get(run_id, [])
        ]
        task_requests = requests_by_task.get(task.id, [])
        task_request_ids = {request.id for request in task_requests}
        task_executions = [
            value
            for request_id in task_request_ids
            for value in executions_by_request.get(request_id, [])
        ]
        task_artifacts = [
            value
            for request_id in task_request_ids
            for value in artifacts_by_request.get(request_id, [])
        ]
        task_reviews = [
            value
            for request_id in task_request_ids
            for value in reviews_by_request.get(request_id, [])
        ]
        task_receipts = [
            value
            for request_id in task_request_ids
            for value in receipts_by_request.get(request_id, [])
        ]
        task_approvals = [
            value
            for run_id in task_run_ids
            for value in approvals_by_run.get(run_id, [])
        ]
        task_result_receipts = result_reviews_by_task.get(task.id, [])
        axes = project_status_axes(
            task=task,
            runs=task_runs,
            events=task_events,
            requests=task_requests,
            executions=task_executions,
            artifacts=task_artifacts,
            reviews=task_reviews,
            runtime_approvals=task_approvals,
            approval_receipts=task_receipts,
            task_result_reviews=task_result_receipts,
        )
        latest_task_run = max(
            task_runs,
            key=lambda value: (value.created_at, value.id),
            default=None,
        )
        latest_result_receipt = next(
            (
                receipt
                for receipt in reversed(task_result_receipts)
                if latest_task_run is not None and receipt.run_id == latest_task_run.id
            ),
            None,
        )
        task_result_review_status = project_task_result_review_status(
            task_status=task.status,
            work_statement=getattr(task, "work_statement", {}),
            receipt_action=(
                latest_result_receipt.action
                if latest_result_receipt is not None
                else None
            ),
        )
        latest_request = max(
            task_requests,
            key=lambda value: (value.updated_at, value.id),
            default=None,
        )
        latest_artifact = max(
            task_artifacts,
            key=lambda value: (value.created_at, value.id),
            default=None,
        )
        latest_review = max(
            task_reviews,
            key=lambda value: (value.updated_at, value.id),
            default=None,
        )
        snapshot = task.executor_snapshot if isinstance(task.executor_snapshot, dict) else {}
        participant_facts = snapshot.get("participants")
        owner = next(
            (
                value
                for value in participant_facts
                if isinstance(value, dict) and value.get("responsibility") == "primary_owner"
            ),
            None,
        ) if isinstance(participant_facts, list) else None
        safe_participants = (
            [
                GroupTaskParticipantOut(
                    agent_id=agent_id,
                    agent_name=str(value.get("agent_name") or "Agent"),
                    responsibility=(
                        "primary_owner"
                        if value.get("responsibility") == "primary_owner"
                        else "collaborator"
                    ),
                )
                for value in participant_facts
                if isinstance(value, dict)
                and (agent_id := _uuid(value.get("agent_id"))) is not None
            ]
            if isinstance(participant_facts, list)
            else []
        )
        agent = agents.get(task.agent_id)
        owner_id = _uuid(owner.get("agent_id")) if isinstance(owner, dict) else task.agent_id
        if owner_id is None:
            owner_id = task.agent_id
        owner_name = (
            str(owner.get("agent_name"))
            if isinstance(owner, dict) and owner.get("agent_name")
            else agent.name if agent is not None else str(snapshot.get("agent_name") or "Agent")
        )
        origin = _origin(task)
        linked_session_id = _session_id(task)
        update = max(
            task_events,
            key=lambda value: (value.created_at, value.id),
            default=None,
        )
        output.append(
            GroupTaskSummaryOut(
                task_id=task.id,
                title=task.title,
                intent=task.intent,
                task_status=task.status,
                user_stage=project_user_stage(
                    task_status=task.status,
                    execution_status=axes.execution,
                    deliverable_status=(latest_request.status if latest_request else None),
                    artifact_status=(latest_artifact.status if latest_artifact else None),
                    review_status=(latest_review.status if latest_review else None),
                    task_result_review_status=task_result_review_status,
                ),
                status_axes=axes,
                primary_owner_agent_id=owner_id,
                primary_owner_agent_name=owner_name,
                participants=safe_participants,
                runs=[
                    GroupTaskRunSummaryOut(
                        id=run.id,
                        agent_id=run.agent_id,
                        agent_name=(
                            agents[run.agent_id].name
                            if run.agent_id in agents
                            else "Group planner" if run.system_role == "group_planning" else None
                        ),
                        parent_run_id=run.parent_run_id,
                        root_run_id=run.root_run_id,
                        run_kind=run.run_kind,
                        latest_event=(
                            latest_event_by_run[run.id].event_type
                            if run.id in latest_event_by_run
                            else None
                        ),
                        delivery_status=run.delivery_status,
                        created_at=run.created_at,
                        updated_at=run.updated_at,
                    )
                    for run in sorted(
                        task_runs,
                        key=lambda value: (
                            value.created_at,
                            value.parent_run_id is not None,
                            value.id,
                        ),
                    )
                ],
                group_id=group_id,
                group_session_id=linked_session_id,
                source_message_id=_uuid(origin.get("message_id")),
                source_message_cursor=(
                    str(origin.get("message_cursor")) if origin.get("message_cursor") else None
                ),
                latest_update=(
                    update.event_type.replace("_", " ") if update else task.status
                ),
                latest_update_at=update.created_at if update else task.updated_at,
                next_actions=actions_by_task.get(task.id, []),
                work_link=f"/work/{task.id}",
                group_link=(
                    f"/groups/{group_id}/{linked_session_id}"
                    if linked_session_id is not None
                    else None
                ),
                created_by=task.created_by,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )
    return output


__all__ = ["load_group_task_summaries"]
