"""Deck outline pipeline and paid-work gates for v2 presentations (FR-P1~P5).

Mirrors the M2 storyboard discipline: the outline draft run produces only
files, the customer approves the outline before any rendering or image
generation happens, and every paid Tool seam (PPTX/PDF conversion, managed
image generation) fails closed when the outline approval receipt is missing.
Semantic safety is enforced twice — the source_refs/fact-assertion
reconciliation is a hard gate inside the presentation contract validator, and
the post-run ``semantic_qa`` unit receipt binds the verdict to the slide_spec
hash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.deliverable import (
    DeliverableApprovalReceipt,
    DeliverableCreativeBrief,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services.candidate_qa import (
    CandidateQaCheck,
    CandidateQaReport,
    candidate_qa_enforcement_for_request,
)
from app.services.creative_briefs import (
    PRESENTATION_BRIEF_SCHEMA_VERSION,
    PRESENTATION_V2_WORKFLOW_ID,
    PresentationBrief,
    brief_sha256,
    compile_presentation_brief,
)
from app.services.media_assets import MediaContractError
from app.services.source_inventory import (
    SEMANTIC_QA_SCHEMA_VERSION,
    SourceInventoryEntry,
    compile_source_inventory,
    reconcile_slide_semantics,
    semantic_report_checks,
)
from app.services.storage import agent_storage_key, get_storage_backend


DECK_OUTLINE_SCHEMA_VERSION = "deck-outline-v1"

# rollout §8.1: the customer-facing editability contract maps to one explicit
# renderer mode; the managed conversion Tool rejects any other mode for v2.
EDITABILITY_RENDER_MODES = {
    "editable": "hybrid_editable",
    "hybrid": "hybrid_editable",
    "visual_fidelity": "visual",
}

_RENDER_TARGET_RE = re.compile(
    r"^workspace/deliverables/(?P<request_id>[0-9a-fA-F-]{36})/"
    r"[^/]+\.(?P<fmt>pptx|pdf)$"
)
_IMAGE_ASSET_RE = re.compile(
    r"^workspace/deliverables/(?P<request_id>[0-9a-fA-F-]{36})/"
    r"assets/[A-Za-z0-9_\-]+\.(?:png|jpg|jpeg|webp)$"
)

_ACTIVE_UNIT_STATUSES = frozenset({"pending", "running", "blocked", "reconciling"})


class DeckOutlineError(RuntimeError):
    """A drafted deck outline violates the pre-production outline contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OutlineSlide(BaseModel):
    """One planned slide; a superset of the v1 outline slide contract."""

    slide_id: str = Field(pattern=r"^slide-\d{2}$")
    purpose: str = Field(min_length=1, max_length=500)
    headline: str = Field(min_length=1, max_length=200)
    evidence: tuple[str, ...] = Field(min_length=1)
    visual_intent: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid", frozen=True)


