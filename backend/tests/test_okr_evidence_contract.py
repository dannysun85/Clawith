"""Contracts for linking terminal work evidence to OKR progress."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.okr import (
    ProgressUpdate,
    _evidence_snapshots_with_validity,
    _resolve_progress_evidence,
)
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.models.okr import OKRProgressLog
from app.models.task import Task


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value

    def all(self):
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _statement):
        assert self.values, "unexpected evidence query"
        return _ScalarResult(self.values.pop(0))


def _task(*, status: str = "done") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        title="完成客户调研",
        description="已整理访谈结论",
        intent="完成客户调研并给出结论",
        work_type="document",
        executor_kind="agent_employee",
        executor_snapshot={},
        status=status,
        completed_at=now if status == "done" else None,
        updated_at=now,
    )


def _deliverable(*, task_id: uuid.UUID | None = None) -> DeliverableRequest:
    now = datetime.now(timezone.utc)
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=task_id,
        goal="面向客户的正式商业提案",
        work_type="presentation",
        status="succeeded",
        completed_at=now,
        updated_at=now,
    )


def _artifact(request: DeliverableRequest) -> DeliverableArtifactRevision:
    now = datetime.now(timezone.utc)
    return DeliverableArtifactRevision(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        workspace_path="workspace/deliverables/final.pptx",
        artifact_type="pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        content_hash="a" * 64,
        status="approved",
        approved_at=now,
        created_at=now,
    )


@pytest.mark.asyncio
async def test_completed_task_becomes_an_immutable_evidence_snapshot() -> None:
    task = _task()
    db = _Session(task, "研究员")

    task_id, request_id, snapshot = await _resolve_progress_evidence(
        db,
        tenant_id=task.tenant_id,
        body=ProgressUpdate(value=80, source_task_id=task.id),
    )

    assert task_id == task.id
    assert request_id is None
    assert snapshot == {
        "kind": "task",
        "title": "完成客户调研",
        "summary": "已整理访谈结论",
        "work_type": "document",
        "owner_name": "研究员",
        "task_id": str(task.id),
        "deliverable_request_id": None,
        "completed_at": task.completed_at.isoformat(),
        "deep_link": f"/agents/{task.agent_id}/chat?task_id={task.id}",
        "artifact": None,
    }


@pytest.mark.asyncio
async def test_unfinished_task_is_rejected_as_okr_evidence() -> None:
    task = _task(status="doing")

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_progress_evidence(
            _Session(task),
            tenant_id=task.tenant_id,
            body=ProgressUpdate(value=50, source_task_id=task.id),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "task_evidence_not_completed"


@pytest.mark.asyncio
async def test_formal_delivery_requires_an_approved_artifact() -> None:
    request = _deliverable()

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_progress_evidence(
            _Session(request, None),
            tenant_id=request.tenant_id,
            body=ProgressUpdate(
                value=100,
                source_deliverable_request_id=request.id,
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "deliverable_evidence_not_approved"


@pytest.mark.asyncio
async def test_approved_delivery_snapshot_keeps_artifact_identity() -> None:
    request = _deliverable()
    artifact = _artifact(request)
    db = _Session(request, artifact, "方案专家")

    task_id, request_id, snapshot = await _resolve_progress_evidence(
        db,
        tenant_id=request.tenant_id,
        body=ProgressUpdate(
            value=100,
            source_deliverable_request_id=request.id,
        ),
    )

    assert task_id is None
    assert request_id == request.id
    assert snapshot["kind"] == "deliverable"
    assert snapshot["artifact"]["id"] == str(artifact.id)
    assert snapshot["artifact"]["content_hash"] == "a" * 64
    assert snapshot["artifact"]["download_url"].endswith(
        f"/{artifact.id}/download?inline=true"
    )


@pytest.mark.asyncio
async def test_task_and_delivery_sources_must_share_one_work_chain() -> None:
    task = _task()
    request = _deliverable(task_id=uuid.uuid4())
    request.tenant_id = task.tenant_id
    artifact = _artifact(request)

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_progress_evidence(
            _Session(task, request, artifact),
            tenant_id=task.tenant_id,
            body=ProgressUpdate(
                value=100,
                source_task_id=task.id,
                source_deliverable_request_id=request.id,
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "evidence_source_mismatch"


@pytest.mark.asyncio
async def test_task_cannot_be_paired_with_a_standalone_delivery() -> None:
    task = _task()
    request = _deliverable(task_id=None)
    request.tenant_id = task.tenant_id
    artifact = _artifact(request)

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_progress_evidence(
            _Session(task, request, artifact),
            tenant_id=task.tenant_id,
            body=ProgressUpdate(
                value=100,
                source_task_id=task.id,
                source_deliverable_request_id=request.id,
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "evidence_source_mismatch"


@pytest.mark.asyncio
async def test_live_validity_marks_a_replaced_artifact_without_mutating_snapshot() -> None:
    tenant_id = uuid.uuid4()
    request_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    kr_id = uuid.uuid4()
    frozen = {
        "kind": "deliverable",
        "deliverable_request_id": str(request_id),
        "artifact": {"id": str(artifact_id), "content_hash": "a" * 64},
    }
    log = OKRProgressLog(
        kr_id=kr_id,
        previous_value=50,
        new_value=80,
        source="manual",
        source_deliverable_request_id=request_id,
        evidence_snapshot=dict(frozen),
    )

    current = await _evidence_snapshots_with_validity(
        _Session(
            [(request_id, "succeeded")],
            [(artifact_id, "approved", "a" * 64)],
        ),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        logs_by_kr={kr_id: log},
    )
    superseded = await _evidence_snapshots_with_validity(
        _Session(
            [(request_id, "succeeded")],
            [(artifact_id, "superseded", "a" * 64)],
        ),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        logs_by_kr={kr_id: log},
    )

    assert current[kr_id]["validity"] == "current"
    assert superseded[kr_id]["validity"] == "superseded"
    assert superseded[kr_id]["validity_reason"] == "source_artifact_superseded"
    assert log.evidence_snapshot == frozen
