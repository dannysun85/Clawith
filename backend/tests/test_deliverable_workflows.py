"""Deliverable contract validation, launch security, and estimate tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from pydantic import ValidationError

from app.models.deliverable import DeliverableRequest
from app.schemas.deliverable import DeliverableInput
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    attach_deliverable_run,
    build_deliverable_prompt,
    list_workflow_manifests,
    prepare_deliverable_launch,
    preflight_workflow,
    request_fingerprint,
    require_workflow,
    validate_workflow_spec,
    sync_deliverable_lifecycle,
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, value: object) -> None:
        self.value = value

    async def execute(self, _statement):
        return _ScalarResult(self.value)


def _request(**overrides):
    tenant_id = overrides.pop("tenant_id", uuid.uuid4())
    user_id = overrides.pop("created_by_user_id", uuid.uuid4())
    agent_id = overrides.pop("agent_id", uuid.uuid4())
    session_id = overrides.pop("session_id", uuid.uuid4())
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "created_by_user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "agent_run_id": None,
        "launch_message_id": None,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "workflow_version": "1.0.0",
        "goal": "Create an investor presentation",
        "inputs": [{"type": "workspace_file", "path": "workspace/source.pdf"}],
        "spec": {
            "audience": "investors",
            "page_count": 8,
            "language": "en-US",
            "style": "professional",
        },
        "tier": "pro",
        "approval_policy": ["outline", "final"],
        "output_contract": ["pptx", "pdf"],
        "status": "ready",
        "current_stage": "brief_confirmed",
        "version": 1,
        "launched_at": None,
        "completed_at": None,
        "last_error_code": None,
    }
    values.update(overrides)
    return DeliverableRequest(**values)


def test_builtin_workflow_manifests_are_versioned_and_unique() -> None:
    workflows = list_workflow_manifests()

    assert [workflow.work_type for workflow in workflows] == [
        "presentation",
        "poster",
        "video",
    ]
    assert len({workflow.workflow_id for workflow in workflows}) == len(workflows)
    assert all(workflow.workflow_version == "1.0.0" for workflow in workflows)
    assert require_workflow("presentation").launch_policy == "agent_runtime"
    assert require_workflow("poster").launch_policy == "dry_run"


def test_presentation_spec_defaults_and_bounds_are_server_validated() -> None:
    workflow = require_workflow("presentation")
    spec = validate_workflow_spec(
        workflow,
        {"audience": "客户", "language": "zh-CN", "style": "简洁"},
    )

    assert spec["page_count"] == 8
    with pytest.raises(DeliverableWorkflowError, match="page_count"):
        validate_workflow_spec(
            workflow,
            {"audience": "客户", "page_count": 30, "language": "zh-CN", "style": "简洁"},
        )
    with pytest.raises(DeliverableWorkflowError, match="Unsupported spec fields"):
        validate_workflow_spec(
            workflow,
            {
                "audience": "客户",
                "language": "zh-CN",
                "style": "简洁",
                "provider": "forbidden",
            },
        )


def test_workspace_inputs_reject_traversal_and_absolute_paths() -> None:
    assert DeliverableInput(type="workspace_file", path="workspace/source.pdf").path == "workspace/source.pdf"

    for path in ("/tmp/source.pdf", "workspace/../secret", "source.pdf"):
        with pytest.raises(ValidationError):
            DeliverableInput(type="workspace_file", path=path)


def test_request_fingerprint_is_order_stable_and_sensitive_to_contract() -> None:
    first = request_fingerprint({"goal": "A", "spec": {"b": 2, "a": 1}})
    reordered = request_fingerprint({"spec": {"a": 1, "b": 2}, "goal": "A"})
    changed = request_fingerprint({"goal": "B", "spec": {"a": 1, "b": 2}})

    assert first == reordered
    assert first != changed


def test_execution_prompt_contains_contract_but_never_provider_selection() -> None:
    prompt = build_deliverable_prompt(_request())

    assert "builtin.presentation.v1" in prompt
    assert "workspace/deliverables/" in prompt
    assert '"tier": "pro"' in prompt
    assert '"provider"' not in prompt
    assert '"model"' not in prompt


@pytest.mark.asyncio
async def test_prepare_launch_enforces_exact_tenant_user_agent_and_session(monkeypatch) -> None:
    request = _request()
    message_id = uuid.uuid4()
    preflight = AsyncMock(return_value={"launchable": True, "reasons": []})
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        preflight,
    )
    prepared = await prepare_deliverable_launch(
        _Session(request),  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=message_id,
    )

    assert prepared.request is request
    assert request.launch_message_id == message_id
    assert request.status == "running"
    assert request.current_stage == "execution_queued"
    assert request.version == 2

    repeated = await prepare_deliverable_launch(
        _Session(request),  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=message_id,
    )
    assert repeated.request is request
    assert request.version == 2
    assert preflight.await_count == 1

    other = _request()
    with pytest.raises(DeliverableWorkflowError, match="not available in this chat"):
        await prepare_deliverable_launch(
            _Session(other),  # type: ignore[arg-type]
            request_id=other.id,
            tenant_id=other.tenant_id,
            user_id=uuid.uuid4(),
            agent_id=other.agent_id,
            session_id=other.session_id,
            message_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_prepare_launch_rechecks_capability_without_mutating_blocked_request(monkeypatch) -> None:
    request = _request()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.preflight_workflow",
        AsyncMock(
            return_value={
                "launchable": False,
                "reasons": ["presentation_tool_unavailable"],
            }
        ),
    )

    with pytest.raises(DeliverableWorkflowError) as error:
        await prepare_deliverable_launch(
            _Session(request),  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )

    assert error.value.code == "deliverable_preflight_failed"
    assert "presentation_tool_unavailable" in str(error.value)
    assert request.status == "ready"
    assert request.launch_message_id is None
    assert request.version == 1


@pytest.mark.asyncio
async def test_dry_run_workflow_cannot_be_launched() -> None:
    request = _request(
        work_type="poster",
        workflow_id="builtin.poster.v1",
        spec={"channel": "social", "aspect_ratio": "3:4", "style": "commercial"},
        output_contract=["png"],
    )

    with pytest.raises(DeliverableWorkflowError, match="planning only"):
        await prepare_deliverable_launch(
            _Session(request),  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )


def test_attach_run_is_idempotent_only_for_the_same_run() -> None:
    request = _request()
    prepared = SimpleNamespace(request=request)
    run_id = uuid.uuid4()
    launched_at = datetime.now(UTC)

    attach_deliverable_run(prepared, run_id=run_id, launched_at=launched_at)
    attach_deliverable_run(prepared, run_id=run_id, launched_at=launched_at)
    assert request.agent_run_id == run_id

    with pytest.raises(DeliverableWorkflowError, match="another run"):
        attach_deliverable_run(prepared, run_id=uuid.uuid4(), launched_at=launched_at)


@pytest.mark.asyncio
async def test_poster_preflight_is_available_but_not_launchable_in_phase_one(monkeypatch) -> None:
    workflow = require_workflow("poster")
    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_tenant_entitlements",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows.get_agent_media_capabilities",
        AsyncMock(
            return_value=[
                {"modality": "image", "available": True, "reason": None},
            ]
        ),
    )

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec={"channel": "social", "aspect_ratio": "3:4", "style": "commercial"},
    )

    assert result["available"] is True
    assert result["launchable"] is False
    assert result["reasons"] == ["workflow_execution_not_enabled"]
    assert result["credit_estimate"] == {
        "mode": "estimate",
        "minimum": 11,
        "maximum": 11,
        "billing_unit": "3_candidate_images",
    }
    assert result["creates_reservation"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle_status", "expected_status", "expected_error"),
    [
        ("completed", "waiting_approval", None),
        ("failed", "failed", "slide_render_failed"),
        ("cancelled", "cancelled", None),
    ],
)
async def test_runtime_terminal_state_closes_the_linked_deliverable(
    lifecycle_status: str,
    expected_status: str,
    expected_error: str | None,
    monkeypatch,
) -> None:
    request = _request(status="running", current_stage="running", agent_run_id=uuid.uuid4())
    if lifecycle_status == "completed":
        monkeypatch.setattr(
            "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
            AsyncMock(
                return_value=SimpleNamespace(
                    complete=True,
                    missing_types=(),
                    invalid_types=(),
                    unavailable_types=(),
                )
            ),
        )
    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status=lifecycle_status,
        lifecycle={"error": {"code": "slide_render_failed"}},
    )

    assert result is request
    assert request.status == expected_status
    expected_stage = "output_review" if expected_status == "waiting_approval" else expected_status
    assert request.current_stage == expected_stage
    assert request.last_error_code == expected_error
    assert (request.completed_at is None) is (expected_status == "waiting_approval")
    assert request.version == 2

    repeated = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status=lifecycle_status,
        lifecycle={"error": {"code": "slide_render_failed"}},
    )
    assert repeated is request
    assert request.version == 2


@pytest.mark.asyncio
async def test_completed_runtime_fails_deliverable_when_required_artifact_is_missing(monkeypatch) -> None:
    request = _request(status="running", current_stage="running", agent_run_id=uuid.uuid4())
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        AsyncMock(
            return_value=SimpleNamespace(
                complete=False,
                missing_types=("pdf",),
                invalid_types=(),
                unavailable_types=(),
            )
        ),
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "failed"
    assert request.current_stage == "artifact_verification_failed"
    assert request.last_error_code == "deliverable_artifact_missing"
    assert request.completed_at is not None


@pytest.mark.asyncio
async def test_runtime_replay_never_regresses_an_approved_deliverable(monkeypatch) -> None:
    request = _request(
        status="succeeded",
        current_stage="delivered",
        agent_run_id=uuid.uuid4(),
        version=3,
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(
        "app.services.deliverable_workflows.reconcile_runtime_deliverable_artifacts",
        reconcile,
    )

    result = await sync_deliverable_lifecycle(
        _Session(request),  # type: ignore[arg-type]
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        lifecycle_status="completed",
    )

    assert result is request
    assert request.status == "succeeded"
    assert request.current_stage == "delivered"
    assert request.version == 3
    reconcile.assert_not_awaited()
