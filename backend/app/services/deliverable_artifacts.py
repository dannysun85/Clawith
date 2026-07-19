"""Authoritative artifact reconciliation for durable deliverable requests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_tool_execution import AgentToolExecution
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.services.storage import agent_storage_key, get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend, WriteCondition


MAX_DELIVERABLE_ARTIFACT_BYTES = 200 * 1024 * 1024
PRESENTATION_TOOL_BY_TYPE = {
    "pptx": "convert_html_to_pptx",
    "pdf": "convert_html_to_pdf",
}
MIME_BY_TYPE = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


class DeliverableArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeliverableArtifactReconciliation:
    artifacts: tuple[DeliverableArtifactRevision, ...]
    missing_types: tuple[str, ...] = ()
    invalid_types: tuple[str, ...] = ()
    unavailable_types: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not (self.missing_types or self.invalid_types or self.unavailable_types)


@dataclass(frozen=True, slots=True)
class _VerifiedArtifact:
    artifact_type: str
    workspace_path: str
    content_hash: str
    size_bytes: int
    tool_call_id: str
    data: bytes


def deliverable_artifact_snapshot_key(artifact: DeliverableArtifactRevision) -> str:
    """Return the private content-addressed storage key for an artifact revision."""

    content_hash = str(artifact.content_hash or "").lower()
    artifact_type = str(artifact.artifact_type or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValueError("Deliverable artifact has an invalid content hash")
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", artifact_type):
        raise ValueError("Deliverable artifact has an invalid type")
    return normalize_storage_key(
        "deliverable_artifacts/"
        f"{artifact.tenant_id}/{artifact.request_id}/{artifact_type}/{content_hash}.{artifact_type}"
    )


def _workspace_artifact_path(
    reference: str,
    *,
    agent_id: uuid.UUID,
    request_id: uuid.UUID,
    artifact_type: str,
) -> str | None:
    try:
        parsed = urlsplit(reference)
        raw_path = unquote(parsed.path).replace("\\", "/").lstrip("/")
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "workspace"
        or parsed.netloc != str(agent_id)
        or parsed.query
        or parsed.fragment
        or not raw_path
        or "\x00" in raw_path
        or any(part == ".." for part in raw_path.split("/"))
    ):
        return None
    normalized = normalize_storage_key(raw_path)
    expected_prefix = f"workspace/deliverables/{request_id}/"
    if not normalized.startswith(expected_prefix):
        return None
    if Path(normalized).suffix.lower() != f".{artifact_type}":
        return None
    return normalized


def _valid_artifact_bytes(artifact_type: str, data: bytes) -> bool:
    if not data:
        return False
    if artifact_type == "pdf":
        return data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:]
    if artifact_type != "pptx":
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if archive.testzip() is not None:
                return False
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return {"[Content_Types].xml", "ppt/presentation.xml"} <= names


async def _verify_storage_artifact(
    storage: StorageBackend,
    *,
    agent_id: uuid.UUID,
    artifact_type: str,
    workspace_path: str,
    tool_call_id: str,
) -> tuple[_VerifiedArtifact | None, str | None]:
    try:
        key = agent_storage_key(agent_id, workspace_path)
        version = await storage.get_version(key)
        if not version.exists or version.is_dir:
            return None, "missing"
        if version.size <= 0 or version.size > MAX_DELIVERABLE_ARTIFACT_BYTES:
            return None, "invalid"
        data = await storage.read_bytes(key)
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "unavailable"
    if len(data) > MAX_DELIVERABLE_ARTIFACT_BYTES or not _valid_artifact_bytes(artifact_type, data):
        return None, "invalid"
    return (
        _VerifiedArtifact(
            artifact_type=artifact_type,
            workspace_path=workspace_path,
            content_hash=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            tool_call_id=tool_call_id,
            data=data,
        ),
        None,
    )


async def _ensure_immutable_snapshot(
    storage: StorageBackend,
    *,
    artifact: DeliverableArtifactRevision,
    data: bytes,
) -> None:
    key = deliverable_artifact_snapshot_key(artifact)
    try:
        result = await storage.write_bytes_if_match(
            key,
            data,
            condition=WriteCondition(require_absent=True),
            content_type=artifact.mime_type,
        )
    except Exception as exc:
        raise DeliverableArtifactError(
            "deliverable_artifact_snapshot_unavailable",
            f"Artifact {artifact.artifact_key} snapshot could not be stored",
        ) from exc
    if result.ok:
        return
    try:
        existing = await storage.read_bytes(key)
    except Exception as exc:
        raise DeliverableArtifactError(
            "deliverable_artifact_snapshot_unavailable",
            f"Artifact {artifact.artifact_key} snapshot could not be verified",
        ) from exc
    if hashlib.sha256(existing).hexdigest() != artifact.content_hash or existing != data:
        raise DeliverableArtifactError(
            "deliverable_artifact_snapshot_conflict",
            f"Artifact {artifact.artifact_key} snapshot conflicts with verified content",
        )


async def _verify_immutable_snapshot(
    storage: StorageBackend,
    *,
    artifact: DeliverableArtifactRevision,
) -> bool:
    try:
        await read_deliverable_artifact_snapshot(storage, artifact=artifact)
    except DeliverableArtifactError:
        return False
    return True


async def read_deliverable_artifact_snapshot(
    storage: StorageBackend,
    *,
    artifact: DeliverableArtifactRevision,
) -> bytes:
    """Read and hash-check the private immutable bytes before serving them."""

    try:
        key = deliverable_artifact_snapshot_key(artifact)
        version = await storage.get_version(key)
        if (
            not version.exists
            or version.is_dir
            or version.size <= 0
            or version.size > MAX_DELIVERABLE_ARTIFACT_BYTES
        ):
            raise FileNotFoundError(key)
        data = await storage.read_bytes(key)
    except Exception as exc:
        raise DeliverableArtifactError(
            "deliverable_artifact_snapshot_unavailable",
            f"Artifact {artifact.artifact_key} immutable snapshot is unavailable",
        ) from exc
    if (
        len(data) != artifact.size_bytes
        or hashlib.sha256(data).hexdigest() != artifact.content_hash
        or not _valid_artifact_bytes(artifact.artifact_type, data)
    ):
        raise DeliverableArtifactError(
            "deliverable_artifact_snapshot_changed",
            f"Artifact {artifact.artifact_key} immutable snapshot is invalid",
        )
    return data


def _artifact_refs(execution: AgentToolExecution) -> tuple[str, ...]:
    metadata = execution.result_metadata
    refs = metadata.get("artifact_refs") if isinstance(metadata, Mapping) else None
    if not isinstance(refs, list):
        return ()
    return tuple(ref for ref in refs if isinstance(ref, str) and ref)


async def reconcile_runtime_deliverable_artifacts(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    run_id: uuid.UUID,
    storage: StorageBackend | None = None,
) -> DeliverableArtifactReconciliation:
    """Persist structurally verified output revisions from this request's Runtime ledger."""

    required_types = tuple(dict.fromkeys(str(item).strip().lower() for item in request.output_contract))
    execution_result = await db.execute(
        select(AgentToolExecution)
        .where(
            AgentToolExecution.tenant_id == request.tenant_id,
            AgentToolExecution.run_id == run_id,
            AgentToolExecution.status == "succeeded",
            AgentToolExecution.tool_name.in_(tuple(PRESENTATION_TOOL_BY_TYPE.values())),
        )
        .order_by(
            AgentToolExecution.completed_at.desc().nullslast(),
            AgentToolExecution.id.desc(),
        )
    )
    executions = tuple(execution_result.scalars().all())
    existing_result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
            DeliverableArtifactRevision.request_id == request.id,
        )
        .order_by(
            DeliverableArtifactRevision.artifact_key,
            DeliverableArtifactRevision.revision_number.desc(),
        )
    )
    existing = tuple(existing_result.scalars().all())
    storage_backend = storage or get_storage_backend()

    verified_by_type: dict[str, _VerifiedArtifact] = {}
    observed_errors: dict[str, set[str]] = {artifact_type: set() for artifact_type in required_types}
    for artifact_type in required_types:
        expected_tool = PRESENTATION_TOOL_BY_TYPE.get(artifact_type)
        if expected_tool is None:
            continue
        for execution in executions:
            if execution.tool_name != expected_tool:
                continue
            for reference in _artifact_refs(execution):
                workspace_path = _workspace_artifact_path(
                    reference,
                    agent_id=request.agent_id,
                    request_id=request.id,
                    artifact_type=artifact_type,
                )
                if workspace_path is None:
                    continue
                verified, error = await _verify_storage_artifact(
                    storage_backend,
                    agent_id=request.agent_id,
                    artifact_type=artifact_type,
                    workspace_path=workspace_path,
                    tool_call_id=execution.tool_call_id,
                )
                if verified is not None:
                    verified_by_type[artifact_type] = verified
                    break
                if error is not None:
                    observed_errors[artifact_type].add(error)
            if artifact_type in verified_by_type:
                break

    latest_by_key: dict[str, DeliverableArtifactRevision] = {}
    for artifact in existing:
        latest_by_key.setdefault(artifact.artifact_key, artifact)

    persisted: list[DeliverableArtifactRevision] = []
    snapshotted_types: set[str] = set()
    for artifact_type, verified in verified_by_type.items():
        latest = latest_by_key.get(artifact_type)
        if (
            latest is not None
            and latest.workspace_path == verified.workspace_path
            and latest.content_hash == verified.content_hash
        ):
            artifact = latest
        else:
            artifact = DeliverableArtifactRevision(
                id=uuid.uuid4(),
                tenant_id=request.tenant_id,
                request_id=request.id,
                parent_revision_id=latest.id if latest is not None else None,
                artifact_key=artifact_type,
                artifact_type=artifact_type,
                workspace_path=verified.workspace_path,
                mime_type=MIME_BY_TYPE.get(artifact_type),
                content_hash=verified.content_hash,
                size_bytes=verified.size_bytes,
                revision_number=(latest.revision_number + 1) if latest is not None else 1,
                status="candidate",
                evaluation={
                    "version": 1,
                    "verified": True,
                    "verification_level": "structural",
                    "source": "runtime_tool_execution",
                    "run_id": str(run_id),
                    "tool_call_id": verified.tool_call_id,
                    "checks": [
                        "tenant_scope",
                        "agent_scope",
                        "request_path",
                        "storage_file",
                        "file_signature",
                        "immutable_snapshot",
                    ],
                },
            )
        try:
            await _ensure_immutable_snapshot(
                storage_backend,
                artifact=artifact,
                data=verified.data,
            )
        except DeliverableArtifactError:
            observed_errors[artifact_type].add("unavailable")
            continue
        if artifact is not latest:
            for prior in existing:
                if prior.artifact_key == artifact_type and prior.status == "candidate":
                    prior.status = "superseded"
            db.add(artifact)
        persisted.append(artifact)
        snapshotted_types.add(artifact_type)

    missing: list[str] = []
    invalid: list[str] = []
    unavailable: list[str] = []
    for artifact_type in required_types:
        if artifact_type in snapshotted_types:
            continue
        errors = observed_errors.get(artifact_type, set())
        if "unavailable" in errors:
            unavailable.append(artifact_type)
        elif "invalid" in errors:
            invalid.append(artifact_type)
        else:
            missing.append(artifact_type)
    return DeliverableArtifactReconciliation(
        artifacts=tuple(persisted),
        missing_types=tuple(missing),
        invalid_types=tuple(invalid),
        unavailable_types=tuple(unavailable),
    )


