"""Read-only Work detail and action-inbox projections over authoritative facts.

This module deliberately owns no workflow state.  Runtime, Deliverable and
Approval domain tables remain the sources of truth; Work only joins them into
creator/reviewer/manager-scoped product views and links back to domain actions.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Iterable, Literal

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import build_manageable_agents_query
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.models.audit import ApprovalRequest
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableQualityReview,
    DeliverableQualityReviewAssignment,
    DeliverableRequest,
)
from app.models.task import Task, TaskLog, TaskResultReviewReceipt
from app.models.user import User
from app.schemas.work import (
    WorkApprovalSummaryOut,
    WorkArtifactDetailOut,
    WorkDeliverableSummaryOut,
    WorkInboxOut,
    WorkItemOut,
    WorkNextActionOut,
    WorkReviewSummaryOut,
    WorkRunSummaryOut,
    WorkStatusAxesOut,
    WorkTaskDetailOut,
    WorkTaskResultReviewReceiptOut,
    WorkTimelineEventOut,
)
from app.services.work_projection import (
    project_execution_status,
    project_task_result_review_status,
    work_requires_owner_review,
)
from app.services.agent_runtime.tool_execution import (
    can_user_reconcile_unknown_execution,
)


_RUNTIME_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "run_created",
        "status_changed",
        "waiting_started",
        "resumed",
        "run_completed",
        "run_failed",
        "run_cancelled",
    }
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sort_key(value: datetime, stable_id: str) -> tuple[datetime, str]:
    return _aware(value), stable_id


def _task_id_for_run(run: AgentRun) -> uuid.UUID | None:
    raw_candidates: list[str] = []
    if run.correlation_id and run.correlation_id.startswith("work-task:"):
        raw_candidates.append(run.correlation_id.removeprefix("work-task:"))
    if run.source_type == "task" and run.source_id:
        raw_candidates.append(run.source_id)
    if not raw_candidates:
        return None
    try:
        task_ids = {uuid.UUID(str(raw)) for raw in raw_candidates}
    except ValueError:
        return None
    return next(iter(task_ids)) if len(task_ids) == 1 else None


def _runtime_scope_run_id(approval: ApprovalRequest) -> uuid.UUID | None:
    details = approval.details if isinstance(approval.details, dict) else {}
    scope = details.get("runtime_scope")
    if not isinstance(scope, dict):
        return None
    try:
        return uuid.UUID(str(scope.get("run_id")))
    except (TypeError, ValueError):
        return None


def _safe_origin(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "kind",
        "group_id",
        "session_id",
        "message_id",
        "message_cursor",
        "message_excerpt",
    }
    return {key: item for key, item in value.items() if key in allowed}


def collaboration_safe_work_item(item: WorkItemOut) -> WorkItemOut:
    """Remove privileged delivery/runtime payloads from a collaboration summary."""

    snapshot = item.executor_snapshot if isinstance(item.executor_snapshot, dict) else {}
    statement = item.work_statement if isinstance(item.work_statement, dict) else {}
    origin = _safe_origin(snapshot.get("origin") or statement.get("origin"))
    participants = []
    for participant in snapshot.get("participants", []):
        if not isinstance(participant, dict):
            continue
        participants.append(
            {
                key: participant[key]
                for key in ("participant_id", "agent_id", "agent_name", "responsibility")
                if participant.get(key) is not None
            }
        )
    safe_snapshot = {
        key: snapshot[key]
        for key in (
            "agent_id",
            "agent_name",
            "group_id",
            "group_name",
            "group_session_id",
            "group_session_title",
        )
        if snapshot.get(key) is not None
    }
    if participants:
        safe_snapshot["participants"] = participants
    if origin:
        safe_snapshot["origin"] = origin
    safe_statement = {
        key: statement[key]
        for key in (
            "version",
            "title",
            "objective",
            "work_type",
            "priority",
            "acceptance_contract",
        )
        if statement.get(key) is not None
    }
    if origin:
        safe_statement["origin"] = origin
    task_id = item.task_id or (item.id if item.kind == "task" else None)
    return item.model_copy(
        deep=True,
        update={
            "executor_snapshot": safe_snapshot,
            "work_statement": safe_statement,
            "formal_delivery_spec": {},
            "deliverable_id": None,
            "artifacts": [],
            "latest_update": None,
            "latest_update_at": None,
            "deep_link": (
                item.deep_link
                if item.executor_kind == "group"
                else f"/work/{task_id}" if task_id is not None else "/work"
            ),
            "formal_delivery_link": None,
        },
    )


def collaboration_safe_work_detail(detail: WorkTaskDetailOut) -> WorkTaskDetailOut:
    """Return only Group-safe task/run facts plus actions owned by this viewer."""

    safe_timeline: list[WorkTimelineEventOut] = []
    for event in detail.timeline:
        if event.source_type not in {"task", "agent_run", "agent_run_event"}:
            continue
        if event.source_type == "agent_run":
            metadata = {
                key: event.metadata[key]
                for key in ("run_kind", "parent_run_id", "root_run_id")
                if event.metadata.get(key) is not None
            }
        elif event.source_type == "agent_run_event":
            metadata = (
                {"run_id": event.metadata["run_id"]}
                if event.metadata.get("run_id") is not None
                else {}
            )
        else:
            metadata = {}
        safe_timeline.append(
            event.model_copy(
                deep=True,
                update={
                    "summary": event.summary if event.type == "task_created" else None,
                    "metadata": metadata,
                },
            )
        )
    safe_summary = collaboration_safe_work_item(detail.summary)
    links = {"work_index": "/work"}
    if safe_summary.executor_kind == "group":
        links["executor"] = safe_summary.deep_link
    return detail.model_copy(
        deep=True,
        update={
            "detail_scope": "collaboration",
            "summary": safe_summary,
            "timeline": safe_timeline,
            "task_result_reviews": [],
            "deliverables": [],
            "artifacts": [],
            "reviews": [],
            "approvals": [],
            "links": links,
        },
    )


def _encode_cursor(action: WorkNextActionOut) -> str:
    payload = json.dumps(
        [_aware(action.created_at).isoformat(), action.id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        occurred_at, stable_id = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(occurred_at), str(stable_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Work inbox cursor") from exc


def latest_runtime_lifecycle_event_by_run(
    events: Iterable[AgentRunEvent],
) -> dict[uuid.UUID, AgentRunEvent]:
    """Return the latest execution-state event, never a delivery receipt.

    Runtime delivery events report whether the outcome notification reached its
    target.  They do not change whether the execution itself completed or
    failed, so projecting them as a run status would conflate two independent
    product facts.
    """
    latest: dict[uuid.UUID, AgentRunEvent] = {}
    for event in sorted(
        events,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        reverse=True,
    ):
        if event.event_type not in _RUNTIME_LIFECYCLE_EVENT_TYPES:
            continue
        latest.setdefault(event.run_id, event)
    return latest


def _runtime_approval_status(approval: ApprovalRequest | None) -> str:
    if approval is None:
        return "not_required"
    if approval.status == "pending":
        return "pending"
    if approval.status == "rejected":
        return "rejected"
    if approval.execution_status == "executing":
        return "executing"
    if approval.execution_status in {"succeeded", "failed", "ambiguous"}:
        return str(approval.execution_status)
    return "approved"


def _execution_axis(
    *,
    task: Task,
    runs: list[AgentRun],
    events: list[AgentRunEvent],
) -> str:
    if task.status == "done":
        return "completed"
    if task.status == "failed":
        return "failed"
    latest_event = max(
        (
            event
            for event in events
            if event.event_type in _RUNTIME_LIFECYCLE_EVENT_TYPES
        ),
        key=lambda event: _sort_key(event.created_at, str(event.id)),
        default=None,
    )
    if task.status == "doing" and latest_event and latest_event.event_type == "waiting_started":
        return "waiting"
    if task.status == "doing":
        terminal = (
            latest_event.event_type
            if latest_event is not None
            and latest_event.event_type in {"run_completed", "run_failed", "run_cancelled"}
            else None
        )
        return project_execution_status(
            task_status=task.status,
            terminal_run_event=terminal,
        )
    terminal_events = [
        event for event in events if event.event_type in {"run_failed", "run_cancelled"}
    ]
    latest_terminal = max(
        terminal_events,
        key=lambda event: _sort_key(event.created_at, str(event.id)),
        default=None,
    )
    terminal = latest_terminal.event_type if latest_terminal is not None else None
    return project_execution_status(task_status=task.status, terminal_run_event=terminal)


def project_status_axes(
    *,
    task: Task,
    runs: list[AgentRun],
    events: list[AgentRunEvent],
    requests: list[DeliverableRequest],
    executions: list[DeliverableExecution],
    artifacts: list[DeliverableArtifactRevision],
    reviews: list[DeliverableQualityReview],
    runtime_approvals: list[ApprovalRequest],
    approval_receipts: list[DeliverableApprovalReceipt],
    task_result_reviews: list[TaskResultReviewReceipt] | None = None,
) -> WorkStatusAxesOut:
    task_result_reviews = task_result_reviews or []
    latest_request = max(
        requests,
        key=lambda item: _sort_key(item.updated_at, str(item.id)),
        default=None,
    )
    current_request_id = latest_request.id if latest_request is not None else None
    current_executions = [
        item for item in executions if item.request_id == current_request_id
    ]
    current_artifacts = [
        item for item in artifacts if item.request_id == current_request_id
    ]
    current_reviews = [item for item in reviews if item.request_id == current_request_id]
    current_receipts = [
        item for item in approval_receipts if item.request_id == current_request_id
    ]
    latest_execution = max(
        current_executions,
        key=lambda item: _sort_key(item.updated_at, str(item.id)),
        default=None,
    )
    latest_artifact = max(
        current_artifacts,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        default=None,
    )
    latest_review = max(
        current_reviews,
        key=lambda item: _sort_key(item.updated_at, str(item.id)),
        default=None,
    )
    latest_runtime_approval = max(
        runtime_approvals,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        default=None,
    )
    final_receipts = [receipt for receipt in current_receipts if receipt.stage == "final"]
    latest_receipt = max(
        final_receipts,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        default=None,
    )
    latest_run = max(
        runs,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        default=None,
    )
    latest_task_result_review = next(
        (
            receipt
            for receipt in reversed(task_result_reviews)
            if latest_run is not None and receipt.run_id == latest_run.id
        ),
        None,
    )
    task_result_review_status = project_task_result_review_status(
        task_status=task.status,
        work_statement=getattr(task, "work_statement", {}),
        receipt_action=(
            latest_task_result_review.action
            if latest_task_result_review is not None
            else None
        ),
    )

    if latest_receipt is not None:
        delivery_approval = {
            "approve": "approved",
            "request_changes": "request_changes",
            "cancel": "cancelled",
        }[latest_receipt.action]
    elif (
        latest_request is not None
        and latest_request.status == "waiting_approval"
        and latest_request.current_stage == "output_review"
    ):
        delivery_approval = "pending"
    else:
        delivery_approval = "not_required"

    if latest_request is None:
        delivery = "not_requested"
    elif latest_execution is not None and latest_execution.status == "reconciling":
        delivery = "reconciling"
    elif latest_execution is not None and latest_execution.status == "failed":
        delivery = "failed"
    elif latest_execution is not None and latest_execution.status == "cancelled":
        delivery = "cancelled"
    elif latest_request.status == "failed":
        delivery = "failed"
    elif latest_request.status == "cancelled":
        delivery = "cancelled"
    elif (
        latest_request.status == "succeeded"
        and latest_artifact is not None
        and latest_artifact.status == "approved"
    ):
        delivery = "delivered"
    else:
        delivery = "pending"

    return WorkStatusAxesOut(
        execution=_execution_axis(task=task, runs=runs, events=events),
        artifact=latest_artifact.status if latest_artifact is not None else "missing",
        quality=(
            latest_review.status
            if latest_review is not None
            else {
                "pending": "open",
                "approved": "passed",
                "request_changes": "blocked",
            }.get(task_result_review_status, "not_required")
        ),
        runtime_approval=_runtime_approval_status(latest_runtime_approval),
        delivery_approval=delivery_approval,
        delivery=delivery,
    )


async def _task_runs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_ids: list[uuid.UUID],
) -> list[AgentRun]:
    if not task_ids:
        return []
    source_ids = [str(task_id) for task_id in task_ids]
    correlation_ids = [f"work-task:{task_id}" for task_id in task_ids]
    return list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    or_(
                        (AgentRun.source_type == "task")
                        & AgentRun.source_id.in_(source_ids),
                        AgentRun.correlation_id.in_(correlation_ids),
                    ),
                )
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        ).scalars().all()
    )


async def _run_events(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_ids: list[uuid.UUID],
) -> list[AgentRunEvent]:
    if not run_ids:
        return []
    return list(
        (
            await db.execute(
                select(AgentRunEvent)
                .where(
                    AgentRunEvent.tenant_id == tenant_id,
                    AgentRunEvent.run_id.in_(run_ids),
                )
                .order_by(AgentRunEvent.created_at, AgentRunEvent.id)
            )
        ).scalars().all()
    )


async def load_work_inbox_actions(
    db: AsyncSession,
    *,
    user: User,
) -> list[WorkNextActionOut]:
    """Project unresolved actions assigned to this human from domain facts."""

    tenant_id = user.tenant_id
    if tenant_id is None:
        return []
    actions: list[WorkNextActionOut] = []

    reviewer_rows = list(
        (
            await db.execute(
                select(
                    DeliverableQualityReviewAssignment,
                    DeliverableQualityReview,
                    DeliverableRequest,
                )
                .join(
                    DeliverableQualityReview,
                    DeliverableQualityReview.id
                    == DeliverableQualityReviewAssignment.review_id,
                )
                .join(
                    DeliverableRequest,
                    DeliverableRequest.id == DeliverableQualityReview.request_id,
                )
                .where(
                    DeliverableQualityReviewAssignment.tenant_id == tenant_id,
                    DeliverableQualityReviewAssignment.reviewer_user_id == user.id,
                    DeliverableQualityReviewAssignment.status == "assigned",
                    DeliverableQualityReview.status == "open",
                    DeliverableRequest.tenant_id == tenant_id,
                )
            )
        ).all()
    )
    for assignment, review, request in reviewer_rows:
        actions.append(
            WorkNextActionOut(
                id=f"quality_review:{assignment.id}",
                task_id=request.task_id,
                kind="quality_review",
                title="Submit assigned quality review",
                reason_code="quality_review_assigned",
                source_type="quality_review_assignment",
                source_id=str(assignment.id),
                action_url=f"/quality-reviews/{review.id}",
                created_at=assignment.created_at,
                version=str(review.version),
            )
        )

    final_requests = list(
        (
            await db.execute(
                select(DeliverableRequest).where(
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.created_by_user_id == user.id,
                    DeliverableRequest.status == "waiting_approval",
                    DeliverableRequest.current_stage == "output_review",
                )
            )
        ).scalars().all()
    )
    for request in final_requests:
        actions.append(
            WorkNextActionOut(
                id=f"delivery_approval:{request.id}",
                task_id=request.task_id,
                kind="delivery_approval",
                title="Approve or request changes to the formal delivery",
                reason_code="formal_delivery_waiting_for_owner",
                source_type="deliverable_request",
                source_id=str(request.id),
                action_url=(
                    f"/agents/{request.agent_id}/chat?session_id={request.session_id}"
                    + (f"&task_id={request.task_id}" if request.task_id else "")
                ),
                created_at=request.updated_at,
                version=str(request.version),
            )
        )

    manageable_agent_ids = build_manageable_agents_query(
        user,
        tenant_id=tenant_id,
    ).with_only_columns(Agent.id)
    runtime_approvals = list(
        (
            await db.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.agent_id.in_(manageable_agent_ids),
                    ApprovalRequest.status == "pending",
                )
            )
        ).scalars().all()
    )
    approval_run_ids = [
        run_id
        for approval in runtime_approvals
        if (run_id := _runtime_scope_run_id(approval)) is not None
    ]
    approval_runs: dict[uuid.UUID, AgentRun] = {}
    if approval_run_ids:
        approval_runs = {
            run.id: run
            for run in (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.tenant_id == tenant_id,
                        AgentRun.id.in_(approval_run_ids),
                    )
                )
            ).scalars().all()
        }
    mapped_approval_task_ids = {
        task_id
        for run in approval_runs.values()
        if (task_id := _task_id_for_run(run)) is not None
    }
    work_task_ids: set[uuid.UUID] = set()
    if mapped_approval_task_ids:
        work_task_ids = set(
            (
                await db.execute(
                    select(Task.id).where(
                        Task.tenant_id == tenant_id,
                        Task.id.in_(mapped_approval_task_ids),
                        Task.origin_type.in_(("workbench", "group")),
                    )
                )
            ).scalars().all()
        )
    for approval in runtime_approvals:
        run_id = _runtime_scope_run_id(approval)
        run = approval_runs.get(run_id) if run_id is not None else None
        task_id = _task_id_for_run(run) if run is not None else None
        if (
            run is None
            or approval.agent_id is None
            or task_id not in work_task_ids
        ):
            continue
        actions.append(
            WorkNextActionOut(
                id=f"runtime_approval:{approval.id}",
                task_id=task_id,
                kind="runtime_approval",
                title="Review a high-risk Runtime action",
                reason_code="runtime_l3_approval_pending",
                source_type="approval_request",
                source_id=str(approval.id),
                action_url=f"/agents/{approval.agent_id}/settings#approvals",
                created_at=approval.created_at,
                version=f"{approval.status}:{approval.execution_status or ''}",
            )
        )

    owned_tasks = list(
        (
            await db.execute(
                select(Task).where(
                    Task.tenant_id == tenant_id,
                    Task.created_by == user.id,
                )
            )
        ).scalars().all()
    )
    task_by_id = {task.id: task for task in owned_tasks}
    task_runs = await _task_runs(db, tenant_id=tenant_id, task_ids=list(task_by_id))
    latest_run_by_task: dict[uuid.UUID, AgentRun] = {}
    for run in sorted(
        task_runs,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        reverse=True,
    ):
        task_id = _task_id_for_run(run)
        if task_id in task_by_id:
            latest_run_by_task.setdefault(task_id, run)
    reviewable_task_ids = [
        task.id
        for task in owned_tasks
        if task.status == "done"
        and work_requires_owner_review(getattr(task, "work_statement", {}))
    ]
    task_result_receipts = (
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
    latest_result_review_by_run: dict[uuid.UUID, TaskResultReviewReceipt] = {}
    for receipt in sorted(
        task_result_receipts,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        reverse=True,
    ):
        latest_result_review_by_run.setdefault(receipt.run_id, receipt)
    for task_id, task in task_by_id.items():
        latest_run = latest_run_by_task.get(task_id)
        if (
            latest_run is None
            or task.status != "done"
            or not work_requires_owner_review(getattr(task, "work_statement", {}))
        ):
            continue
        latest_result_review = latest_result_review_by_run.get(latest_run.id)
        if latest_result_review is not None:
            if latest_result_review.action == "request_changes":
                actions.append(
                    WorkNextActionOut(
                        id=f"task_recovery:{latest_result_review.id}",
                        task_id=task_id,
                        kind="task_recovery",
                        title="Retry the task after the owner requested changes",
                        reason_code="task_result_changes_requested",
                        source_type="task_result_review_receipt",
                        source_id=str(latest_result_review.id),
                        action_url=f"/work/{task_id}",
                        created_at=latest_result_review.created_at,
                        version=str(latest_result_review.id),
                    )
                )
            continue
        actions.append(
            WorkNextActionOut(
                id=f"task_result_review:{latest_run.id}",
                task_id=task_id,
                kind="task_result_review",
                title="Review the completed task result against the confirmed criteria",
                reason_code="task_result_owner_review_pending",
                source_type="agent_run",
                source_id=str(latest_run.id),
                action_url=f"/work/{task_id}",
                created_at=latest_run.updated_at,
                version=str(latest_run.id),
            )
        )
    task_events = await _run_events(
        db,
        tenant_id=tenant_id,
        run_ids=[run.id for run in task_runs],
    )
    run_by_id = {run.id: run for run in task_runs}
    latest_event_by_run = latest_runtime_lifecycle_event_by_run(task_events)
    waiting_event_by_run = {
        run_id: event
        for run_id, event in latest_event_by_run.items()
        if event.event_type == "waiting_started"
        and isinstance(event.payload, dict)
        and event.payload.get("waiting_type") == "user"
    }
    reconciliation_run_ids = [
        run_id
        for run_id in waiting_event_by_run
        if (run := run_by_id.get(run_id)) is not None
        and run.source_type == "task"
        and _task_id_for_run(run) in task_by_id
    ]
    if reconciliation_run_ids:
        unknown_executions = list(
            (
                await db.execute(
                    select(AgentToolExecution)
                    .where(
                        AgentToolExecution.tenant_id == tenant_id,
                        AgentToolExecution.run_id.in_(reconciliation_run_ids),
                        AgentToolExecution.status == "unknown",
                    )
                    .order_by(
                        AgentToolExecution.started_at,
                        AgentToolExecution.id,
                    )
                )
            ).scalars().all()
        )
        for execution in unknown_executions:
            if not can_user_reconcile_unknown_execution(execution):
                continue
            waiting_event = waiting_event_by_run.get(execution.run_id)
            payload = (
                waiting_event.payload
                if waiting_event is not None and isinstance(waiting_event.payload, dict)
                else {}
            )
            correlation_id = payload.get("correlation_id")
            expected_correlations = {
                str(
                    uuid.uuid5(
                        execution.run_id,
                        f"tool-reconcile:{execution.tool_call_id}",
                    )
                ),
                f"tool-confirm:{execution.run_id}",
            }
            if correlation_id not in expected_correlations:
                continue
            run = run_by_id.get(execution.run_id)
            task_id = _task_id_for_run(run) if run is not None else None
            if task_id not in task_by_id:
                continue
            actions.append(
                WorkNextActionOut(
                    id=f"tool_reconciliation:{execution.id}",
                    task_id=task_id,
                    kind="tool_reconciliation",
                    title=f"Confirm the outcome of {execution.tool_name}",
                    reason_code=(
                        str(execution.result_metadata.get("error_code"))
                        if isinstance(execution.result_metadata, dict)
                        and execution.result_metadata.get("error_code")
                        else "tool_outcome_unknown"
                    ),
                    source_type="agent_tool_execution",
                    source_id=str(execution.id),
                    action_url=f"/work/{task_id}",
                    created_at=execution.updated_at,
                    version=f"{execution.status}:{execution.attempt_count}",
                )
            )
    latest_recovery_by_task: dict[uuid.UUID, tuple[AgentRun, AgentRunEvent]] = {}
    for event in sorted(
        task_events,
        key=lambda item: _sort_key(item.created_at, str(item.id)),
        reverse=True,
    ):
        if event.event_type not in {"run_failed", "run_cancelled"}:
            continue
        run = run_by_id.get(event.run_id)
        if run is None:
            continue
        task_id = _task_id_for_run(run)
        task = task_by_id.get(task_id) if task_id is not None else None
        if task is None or task.status not in {"pending", "failed"}:
            continue
        latest_recovery_by_task.setdefault(task_id, (run, event))
    for task_id, (run, latest_event) in latest_recovery_by_task.items():
        actions.append(
            WorkNextActionOut(
                id=f"task_recovery:{run.id}",
                task_id=task_id,
                kind="task_recovery",
                title="Retry the failed or cancelled task attempt",
                reason_code=latest_event.event_type,
                source_type="agent_run",
                source_id=str(run.id),
                action_url=f"/work/{task_id}",
                created_at=latest_event.created_at,
                version=str(run.id),
            )
        )

    recovery_rows = list(
        (
            await db.execute(
                select(DeliverableExecution, DeliverableRequest)
                .join(
                    DeliverableRequest,
                    DeliverableRequest.id == DeliverableExecution.request_id,
                )
                .where(
                    DeliverableExecution.tenant_id == tenant_id,
                    DeliverableExecution.status.in_({"failed", "blocked", "reconciling"}),
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.created_by_user_id == user.id,
                    DeliverableRequest.current_execution_id == DeliverableExecution.id,
                )
            )
        ).all()
    )
    for execution, request in recovery_rows:
        actions.append(
            WorkNextActionOut(
                id=f"delivery_recovery:{execution.id}",
                task_id=request.task_id,
                kind="delivery_recovery",
                title="Resolve the blocked formal delivery",
                reason_code=f"deliverable_execution_{execution.status}",
                source_type="deliverable_execution",
                source_id=str(execution.id),
                action_url=(
                    f"/agents/{request.agent_id}/chat?session_id={request.session_id}"
                    + (f"&task_id={request.task_id}" if request.task_id else "")
                ),
                created_at=execution.updated_at,
                version=f"{execution.execution_number}:{execution.status}",
            )
        )

    actions.sort(
        key=lambda action: _sort_key(action.created_at, action.id),
        reverse=True,
    )
    return actions


async def load_work_inbox(
    db: AsyncSession,
    *,
    user: User,
    limit: int,
    cursor: str | None,
    kind: str | None,
) -> WorkInboxOut:
    actions = await load_work_inbox_actions(db, user=user)
    if kind is not None:
        actions = [action for action in actions if action.kind == kind]
    if cursor:
        cursor_key = _decode_cursor(cursor)
        actions = [
            action
            for action in actions
            if _sort_key(action.created_at, action.id) < cursor_key
        ]
    page = actions[:limit]
    return WorkInboxOut(
        items=page,
        next_cursor=_encode_cursor(page[-1]) if len(actions) > limit and page else None,
    )


async def load_work_task_detail(
    db: AsyncSession,
    *,
    user: User,
    task: Task,
    summary: WorkItemOut,
    detail_scope: Literal["full", "collaboration"] = "full",
) -> WorkTaskDetailOut:
    tenant_id = task.tenant_id
    runs = await _task_runs(db, tenant_id=tenant_id, task_ids=[task.id])
    events = await _run_events(
        db,
        tenant_id=tenant_id,
        run_ids=[run.id for run in runs],
    )
    logs = list(
        (
            await db.execute(
                select(TaskLog)
                .where(TaskLog.task_id == task.id)
                .order_by(TaskLog.created_at, TaskLog.id)
            )
        ).scalars().all()
    )
    task_result_reviews = list(
        (
            await db.execute(
                select(TaskResultReviewReceipt)
                .where(
                    TaskResultReviewReceipt.tenant_id == tenant_id,
                    TaskResultReviewReceipt.task_id == task.id,
                )
                .order_by(
                    TaskResultReviewReceipt.created_at,
                    TaskResultReviewReceipt.id,
                )
            )
        ).scalars().all()
    )
    requests = list(
        (
            await db.execute(
                select(DeliverableRequest)
                .where(
                    DeliverableRequest.tenant_id == tenant_id,
                    DeliverableRequest.task_id == task.id,
                    DeliverableRequest.created_by_user_id == task.created_by,
                )
                .order_by(DeliverableRequest.created_at, DeliverableRequest.id)
            )
        ).scalars().all()
    )
    request_ids = [request.id for request in requests]

    artifacts: list[DeliverableArtifactRevision] = []
    executions: list[DeliverableExecution] = []
    reviews: list[DeliverableQualityReview] = []
    assignments: list[DeliverableQualityReviewAssignment] = []
    receipts: list[DeliverableApprovalReceipt] = []
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
                        DeliverableArtifactRevision.created_at,
                        DeliverableArtifactRevision.id,
                    )
                )
            ).scalars().all()
        )
        executions = list(
            (
                await db.execute(
                    select(DeliverableExecution)
                    .where(
                        DeliverableExecution.tenant_id == tenant_id,
                        DeliverableExecution.request_id.in_(request_ids),
                    )
                    .order_by(DeliverableExecution.created_at, DeliverableExecution.id)
                )
            ).scalars().all()
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
                        DeliverableQualityReview.created_at,
                        DeliverableQualityReview.id,
                    )
                )
            ).scalars().all()
        )
        review_ids = [review.id for review in reviews]
        if review_ids:
            assignments = list(
                (
                    await db.execute(
                        select(DeliverableQualityReviewAssignment).where(
                            DeliverableQualityReviewAssignment.tenant_id == tenant_id,
                            DeliverableQualityReviewAssignment.review_id.in_(review_ids),
                        )
                    )
                ).scalars().all()
            )
        receipts = list(
            (
                await db.execute(
                    select(DeliverableApprovalReceipt)
                    .where(
                        DeliverableApprovalReceipt.tenant_id == tenant_id,
                        DeliverableApprovalReceipt.request_id.in_(request_ids),
                    )
                    .order_by(
                        DeliverableApprovalReceipt.created_at,
                        DeliverableApprovalReceipt.id,
                    )
                )
            ).scalars().all()
        )

    run_ids = {run.id for run in runs}
    runtime_approvals: list[ApprovalRequest] = []
    agent_ids = {run.agent_id for run in runs if run.agent_id is not None}
    if agent_ids:
        possible_approvals = list(
            (
                await db.execute(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.agent_id.in_(agent_ids))
                    .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
                )
            ).scalars().all()
        )
        runtime_approvals = [
            approval
            for approval in possible_approvals
            if _runtime_scope_run_id(approval) in run_ids
        ]

    latest_event = latest_runtime_lifecycle_event_by_run(events)
    assignment_by_review = {
        assignment.review_id: assignment
        for assignment in assignments
        if assignment.reviewer_user_id == user.id
    }
    all_actions = await load_work_inbox_actions(db, user=user)
    next_actions = [action for action in all_actions if action.task_id == task.id]

    timeline: list[WorkTimelineEventOut] = [
        WorkTimelineEventOut(
            id=f"task:{task.id}:created",
            type="task_created",
            occurred_at=task.created_at,
            source_type="task",
            source_id=str(task.id),
            status=task.status,
            title="Task created",
            summary=task.intent,
            actor_type="user",
            actor_id=task.created_by,
        )
    ]
    if task.confirmed_at is not None:
        timeline.append(
            WorkTimelineEventOut(
                id=f"task:{task.id}:confirmed",
                type="task_confirmed",
                occurred_at=task.confirmed_at,
                source_type="task",
                source_id=str(task.id),
                status="confirmed",
                title="Work statement confirmed",
                actor_type="user",
                actor_id=task.created_by,
                metadata={"confirmation_fingerprint": task.confirmation_fingerprint},
            )
        )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"task_log:{log.id}",
            type="task_log",
            occurred_at=log.created_at,
            source_type="task_log",
            source_id=str(log.id),
            title="Task update",
            summary=log.content,
        )
        for log in logs
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"task_result_review:{receipt.id}",
            type="task_result_review",
            occurred_at=receipt.created_at,
            source_type="task_result_review_receipt",
            source_id=str(receipt.id),
            status=receipt.action,
            title="Task result approved" if receipt.action == "approve" else "Task changes requested",
            summary=receipt.comment,
            actor_type="user",
            actor_id=receipt.actor_user_id,
            metadata={"run_id": str(receipt.run_id)},
        )
        for receipt in task_result_reviews
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"run:{run.id}",
            type="run_registered",
            occurred_at=run.created_at,
            source_type="agent_run",
            source_id=str(run.id),
            status=latest_event.get(run.id).event_type if latest_event.get(run.id) else None,
            title="Runtime attempt registered",
            summary=run.goal,
            actor_type="agent" if run.agent_id else "system",
            actor_id=run.agent_id,
            metadata={
                "run_kind": run.run_kind,
                "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
                "root_run_id": str(run.root_run_id) if run.root_run_id else None,
                "correlation_id": run.correlation_id,
            },
        )
        for run in runs
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"run_event:{event.id}",
            type=event.event_type,
            occurred_at=event.created_at,
            source_type="agent_run_event",
            source_id=str(event.id),
            status=event.event_type,
            title=event.event_type.replace("_", " ").title(),
            summary=event.summary,
            actor_type="agent" if event.agent_id else "system",
            actor_id=event.agent_id,
            metadata={
                "run_id": str(event.run_id),
                "artifact_refs": list(event.artifact_refs or []),
            },
        )
        for event in events
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"deliverable:{request.id}",
            type="deliverable_requested",
            occurred_at=request.created_at,
            source_type="deliverable_request",
            source_id=str(request.id),
            status=request.status,
            title="Formal delivery requested",
            summary=request.goal,
            actor_type="user",
            actor_id=request.created_by_user_id,
            metadata={"work_type": request.work_type, "current_stage": request.current_stage},
        )
        for request in requests
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"deliverable_execution:{execution.id}",
            type="deliverable_execution",
            occurred_at=execution.created_at,
            source_type="deliverable_execution",
            source_id=str(execution.id),
            status=execution.status,
            title=f"Formal delivery attempt {execution.execution_number}",
            summary=execution.blocked_reason or execution.last_error_code,
            metadata={
                "request_id": str(execution.request_id),
                "kind": execution.kind,
                "current_stage": execution.current_stage,
            },
        )
        for execution in executions
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"artifact:{artifact.id}",
            type="artifact_revision",
            occurred_at=artifact.created_at,
            source_type="deliverable_artifact_revision",
            source_id=str(artifact.id),
            status=artifact.status,
            title=f"Artifact revision {artifact.revision_number}",
            metadata={
                "request_id": str(artifact.request_id),
                "artifact_key": artifact.artifact_key,
                "artifact_type": artifact.artifact_type,
                "workspace_path": artifact.workspace_path,
                "content_hash": artifact.content_hash,
            },
        )
        for artifact in artifacts
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"quality_review:{review.id}",
            type="quality_review",
            occurred_at=review.created_at,
            source_type="deliverable_quality_review",
            source_id=str(review.id),
            status=review.status,
            title="Quality review opened",
            metadata={
                "request_id": str(review.request_id),
                "modality": review.modality,
                "assigned_reviewer_count": review.assigned_reviewer_count,
            },
        )
        for review in reviews
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"runtime_approval:{approval.id}",
            type="runtime_approval",
            occurred_at=approval.created_at,
            source_type="approval_request",
            source_id=str(approval.id),
            status=approval.status,
            title="High-risk Runtime action approval",
            summary=approval.action_type,
            metadata={
                "run_id": str(_runtime_scope_run_id(approval)),
                "execution_status": approval.execution_status,
            },
        )
        for approval in runtime_approvals
    )
    timeline.extend(
        WorkTimelineEventOut(
            id=f"delivery_approval:{receipt.id}",
            type="delivery_approval",
            occurred_at=receipt.created_at,
            source_type="deliverable_approval_receipt",
            source_id=str(receipt.id),
            status=receipt.action,
            title="Formal delivery decision",
            summary=receipt.instruction,
            actor_type="user",
            actor_id=receipt.actor_user_id,
            metadata={
                "request_id": str(receipt.request_id),
                "execution_id": str(receipt.execution_id),
                "stage": receipt.stage,
            },
        )
        for receipt in receipts
    )
    timeline.sort(
        key=lambda event: _sort_key(event.occurred_at, event.id),
        reverse=True,
    )

    detail = WorkTaskDetailOut(
        detail_scope="full",
        summary=summary,
        status_axes=project_status_axes(
            task=task,
            runs=runs,
            events=events,
            requests=requests,
            executions=executions,
            artifacts=artifacts,
            reviews=reviews,
            runtime_approvals=runtime_approvals,
            approval_receipts=receipts,
            task_result_reviews=task_result_reviews,
        ),
        timeline=timeline,
        next_actions=next_actions,
        runs=[
            WorkRunSummaryOut(
                id=run.id,
                agent_id=run.agent_id,
                parent_run_id=run.parent_run_id,
                root_run_id=run.root_run_id,
                run_kind=run.run_kind,
                latest_event=(
                    latest_event[run.id].event_type if run.id in latest_event else None
                ),
                delivery_status=run.delivery_status,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            for run in sorted(
                runs,
                key=lambda item: _sort_key(item.created_at, str(item.id)),
                reverse=True,
            )
        ],
        task_result_reviews=[
            WorkTaskResultReviewReceiptOut.model_validate(receipt)
            for receipt in reversed(task_result_reviews)
        ],
        deliverables=[
            WorkDeliverableSummaryOut.model_validate(request, from_attributes=True)
            for request in reversed(requests)
        ],
        artifacts=[
            WorkArtifactDetailOut.model_validate(artifact, from_attributes=True)
            for artifact in reversed(artifacts)
        ],
        reviews=[
            WorkReviewSummaryOut(
                id=review.id,
                request_id=review.request_id,
                status=review.status,
                modality=review.modality,
                minimum_reviewers=review.minimum_reviewers,
                assigned_reviewer_count=review.assigned_reviewer_count,
                current_user_assignment_status=(
                    assignment_by_review[review.id].status
                    if review.id in assignment_by_review
                    else None
                ),
                version=review.version,
                created_at=review.created_at,
                updated_at=review.updated_at,
            )
            for review in reversed(reviews)
        ],
        approvals=[
            *[
                WorkApprovalSummaryOut(
                    id=approval.id,
                    kind="runtime",
                    source_id=str(_runtime_scope_run_id(approval)),
                    status=approval.status,
                    action_type=approval.action_type,
                    execution_status=approval.execution_status,
                    created_at=approval.created_at,
                    resolved_at=approval.resolved_at,
                )
                for approval in reversed(runtime_approvals)
            ],
            *[
                WorkApprovalSummaryOut(
                    id=receipt.id,
                    kind="delivery",
                    source_id=str(receipt.request_id),
                    status=receipt.action,
                    action_type=receipt.action,
                    created_at=receipt.created_at,
                )
                for receipt in reversed(receipts)
            ],
        ],
        links={
            "work_index": "/work",
            "executor": summary.deep_link,
            **(
                {"formal_delivery": summary.formal_delivery_link}
                if summary.formal_delivery_link
                else {}
            ),
        },
    )
    return (
        collaboration_safe_work_detail(detail)
        if detail_scope == "collaboration"
        else detail
    )


__all__ = [
    "collaboration_safe_work_detail",
    "collaboration_safe_work_item",
    "load_work_inbox",
    "load_work_inbox_actions",
    "load_work_task_detail",
    "project_status_axes",
]
