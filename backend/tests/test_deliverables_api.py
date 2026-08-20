"""Approval-state API tests for verified deliverable outputs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api import deliverables
from app.core.security import BROWSER_SESSION_COOKIE, create_access_token
from app.database import get_db
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.schemas.deliverable import DeliverableActionIn, DeliverableRequestCreate
from app.services.deliverable_artifacts import DeliverableArtifactError


class _Session:
    def __init__(self, execute_value: object | None = None) -> None:
        self.flush_count = 0
        self.refresh_count = 0
        self.execute_value = execute_value

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, _statement):
        return _Result(self.execute_value)

    async def refresh(self, _instance) -> None:
        self.refresh_count += 1


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


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        return False


class _CreateSession(_Session):
    def __init__(self) -> None:
        super().__init__(None)
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()


class _SequencedCreateSession(_CreateSession):
    def __init__(self, *execute_values: object | None) -> None:
        super().__init__()
        self.execute_values = list(execute_values)

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))


class _Storage:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def exists(self, _key: str) -> bool:
        return True

    async def is_file(self, _key: str) -> bool:
        return True

    async def presign_download_url(self, _key: str, *, filename: str, inline: bool):
        del filename, inline
        return None

    async def local_path_for(self, _key: str):
        return None

    async def read_bytes(self, _key: str) -> bytes:
        return self.data


class _HttpSession:
    def __init__(self, *execute_values: object | None) -> None:
        self.execute_values = list(execute_values)
        self.tenant = SimpleNamespace(is_active=True)

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def get(self, _model, _key):
        return self.tenant

    async def commit(self) -> None:
        return None


def _request() -> DeliverableRequest:
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_run_id=uuid.uuid4(),
        launch_message_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="f" * 64,
        work_type="presentation",
        workflow_id="builtin.presentation.v1",
        workflow_version="1.0.0",
        goal="Create a launch deck",
        inputs=[],
        spec={},
        tier="pro",
        approval_policy=["outline", "final"],
        output_contract=["pptx", "pdf"],
        status="waiting_approval",
        current_stage="output_review",
        version=3,
    )


def _artifact(request: DeliverableRequest, artifact_type: str) -> DeliverableArtifactRevision:
    return DeliverableArtifactRevision(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        artifact_key=artifact_type,
        artifact_type=artifact_type,
        workspace_path=f"workspace/deliverables/{request.id}/result.{artifact_type}",
        content_hash="a" * 64,
        revision_number=1,
        status="candidate",
        evaluation={"verified": True},
    )


@pytest.mark.asyncio
async def test_create_brief_persists_provider_free_preflight_without_launch(monkeypatch) -> None:
    """Saving an unavailable brief records readiness but never launches work."""

    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    workflow = SimpleNamespace(
        workflow_id="builtin.video.v1",
        workflow_version="1.0.0",
        approval_policy=["storyboard", "final"],
        output_contract=["mp4"],
    )
    data = DeliverableRequestCreate(
        client_request_id=uuid.uuid4(),
        agent_id=agent_id,
        session_id=uuid.uuid4(),
        work_type="video",
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.workflow_version,
        goal="Prepare a people-led product advertisement",
        spec={"aspect_ratio": "9:16", "duration": 6, "audio_mode": "voiceover"},
        tier="lite",
    )
    db = _CreateSession()

    monkeypatch.setattr(deliverables, "check_agent_access", AsyncMock(return_value=(agent, None)))
    monkeypatch.setattr(deliverables, "_require_direct_session", AsyncMock())
    monkeypatch.setattr(deliverables, "require_workflow", lambda *_args: workflow)
    monkeypatch.setattr(
        deliverables,
        "validate_workflow_spec",
        lambda _workflow, spec: dict(spec),
    )
    shadow = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(deliverables, "add_initial_execution_shadow", lambda *_args: shadow)
    preflight_result = {
        "available": False,
        "launchable": False,
        "reasons": ["pool_unavailable"],
        "capability_status": "unavailable",
        "next_action": "Configure a provider route and recheck.",
        "tier": "lite",
        "normalized_spec": dict(data.spec),
        "credit_estimate": {"mode": "estimate"},
        "creates_reservation": False,
    }
    preflight = AsyncMock(return_value=preflight_result)
    monkeypatch.setattr(deliverables, "preflight_workflow", preflight)
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(side_effect=lambda _db, request: request))

    request = await deliverables.create_deliverable_request(data, user, db)  # type: ignore[arg-type]

    assert request.status == "ready"
    assert request.current_stage == "brief_confirmed"
    assert request.work_type == "video"
    assert request.current_execution_id == shadow.id
    assert request.latest_preflight["launchable"] is False
    assert request.latest_preflight["next_action"] == preflight_result["next_action"]
    assert shadow.status == "blocked"
    assert shadow.blocked_reason == "pool_unavailable"
    assert db.flush_count == 3
    assert db.added == [request]
    preflight.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "inputs", "expected_mismatch"),
    [
        (
            {"aspect_ratio": "3:4", "exact_copy": "量化交易平台\n副标题\n标语\n立即体验"},
            [],
            "aspect_ratio",
        ),
        (
            {
                "aspect_ratio": "9:16",
                "exact_copy": "量化交易平台\n副标题\n标语\n立即体验",
            },
            [{"type": "workspace_file", "path": "workspace/audio/unrelated.mp3"}],
            "inputs",
        ),
    ],
)
async def test_linked_work_task_rejects_drifted_formal_delivery_contract(
    monkeypatch,
    spec,
    inputs,
    expected_mismatch,
) -> None:
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    objective = "\n".join(
        [
            "竖版 9:16 商业宣传海报",
            "主标题：量化交易平台",
            "副标题：副标题",
            "标语：标语",
            "CTA：立即体验",
        ]
    )
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        created_by=user.id,
        agent_id=agent_id,
        status="done",
        work_type="image",
        intent=objective,
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )
    workflow = SimpleNamespace(
        workflow_id="builtin.poster.v1",
        workflow_version="1.0.0",
        approval_policy=["composition", "final"],
        output_contract=["png"],
    )
    data = DeliverableRequestCreate(
        client_request_id=uuid.uuid4(),
        agent_id=agent_id,
        session_id=uuid.uuid4(),
        task_id=task.id,
        work_type="poster",
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.workflow_version,
        goal=objective,
        inputs=inputs,
        spec=spec,
        tier="ultra",
    )

    monkeypatch.setattr(deliverables, "check_agent_access", AsyncMock(return_value=(agent, None)))
    monkeypatch.setattr(deliverables, "_require_direct_session", AsyncMock())
    monkeypatch.setattr(deliverables, "require_workflow", lambda *_args: workflow)
    monkeypatch.setattr(deliverables, "validate_workflow_spec", lambda _workflow, value: dict(value))

    with pytest.raises(HTTPException) as error:
        await deliverables.create_deliverable_request(
            data,
            user,  # type: ignore[arg-type]
            _Session(task),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "task_deliverable_contract_mismatch"
    assert expected_mismatch in error.value.detail["message"]


@pytest.mark.asyncio
async def test_linked_work_task_accepts_the_exact_formal_delivery_contract(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    exact_copy = "量化交易平台\n副标题\n标语\n立即体验"
    objective = "\n".join(
        [
            "竖版 9:16 商业宣传海报",
            "主标题：量化交易平台",
            "副标题：副标题",
            "标语：标语",
            "CTA：立即体验",
        ]
    )
    task = SimpleNamespace(
        id=uuid.uuid4(),
        status="done",
        work_type="image",
        intent=objective,
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )
    workflow = SimpleNamespace(
        workflow_id="builtin.poster.v1",
        workflow_version="1.0.0",
        approval_policy=["composition", "final"],
        output_contract=["png"],
    )
    data = DeliverableRequestCreate(
        client_request_id=uuid.uuid4(),
        agent_id=agent_id,
        session_id=uuid.uuid4(),
        task_id=task.id,
        work_type="poster",
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.workflow_version,
        goal=objective,
        spec={"aspect_ratio": "9:16", "exact_copy": exact_copy},
        tier="ultra",
    )
    db = _SequencedCreateSession(task, None)

    monkeypatch.setattr(deliverables, "check_agent_access", AsyncMock(return_value=(agent, None)))
    monkeypatch.setattr(deliverables, "_require_direct_session", AsyncMock())
    monkeypatch.setattr(deliverables, "require_workflow", lambda *_args: workflow)
    monkeypatch.setattr(deliverables, "validate_workflow_spec", lambda _workflow, value: dict(value))
    shadow = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(deliverables, "add_initial_execution_shadow", lambda *_args: shadow)
    monkeypatch.setattr(
        deliverables,
        "preflight_workflow",
        AsyncMock(
            return_value={
                "available": True,
                "launchable": True,
                "reasons": [],
                "capability_status": "available",
                "next_action": "Ready to launch.",
                "tier": "ultra",
                "normalized_spec": dict(data.spec),
                "credit_estimate": {"mode": "estimate"},
                "creates_reservation": False,
            }
        ),
    )
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(side_effect=lambda _db, request: request))

    request = await deliverables.create_deliverable_request(
        data,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert request.task_id == task.id
    assert request.work_type == "poster"
    assert request.goal == objective
    assert request.inputs == []
    assert request.spec == {"aspect_ratio": "9:16", "exact_copy": exact_copy}
    assert request.current_execution_id == shadow.id
    assert request.latest_preflight["launchable"] is True


@pytest.mark.asyncio
async def test_output_review_approval_marks_verified_artifacts_and_request_delivered(monkeypatch) -> None:
    request = _request()
    artifacts = [_artifact(request, "pptx"), _artifact(request, "pdf")]
    user = SimpleNamespace(id=request.created_by_user_id)
    db = _Session()
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        deliverables,
        "approve_deliverable_artifacts",
        AsyncMock(return_value=tuple(artifacts)),
    )
    monkeypatch.setattr(
        deliverables,
        "_synchronize_and_require_output_execution",
        AsyncMock(return_value=tuple(artifacts)),
    )
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(return_value=request))

    result = await deliverables.apply_deliverable_action(
        request.id,
        DeliverableActionIn(action="approve", expected_version=3),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result is request
    assert request.status == "succeeded"
    assert request.current_stage == "delivered"
    assert request.completed_at is not None
    assert request.version == 4
    assert db.flush_count == 1
    assert all(artifact.status == "approved" for artifact in artifacts)
    assert all(artifact.approved_by_user_id == user.id for artifact in artifacts)


@pytest.mark.asyncio
async def test_request_out_refreshes_expired_server_updated_fields(monkeypatch) -> None:
    request = _request()
    artifact = _artifact(request, "pdf")
    db = _Session(artifact)
    state = SimpleNamespace(expired_attributes={"updated_at"})
    monkeypatch.setattr(deliverables, "sa_inspect", lambda _request: state)

    request.created_at = request.updated_at = datetime.now(UTC)
    artifact.created_at = request.created_at
    result = await deliverables._request_out(db, request)  # type: ignore[arg-type]

    assert result.id == request.id
    assert result.artifacts[0].id == artifact.id
    assert result.approval_readiness.approvable is False
    assert result.approval_readiness.blockers == ["deliverable_artifact_missing"]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_output_review_approval_fails_closed_when_artifact_changed(monkeypatch) -> None:
    request = _request()
    user = SimpleNamespace(id=request.created_by_user_id)
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        deliverables,
        "approve_deliverable_artifacts",
        AsyncMock(
            side_effect=DeliverableArtifactError(
                "deliverable_artifact_changed",
                "Artifact changed before approval",
            )
        ),
    )
    monkeypatch.setattr(
        deliverables,
        "_synchronize_and_require_output_execution",
        AsyncMock(return_value=()),
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.apply_deliverable_action(
            request.id,
            DeliverableActionIn(action="approve", expected_version=3),
            user,  # type: ignore[arg-type]
            _Session(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_artifact_changed"
    assert request.status == "waiting_approval"
    assert request.version == 3


@pytest.mark.asyncio
async def test_output_review_approval_fails_before_artifact_approval_when_execution_incomplete(
    monkeypatch,
) -> None:
    request = _request()
    user = SimpleNamespace(id=request.created_by_user_id)
    artifact_approval = AsyncMock()
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(deliverables, "approve_deliverable_artifacts", artifact_approval)
    monkeypatch.setattr(
        deliverables,
        "_synchronize_and_require_output_execution",
        AsyncMock(
            side_effect=HTTPException(
                status_code=409,
                detail={
                    "code": "deliverable_execution_incomplete",
                    "message": "Every execution unit must succeed",
                },
            )
        ),
    )

    with pytest.raises(HTTPException) as error:
        await deliverables.apply_deliverable_action(
            request.id,
            DeliverableActionIn(action="approve", expected_version=3),
            user,  # type: ignore[arg-type]
            _Session(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deliverable_execution_incomplete"
    artifact_approval.assert_not_awaited()
    assert request.status == "waiting_approval"
    assert request.version == 3


@pytest.mark.asyncio
async def test_artifact_download_serves_private_snapshot_after_owned_request_check(monkeypatch) -> None:
    request = _request()
    artifact = _artifact(request, "pdf")
    artifact.mime_type = "application/pdf"
    user = SimpleNamespace(id=request.created_by_user_id, tenant_id=request.tenant_id)
    db = _Session(artifact)
    owned_request = AsyncMock(return_value=request)
    snapshot = AsyncMock(return_value=b"%PDF-1.7\n%%EOF")
    storage = _Storage(b"unused")
    monkeypatch.setattr(deliverables, "_owned_request", owned_request)
    monkeypatch.setattr(deliverables, "read_deliverable_artifact_snapshot", snapshot)
    monkeypatch.setattr(deliverables, "get_storage_backend", lambda: storage)

    response = await deliverables.download_deliverable_artifact(
        artifact.id,
        True,
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert response.body == b"%PDF-1.7\n%%EOF"
    assert response.media_type == "application/pdf"
    owned_request.assert_awaited_once_with(
        db,
        request_id=request.id,
        user=user,
    )
    snapshot.assert_awaited_once_with(storage, artifact=artifact)


def test_artifact_download_accepts_same_origin_browser_session_cookie(monkeypatch) -> None:
    request = _request()
    artifact = _artifact(request, "pdf")
    artifact.mime_type = "application/pdf"
    identity = SimpleNamespace(is_active=True, auth_version=0)
    user = SimpleNamespace(
        id=request.created_by_user_id,
        tenant_id=request.tenant_id,
        is_active=True,
        identity=identity,
    )
    db = _HttpSession(user, artifact)
    storage = _Storage(b"unused")
    owned_request = AsyncMock(return_value=request)
    snapshot = AsyncMock(return_value=b"%PDF-1.7\n%%EOF")
    monkeypatch.setattr(deliverables, "_owned_request", owned_request)
    monkeypatch.setattr(deliverables, "read_deliverable_artifact_snapshot", snapshot)
    monkeypatch.setattr(deliverables, "get_storage_backend", lambda: storage)

    app = FastAPI()
    app.include_router(deliverables.router)
    app.dependency_overrides[get_db] = lambda: db
    token = create_access_token(str(user.id), "member", auth_version=0)

    with TestClient(app) as client:
        client.cookies.set(BROWSER_SESSION_COOKIE, token)
        response = client.get(f"/api/deliverables/artifacts/{artifact.id}/download?inline=true")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.7\n%%EOF"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    owned_request.assert_awaited_once_with(db, request_id=request.id, user=user)
    snapshot.assert_awaited_once_with(storage, artifact=artifact)


def test_artifact_download_rejects_missing_or_cross_tenant_browser_session(monkeypatch) -> None:
    request = _request()
    identity = SimpleNamespace(is_active=True, auth_version=0)
    user = SimpleNamespace(
        id=request.created_by_user_id,
        tenant_id=request.tenant_id,
        is_active=True,
        identity=identity,
    )
    token = create_access_token(str(user.id), "member", auth_version=0)
    owned_request = AsyncMock(return_value=request)
    monkeypatch.setattr(deliverables, "_owned_request", owned_request)

    unauthenticated_app = FastAPI()
    unauthenticated_app.include_router(deliverables.router)
    unauthenticated_app.dependency_overrides[get_db] = lambda: _HttpSession()
    with TestClient(unauthenticated_app) as client:
        missing = client.get(f"/api/deliverables/artifacts/{uuid.uuid4()}/download")
    assert missing.status_code == 401

    cross_tenant_db = _HttpSession(user, None)
    cross_tenant_app = FastAPI()
    cross_tenant_app.include_router(deliverables.router)
    cross_tenant_app.dependency_overrides[get_db] = lambda: cross_tenant_db
    with TestClient(cross_tenant_app) as client:
        client.cookies.set(BROWSER_SESSION_COOKIE, token)
        hidden = client.get(f"/api/deliverables/artifacts/{uuid.uuid4()}/download")

    assert hidden.status_code == 404
    owned_request.assert_not_awaited()
