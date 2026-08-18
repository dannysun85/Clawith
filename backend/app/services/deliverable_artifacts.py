"""Authoritative artifact reconciliation for durable deliverable requests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit
import uuid
import zipfile
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_tool_execution import AgentToolExecution
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.config import get_settings
from app.services.deliverable_quality_gate import (
    DeliverableQualityGateError,
    creative_quality_gate_required_for_request,
    enforce_deliverable_quality_gate,
)
from app.services.document_conversion.presentation_contract import (
    validate_presentation_visible_text,
)
from app.services.presentation_visual_policy import (
    MINIMUM_PICTURE_COVERAGE_RATIO,
    presentation_brief_is_image_led,
)
from app.services.poster_contract import poster_exact_copy_contract
from app.services.storage import agent_storage_key, get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend, WriteCondition


MAX_DELIVERABLE_ARTIFACT_BYTES = 200 * 1024 * 1024
ARTIFACT_TOOLS_BY_TYPE = {
    "pptx": ("convert_html_to_pptx",),
    "pdf": ("convert_html_to_pdf",),
    "png": ("generate_image_minimax",),
    # Prefer the deterministic post-production result when it exists. Silent
    # contracts may legitimately finish at the provider-neutral generation
    # tool without an audio composition step.
    "mp4": ("compose_video_audio", "generate_video_minimax"),
}
MIME_BY_TYPE = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "png": "image/png",
}
PRESENTATION_ASPECT_RATIO = 16 / 9
PRESENTATION_ASPECT_RATIO_TOLERANCE = 0.015


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
    attempted_types: tuple[str, ...] = ()
    failed_types: tuple[str, ...] = ()
    created_types: tuple[str, ...] = ()
    failure_codes: tuple[tuple[str, str], ...] = ()

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


def _pptx_facts(data: bytes) -> dict[str, int | float]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
        slide_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=lambda name: int(re.search(r"(\d+)", name).group(1)),
        )
        slide_roots = [
            ElementTree.fromstring(archive.read(name)) for name in slide_names
        ]
        slide_text = " ".join(
            node.text or ""
            for slide_root in slide_roots
            for node in slide_root.iter()
            if node.tag.endswith("}t")
        )
    validate_presentation_visible_text(slide_text)
    namespace = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    slide_ids = root.findall("./p:sldIdLst/p:sldId", namespace)
    slide_size = root.find("./p:sldSz", namespace)
    if not slide_ids or slide_size is None:
        raise ValueError("PPTX has no slides or slide size")
    width = int(slide_size.attrib["cx"])
    height = int(slide_size.attrib["cy"])
    if width <= 0 or height <= 0:
        raise ValueError("PPTX slide size is invalid")
    picture_count_by_slide: list[int] = []
    picture_coverage_ratio_by_slide: list[float] = []
    picture_namespace = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    }
    slide_area = width * height
    for slide_root in slide_roots:
        picture_count = 0
        picture_area = 0
        for picture in slide_root.findall(".//p:pic", picture_namespace):
            transform = picture.find("./p:spPr/a:xfrm", picture_namespace)
            if transform is None:
                continue
            offset = transform.find("./a:off", picture_namespace)
            extent = transform.find("./a:ext", picture_namespace)
            if offset is None or extent is None:
                continue
            try:
                left = int(offset.attrib["x"])
                top = int(offset.attrib["y"])
                picture_width = int(extent.attrib["cx"])
                picture_height = int(extent.attrib["cy"])
            except (KeyError, TypeError, ValueError):
                continue
            right = left + picture_width
            bottom = top + picture_height
            visible_left = max(0, min(width, min(left, right)))
            visible_top = max(0, min(height, min(top, bottom)))
            visible_right = max(0, min(width, max(left, right)))
            visible_bottom = max(0, min(height, max(top, bottom)))
            if visible_right <= visible_left or visible_bottom <= visible_top:
                continue
            picture_count += 1
            picture_area += (visible_right - visible_left) * (
                visible_bottom - visible_top
            )
        picture_count_by_slide.append(picture_count)
        picture_coverage_ratio_by_slide.append(
            round(min(picture_area / slide_area, 1.0), 6)
        )
    while len(picture_count_by_slide) < len(slide_ids):
        # A structurally minimal fixture may declare slide ids without
        # shipping slide XML.  Preserve the existing page-count facts while
        # treating its unobservable picture area as zero.
        picture_count_by_slide.append(0)
        picture_coverage_ratio_by_slide.append(0.0)
    slide_count = len(slide_ids)
    return {
        "page_count": len(slide_ids),
        "width": width,
        "height": height,
        "aspect_ratio": width / height,
        "picture_count": sum(picture_count_by_slide),
        "picture_count_by_slide": picture_count_by_slide,
        "slides_with_pictures": sum(
            count > 0 for count in picture_count_by_slide
        ),
        "slides_with_pictures_ratio": round(
            sum(count > 0 for count in picture_count_by_slide)
            / slide_count,
            6,
        ),
        "picture_coverage_ratio_by_slide": picture_coverage_ratio_by_slide,
        "picture_coverage_ratio_mean": round(
            sum(picture_coverage_ratio_by_slide) / slide_count,
            6,
        ),
    }


def _pdf_facts(data: bytes) -> dict[str, int | float]:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as document:
        if document.page_count <= 0:
            raise ValueError("PDF has no pages")
        page_sizes = [(float(page.rect.width), float(page.rect.height)) for page in document]
        page_text = " ".join(page.get_text("text") for page in document)
    validate_presentation_visible_text(page_text)
    width, height = page_sizes[0]
    if width <= 0 or height <= 0:
        raise ValueError("PDF page size is invalid")
    if any(abs(other_width - width) > 0.5 or abs(other_height - height) > 0.5 for other_width, other_height in page_sizes):
        raise ValueError("PDF page sizes are inconsistent")
    return {
        "page_count": len(page_sizes),
        "width": width,
        "height": height,
        "aspect_ratio": width / height,
    }


def _presentation_contract_facts(
    request: DeliverableRequest,
    verified_by_type: Mapping[str, _VerifiedArtifact],
) -> tuple[dict[str, dict[str, int | float]], set[str]]:
    facts: dict[str, dict[str, int | float]] = {}
    invalid_types: set[str] = set()
    inspectors = {"pptx": _pptx_facts, "pdf": _pdf_facts}
    for artifact_type, inspector in inspectors.items():
        verified = verified_by_type.get(artifact_type)
        if verified is None:
            continue
        try:
            facts[artifact_type] = inspector(verified.data)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile):
            invalid_types.add(artifact_type)

    expected_page_count = request.spec.get("page_count") if isinstance(request.spec, Mapping) else None
    if not isinstance(expected_page_count, int) or isinstance(expected_page_count, bool):
        invalid_types.update(facts)
        return facts, invalid_types
    for artifact_type, artifact_facts in facts.items():
        if artifact_facts["page_count"] != expected_page_count:
            invalid_types.add(artifact_type)
        if abs(float(artifact_facts["aspect_ratio"]) - PRESENTATION_ASPECT_RATIO) > PRESENTATION_ASPECT_RATIO_TOLERANCE:
            invalid_types.add(artifact_type)
    if {"pptx", "pdf"} <= facts.keys() and facts["pptx"]["page_count"] != facts["pdf"]["page_count"]:
        invalid_types.update(("pptx", "pdf"))
    if "pptx" in facts and presentation_brief_is_image_led(
        request.goal,
        request.spec if isinstance(request.spec, Mapping) else {},
    ):
        pptx_facts = facts["pptx"]
        observed_coverage = float(
            pptx_facts.get("picture_coverage_ratio_mean") or 0.0
        )
        pptx_facts["minimum_picture_coverage_ratio"] = (
            MINIMUM_PICTURE_COVERAGE_RATIO
        )
        pptx_facts["picture_coverage_gate"] = int(
            observed_coverage >= MINIMUM_PICTURE_COVERAGE_RATIO
        )
        if observed_coverage < MINIMUM_PICTURE_COVERAGE_RATIO:
            invalid_types.add("pptx")
            if "pdf" in facts:
                invalid_types.add("pdf")
    return facts, invalid_types


async def _video_contract_facts(
    request: DeliverableRequest,
    verified_by_type: Mapping[str, _VerifiedArtifact],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    verified = verified_by_type.get("mp4")
    if verified is None:
        return {}, set()
    try:
        from app.services.media_assets import (
            validate_generated_video,
            validate_video_delivery_contract,
        )

        info = await validate_generated_video(
            verified.data,
            label="Deliverable video",
            require_browser_safe=True,
        )
        spec = request.spec if isinstance(request.spec, Mapping) else {}
        audio_mode = str(spec.get("audio_mode") or "voiceover").strip().lower()
        validate_video_delivery_contract(
            info,
            expected_duration_seconds=spec.get("duration"),
            expected_aspect_ratio=str(spec.get("aspect_ratio") or ""),
            require_audio=audio_mode == "voiceover",
        )
    except (OSError, TypeError, ValueError):
        return {}, {"mp4"}
    return (
        {
            "mp4": {
                "width": info.width,
                "height": info.height,
                "duration_seconds": info.duration_seconds,
                "video_codec": info.codec_name,
                "pixel_format": info.pixel_format,
                "audio_codec": info.audio_codec_name,
                "fast_start": info.fast_start,
                "audio_mode": audio_mode,
            }
        },
        set(),
    )


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
    if artifact_type == "mp4":
        from app.services.media_assets import valid_mp4

        return valid_mp4(data)
    if artifact_type == "png":
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                image.verify()
                return image.format == "PNG"
        except (OSError, ValueError):
            return False
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


def _poster_copy_receipt_facts(
    request: DeliverableRequest,
    execution: AgentToolExecution,
    *,
    expected_digest: str | None,
) -> dict[str, Any] | None:
    """Verify the server-issued formal-poster receipt carried by Runtime."""

    if not expected_digest:
        return {}
    metadata = (
        execution.result_metadata
        if isinstance(execution.result_metadata, Mapping)
        else {}
    )
    request_id = str(metadata.get("deliverable_request_id") or "")
    receipt_digest = str(metadata.get("expected_overlay_blocks_sha256") or "")
    if request_id != str(request.id) or not hmac.compare_digest(
        receipt_digest,
        expected_digest,
    ):
        return None
    from app.services.poster_contract import poster_execution_policy

    expected_policy = poster_execution_policy(request.spec)
    if (
        str(metadata.get("execution_strategy") or "")
        != expected_policy.execution_strategy
        or metadata.get("allow_degraded_fallback")
        is not expected_policy.allow_degraded_fallback
        or str(metadata.get("layout_version") or "") != "poster-v3"
        or metadata.get("layout_bounds_verified") is not True
    ):
        return None
    integer_fields = (
        "content_left",
        "content_top",
        "content_right",
        "content_bottom",
        "safe_margin_x",
        "safe_margin_y",
        "source_width",
        "source_height",
    )
    if any(
        not isinstance(metadata.get(field), int)
        or isinstance(metadata.get(field), bool)
        for field in integer_fields
    ):
        return None
    left = int(metadata["content_left"])
    top = int(metadata["content_top"])
    right = int(metadata["content_right"])
    bottom = int(metadata["content_bottom"])
    margin_x = int(metadata["safe_margin_x"])
    margin_y = int(metadata["safe_margin_y"])
    source_width = int(metadata["source_width"])
    source_height = int(metadata["source_height"])
    if (
        margin_x < 0
        or margin_y < 0
        or source_width <= 0
        or source_height <= 0
        or left < margin_x
        or top < margin_y
        or right > source_width - margin_x
        or bottom > source_height - margin_y
        or left >= right
        or top >= bottom
    ):
        return None
    return {
        "deliverable_request_id": request_id,
        "expected_overlay_blocks_sha256": expected_digest,
        "execution_strategy": expected_policy.execution_strategy,
        "allow_degraded_fallback": expected_policy.allow_degraded_fallback,
        "layout_version": "poster-v3",
        "layout_bounds_verified": True,
        **{field: metadata[field] for field in integer_fields},
    }


def _poster_artifact_facts_match(
    request: DeliverableRequest,
    facts: object,
    *,
    expected_digest: str,
) -> bool:
    """Revalidate persisted candidate facts without trusting old gate output."""

    if not isinstance(facts, Mapping):
        return False
    synthetic_execution = type(
        "PosterReceiptExecution",
        (),
        {"result_metadata": facts},
    )()
    return _poster_copy_receipt_facts(
        request,
        synthetic_execution,
        expected_digest=expected_digest,
    ) is not None


def _execution_targets_request_artifact(
    execution: AgentToolExecution,
    *,
    request_id: uuid.UUID,
    artifact_type: str,
) -> bool:
    """Return whether the execution explicitly targeted this deliverable output."""

    arguments = execution.sanitized_arguments
    if not isinstance(arguments, Mapping):
        return False
    expected_prefix = f"workspace/deliverables/{request_id}/"
    for field in ("target_path", "output_path"):
        raw_value = arguments.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        raw_path = raw_value.strip().replace("\\", "/")
        if raw_path.startswith("workspace://"):
            try:
                parsed = urlsplit(raw_path)
                raw_path = unquote(parsed.path).lstrip("/")
            except (TypeError, ValueError):
                continue
        try:
            normalized = normalize_storage_key(raw_path.lstrip("/"))
        except ValueError:
            continue
        if (
            normalized.startswith(expected_prefix)
            and Path(normalized).suffix.lower() == f".{artifact_type}"
        ):
            return True
    return False


async def reconcile_runtime_deliverable_artifacts(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    run_id: uuid.UUID,
    storage: StorageBackend | None = None,
) -> DeliverableArtifactReconciliation:
    """Persist structurally verified output revisions from this request's Runtime ledger."""

    required_types = tuple(dict.fromkeys(str(item).strip().lower() for item in request.output_contract))
    execution_query = select(AgentToolExecution).where(
        AgentToolExecution.tenant_id == request.tenant_id,
        AgentToolExecution.run_id == run_id,
        AgentToolExecution.tool_name.in_(
            tuple(
                dict.fromkeys(
                    tool_name
                    for tool_names in ARTIFACT_TOOLS_BY_TYPE.values()
                    for tool_name in tool_names
                )
            )
        ),
    )
    execution_result = await db.execute(
        execution_query
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

    _poster_blocks, poster_expected_digest = (
        poster_exact_copy_contract(request.spec)
        if request.work_type == "poster"
        else ((), None)
    )
    # FR-I5 (enforcing only): candidates whose automated QA failed are never
    # registered as the final artifact.  Shadow mode records reports without
    # changing reconciliation; v1 requests never take this branch.
    poster_v2_failed_candidates: set[str] = set()
    if (
        request.work_type == "poster"
        and request.workflow_id == "builtin.poster.v2"
        and request.current_execution_id is not None
    ):
        failed_qa_result = await db.execute(
            select(DeliverableExecutionUnit).where(
                DeliverableExecutionUnit.tenant_id == request.tenant_id,
                DeliverableExecutionUnit.execution_id == request.current_execution_id,
                DeliverableExecutionUnit.stage_key == "candidate_qa",
                DeliverableExecutionUnit.status == "failed",
            )
        )
        poster_v2_failed_candidates = {
            unit.unit_key for unit in failed_qa_result.scalars().all()
        }
    verified_by_type: dict[str, _VerifiedArtifact] = {}
    poster_receipt_facts: dict[str, dict[str, str]] = {}
    observed_errors: dict[str, set[str]] = {artifact_type: set() for artifact_type in required_types}
    attempted_types: set[str] = set()
    failed_types: set[str] = set()
    failure_codes: dict[str, str] = {}
    for artifact_type in required_types:
        expected_tools = ARTIFACT_TOOLS_BY_TYPE.get(artifact_type) or ()
        for execution in executions:
            if execution.tool_name not in expected_tools:
                continue
            scoped_reference = any(
                _workspace_artifact_path(
                    reference,
                    agent_id=request.agent_id,
                    request_id=request.id,
                    artifact_type=artifact_type,
                )
                is not None
                for reference in _artifact_refs(execution)
            )
            scoped_target = _execution_targets_request_artifact(
                execution,
                request_id=request.id,
                artifact_type=artifact_type,
            )
            if not (scoped_reference or scoped_target):
                continue
            attempted_types.add(artifact_type)
            if execution.status == "succeeded":
                continue
            failed_types.add(artifact_type)
            metadata = (
                execution.result_metadata
                if isinstance(execution.result_metadata, Mapping)
                else {}
            )
            error_code = str(metadata.get("error_code") or "").strip()
            if error_code:
                failure_codes.setdefault(artifact_type, error_code[:200])
            observed_errors[artifact_type].add(
                "unavailable"
                if execution.status in {"started", "unknown"}
                else "invalid"
            )

    for artifact_type in required_types:
        expected_tools = ARTIFACT_TOOLS_BY_TYPE.get(artifact_type)
        if expected_tools is None:
            continue
        # Tool order is a quality contract, not just a filter. In particular,
        # a voiceover workflow can expose both the silent provider output and
        # the later composed MP4; the composed result must win regardless of
        # execution timestamps.
        for expected_tool in expected_tools:
            for execution in executions:
                if execution.tool_name != expected_tool or execution.status != "succeeded":
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
                    if poster_v2_failed_candidates:
                        from app.services.prompt_compiler import (
                            poster_v2_candidate_unit_key,
                        )

                        failed_unit_key = poster_v2_candidate_unit_key(workspace_path)
                        if (
                            failed_unit_key is not None
                            and failed_unit_key in poster_v2_failed_candidates
                        ):
                            observed_errors[artifact_type].add("invalid")
                            failure_codes.setdefault(
                                artifact_type,
                                "deliverable_candidate_qa_failed",
                            )
                            continue
                    if artifact_type == "png" and request.work_type == "poster":
                        receipt_facts = _poster_copy_receipt_facts(
                            request,
                            execution,
                            expected_digest=poster_expected_digest,
                        )
                        if receipt_facts is None:
                            observed_errors[artifact_type].add("invalid")
                            failure_codes.setdefault(
                                artifact_type,
                                "deliverable_poster_copy_receipt_mismatch",
                            )
                            continue
                    else:
                        receipt_facts = {}
                    verified, error = await _verify_storage_artifact(
                        storage_backend,
                        agent_id=request.agent_id,
                        artifact_type=artifact_type,
                        workspace_path=workspace_path,
                        tool_call_id=execution.tool_call_id,
                    )
                    if verified is not None:
                        verified_by_type[artifact_type] = verified
                        if receipt_facts:
                            poster_receipt_facts[artifact_type] = receipt_facts
                        break
                    if error is not None:
                        observed_errors[artifact_type].add(error)
                if artifact_type in verified_by_type:
                    break
            if artifact_type in verified_by_type:
                break

    latest_by_key: dict[str, DeliverableArtifactRevision] = {}
    for artifact in existing:
        latest_by_key.setdefault(artifact.artifact_key, artifact)

    # A repair run may regenerate only the broken member of a multi-file
    # contract. Reuse an untouched candidate only after revalidating both its
    # mutable workspace copy and immutable snapshot. Never reuse the previous
    # candidate for a type that this run explicitly attempted, because doing so
    # would hide a failed repair behind stale success.
    for artifact_type in required_types:
        if artifact_type in attempted_types or artifact_type in verified_by_type:
            continue
        latest = latest_by_key.get(artifact_type)
        if latest is None or latest.status != "candidate":
            continue
        if artifact_type == "png" and poster_expected_digest:
            evaluation = latest.evaluation if isinstance(latest.evaluation, Mapping) else {}
            facts = evaluation.get("facts") if isinstance(evaluation, Mapping) else {}
            if not _poster_artifact_facts_match(
                request,
                facts,
                expected_digest=poster_expected_digest,
            ):
                observed_errors[artifact_type].add("invalid")
                failure_codes.setdefault(
                    artifact_type,
                    "deliverable_poster_copy_receipt_mismatch",
                )
                continue
        verified, error = await _verify_storage_artifact(
            storage_backend,
            agent_id=request.agent_id,
            artifact_type=artifact_type,
            workspace_path=latest.workspace_path,
            tool_call_id=str(
                (latest.evaluation or {}).get("tool_call_id")
                if isinstance(latest.evaluation, Mapping)
                else "repair_reuse"
            ),
        )
        if (
            verified is not None
            and verified.content_hash == latest.content_hash
            and await _verify_immutable_snapshot(storage_backend, artifact=latest)
        ):
            verified_by_type[artifact_type] = verified
        elif error is not None:
            observed_errors[artifact_type].add(error)
        else:
            observed_errors[artifact_type].add("invalid")

    contract_facts: dict[str, dict[str, Any]] = dict(poster_receipt_facts)
    if request.work_type == "presentation":
        contract_facts, contract_invalid_types = _presentation_contract_facts(
            request,
            verified_by_type,
        )
        if (
            contract_facts.get("pptx", {}).get("picture_coverage_gate") == 0
        ):
            for artifact_type in contract_invalid_types:
                failure_codes.setdefault(
                    artifact_type,
                    "presentation_picture_coverage_below_minimum",
                )
        for artifact_type in contract_invalid_types:
            observed_errors.setdefault(artifact_type, set()).add("invalid")
            verified_by_type.pop(artifact_type, None)
    elif request.work_type == "video":
        contract_facts, contract_invalid_types = await _video_contract_facts(
            request,
            verified_by_type,
        )
        for artifact_type in contract_invalid_types:
            observed_errors.setdefault(artifact_type, set()).add("invalid")
            verified_by_type.pop(artifact_type, None)

    persisted: list[DeliverableArtifactRevision] = []
    snapshotted_types: set[str] = set()
    created_types: set[str] = set()
    for artifact_type, verified in verified_by_type.items():
        latest = latest_by_key.get(artifact_type)
        if (
            latest is not None
            and latest.status == "candidate"
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
                    "verification_level": "contract",
                    "source": "runtime_tool_execution",
                    "run_id": str(run_id),
                    "tool_call_id": verified.tool_call_id,
                    "checks": [
                        "tenant_scope",
                        "agent_scope",
                        "request_path",
                        "storage_file",
                        "file_signature",
                        "aspect_ratio",
                        "immutable_snapshot",
                        *(
                            ["page_count", "page_size"]
                            if request.work_type == "presentation"
                            else [
                                "duration",
                                "resolution",
                                "browser_codec",
                                "audio_contract",
                            ]
                            if request.work_type == "video"
                            else []
                        ),
                    ],
                    "facts": contract_facts.get(artifact_type, {}),
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
            created_types.add(artifact_type)
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
        attempted_types=tuple(
            artifact_type
            for artifact_type in required_types
            if artifact_type in attempted_types
        ),
        failed_types=tuple(
            artifact_type
            for artifact_type in required_types
            if artifact_type in failed_types
        ),
        created_types=tuple(
            artifact_type
            for artifact_type in required_types
            if artifact_type in created_types
        ),
        failure_codes=tuple(
            (artifact_type, failure_codes[artifact_type])
            for artifact_type in required_types
            if artifact_type in failure_codes
        ),
    )


async def approve_deliverable_artifacts(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    storage: StorageBackend | None = None,
    require_creative_quality_gate: bool | None = None,
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
    if request.work_type == "poster":
        _poster_blocks, poster_expected_digest = poster_exact_copy_contract(request.spec)
        if poster_expected_digest:
            poster_artifact = latest_candidates.get("png")
            evaluation = (
                poster_artifact.evaluation
                if poster_artifact is not None
                and isinstance(poster_artifact.evaluation, Mapping)
                else {}
            )
            facts = evaluation.get("facts") if isinstance(evaluation, Mapping) else {}
            if not _poster_artifact_facts_match(
                request,
                facts,
                expected_digest=poster_expected_digest,
            ):
                raise DeliverableArtifactError(
                    "deliverable_poster_copy_receipt_mismatch",
                    "Poster artifact has no receipt for the persisted exact-copy contract",
                )
    if require_creative_quality_gate is None:
        settings = get_settings()
        quality_gate_required = creative_quality_gate_required_for_request(
            request,
            enabled=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_REQUIRED,
            tenant_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS,
            agent_ids=settings.DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS,
        )
    else:
        quality_gate_required = require_creative_quality_gate
    try:
        enforce_deliverable_quality_gate(
            request,
            selected,
            require_creative_quality_gate=quality_gate_required,
        )
    except DeliverableQualityGateError as exc:
        raise DeliverableArtifactError(exc.code, str(exc)) from exc
    verified_by_type: dict[str, _VerifiedArtifact] = {}
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
        verified_by_type[artifact.artifact_type] = verified
        if not await _verify_immutable_snapshot(storage_backend, artifact=artifact):
            raise DeliverableArtifactError(
                "deliverable_artifact_snapshot_changed",
                f"Artifact {artifact.artifact_key} immutable snapshot is unavailable or invalid",
            )
    if request.work_type == "presentation":
        _, invalid_types = _presentation_contract_facts(request, verified_by_type)
        if invalid_types:
            raise DeliverableArtifactError(
                "deliverable_artifact_contract_invalid",
                "Presentation artifacts fail structure or visible-content policy checks: "
                + ", ".join(sorted(invalid_types)),
            )
    elif request.work_type == "video":
        _, invalid_types = await _video_contract_facts(request, verified_by_type)
        if invalid_types:
            raise DeliverableArtifactError(
                "deliverable_artifact_contract_invalid",
                "Video artifact fails duration, aspect-ratio, browser-codec, or audio checks: "
                + ", ".join(sorted(invalid_types)),
            )
    selected_ids = {artifact.id for artifact in selected}
    for artifact in artifacts:
        if artifact.status == "approved" and artifact.id not in selected_ids:
            artifact.status = "superseded"
    return selected


__all__ = [
    "DeliverableArtifactError",
    "DeliverableArtifactReconciliation",
    "approve_deliverable_artifacts",
    "deliverable_artifact_snapshot_key",
    "read_deliverable_artifact_snapshot",
    "reconcile_runtime_deliverable_artifacts",
]