async def approve_deliverable_artifacts(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    storage: StorageBackend | None = None,
) -> tuple[DeliverableArtifactRevision, ...]:
    """Revalidate and approve the latest complete artifact set without trusting mutable paths."""

    result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
            DeliverableArtifactRevision.request_id == request.id,
        )
        .order_by(
            DeliverableArtifactRevision.artifact_key,
            DeliverableArtifactRevision.revision_number.desc(),
        )
        .with_for_update()
    )
    artifacts = tuple(result.scalars().all())
    latest_candidates: dict[str, DeliverableArtifactRevision] = {}
    for artifact in artifacts:
        if artifact.status == "candidate":
            latest_candidates.setdefault(artifact.artifact_key, artifact)
    required_types = tuple(dict.fromkeys(str(item).strip().lower() for item in request.output_contract))
    missing = [item for item in required_types if item not in latest_candidates]
    if missing:
        raise DeliverableArtifactError(
            "deliverable_artifact_missing",
            "Required artifacts are missing: " + ", ".join(missing),
        )

    storage_backend = storage or get_storage_backend()
    selected = tuple(latest_candidates[item] for item in required_types)
    for artifact in selected:
        if not isinstance(artifact.evaluation, Mapping) or artifact.evaluation.get("verified") is not True:
            raise DeliverableArtifactError(
                "deliverable_artifact_unverified",
                f"Artifact {artifact.artifact_key} has no verification evidence",
            )
        verified, error = await _verify_storage_artifact(
            storage_backend,
            agent_id=request.agent_id,
            artifact_type=artifact.artifact_type,
            workspace_path=artifact.workspace_path,
            tool_call_id=str(artifact.evaluation.get("tool_call_id") or "approval_recheck"),
        )
        if error is not None or verified is None or verified.content_hash != artifact.content_hash:
            raise DeliverableArtifactError(
                "deliverable_artifact_changed",
                f"Artifact {artifact.artifact_key} changed or became unavailable before approval",
            )
        if not await _verify_immutable_snapshot(storage_backend, artifact=artifact):
            raise DeliverableArtifactError(
                "deliverable_artifact_snapshot_changed",
                f"Artifact {artifact.artifact_key} immutable snapshot is unavailable or invalid",
            )
    return selected


__all__ = [
    "DeliverableArtifactError",
    "DeliverableArtifactReconciliation",
    "approve_deliverable_artifacts",
    "deliverable_artifact_snapshot_key",
    "read_deliverable_artifact_snapshot",
    "reconcile_runtime_deliverable_artifacts",
]
