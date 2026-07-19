"""Authoritative Runtime artifact reconciliation for deliverable requests."""

from __future__ import annotations

import hashlib
from io import BytesIO
import uuid
import zipfile

import pytest

from app.models.agent_tool_execution import AgentToolExecution
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.services.deliverable_artifacts import (
    DeliverableArtifactError,
    approve_deliverable_artifacts,
    deliverable_artifact_snapshot_key,
    reconcile_runtime_deliverable_artifacts,
)
from app.services.storage import agent_storage_key
from app.services.storage_runtime.local import LocalStorageBackend


class _Result:
    def __init__(self, values: object) -> None:
        self.values = values

    def scalar_one_or_none(self):
        if isinstance(self.values, list):
            return self.values[0] if self.values else None
        return self.values

    def scalars(self):
        return self

    def all(self):
        return self.values if isinstance(self.values, list) else [self.values]


class _Session:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.added: list[DeliverableArtifactRevision] = []

    async def execute(self, _statement):
        return _Result(self.results.pop(0))

    def add(self, artifact: DeliverableArtifactRevision) -> None:
        self.added.append(artifact)


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
        spec={"audience": "customers", "page_count": 8, "language": "en-US", "style": "clean"},
        tier="pro",
        approval_policy=["outline", "final"],
        output_contract=["pptx", "pdf"],
        status="running",
        current_stage="running",
        version=2,
    )


def _execution(
    request: DeliverableRequest,
    *,
    tool_name: str,
    artifact_type: str,
    path: str | None = None,
) -> AgentToolExecution:
    workspace_path = path or f"workspace/deliverables/{request.id}/result.{artifact_type}"
    return AgentToolExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        run_id=request.agent_run_id,
        tool_call_id=f"call-{artifact_type}",
        tool_name=tool_name,
        assistant_message_id=f"assistant-{artifact_type}",
        arguments_hash="a" * 64,
        sanitized_arguments={},
        effect="write",
        retry_policy="conditional",
        status="succeeded",
        result_metadata={
            "artifact_refs": [f"workspace://{request.agent_id}/{workspace_path}"],
        },
    )


def _pptx_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")
    return output.getvalue()


@pytest.mark.asyncio
async def test_reconcile_persists_only_exact_request_scoped_structural_outputs(tmp_path) -> None:
    request = _request()
    storage = LocalStorageBackend(str(tmp_path))
    pptx_path = f"workspace/deliverables/{request.id}/result.pptx"
    pdf_path = f"workspace/deliverables/{request.id}/result.pdf"
    await storage.write_bytes(agent_storage_key(request.agent_id, pptx_path), _pptx_bytes())
    await storage.write_bytes(
        agent_storage_key(request.agent_id, pdf_path),
        b"%PDF-1.7\n1 0 obj\n%%EOF",
    )
    executions = [
        _execution(request, tool_name="convert_html_to_pptx", artifact_type="pptx"),
        _execution(request, tool_name="convert_html_to_pdf", artifact_type="pdf"),
        _execution(
            request,
            tool_name="convert_html_to_pdf",
            artifact_type="pdf",
            path="workspace/deliverables/another-request/foreign.pdf",
        ),
    ]
    db = _Session(executions, [])

    result = await reconcile_runtime_deliverable_artifacts(
        db,  # type: ignore[arg-type]
        request=request,
        run_id=request.agent_run_id,
        storage=storage,
    )

    assert result.complete is True
    assert {artifact.artifact_key for artifact in result.artifacts} == {"pptx", "pdf"}
    assert {artifact.workspace_path for artifact in result.artifacts} == {pptx_path, pdf_path}
    assert len(db.added) == 2
    assert all(artifact.evaluation["verified"] is True for artifact in db.added)
    assert all(artifact.evaluation["verification_level"] == "structural" for artifact in db.added)
    for artifact in db.added:
        snapshot = await storage.read_bytes(deliverable_artifact_snapshot_key(artifact))
        assert artifact.content_hash == hashlib.sha256(snapshot).hexdigest()


@pytest.mark.asyncio
async def test_reconcile_fails_closed_when_required_pdf_is_invalid(tmp_path) -> None:
    request = _request()
    storage = LocalStorageBackend(str(tmp_path))
    pptx_path = f"workspace/deliverables/{request.id}/result.pptx"
    pdf_path = f"workspace/deliverables/{request.id}/result.pdf"
    await storage.write_bytes(agent_storage_key(request.agent_id, pptx_path), _pptx_bytes())
    await storage.write_bytes(agent_storage_key(request.agent_id, pdf_path), b"not a pdf")
    db = _Session(
        [
            _execution(request, tool_name="convert_html_to_pptx", artifact_type="pptx"),
            _execution(request, tool_name="convert_html_to_pdf", artifact_type="pdf"),
        ],
        [],
    )

    result = await reconcile_runtime_deliverable_artifacts(
        db,  # type: ignore[arg-type]
        request=request,
        run_id=request.agent_run_id,
        storage=storage,
    )

    assert result.complete is False
    assert result.invalid_types == ("pdf",)
    assert [artifact.artifact_key for artifact in db.added] == ["pptx"]


@pytest.mark.asyncio
async def test_approval_rechecks_content_hash_before_accepting_mutable_workspace_path(tmp_path) -> None:
    request = _request()
    storage = LocalStorageBackend(str(tmp_path))
    pptx_path = f"workspace/deliverables/{request.id}/result.pptx"
    pdf_path = f"workspace/deliverables/{request.id}/result.pdf"
    await storage.write_bytes(agent_storage_key(request.agent_id, pptx_path), _pptx_bytes())
    await storage.write_bytes(agent_storage_key(request.agent_id, pdf_path), b"%PDF-1.7\n%%EOF")
    reconcile_db = _Session(
        [
            _execution(request, tool_name="convert_html_to_pptx", artifact_type="pptx"),
            _execution(request, tool_name="convert_html_to_pdf", artifact_type="pdf"),
        ],
        [],
    )
    reconciled = await reconcile_runtime_deliverable_artifacts(
        reconcile_db,  # type: ignore[arg-type]
        request=request,
        run_id=request.agent_run_id,
        storage=storage,
    )

    approved = await approve_deliverable_artifacts(
        _Session(list(reconciled.artifacts)),  # type: ignore[arg-type]
        request=request,
        storage=storage,
    )
    assert {artifact.artifact_key for artifact in approved} == {"pptx", "pdf"}

    for artifact in reconciled.artifacts:
        assert await storage.exists(deliverable_artifact_snapshot_key(artifact))

    await storage.write_bytes(agent_storage_key(request.agent_id, pdf_path), b"%PDF-1.7\nchanged\n%%EOF")
    with pytest.raises(DeliverableArtifactError) as error:
        await approve_deliverable_artifacts(
            _Session(list(reconciled.artifacts)),  # type: ignore[arg-type]
            request=request,
            storage=storage,
        )
    assert error.value.code == "deliverable_artifact_changed"

    await storage.write_bytes(agent_storage_key(request.agent_id, pdf_path), b"%PDF-1.7\n%%EOF")
    pdf_artifact = next(item for item in reconciled.artifacts if item.artifact_type == "pdf")
    await storage.write_bytes(deliverable_artifact_snapshot_key(pdf_artifact), b"tampered snapshot")
    with pytest.raises(DeliverableArtifactError) as snapshot_error:
        await approve_deliverable_artifacts(
            _Session(list(reconciled.artifacts)),  # type: ignore[arg-type]
            request=request,
            storage=storage,
        )
    assert snapshot_error.value.code == "deliverable_artifact_snapshot_changed"
