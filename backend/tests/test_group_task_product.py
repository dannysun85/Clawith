"""Group-to-Task explicit conversion and linked-task projection contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.api import groups as groups_api
from app.api import work as work_api
from app.models.task import Task, TaskLog
from app.schemas.work import (
    WorkArtifactSummary,
    WorkExecutorProposalOut,
    WorkItemOut,
    WorkStatusAxesOut,
    WorkTaskCreate,
    WorkTaskDetailOut,
    WorkTimelineEventOut,
)
from app.services.group_chat_service import GroupChatServiceError
from app.services.group_task_projection import load_group_task_summaries
from app.services.work_detail_projection import (
    collaboration_safe_work_detail,
    collaboration_safe_work_item,
)


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


def _source_payload(**overrides):
    group_id = overrides.pop("group_id", uuid.uuid4())
    session_id = overrides.pop("session_id", uuid.uuid4())
    values = {
        "title": "Prepare the launch decision",
        "intent": "Prepare a launch decision with risks and owner actions.",
        "work_type": "general",
        "priority": "medium",
        "routing_mode": "manual",
        "executor_kind": "group",
        "group_id": group_id,
        "group_session_id": session_id,
        "group_agent_participant_ids": [uuid.uuid4()],
        "source_kind": "group_message",
        "source_group_id": group_id,
        "source_session_id": session_id,
        "source_message_id": uuid.uuid4(),
        "source_message_cursor": f"{NOW.isoformat()}|{uuid.uuid4()}",
    }
    values.update(overrides)
    return values


def _create_contract(**overrides) -> WorkTaskCreate:
    return WorkTaskCreate(
        **_source_payload(**overrides),
        client_request_id=uuid.uuid4(),
        confirmation_fingerprint="a" * 64,
    )


def _item(task_id: uuid.UUID, agent_id: uuid.UUID) -> WorkItemOut:
    return WorkItemOut(
        id=task_id,
        kind="task",
        title="Prepare the launch decision",
        intent="Prepare a launch decision with risks and owner actions.",
        origin_type="group",
        executor_kind="group",
        agent_id=agent_id,
        agent_name="Planner",
        task_id=task_id,
        task_status="pending",
        execution_status="queued",
        delivery_status="not_requested",
        delivery_mode="task_only",
        user_stage="in_progress",
        deep_link=f"/groups/{uuid.uuid4()}/{uuid.uuid4()}",
        created_at=NOW,
        updated_at=NOW,
    )


def test_group_message_source_requires_matching_manual_group_context() -> None:
    with pytest.raises(ValidationError, match="only valid for manual routing"):
        WorkTaskCreate(
            **_source_payload(routing_mode="auto", executor_kind=None),
            client_request_id=uuid.uuid4(),
            confirmation_fingerprint="a" * 64,
        )

    with pytest.raises(ValidationError, match="must match the selected Group and session"):
        _create_contract(source_session_id=uuid.uuid4())

    with pytest.raises(ValidationError, match="only valid for a Group message source"):
        WorkTaskCreate(
            title="Normal task",
            intent="This remains a normal Workbench task.",
            source_message_id=uuid.uuid4(),
            client_request_id=uuid.uuid4(),
            confirmation_fingerprint="a" * 64,
        )


class _Result:
    def __init__(self, values):
        self.values = values

    def scalar_one_or_none(self):
        if isinstance(self.values, list):
            return self.values[0] if self.values else None
        return self.values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _QueuedDb:
    def __init__(self, values):
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.values.pop(0))


@pytest.mark.asyncio
async def test_group_source_resolution_locks_and_snapshots_visible_message(monkeypatch) -> None:
    data = _create_contract()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    actor = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(id=data.group_session_id, title="Launch room")
    message = SimpleNamespace(
        id=data.source_message_id,
        role="user",
        content="Launch only after legal approval.",
        created_at=NOW,
    )
    group = SimpleNamespace(id=data.group_id, name="Launch team")
    agent_id = uuid.uuid4()
    agent_participant = SimpleNamespace(
        id=data.group_agent_participant_ids[0],
        type="agent",
        ref_id=agent_id,
    )
    membership = SimpleNamespace(participant_id=agent_participant.id)
    agent = SimpleNamespace(id=agent_id, name="Launch Planner", role_description="Planner")
    db = _QueuedDb(
        [
            actor,
            message,
            group,
            [(membership, agent_participant)],
            [agent],
        ]
    )

    async def authorize(*_args, **_kwargs):
        return session

    monkeypatch.setattr(work_api, "authorize_group_session", authorize)
    monkeypatch.setattr(work_api, "is_agent_executable", lambda _agent: True)

    async def can_use(*_args, **_kwargs):
        return True

    monkeypatch.setattr(work_api, "can_use_agent", can_use)

    resolved = await work_api._resolve_executor(  # type: ignore[attr-defined]
        db,  # type: ignore[arg-type]
        data=data,
        user=user,
        lock_source=True,
    )

    assert resolved.primary_agent == agent
    assert resolved.snapshot["origin"] == {
        "kind": "group_message",
        "group_id": str(data.group_id),
        "session_id": str(data.group_session_id),
        "message_id": str(data.source_message_id),
        "message_cursor": f"{NOW.isoformat()}|{data.source_message_id}",
        "message_excerpt": "Launch only after legal approval.",
    }
    assert "FOR UPDATE" in str(db.statements[1])
    assert resolved.snapshot["participants"][0]["responsibility"] == "primary_owner"


@pytest.mark.asyncio
async def test_system_message_is_rejected_before_group_or_agent_resolution(monkeypatch) -> None:
    data = _create_contract()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    actor = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(id=data.group_session_id, title="Launch room")
    message = SimpleNamespace(
        id=data.source_message_id,
        role="system",
        content="Internal system event",
        created_at=NOW,
    )
    db = _QueuedDb([actor, message])

    async def authorize(*_args, **_kwargs):
        return session

    monkeypatch.setattr(work_api, "authorize_group_session", authorize)

    with pytest.raises(HTTPException) as exc:
        await work_api._resolve_executor(  # type: ignore[attr-defined]
            db,  # type: ignore[arg-type]
            data=data,
            user=user,
            lock_source=True,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "group_source_message_not_convertible"
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_group_task_rejects_agent_the_current_user_cannot_use(monkeypatch) -> None:
    data = _create_contract()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    actor = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(id=data.group_session_id, title="Launch room")
    message = SimpleNamespace(
        id=data.source_message_id,
        role="user",
        content="Prepare the decision",
        created_at=NOW,
    )
    group = SimpleNamespace(id=data.group_id, name="Launch team")
    agent_id = uuid.uuid4()
    participant = SimpleNamespace(
        id=data.group_agent_participant_ids[0],
        type="agent",
        ref_id=agent_id,
    )
    membership = SimpleNamespace(participant_id=participant.id)
    agent = SimpleNamespace(id=agent_id, name="Private Planner", role_description="Planner")
    db = _QueuedDb([actor, message, group, [(membership, participant)], [agent]])

    async def authorize(*_args, **_kwargs):
        return session

    async def denied(*_args, **_kwargs):
        return False

    monkeypatch.setattr(work_api, "authorize_group_session", authorize)
    monkeypatch.setattr(work_api, "is_agent_executable", lambda _agent: True)
    monkeypatch.setattr(work_api, "can_use_agent", denied)

    with pytest.raises(HTTPException) as exc:
        await work_api._resolve_executor(  # type: ignore[attr-defined]
            db,  # type: ignore[arg-type]
            data=data,
            user=user,
            lock_source=True,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "group_agent_access_denied"


class _CreateDb:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _Result(None)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if isinstance(value, Task) and value.id is None:
                value.id = uuid.uuid4()

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def commit(self):
        self.commits += 1


def _selection(data: WorkTaskCreate):
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, name="Planner")
    origin = {
        "kind": "group_message",
        "group_id": str(data.group_id),
        "session_id": str(data.group_session_id),
        "message_id": str(data.source_message_id),
        "message_cursor": data.source_message_cursor,
        "message_excerpt": data.intent,
    }
    resolved = work_api._ResolvedExecutor(  # type: ignore[attr-defined]
        primary_agent=agent,
        agents=(agent,),
        snapshot={
            "agent_id": str(agent_id),
            "agent_name": agent.name,
            "group_id": str(data.group_id),
            "group_name": "Launch team",
            "group_session_id": str(data.group_session_id),
            "group_session_title": "Launch room",
            "participants": [
                {
                    "participant_id": str(data.group_agent_participant_ids[0]),
                    "agent_id": str(agent_id),
                    "agent_name": agent.name,
                    "responsibility": "primary_owner",
                }
            ],
            "origin": origin,
        },
        executor_kind="group",
    )
    proposal = WorkExecutorProposalOut(
        policy_version="test",
        chosen_executor_kind="group",
        agent_id=agent_id,
        agent_name=agent.name,
        confidence=1,
    )
    return work_api._ExecutorSelection(  # type: ignore[attr-defined]
        resolved=resolved,
        proposal=proposal,
        candidate_facts_hash="facts",
    )


@pytest.mark.asyncio
async def test_explicit_conversion_persists_one_group_task_and_audit_log(monkeypatch) -> None:
    data = _create_contract()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    db = _CreateDb()
    selection = _selection(data)
    selected_with_lock = []

    async def select_executor(*_args, **kwargs):
        selected_with_lock.append(kwargs.get("lock_source"))
        return selection

    async def no_source_task(*_args, **_kwargs):
        return None

    async def capability(*_args, **_kwargs):
        return "available", [], None

    async def enqueue(*_args, **_kwargs):
        return SimpleNamespace(run_id=uuid.uuid4(), created=True)

    async def project(_db, *, task_id, **_kwargs):
        return _item(task_id, selection.resolved.primary_agent.id)

    monkeypatch.setattr(work_api, "_select_executor", select_executor)
    monkeypatch.setattr(work_api, "_existing_group_source_task", no_source_task)
    monkeypatch.setattr(work_api, "_executor_capability", capability)
    monkeypatch.setattr(work_api, "_confirmation_fingerprint", lambda *_args, **_kwargs: "a" * 64)
    monkeypatch.setattr(work_api, "enqueue_group_task_runtime", enqueue)
    monkeypatch.setattr(work_api, "_work_item_for_task", project)

    result = await work_api.create_work_task(data, current_user=user, db=db)  # type: ignore[arg-type]

    task = next(value for value in db.added if isinstance(value, Task))
    log = next(value for value in db.added if isinstance(value, TaskLog))
    assert result.created is True
    assert selected_with_lock == [True]
    assert task.origin_type == "group"
    assert task.group_id == data.group_id
    assert task.executor_snapshot["origin"]["message_id"] == str(data.source_message_id)
    assert task.work_statement["origin"] == task.executor_snapshot["origin"]
    assert log.task_id == task.id
    assert str(data.source_message_id) in log.content
    assert db.commits == 1


@pytest.mark.asyncio
async def test_same_group_source_conversion_returns_the_authoritative_task(monkeypatch) -> None:
    data = _create_contract()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    db = _CreateDb()
    selection = _selection(data)
    source_task = SimpleNamespace(
        id=uuid.uuid4(),
        request_fingerprint=work_api._fingerprint(data),  # type: ignore[attr-defined]
    )
    projected = _item(source_task.id, selection.resolved.primary_agent.id)
    authorized_task = None

    async def select_executor(*_args, **kwargs):
        assert kwargs.get("lock_source") is True
        return selection

    async def existing_source(*_args, **_kwargs):
        return source_task

    async def project(_db, *, task_id, authorized_task: object | None = None, **_kwargs):
        nonlocal authorized_task_seen
        authorized_task_seen = authorized_task
        assert task_id == source_task.id
        return projected

    authorized_task_seen = authorized_task
    monkeypatch.setattr(work_api, "_select_executor", select_executor)
    monkeypatch.setattr(work_api, "_existing_group_source_task", existing_source)
    monkeypatch.setattr(work_api, "_work_item_for_task", project)

    result = await work_api.create_work_task(data, current_user=user, db=db)  # type: ignore[arg-type]

    assert result.created is False
    assert result.item.id == source_task.id
    assert authorized_task_seen is source_task
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_group_source_reauthorization_happens_before_idempotent_lookup(monkeypatch) -> None:
    data = _create_contract()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    db = _CreateDb()

    async def revoked(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="Group membership is required")

    monkeypatch.setattr(work_api, "_select_executor", revoked)
    with pytest.raises(HTTPException) as exc:
        await work_api.create_work_task(data, current_user=user, db=db)  # type: ignore[arg-type]

    assert exc.value.status_code == 403
    assert db.execute_calls == 0
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_group_task_read_fails_before_projection_for_removed_member(monkeypatch) -> None:
    group_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), is_active=True)
    participant = SimpleNamespace(id=uuid.uuid4())
    projected = False

    async def current_participant(*_args, **_kwargs):
        return participant

    async def denied(*_args, **_kwargs):
        raise GroupChatServiceError("group_access_denied", "Active membership is required")

    async def projection(*_args, **_kwargs):
        nonlocal projected
        projected = True
        return []

    monkeypatch.setattr(groups_api, "_current_participant", current_participant)
    monkeypatch.setattr(groups_api.group_chat_service, "authorize_group_session", denied)
    monkeypatch.setattr(groups_api, "load_group_task_summaries", projection)

    with pytest.raises(HTTPException) as exc:
        await groups_api.list_group_tasks(
            group_id,
            session_id=session_id,
            limit=50,
            current_user=user,
            db=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 403
    assert projected is False


@pytest.mark.asyncio
async def test_group_task_projection_keeps_status_axes_and_bidirectional_links() -> None:
    tenant_id = uuid.uuid4()
    group_id = uuid.uuid4()
    session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        group_id=group_id,
        executor_kind="group",
        executor_snapshot={
            "group_session_id": str(session_id),
            "participants": [
                {
                    "agent_id": str(agent_id),
                    "agent_name": "Planner",
                    "responsibility": "primary_owner",
                }
            ],
            "origin": {
                "kind": "group_message",
                "session_id": str(session_id),
                "message_id": str(uuid.uuid4()),
                "message_cursor": "cursor-1",
            },
        },
        agent_id=agent_id,
        title="Launch decision",
        intent="Decide whether to launch",
        status="pending",
        created_by=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )
    agent = SimpleNamespace(id=agent_id, name="Planner")
    db = _QueuedDb([[task], [], [agent], []])

    output = await load_group_task_summaries(
        db,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        group_id=group_id,
        session_id=session_id,
        limit=50,
    )

    assert len(output) == 1
    summary = output[0]
    assert summary.status_axes.execution == "queued"
    assert summary.status_axes.delivery == "not_requested"
    assert summary.primary_owner_agent_id == agent_id
    assert summary.work_link == f"/work/{task_id}"
    assert summary.group_link == f"/groups/{group_id}/{session_id}"


@pytest.mark.asyncio
async def test_group_task_projection_exposes_safe_root_and_failed_participant_runs() -> None:
    tenant_id = uuid.uuid4()
    group_id = uuid.uuid4()
    session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        group_id=group_id,
        executor_kind="group",
        executor_snapshot={
            "group_session_id": str(session_id),
            "participants": [
                {
                    "agent_id": str(agent_id),
                    "agent_name": "Planner",
                    "responsibility": "primary_owner",
                }
            ],
            "origin": {"kind": "group_message", "session_id": str(session_id)},
        },
        agent_id=agent_id,
        title="Launch decision",
        intent="Decide whether to launch",
        status="pending",
        created_by=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )
    root = SimpleNamespace(
        id=root_id,
        tenant_id=tenant_id,
        agent_id=None,
        system_role="group_planning",
        source_type="task",
        source_id=str(task_id),
        correlation_id=f"work-task:{task_id}",
        parent_run_id=None,
        root_run_id=None,
        run_kind="orchestration",
        delivery_status="not_required",
        created_at=NOW,
        updated_at=NOW,
    )
    child = SimpleNamespace(
        id=child_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        system_role=None,
        source_type="task",
        source_id=str(task_id),
        correlation_id=f"work-task:{task_id}",
        parent_run_id=root_id,
        root_run_id=root_id,
        run_kind="foreground",
        delivery_status="failed",
        created_at=NOW,
        updated_at=NOW,
    )
    root_event = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=root_id,
        event_type="run_completed",
        created_at=NOW,
    )
    child_event = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=child_id,
        event_type="run_failed",
        created_at=NOW,
    )
    child_delivery_receipt = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=child_id,
        event_type="delivery_succeeded",
        created_at=NOW + timedelta(seconds=1),
    )
    agent = SimpleNamespace(id=agent_id, name="Planner")
    db = _QueuedDb(
        [
            [task],
            [root, child],
            [agent],
            [root_event, child_event, child_delivery_receipt],
            [],
            [],
        ]
    )

    output = await load_group_task_summaries(
        db,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        group_id=group_id,
        session_id=session_id,
        limit=50,
    )

    assert output[0].status_axes.execution == "failed"
    assert [(run.run_kind, run.agent_name, run.latest_event) for run in output[0].runs] == [
        ("orchestration", "Group planner", "run_completed"),
        ("foreground", "Planner", "run_failed"),
    ]


def test_collaboration_projection_redacts_privileged_task_and_delivery_facts() -> None:
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    full_item = _item(task_id, agent_id).model_copy(
        update={
            "executor_snapshot": {
                "agent_id": str(agent_id),
                "agent_name": "Planner",
                "group_id": str(uuid.uuid4()),
                "sender_participant_id": str(uuid.uuid4()),
                "routing_decision": {"candidate_facts_hash": "private"},
                "participants": [
                    {
                        "participant_id": str(uuid.uuid4()),
                        "agent_id": str(agent_id),
                        "agent_name": "Planner",
                        "role_description": "internal role prompt",
                        "responsibility": "primary_owner",
                    }
                ],
                "origin": {
                    "kind": "group_message",
                    "message_id": str(source_message_id),
                    "message_excerpt": "Prepare the launch decision",
                    "private": "must not escape",
                },
            },
            "work_statement": {
                "version": 1,
                "title": "Launch decision",
                "objective": "Decide whether to launch",
                "cost": {"internal": "private"},
            },
            "formal_delivery_spec": {"private": "delivery contract"},
            "deliverable_id": uuid.uuid4(),
            "artifacts": [
                WorkArtifactSummary(
                    id=artifact_id,
                    artifact_type="document",
                    status="candidate",
                    workspace_path="/private/report.pdf",
                    revision_number=1,
                )
            ],
            "latest_update": "private delivery body",
            "latest_update_at": NOW,
            "formal_delivery_link": "/private-delivery",
        }
    )

    safe_item = collaboration_safe_work_item(full_item)

    assert safe_item.executor_snapshot["origin"]["message_id"] == str(source_message_id)
    assert "private" not in safe_item.executor_snapshot["origin"]
    assert "routing_decision" not in safe_item.executor_snapshot
    assert "sender_participant_id" not in safe_item.executor_snapshot
    assert "role_description" not in safe_item.executor_snapshot["participants"][0]
    assert "cost" not in safe_item.work_statement
    assert safe_item.formal_delivery_spec == {}
    assert safe_item.deliverable_id is None
    assert safe_item.artifacts == []
    assert safe_item.latest_update is None
    assert safe_item.formal_delivery_link is None

    detail = WorkTaskDetailOut.model_construct(
        detail_scope="full",
        summary=full_item,
        status_axes=WorkStatusAxesOut(
            execution="running",
            artifact="candidate",
            quality="open",
            runtime_approval="pending",
            delivery_approval="pending",
            delivery="pending",
        ),
        timeline=[
            WorkTimelineEventOut(
                id="task",
                type="task_created",
                occurred_at=NOW,
                source_type="task",
                source_id=str(task_id),
                title="Task created",
                summary="Shared objective",
                metadata={"confirmation_fingerprint": "private"},
            ),
            WorkTimelineEventOut(
                id="run-event",
                type="tool_finished",
                occurred_at=NOW,
                source_type="agent_run_event",
                source_id=str(uuid.uuid4()),
                title="Tool finished",
                summary="private tool output",
                metadata={"run_id": str(uuid.uuid4()), "artifact_refs": ["private"]},
            ),
            WorkTimelineEventOut(
                id="artifact",
                type="artifact_revision",
                occurred_at=NOW,
                source_type="deliverable_artifact_revision",
                source_id=str(artifact_id),
                title="Artifact",
                summary="private artifact",
            ),
        ],
        next_actions=[],
        runs=[],
        deliverables=["private"],
        artifacts=["private"],
        reviews=["private"],
        approvals=["private"],
        links={
            "work_index": "/work",
            "executor": full_item.deep_link,
            "formal_delivery": "/private-delivery",
        },
    )

    safe_detail = collaboration_safe_work_detail(detail)

    assert safe_detail.detail_scope == "collaboration"
    assert [event.source_type for event in safe_detail.timeline] == ["task", "agent_run_event"]
    assert safe_detail.timeline[0].metadata == {}
    assert safe_detail.timeline[1].summary is None
    assert set(safe_detail.timeline[1].metadata) == {"run_id"}
    assert safe_detail.deliverables == []
    assert safe_detail.artifacts == []
    assert safe_detail.reviews == []
    assert safe_detail.approvals == []
    assert "formal_delivery" not in safe_detail.links


def _retry_group_task(*, owner_agent_id: uuid.UUID, participant_id: uuid.UUID):
    group_id = uuid.uuid4()
    session_id = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        group_id=group_id,
        executor_kind="group",
        agent_id=owner_agent_id,
        executor_snapshot={
            "group_id": str(group_id),
            "group_session_id": str(session_id),
            "participants": [
                {
                    "participant_id": str(participant_id),
                    "agent_id": str(owner_agent_id),
                    "agent_name": "Planner",
                    "responsibility": "primary_owner",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_group_retry_fails_closed_after_creator_membership_revocation(monkeypatch) -> None:
    task = _retry_group_task(
        owner_agent_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=task.tenant_id)
    actor = SimpleNamespace(id=uuid.uuid4())
    db = _QueuedDb([actor])
    agent_checked = False

    async def revoked(*_args, **_kwargs):
        raise GroupChatServiceError("group_access_denied", "Active membership is required")

    async def check_agent(*_args, **_kwargs):
        nonlocal agent_checked
        agent_checked = True

    monkeypatch.setattr(work_api, "authorize_group_session", revoked)
    monkeypatch.setattr(work_api, "check_agent_access", check_agent)

    with pytest.raises(HTTPException) as exc:
        await work_api._retry_executor(db, task=task, user=user)  # type: ignore[arg-type,attr-defined]

    assert exc.value.status_code == 403
    assert agent_checked is False


@pytest.mark.asyncio
async def test_group_retry_rejects_removed_immutable_participant(monkeypatch) -> None:
    task = _retry_group_task(
        owner_agent_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=task.tenant_id)
    actor = SimpleNamespace(id=uuid.uuid4())
    db = _QueuedDb([actor, []])

    async def authorize(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(work_api, "authorize_group_session", authorize)

    with pytest.raises(HTTPException) as exc:
        await work_api._retry_executor(db, task=task, user=user)  # type: ignore[arg-type,attr-defined]

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "group_retry_participant_snapshot_changed"