class DeckOutline(BaseModel):
    """Validated, hash-bound deck outline receipt payload (FR-P3)."""

    schema_version: str = DECK_OUTLINE_SCHEMA_VERSION
    deck_title: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=500)
    core_message: str = Field(min_length=1, max_length=1000)
    one_sentence_claim: str = Field(min_length=1, max_length=500)
    storyline: tuple[str, ...] = Field(min_length=1)
    slides: tuple[OutlineSlide, ...]
    outline_sha256: str = Field(min_length=64, max_length=64)

    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_deck_outline(
    brief: PresentationBrief,
    raw_outline: Mapping[str, Any] | None,
) -> DeckOutline:
    """Normalize the Runtime-drafted outline into a validated deck plan.

    Fail closed on any contract violation: an outline that does not match the
    confirmed brief must never reach the approval card, let alone rendering.
    """

    if not isinstance(raw_outline, Mapping):
        raise DeckOutlineError(
            "deliverable_outline_not_object",
            "outline.json must contain a JSON object",
        )
    top_fields: dict[str, str] = {}
    for field in ("deck_title", "audience", "core_message", "one_sentence_claim"):
        value = str(raw_outline.get(field) or "").strip()
        if not value:
            raise DeckOutlineError(
                "deliverable_outline_missing_field",
                f"outline.json must contain a non-empty {field}",
            )
        top_fields[field] = value
    raw_storyline = raw_outline.get("storyline")
    if not isinstance(raw_storyline, Sequence) or isinstance(raw_storyline, (str, bytes)):
        raise DeckOutlineError(
            "deliverable_outline_storyline_missing",
            "outline.json must contain a storyline array",
        )
    storyline = tuple(
        line for line in (str(item or "").strip() for item in raw_storyline) if line
    )
    if not storyline:
        raise DeckOutlineError(
            "deliverable_outline_storyline_missing",
            "outline.json storyline must contain at least one non-empty beat",
        )

    raw_slides = raw_outline.get("slides")
    if not isinstance(raw_slides, Sequence) or isinstance(raw_slides, (str, bytes)):
        raise DeckOutlineError(
            "deliverable_outline_slides_missing",
            "outline.json must contain a slides array",
        )
    expected = brief.page_count
    if len(raw_slides) != expected:
        raise DeckOutlineError(
            "deliverable_outline_slide_count_mismatch",
            f"outline must contain exactly {expected} slides, found {len(raw_slides)}",
        )
    slides: list[OutlineSlide] = []
    for index, raw_slide in enumerate(raw_slides, start=1):
        expected_id = f"slide-{index:02d}"
        if not isinstance(raw_slide, Mapping):
            raise DeckOutlineError(
                "deliverable_outline_slide_invalid",
                f"{expected_id} must be a JSON object",
            )
        candidate = dict(raw_slide)
        candidate["slide_id"] = str(candidate.get("slide_id") or expected_id)
        evidence = candidate.get("evidence")
        if isinstance(evidence, str):
            candidate["evidence"] = [evidence]
        try:
            slide = OutlineSlide.model_validate(candidate)
        except ValidationError as exc:
            raise DeckOutlineError(
                "deliverable_outline_slide_invalid",
                f"{expected_id} is invalid: {exc.errors()[0].get('msg')}",
            ) from exc
        if slide.slide_id != expected_id:
            raise DeckOutlineError(
                "deliverable_outline_slide_id_sequence",
                f"slides must be ordered slide-01..slide-{expected:02d}; "
                f"found {slide.slide_id}",
            )
        slides.append(slide)

    digest = _canonical_sha256(
        {
            "schema_version": DECK_OUTLINE_SCHEMA_VERSION,
            "brief_sha256": brief_sha256(brief),
            "deck_title": top_fields["deck_title"],
            "audience": top_fields["audience"],
            "core_message": top_fields["core_message"],
            "one_sentence_claim": top_fields["one_sentence_claim"],
            "storyline": list(storyline),
            "slides": [slide.model_dump(mode="json") for slide in slides],
        }
    )
    return DeckOutline(
        deck_title=top_fields["deck_title"],
        audience=top_fields["audience"],
        core_message=top_fields["core_message"],
        one_sentence_claim=top_fields["one_sentence_claim"],
        storyline=storyline,
        slides=tuple(slides),
        outline_sha256=digest,
    )


def parse_slide_spec_slides(
    raw_spec: Mapping[str, Any] | None,
    *,
    expected_page_count: int,
) -> tuple[dict[str, Any], ...]:
    """Parse the slide_spec plan enough for semantic reconciliation.

    The full structural contract is re-validated at conversion time; this
    parser only guarantees the fields the semantic gate reads.
    """

    if not isinstance(raw_spec, Mapping):
        raise DeckOutlineError(
            "deliverable_slide_spec_not_object",
            "slide_spec.json must contain a JSON object",
        )
    raw_slides = raw_spec.get("slides")
    if not isinstance(raw_slides, Sequence) or isinstance(raw_slides, (str, bytes)):
        raise DeckOutlineError(
            "deliverable_slide_spec_invalid",
            "slide_spec.json must contain a slides array",
        )
    if len(raw_slides) != expected_page_count:
        raise DeckOutlineError(
            "deliverable_slide_spec_invalid",
            f"slide_spec must contain exactly {expected_page_count} slides, "
            f"found {len(raw_slides)}",
        )
    slides: list[dict[str, Any]] = []
    for index, raw_slide in enumerate(raw_slides, start=1):
        expected_id = f"slide-{index:02d}"
        if not isinstance(raw_slide, Mapping):
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec.slides[{index}] must be a JSON object",
            )
        slide = dict(raw_slide)
        slide_id = str(slide.get("slide_id") or "").strip()
        if slide_id != expected_id:
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec slides must be ordered slide-01..; "
                f"slides[{index}] has '{slide_id or '<empty>'}'",
            )
        if not str(slide.get("headline") or "").strip():
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec.slides[{index}].headline must be non-empty",
            )
        body_points = slide.get("body_points")
        if not isinstance(body_points, Sequence) or isinstance(body_points, (str, bytes)):
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec.slides[{index}].body_points must be an array",
            )
        source_refs = slide.get("source_refs")
        if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec.slides[{index}].source_refs must be an array",
            )
        if slide.get("data_slide") not in (None, True, False):
            raise DeckOutlineError(
                "deliverable_slide_spec_invalid",
                f"slide_spec.slides[{index}].data_slide must be a boolean when present",
            )
        slides.append(slide)
    return tuple(slides)


