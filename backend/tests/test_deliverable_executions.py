"""Provider-free contracts for revision-safe creative deliverable executions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.api import deliverables
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableArtifactRevision,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.schemas.deliverable import DeliverableApprovalIn
from app.services.deliverable_executions import (
    build_execution_shadow,
    create_revision_execution,
    execution_unit_blueprints,
    project_execution_lifecycle,
    safe_preflight_snapshot,
)


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, *execute_values: object | None) -> None:
        self.execute_values = list(execute_values)
        self.added: list[object] = []
        self.flush_count = 0

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)

    def add_all(self, values) -> None:
        self.added.extend(values)


def _request(*, work_type: str = "presentation", spec: dict | None = None) -> DeliverableRequest:
    output_contract = {
        "presentation": ["pptx", "pdf"],
        "poster": ["png"],
        "video": ["mp4"],
    }[work_type]
    workflow_id = {
        "presentation": "builtin.presentation.v1",
        "poster": "builtin.poster.v1",
        "video": "builtin.video.v1",
    }[work_type]
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        work_type=work_type,
        workflow_id=workflow_id,
        workflow_version="1.0.0",
        goal="Create a commercial customer deliverable",
        inputs=[],
        spec=spec or {},
        tier="pro",
        approval_policy=["outline", "final"],
        output_contract=output_contract,
        status="waiting_approval",
        current_stage="output_review",
        version=3,
        contract_revision=1,
    )


def test_unit_blueprints_model_pages_candidates_and_shots_without_provider_calls() -> None:
    presentation = _request(spec={"page_count": 6})
    presentation_units = execution_unit_blueprints(presentation)
    assert len(presentation_units) == 16
    assert [unit.unit_key for unit in presentation_units if unit.stage_key == "slide_render"] == [
        "slide-01",
        "slide-02",
        "slide-03",
        "slide-04",
        "slide-05",
        "slide-06",
    ]
    targeted = execution_unit_blueprints(presentation, target_units=["slide-03"])
    assert [unit.unit_key for unit in targeted if unit.stage_key == "slide_render"] == [
        "slide-03"
    ]

    poster = _request(work_type="poster")
    poster_units = execution_unit_blueprints(poster)
    assert [unit.unit_key for unit in poster_units if unit.stage_key == "candidate_generate"] == [
        "candidate-01",
        "candidate-02",
    ]

    video = _request(work_type="video", spec={"duration": 10})
    video_units = execution_unit_blueprints(video)
    assert [unit.unit_key for unit in video_units if unit.stage_key == "shot_generate"] == [
        "shot-01",
        "shot-02",
        "shot-03",
    ]


def test_preflight_snapshot_is_secret_free_and_build_is_deterministic() -> None:
    snapshot = safe_preflight_snapshot(
        {
            "launchable": True,
            "available": True,
            "reasons": [],
            "tier": "pro",
            "api_key": "must-not-persist",
            "provider_secret": "must-not-persist",
        }
    )
    assert snapshot["evidence_level"] == "provider_free_preflight"
    assert "api_key" not in snapshot
    assert "provider_secret" not in snapshot

    request = _request(spec={"page_count": 3})
    execution, units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="brief_confirmed",
    )
    assert execution.contract_snapshot["goal"] == request.goal
    assert execution.contract_snapshot["contract_revision"] == 1
    assert all(len(unit.dependency_hash) == 64 for unit in units)
    assert len({(unit.stage_key, unit.unit_key) for unit in units}) == len(units)


@pytest.mark.asyncio
async def test_revision_preserves_prior_execution_and_artifacts_as_history() -> None:
    request = _request(spec={"page_count": 4})
    current, current_units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="output_review",
    )
    current.status = "waiting_approval"
    request.current_execution_id = current.id
    artifact = DeliverableArtifactRevision(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=current.id,
        artifact_key="pptx",
        artifact_type="pptx",
        workspace_path=f"workspace/deliverables/{request.id}/final.pptx",
        content_hash="b" * 64,
        revision_number=1,
        status="candidate",
        evaluation={"verified": True},
    )
    client_revision_id = uuid.uuid4()
    db = _Session(current, None, list(current_units), [artifact])

    revision, created = await create_revision_execution(
        db,  # type: ignore[arg-type]
        request,
        client_revision_id=client_revision_id,
        instruction="Simplify slide 2 and replace its hero visual",
        target_units=["slide-02"],
    )

    assert created is True
    assert current.status == "succeeded"
    assert current.current_stage == "revision_requested"
    assert all(unit.status == "superseded" for unit in current_units)
    assert all(
        unit.result_snapshot["lifecycle_projection"]["evidence_level"]
        == "superseded_by_customer_revision"
        for unit in current_units
    )
    assert artifact.status == "rejected"
    assert request.current_execution_id == revision.id
    assert request.contract_revision == 2
    assert request.version == 4
    assert request.status == "ready"
    assert revision.execution_number == 2
    assert revision.contract_snapshot["target_units"] == ["slide-02"]
    added_units = [item for item in db.added if isinstance(item, DeliverableExecutionUnit)]
    assert [unit.unit_key for unit in added_units if unit.stage_key == "slide_render"] == [
        "slide-02"
    ]
    assert db.flush_count == 1

    replay_db = _Session(revision, revision)
    replay, replay_created = await create_revision_execution(
        replay_db,  # type: ignore[arg-type]
        request,
        client_revision_id=client_revision_id,
        instruction="Simplify slide 2 and replace its hero visual",
        target_units=["slide-02"],
    )
    assert replay is revision
    assert replay_created is False
    assert replay_db.added == []


@pytest.mark.asyncio
async def test_verified_output_projects_remaining_v1_units_to_complete() -> None:
    request = _request(work_type="presentation", spec={"page_count": 3})
    execution, units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="output_review",
    )
    request.current_execution_id = execution.id
    output_unit = next(unit for unit in units if unit.stage_key == "pptx_render")
    output_unit.status = "succeeded"
    output_unit.result_snapshot = {"artifact_revision_id": str(uuid.uuid4())}
    db = _Session(execution, list(units))

    projected = await project_execution_lifecycle(
        db,  # type: ignore[arg-type]
        request,
    )

    assert projected is execution
    assert execution.status == "waiting_approval"
    assert all(unit.status == "succeeded" for unit in units)
    assert "lifecycle_projection" not in output_unit.result_snapshot
    projected_units = [unit for unit in units if unit is not output_unit]
    assert {
        unit.result_snapshot["lifecycle_projection"]["evidence_level"]
        for unit in projected_units
    } == {"projected_v1_runtime_completion"}


@pytest.mark.asyncio
async def test_failed_request_projects_active_v1_units_without_fabricating_success() -> None:
    request = _request(work_type="video", spec={"duration": 8})
    request.status = "failed"
    request.current_stage = "artifact_verification_failed"
    request.last_error_code = "deliverable_artifact_invalid"
    execution, units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="running",
    )
    execution.status = "running"
    request.current_execution_id = execution.id
    db = _Session(execution, list(units))

    await project_execution_lifecycle(db, request)  # type: ignore[arg-type]

    assert execution.status == "failed"
    assert all(unit.status == "failed" for unit in units)
    assert all(
        unit.last_error_code == "deliverable_artifact_invalid" for unit in units
    )
    assert {
        unit.result_snapshot["lifecycle_projection"]["evidence_level"]
        for unit in units
    } == {"projected_v1_runtime_failure"}


@pytest.mark.asyncio
async def test_execution_history_projects_lazy_legacy_shadow_before_response(
    monkeypatch,
) -> None:
    request = _request()
    request.status = "succeeded"
    request.current_stage = "delivered"
    execution, _units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="brief_confirmed",
    )
    original_updated_at = datetime(2026, 7, 28, 7, 27, tzinfo=UTC)
    request.updated_at = original_updated_at
    user = SimpleNamespace(id=request.created_by_user_id)
    db = _Session([execution])
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))

    async def adopt_execution(*_args, **_kwargs):
        request.current_execution_id = execution.id
        request.updated_at = datetime(2026, 8, 1, 4, 20, tzinfo=UTC)
        return execution

    ensure = AsyncMock(side_effect=adopt_execution)
    artifact = DeliverableArtifactRevision(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        artifact_key="pptx",
        artifact_type="pptx",
        workspace_path=f"workspace/deliverables/{request.id}/final.pptx",
        content_hash="f" * 64,
        revision_number=1,
        status="approved",
        evaluation={"verified": True},
    )
    selected_artifacts = (artifact,)
    artifacts = AsyncMock(return_value=selected_artifacts)
    bind = AsyncMock()
    project = AsyncMock(return_value=execution)
    monkeypatch.setattr(deliverables, "ensure_execution_shadow", ensure)
    monkeypatch.setattr(deliverables, "_request_artifacts", artifacts)
    monkeypatch.setattr(deliverables, "bind_artifacts_to_current_execution", bind)
    monkeypatch.setattr(deliverables, "project_execution_lifecycle", project)
    monkeypatch.setattr(
        deliverables,
        "_execution_out",
        AsyncMock(return_value=execution),
    )

    result = await deliverables.list_deliverable_executions(
        request.id,
        20,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result == [execution]
    ensure.assert_awaited_once_with(db, request, lock=True)
    artifacts.assert_awaited_once_with(db, request, lock=True)
    bind.assert_awaited_once_with(db, request, selected_artifacts)
    project.assert_awaited_once_with(db, request)
    assert request.updated_at == original_updated_at
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_final_approval_writes_idempotent_receipt(monkeypatch) -> None:
    request = _request()
    current, _units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="output_review",
    )
    current.status = "waiting_approval"
    request.current_execution_id = current.id
    artifacts = [
        DeliverableArtifactRevision(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            artifact_key=artifact_type,
            artifact_type=artifact_type,
            workspace_path=f"workspace/deliverables/{request.id}/final.{artifact_type}",
            content_hash=("c" if artifact_type == "pptx" else "d") * 64,
            revision_number=1,
            status="candidate",
            evaluation={"verified": True},
        )
        for artifact_type in ("pptx", "pdf")
    ]
    user = SimpleNamespace(id=request.created_by_user_id)
    db = _Session(None)
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(deliverables, "ensure_execution_shadow", AsyncMock(return_value=current))
    monkeypatch.setattr(
        deliverables,
        "approve_deliverable_artifacts",
        AsyncMock(return_value=tuple(artifacts)),
    )
    project = AsyncMock(return_value=current)
    monkeypatch.setattr(deliverables, "project_execution_lifecycle", project)
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(return_value=request))
    action_id = uuid.uuid4()

    result = await deliverables.record_deliverable_approval(
        request.id,
        DeliverableApprovalIn(
            expected_version=3,
            client_action_id=action_id,
            stage="final",
            action="approve",
        ),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result is request
    assert request.status == "succeeded"
    assert request.current_stage == "delivered"
    assert request.version == 4
    receipts = [item for item in db.added if isinstance(item, DeliverableApprovalReceipt)]
    assert len(receipts) == 1
    assert receipts[0].client_action_id == action_id
    assert receipts[0].execution_id == current.id
    assert receipts[0].receipt["result_request_version"] == 4
    assert all(artifact.status == "approved" for artifact in artifacts)
    project.assert_awaited_once()
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_revision_approval_records_old_and_new_execution_lineage(monkeypatch) -> None:
    request = _request(work_type="video", spec={"duration": 8})
    current, _units = build_execution_shadow(
        request,
        execution_number=1,
        kind="initial",
        idempotency_key=request.client_request_id,
        current_stage="output_review",
    )
    current.status = "waiting_approval"
    request.current_execution_id = current.id
    next_execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=2,
        kind="revision",
        status="ready",
        current_stage="revision_ready",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=uuid.uuid4(),
        request_fingerprint="e" * 64,
    )
    user = SimpleNamespace(id=request.created_by_user_id)
    db = _Session(None)
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(deliverables, "ensure_execution_shadow", AsyncMock(return_value=current))

    async def create_revision(*_args, **_kwargs):
        request.current_execution_id = next_execution.id
        request.contract_revision = 2
        request.status = "ready"
        request.current_stage = "revision_ready"
        request.completed_at = None
        request.last_error_code = None
        request.version += 1
        return next_execution, True

    monkeypatch.setattr(deliverables, "create_revision_execution", create_revision)
    monkeypatch.setattr(
        deliverables,
        "_supersede_quality_reviews_for_revision",
        AsyncMock(return_value=(uuid.uuid4(),)),
    )
    monkeypatch.setattr(
        deliverables,
        "project_execution_lifecycle",
        AsyncMock(return_value=next_execution),
    )
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(return_value=request))
    action_id = uuid.uuid4()

    result = await deliverables.record_deliverable_approval(
        request.id,
        DeliverableApprovalIn(
            expected_version=3,
            client_action_id=action_id,
            stage="final",
            action="request_changes",
            instruction="Keep the script but replace shot 2 with a closer product view",
            target_units=["shot-02"],
        ),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result is request
    receipts = [item for item in db.added if isinstance(item, DeliverableApprovalReceipt)]
    assert len(receipts) == 1
    assert receipts[0].execution_id == current.id
    assert receipts[0].receipt["next_execution_id"] == str(next_execution.id)
    assert receipts[0].target_units == ["shot-02"]
    assert request.status == "ready"
    assert request.version == 4
