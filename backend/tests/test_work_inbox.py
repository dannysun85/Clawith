"""Work detail and human action-inbox product contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
import pytest

from app.api import work as work_api
from app.core.security import get_current_user
from app.database import get_db
from app.schemas.work import WorkItemOut, WorkStatusAxesOut, WorkTaskDetailOut
from app.services.work_detail_projection import (
    _task_id_for_run,
    latest_runtime_lifecycle_event_by_run,
    load_work_inbox_actions,
    project_status_axes,
)


NOW = datetime(2026, 8, 19, 2, 30, tzinfo=UTC)


def test_latest_runtime_lifecycle_event_excludes_later_delivery_receipts() -> None:
    run_id = uuid.uuid4()
    failed = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        event_type="run_failed",
        created_at=NOW,
    )
    delivered = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        event_type="delivery_succeeded",
        created_at=NOW + timedelta(seconds=1),
    )
    channel_delivered = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        event_type="channel_delivery_delivered",
        created_at=NOW + timedelta(seconds=2),
    )

    assert latest_runtime_lifecycle_event_by_run(
        [failed, delivered, channel_delivered]
    ) == {run_id: failed}


def test_runtime_run_must_resolve_to_one_unambiguous_work_task() -> None:
    task_id = uuid.uuid4()
    assert _task_id_for_run(
        SimpleNamespace(
            source_type="task",
            source_id=str(task_id),
            correlation_id=f"work-task:{task_id}",
        )
    ) == task_id
    assert _task_id_for_run(
        SimpleNamespace(
            source_type="task",
            source_id=str(uuid.uuid4()),
            correlation_id=f"work-task:{task_id}",
        )
    ) is None
    assert _task_id_for_run(
        SimpleNamespace(
            source_type="chat",
            source_id=str(task_id),
            correlation_id=None,
        )
    ) is None


def _work_item(*, task_id: uuid.UUID, agent_id: uuid.UUID) -> WorkItemOut:
    return WorkItemOut(
        id=task_id,
        kind="task",
        title="Prepare launch brief",
        intent="Prepare the launch brief",
        origin_type="workbench",
        executor_kind="agent_employee",
        agent_id=agent_id,
        agent_name="Planner",
        task_id=task_id,
        task_status="pending",
        priority="medium",
        execution_status="failed",
        delivery_status="not_requested",
        delivery_mode="task_only",
        user_stage="blocked",
        deep_link=f"/agents/{agent_id}/chat?task_id={task_id}",
        formal_delivery_link=f"/agents/{agent_id}/chat?task_id={task_id}",
        created_at=NOW,
        updated_at=NOW,
    )


def test_status_axes_do_not_collapse_runtime_artifact_review_and_delivery() -> None:
    task_id = uuid.uuid4()
    failed_run_id = uuid.uuid4()
    later_completed_child_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, status="pending")
    runs = [
        SimpleNamespace(id=failed_run_id, created_at=NOW),
        SimpleNamespace(id=later_completed_child_id, created_at=NOW + timedelta(seconds=1)),
    ]
    events = [
        SimpleNamespace(
            id=uuid.uuid4(),
            run_id=failed_run_id,
            event_type="run_failed",
            created_at=NOW + timedelta(seconds=2),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            run_id=later_completed_child_id,
            event_type="run_completed",
            created_at=NOW + timedelta(seconds=3),
        ),
    ]
    request_id = uuid.uuid4()
    request = SimpleNamespace(
        id=request_id,
        status="waiting_approval",
        current_stage="output_review",
        updated_at=NOW,
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        request_id=request_id,
        status="reconciling",
        updated_at=NOW,
    )
    artifact = SimpleNamespace(
        id=uuid.uuid4(),
        request_id=request_id,
        status="candidate",
        created_at=NOW,
    )
    review = SimpleNamespace(
        id=uuid.uuid4(),
        request_id=request_id,
        status="open",
        updated_at=NOW,
    )
    approval = SimpleNamespace(
        id=uuid.uuid4(),
        status="pending",
        execution_status=None,
        created_at=NOW,
    )

    axes = project_status_axes(
        task=task,
        runs=runs,
        events=events,
        requests=[request],
        executions=[execution],
        artifacts=[artifact],
        reviews=[review],
        runtime_approvals=[approval],
        approval_receipts=[],
    )

    assert axes == WorkStatusAxesOut(
        execution="failed",
        artifact="candidate",
        quality="open",
        runtime_approval="pending",
        delivery_approval="pending",
        delivery="reconciling",
    )


class _Result:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values

    def scalars(self):
        return self


class _QueuedDb:
    def __init__(self, values):
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.values.pop(0))


@pytest.mark.asyncio
async def test_inbox_projects_all_five_action_kinds_from_authoritative_facts() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    request_id = uuid.uuid4()
    review_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=NOW,
    )
    review = SimpleNamespace(id=review_id, version=2)
    request = SimpleNamespace(
        id=request_id,
        task_id=task_id,
        agent_id=agent_id,
        session_id=uuid.uuid4(),
        updated_at=NOW + timedelta(seconds=1),
        version=3,
    )
    run_id = uuid.uuid4()
    approval = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        action_type="delete_external_resource",
        details={"runtime_scope": {"run_id": str(run_id), "tenant_id": str(tenant_id)}},
        status="pending",
        execution_status=None,
        created_at=NOW + timedelta(seconds=2),
    )
    run = SimpleNamespace(
        id=run_id,
        source_type="task",
        source_id=str(task_id),
        correlation_id=None,
        created_at=NOW,
    )
    task = SimpleNamespace(id=task_id, status="pending")
    failed_event = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        event_type="run_failed",
        created_at=NOW + timedelta(seconds=3),
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        status="blocked",
        execution_number=2,
        updated_at=NOW + timedelta(seconds=4),
    )
    db = _QueuedDb(
        [
            [(assignment, review, request)],
            [request],
            [approval],
            [run],
            [task_id],
            [task],
            [run],
            [failed_event],
            [(execution, request)],
        ]
    )
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="org_admin",
        is_active=True,
    )

    actions = await load_work_inbox_actions(db, user=user)  # type: ignore[arg-type]

    assert {action.kind for action in actions} == {
        "quality_review",
        "runtime_approval",
        "delivery_approval",
        "task_recovery",
        "delivery_recovery",
    }
    assert all(action.task_id == task_id for action in actions)
    assert next(action for action in actions if action.kind == "quality_review").action_url == (
        f"/quality-reviews/{review_id}"
    )
    assert next(action for action in actions if action.kind == "runtime_approval").action_url == (
        f"/agents/{agent_id}/settings#approvals"
    )
    sql = "\n".join(str(statement) for statement in db.statements)
    assert "deliverable_quality_review_assignments.reviewer_user_id" in sql
    assert "deliverable_requests.created_by_user_id" in sql
    assert "tasks.origin_type" in sql
    assert "tasks.created_by" in sql
    assert "notifications" not in sql.lower()


def test_detail_endpoint_is_additive_and_keeps_legacy_get_task_shape(monkeypatch) -> None:
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id, created_by=user.id)
    summary = _work_item(task_id=task_id, agent_id=agent_id)
    detail = WorkTaskDetailOut(
        summary=summary,
        status_axes=WorkStatusAxesOut(
            execution="failed",
            artifact="missing",
            quality="not_required",
            runtime_approval="not_required",
            delivery_approval="not_required",
            delivery="not_requested",
        ),
    )

    async def visible(*_args, **_kwargs):
        return task

    async def item(*_args, **_kwargs):
        return summary

    async def projected(*_args, **_kwargs):
        return detail

    monkeypatch.setattr(work_api, "_visible_work_task", visible)
    monkeypatch.setattr(work_api, "_work_item_for_task", item)
    monkeypatch.setattr(work_api, "load_work_task_detail", projected)

    async def db_dependency():
        yield object()

    app = FastAPI()
    app.include_router(work_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = db_dependency

    with TestClient(app) as client:
        legacy = client.get(f"/api/work/tasks/{task_id}")
        expanded = client.get(f"/api/work/tasks/{task_id}/detail")

    assert legacy.status_code == 200
    assert legacy.json()["task_id"] == str(task_id)
    assert "summary" not in legacy.json()
    assert expanded.status_code == 200
    assert expanded.headers["cache-control"] == "no-store"
    assert expanded.json()["summary"]["task_id"] == str(task_id)
    assert expanded.json()["status_axes"]["execution"] == "failed"


@pytest.mark.asyncio
async def test_non_owner_authorized_detail_is_forced_to_collaboration_scope(monkeypatch) -> None:
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        created_by=uuid.uuid4(),
    )
    summary = _work_item(task_id=task_id, agent_id=uuid.uuid4())
    captured_scope = None

    async def visible(*_args, **_kwargs):
        return task

    async def item(*_args, **_kwargs):
        return summary

    async def projected(*_args, **kwargs):
        nonlocal captured_scope
        captured_scope = kwargs["detail_scope"]
        return WorkTaskDetailOut(
            detail_scope="collaboration",
            summary=summary,
            status_axes=WorkStatusAxesOut(
                execution="running",
                artifact="missing",
                quality="not_required",
                runtime_approval="not_required",
                delivery_approval="not_required",
                delivery="not_requested",
            ),
        )

    monkeypatch.setattr(work_api, "_visible_work_task", visible)
    monkeypatch.setattr(work_api, "_work_item_for_task", item)
    monkeypatch.setattr(work_api, "load_work_task_detail", projected)

    result = await work_api.get_work_task_detail(
        task_id,
        Response(),
        current_user=user,
        db=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert captured_scope == "collaboration"
    assert result.detail_scope == "collaboration"