def outline_workspace_path(request_id: uuid.UUID | str) -> str:
    return f"workspace/deliverables/{request_id}/outline.json"


def slide_spec_workspace_path(request_id: uuid.UUID | str) -> str:
    return f"workspace/deliverables/{request_id}/slide_spec.json"


def presentation_v2_render_request_id(value: object) -> str | None:
    """Parse the deliverable request id out of a managed render target path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    match = _RENDER_TARGET_RE.match(normalized)
    return match.group("request_id") if match else None


def presentation_v2_image_request_id(value: object) -> str | None:
    """Parse the deliverable request id out of a managed deck asset path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    match = _IMAGE_ASSET_RE.match(normalized)
    return match.group("request_id") if match else None


async def outline_approved(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> bool:
    """An outline approval receipt on any execution of the request counts."""

    result = await db.execute(
        select(DeliverableApprovalReceipt)
        .where(
            DeliverableApprovalReceipt.tenant_id == tenant_id,
            DeliverableApprovalReceipt.request_id == request_id,
            DeliverableApprovalReceipt.stage == "outline",
            DeliverableApprovalReceipt.action == "approve",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def load_latest_outline(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> DeckOutline | None:
    """Load the newest successfully compiled outline across executions."""

    result = await db.execute(
        select(DeliverableExecutionUnit)
        .where(
            DeliverableExecutionUnit.tenant_id == tenant_id,
            DeliverableExecutionUnit.request_id == request_id,
            DeliverableExecutionUnit.stage_key == "outline",
            DeliverableExecutionUnit.status == "succeeded",
        )
        .order_by(
            DeliverableExecutionUnit.created_at.desc(),
            DeliverableExecutionUnit.id.desc(),
        )
        .limit(1)
    )
    unit = result.scalar_one_or_none()
    payload = (unit.result_snapshot or {}).get("outline") if unit else None
    if not isinstance(payload, Mapping):
        return None
    try:
        return DeckOutline.model_validate(payload)
    except ValidationError:
        return None


async def _request_brief_row(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> DeliverableCreativeBrief | None:
    result = await db.execute(
        select(DeliverableCreativeBrief)
        .where(
            DeliverableCreativeBrief.tenant_id == tenant_id,
            DeliverableCreativeBrief.request_id == request_id,
            DeliverableCreativeBrief.schema_version == PRESENTATION_BRIEF_SCHEMA_VERSION,
        )
        .order_by(
            DeliverableCreativeBrief.created_at.desc(),
            DeliverableCreativeBrief.id.desc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _inventory_from_brief_row(
    brief_row: DeliverableCreativeBrief | None,
) -> tuple[SourceInventoryEntry, ...]:
    raw = (brief_row.source_inventory if brief_row else None) or ()
    entries: list[SourceInventoryEntry] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            try:
                entries.append(SourceInventoryEntry.model_validate(dict(item)))
            except ValidationError:
                continue
    return tuple(entries)


async def load_presentation_v2_inventory_projection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> tuple[dict[str, Any], ...]:
    """Return the registered source inventory projection for prompts/UI."""

    brief_row = await _request_brief_row(db, tenant_id=tenant_id, request_id=request_id)
    return tuple(
        entry.model_dump(mode="json") for entry in _inventory_from_brief_row(brief_row)
    )


@dataclass(frozen=True, slots=True)
class PresentationV2RenderGate:
    """Server-owned facts the conversion Tool must honor for a v2 deck."""

    request_id: uuid.UUID
    expected_render_mode: str
    editability_contract: str
    source_inventory_entries: tuple[dict[str, Any], ...]


async def _presentation_v2_request(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    request_id: uuid.UUID,
) -> DeliverableRequest | None:
    result = await db.execute(
        select(DeliverableRequest).where(
            DeliverableRequest.id == request_id,
            DeliverableRequest.agent_id == agent_id,
        )
    )
    request = result.scalar_one_or_none()
    if request is None or request.workflow_id != PRESENTATION_V2_WORKFLOW_ID:
        return None
    return request


async def resolve_presentation_v2_render_gate(
    *,
    agent_id: uuid.UUID,
    target_path: str,
    output_format: str,
) -> PresentationV2RenderGate | None:
    """Fail closed before rendering a v2 deck without outline approval.

    Returns ``None`` for non-v2 requests so the v1 path is untouched.  For a
    v2 presentation request the outline approval receipt and the brief's
    editability contract are hard requirements checked before any PPTX/PDF
    artifact can be produced; the returned inventory entries feed the
    semantic gate inside the conversion contract validator.
    """

    raw_request_id = presentation_v2_render_request_id(target_path)
    if raw_request_id is None:
        return None
    async with async_session() as db:
        request = await _presentation_v2_request(
            db,
            agent_id=agent_id,
            request_id=uuid.UUID(raw_request_id),
        )
        if request is None:
            return None
        if not await outline_approved(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        ):
            raise MediaContractError(
                "deliverable_outline_approval_required: the deck outline must be "
                "approved before any rendering or conversion runs"
            )
        brief_row = await _request_brief_row(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        brief_payload = brief_row.brief if brief_row else None
        editability = (
            str(brief_payload.get("editability_contract") or "").strip()
            if isinstance(brief_payload, Mapping)
            else ""
        )
        if editability not in EDITABILITY_RENDER_MODES:
            raise MediaContractError(
                "deliverable_presentation_brief_missing: the confirmed v2 "
                "presentation brief is required before rendering"
            )
        entries = _inventory_from_brief_row(brief_row)
        return PresentationV2RenderGate(
            request_id=request.id,
            expected_render_mode=(
                EDITABILITY_RENDER_MODES[editability] if output_format == "pptx" else ""
            ),
            editability_contract=editability,
            source_inventory_entries=tuple(
                entry.model_dump(mode="json") for entry in entries
            ),
        )


async def resolve_presentation_v2_image_gate(
    *,
    agent_id: uuid.UUID,
    save_path: str,
) -> bool:
    """Block paid deck imagery before outline approval; None-equivalent for v1.

    Returns ``True`` when the call is a v2 deck asset that passed the gate;
    returns ``False`` when the path is not a v2 deck asset at all (the caller
    then follows its normal v1/quick path untouched).
    """

    raw_request_id = presentation_v2_image_request_id(save_path)
    if raw_request_id is None:
        return False
    async with async_session() as db:
        request = await _presentation_v2_request(
            db,
            agent_id=agent_id,
            request_id=uuid.UUID(raw_request_id),
        )
        if request is None:
            return False
        if not await outline_approved(
            db,
            tenant_id=request.tenant_id,
            request_id=request.id,
        ):
            raise MediaContractError(
                "deliverable_outline_approval_required: deck imagery is paid work "
                "and starts only after the customer approves the outline"
            )
        return True


async def evaluate_presentation_v2_semantics(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    storage=None,
) -> CandidateQaReport | None:
    """FR-P2: bind the slide_spec semantic verdict to the semantic_qa unit.

    Shadow mode (the default) only records the report; ``enforcing``
    additionally fails the semantic_qa unit so an unsourced deck cannot reach
    final approval silently.  The conversion-time hard gate in the contract
    validator independently blocks rendering either way.
    """

    if request.workflow_id != PRESENTATION_V2_WORKFLOW_ID or request.current_execution_id is None:
        return None
    brief, _missing = compile_presentation_brief(
        request.goal,
        request.spec,
        request.inputs,
        output_contract=request.output_contract or ("pptx",),
    )
    if brief is None:
        return None
    storage_backend = storage or get_storage_backend()
    spec_path = slide_spec_workspace_path(request.id)
    try:
        raw_bytes = await storage_backend.read_bytes(
            agent_storage_key(request.agent_id, spec_path)
        )
        raw_spec = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None
    brief_row = await _request_brief_row(
        db,
        tenant_id=request.tenant_id,
        request_id=request.id,
    )
    entries = _inventory_from_brief_row(brief_row)
    if not entries:
        # The brief row predates inventory registration (e.g. legacy shadow);
        # re-register provider-free instead of skipping the gate.
        entries = await compile_source_inventory(request, storage=storage_backend)
    try:
        slides = parse_slide_spec_slides(
            raw_spec if isinstance(raw_spec, Mapping) else None,
            expected_page_count=brief.page_count,
        )
    except DeckOutlineError as exc:
        reconciliation = None
        parse_error = exc
    else:
        reconciliation = reconcile_slide_semantics(slides, entries)
        parse_error = None
    artifact_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    settings = get_settings()
    enforcement = candidate_qa_enforcement_for_request(
        request,
        mode=settings.DELIVERABLE_CREATIVE_QA_ENFORCEMENT,
        tenant_ids=settings.DELIVERABLE_CREATIVE_QA_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_CREATIVE_QA_AGENT_IDS,
    )
    now = datetime.now(UTC)
    if parse_error is not None:
        report = CandidateQaReport(
            schema_version=SEMANTIC_QA_SCHEMA_VERSION,
            unit_key="deck",
            artifact_path=spec_path,
            artifact_sha256=artifact_sha256,
            status="failed",
            score=0,
            checks=(
                CandidateQaCheck(
                    name="slide_spec_parseable",
                    status="failed",
                    evidence=(str(parse_error)[:200],),
                ),
            ),
        )
    else:
        assert reconciliation is not None
        checks = tuple(
            CandidateQaCheck(
                name=str(check["name"]),
                status="failed" if check["status"] == "failed" else "passed",
                evidence=tuple(str(item) for item in check["evidence"]),
            )
            for check in semantic_report_checks(reconciliation)
        )
        failed_checks = sum(1 for check in checks if check.status == "failed")
        report = CandidateQaReport(
            schema_version=SEMANTIC_QA_SCHEMA_VERSION,
            unit_key="deck",
            artifact_path=spec_path,
            artifact_sha256=artifact_sha256,
            status="failed" if failed_checks else "passed",
            score=max(0, 100 - 25 * failed_checks),
            checks=checks,
            subject_similarity={
                "inventory_sha256": reconciliation.inventory_sha256,
                "assertion_count": reconciliation.assertion_count,
                "assumption_count": reconciliation.assumption_count,
            },
        )
    unit_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == request.current_execution_id,
            DeliverableExecutionUnit.stage_key == "semantic_qa",
            DeliverableExecutionUnit.unit_key == "deck",
        )
    )
    unit = unit_result.scalar_one_or_none()
    if unit is not None:
        unit.quality_evaluation = {
            "semantic_qa": report.model_dump(mode="json"),
            "enforcement": enforcement,
            "evaluated_at": now.isoformat(),
        }
        if enforcement == "enforcing":
            if report.status == "failed" and unit.status != "failed":
                unit.status = "failed"
                unit.last_error_code = "semantic_qa_failed"
                unit.completed_at = now
            elif report.status == "passed" and unit.status in _ACTIVE_UNIT_STATUSES:
                unit.status = "succeeded"
                unit.completed_at = now
    await db.flush()
    return report


async def advance_presentation_v2_after_run(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    run_id: uuid.UUID,
    lifecycle_status: str = "completed",
    now: datetime | None = None,
    storage=None,
) -> bool:
    """Project a terminated v2 presentation run onto the outline/produce stages.

    Returns ``True`` when the stage was fully handled here (outline drafting);
    the production stage returns ``False`` after recording the semantic QA
    receipt so the caller can run the standard artifact reconciliation for the
    final deck files.
    """

    if request.workflow_id != PRESENTATION_V2_WORKFLOW_ID:
        return False
    del run_id
    timestamp = now or datetime.now(UTC)
    stage = str(request.current_stage or "")
    cancelled = str(lifecycle_status or "").strip().lower() == "cancelled"

    if stage in {"slide_render", "slide_revision"}:
        if cancelled:
            return False
        # FR-P6: a page-targeted revision run reconciles exactly like the
        # production run — only the target slide units exist in the revision
        # execution, and the converted deck becomes a new artifact revision.
        await evaluate_presentation_v2_semantics(db, request=request, storage=storage)
        return False

    if stage != "outline_draft":
        return False

    units_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == request.current_execution_id,
        )
    )
    units = tuple(units_result.scalars().all())
    inventory_unit = next(
        (unit for unit in units if unit.stage_key == "source_inventory"), None
    )
    outline_unit = next((unit for unit in units if unit.stage_key == "outline"), None)
    slide_spec_unit = next(
        (unit for unit in units if unit.stage_key == "slide_spec"), None
    )

    def fail(code: str) -> None:
        request.status = "failed"
        request.current_stage = "outline_invalid"
        request.last_error_code = code[:100]
        request.completed_at = timestamp
        request.version += 1
        if outline_unit is not None:
            outline_unit.status = "failed"
            outline_unit.last_error_code = code[:100]
            outline_unit.completed_at = timestamp

    if cancelled:
        request.status = "cancelled"
        request.current_stage = "cancelled"
        request.completed_at = timestamp
        request.version += 1
        return True

    storage_backend = storage or get_storage_backend()
    brief, missing = compile_presentation_brief(
        request.goal,
        request.spec,
        request.inputs,
        output_contract=request.output_contract or ("pptx",),
    )
    if brief is None:
        fail(f"brief_missing:{next(iter(missing), 'unknown')}")
        return True
    try:
        outline_text = await storage_backend.read_text(
            agent_storage_key(request.agent_id, outline_workspace_path(request.id)),
            encoding="utf-8",
        )
    except Exception:
        fail("deliverable_outline_missing")
        return True
    try:
        raw_outline = json.loads(outline_text)
    except ValueError:
        fail("deliverable_outline_invalid")
        return True
    try:
        outline = compile_deck_outline(
            brief,
            raw_outline if isinstance(raw_outline, Mapping) else None,
        )
    except DeckOutlineError as exc:
        fail(exc.code)
        return True
    try:
        spec_text = await storage_backend.read_text(
            agent_storage_key(request.agent_id, slide_spec_workspace_path(request.id)),
            encoding="utf-8",
        )
    except Exception:
        fail("deliverable_slide_spec_missing")
        return True
    try:
        raw_spec = json.loads(spec_text)
        slides = parse_slide_spec_slides(
            raw_spec if isinstance(raw_spec, Mapping) else None,
            expected_page_count=brief.page_count,
        )
    except (ValueError, DeckOutlineError) as exc:
        code = exc.code if isinstance(exc, DeckOutlineError) else "deliverable_slide_spec_invalid"
        fail(code)
        return True

    # FR-P2: reconcile the plan against the registered inventory now so the
    # review card shows semantic findings before approval.  The hard gate
    # still fires at conversion; this projection is advisory evidence.
    inventory_entries = await compile_source_inventory(request, storage=storage_backend)
    reconciliation = reconcile_slide_semantics(slides, inventory_entries)
    slide_spec_sha256 = hashlib.sha256(spec_text.encode("utf-8")).hexdigest()

    if inventory_unit is not None and inventory_unit.status in _ACTIVE_UNIT_STATUSES:
        inventory_unit.status = "succeeded"
        inventory_unit.completed_at = timestamp
        inventory_unit.result_snapshot = {
            **dict(inventory_unit.result_snapshot or {}),
            "source_inventory": [
                entry.model_dump(mode="json") for entry in inventory_entries
            ],
            "inventory_sha256": reconciliation.inventory_sha256,
        }
    if outline_unit is not None:
        outline_unit.status = "succeeded"
        outline_unit.completed_at = timestamp
        outline_unit.last_error_code = None
        outline_unit.result_snapshot = {
            **dict(outline_unit.result_snapshot or {}),
            "outline": outline.model_dump(mode="json"),
            "outline_sha256": outline.outline_sha256,
        }
    if slide_spec_unit is not None and slide_spec_unit.status in _ACTIVE_UNIT_STATUSES:
        slide_spec_unit.status = "succeeded"
        slide_spec_unit.completed_at = timestamp
        slide_spec_unit.result_snapshot = {
            **dict(slide_spec_unit.result_snapshot or {}),
            "slide_spec_sha256": slide_spec_sha256,
            "semantic_findings": [
                finding.model_dump(mode="json")
                for finding in reconciliation.findings
            ],
            "assertion_count": reconciliation.assertion_count,
            "assumption_count": reconciliation.assumption_count,
        }
    request.status = "waiting_approval"
    request.current_stage = "outline_review"
    request.last_error_code = None
    request.version += 1
    return True


__all__ = [
    "DECK_OUTLINE_SCHEMA_VERSION",
    "EDITABILITY_RENDER_MODES",
    "DeckOutline",
    "DeckOutlineError",
    "OutlineSlide",
    "PresentationV2RenderGate",
    "advance_presentation_v2_after_run",
    "compile_deck_outline",
    "evaluate_presentation_v2_semantics",
    "load_latest_outline",
    "load_presentation_v2_inventory_projection",
    "outline_approved",
    "outline_workspace_path",
    "parse_slide_spec_slides",
    "presentation_v2_image_request_id",
    "presentation_v2_render_request_id",
    "resolve_presentation_v2_image_gate",
    "resolve_presentation_v2_render_gate",
    "slide_spec_workspace_path",
]
