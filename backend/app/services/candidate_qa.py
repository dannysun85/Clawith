"""Automated per-candidate QA for v2 poster deliverables (FR-I5).

Every check here is provider-free: decode/geometry contracts reuse
``media_assets``, OCR reuse the exported evidence-collection primitives, and
subject similarity is a perceptual-hash placeholder until a reviewed deep
similarity model is authorized.  Reports are bound to the candidate artifact
SHA-256 and projected onto ``unit.quality_evaluation``; ``shadow`` mode never
changes lifecycle state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Literal
import unicodedata
from urllib.parse import unquote, urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent_tool_execution import AgentToolExecution
from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
)
from app.services.creative_evidence_collection import (
    _prepare_ocr_variants,
    parse_tesseract_tsv,
)
from app.services.media_assets import (
    MediaContractError,
    validate_generated_image,
    validate_image_delivery_contract,
)
from app.services.poster_contract import poster_exact_copy_blocks
from app.services.prompt_compiler import poster_v2_candidate_unit_key
from app.services.storage import agent_storage_key, get_storage_backend


QA_SCHEMA_VERSION = "candidate-qa-v1"

_QA_IMAGE_TOOLS = ("generate_image_minimax",)


class CandidateQaCheck(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    status: Literal["passed", "failed", "unavailable"]
    evidence: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateQaReport(BaseModel):
    """One candidate QA verdict, hash-bound to the exact artifact bytes."""

    schema_version: str = QA_SCHEMA_VERSION
    unit_key: str = Field(min_length=1, max_length=120)
    artifact_path: str = Field(min_length=1, max_length=1000)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    status: Literal["passed", "failed"]
    score: int = Field(ge=0, le=100)
    checks: tuple[CandidateQaCheck, ...]
    subject_similarity: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_text(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _is_significant_ocr_token(canonical: str) -> bool:
    """Deterministic OCR noise floor for pseudo-text evidence.

    Tesseract emits short garbage tokens ("Pre", "oe.") on texture-heavy
    commercial images even when no text exists.  Only tokens long enough to
    carry meaning count as pseudo-text evidence; OCR absence still never
    proves visual cleanliness on its own (human review stays required).
    """

    if len(canonical) >= 4:
        return True
    cjk_count = sum(1 for character in canonical if "一" <= character <= "鿿")
    return cjk_count >= 2


def image_average_hash(data: bytes) -> str:
    """Perceptual average-hash placeholder for subject similarity (16 hex)."""

    from PIL import Image

    with Image.open(BytesIO(data)) as opened:
        pixels = opened.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        values = list(pixels.tobytes())
    mean = sum(values) / len(values)
    bits = 0
    for value in values:
        bits = (bits << 1) | int(value >= mean)
    return f"{bits:016x}"


def subject_similarity(
    *,
    reference_ahash: str | None,
    candidate_ahash: str,
) -> dict[str, Any]:
    """Placeholder similarity contract; deep models are out of scope for M1."""

    result: dict[str, Any] = {
        "method": "ahash-v1",
        "placeholder": True,
        "candidate_ahash": candidate_ahash,
        "score": None,
        "note": "Perceptual-hash placeholder; deep similarity requires human review",
    }
    if reference_ahash:
        try:
            reference_bits = int(reference_ahash, 16)
            candidate_bits = int(candidate_ahash, 16)
        except ValueError:
            result["status"] = "unavailable"
            return result
        distance = bin(reference_bits ^ candidate_bits).count("1")
        result["score"] = round(1.0 - distance / 64, 4)
        result["status"] = "complete"
    else:
        result["status"] = "unavailable"
    return result


def _ocr_tokens(
    data: bytes,
    *,
    expected_languages: Sequence[str],
    minimum_confidence: float = 10,
) -> tuple[tuple[tuple[str, ...], ...], str | None]:
    """Collect per-variant OCR tokens with the shared pipeline; never raises."""

    tesseract_path = shutil.which("tesseract")
    if tesseract_path is None:
        return (), "tesseract_unavailable"
    language_map = {"zh-CN": "chi_sim", "zh-TW": "chi_tra", "en-US": "eng"}
    languages = tuple(
        sorted({language_map.get(language, language) for language in expected_languages})
    )
    try:
        with tempfile.TemporaryDirectory(prefix="candidate-qa-ocr-") as temp_dir:
            image_path = Path(temp_dir) / "candidate.png"
            image_path.write_bytes(data)
            variants_dir = Path(temp_dir) / "variants"
            variants_dir.mkdir(parents=True, exist_ok=True)
            variants = _prepare_ocr_variants(image_path, output_dir=variants_dir)
            per_variant: list[tuple[str, ...]] = []
            for variant in variants:
                command = [tesseract_path, str(variant), "stdout"]
                if languages:
                    command.extend(["-l", "+".join(languages)])
                command.extend(["--psm", "11", "tsv"])
                process = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    errors="replace",
                    text=True,
                    timeout=90,
                )
                per_variant.append(
                    parse_tesseract_tsv(process.stdout, minimum_confidence=minimum_confidence)
                )
        return tuple(per_variant), None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return (), f"ocr_execution_failed={type(exc).__name__.lower()}"


def evaluate_image_candidate(
    *,
    data: bytes,
    unit_key: str,
    artifact_path: str,
    expected_aspect_ratio: str | None = None,
    expected_copy_texts: Sequence[str] = (),
    prohibited_terms: Sequence[str] = (),
    reference_ahash: str | None = None,
    expected_languages: Sequence[str] = ("zh-CN", "en-US"),
) -> CandidateQaReport:
    """Run every provider-free check against one candidate's bytes."""

    checks: list[CandidateQaCheck] = []
    artifact_sha256 = hashlib.sha256(data).hexdigest()

    width = height = 0
    try:
        width, height = validate_generated_image(data)
        checks.append(
            CandidateQaCheck(
                name="artifact_decodable",
                status="passed",
                evidence=(f"decoded {width}x{height}",),
            )
        )
    except MediaContractError as exc:
        checks.append(
            CandidateQaCheck(
                name="artifact_decodable",
                status="failed",
                evidence=(str(exc)[:200],),
            )
        )

    if width and expected_aspect_ratio:
        try:
            validate_image_delivery_contract(
                width,
                height,
                expected_aspect_ratio=expected_aspect_ratio,
            )
            checks.append(
                CandidateQaCheck(
                    name="aspect_ratio_match",
                    status="passed",
                    evidence=(f"{width}x{height} matches {expected_aspect_ratio}",),
                )
            )
        except MediaContractError as exc:
            checks.append(
                CandidateQaCheck(
                    name="aspect_ratio_match",
                    status="failed",
                    evidence=(str(exc)[:200],),
                )
            )

    candidate_ahash = ""
    if width:
        candidate_ahash = image_average_hash(data)
    similarity = subject_similarity(
        reference_ahash=reference_ahash,
        candidate_ahash=candidate_ahash,
    )

    if width:
        variant_tokens, ocr_error = _ocr_tokens(data, expected_languages=expected_languages)
        if ocr_error is not None:
            checks.append(
                CandidateQaCheck(
                    name="no_pseudo_text",
                    status="unavailable",
                    evidence=(ocr_error,),
                )
            )
            checks.append(
                CandidateQaCheck(
                    name="no_prohibited_terms",
                    status="unavailable",
                    evidence=(ocr_error,),
                )
            )
        else:
            all_tokens = [token for tokens in variant_tokens for token in tokens]
            expected_corpus = _canonical_text(" ".join(expected_copy_texts))
            # Pseudo-text must be a stable, significant foreign token: texture
            # noise never repeats identically across OCR pre-processing
            # variants, while real rendered text does.
            variant_support: dict[str, int] = {}
            display_token: dict[str, str] = {}
            for tokens in variant_tokens:
                seen_in_variant: set[str] = set()
                for token in tokens:
                    canonical = _canonical_text(token)
                    if not canonical or canonical in seen_in_variant:
                        continue
                    seen_in_variant.add(canonical)
                    variant_support[canonical] = variant_support.get(canonical, 0) + 1
                    display_token.setdefault(canonical, token)
            consensus_threshold = 2 if len(variant_tokens) > 1 else 1
            pseudo_tokens = tuple(
                display_token[canonical]
                for canonical, support in sorted(variant_support.items())
                if support >= consensus_threshold
                and _is_significant_ocr_token(canonical)
                and canonical not in expected_corpus
            )
            checks.append(
                CandidateQaCheck(
                    name="no_pseudo_text",
                    status="failed" if pseudo_tokens else "passed",
                    evidence=(
                        tuple(f"pseudo_text={token[:80]}" for token in pseudo_tokens[:8])
                        or ("OCR found only the contracted exact copy",)
                    ),
                )
            )
            full_text = _canonical_text(" ".join(all_tokens))
            matched_terms = tuple(
                term
                for term in prohibited_terms
                if _canonical_text(term) and _canonical_text(term) in full_text
            )
            checks.append(
                CandidateQaCheck(
                    name="no_prohibited_terms",
                    status="failed" if matched_terms else "passed",
                    evidence=(
                        tuple(f"prohibited_term_detected={term}" for term in matched_terms)
                        or ("No prohibited watermark/term detected",)
                    ),
                )
            )

    failed = sum(1 for check in checks if check.status == "failed")
    unavailable = sum(1 for check in checks if check.status == "unavailable")
    undecodable = any(
        check.name == "artifact_decodable" and check.status == "failed" for check in checks
    )
    score = 0 if undecodable else max(0, 100 - 50 * failed - 10 * unavailable)
    return CandidateQaReport(
        unit_key=unit_key,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        status="failed" if failed else "passed",
        score=score,
        checks=tuple(checks),
        subject_similarity=similarity,
    )


def qa_summary_from_evaluation(evaluation: object) -> dict[str, Any] | None:
    """Secret-free QA projection for API reads; never includes prompt text."""

    if not isinstance(evaluation, Mapping):
        return None
    report = evaluation.get("candidate_qa")
    if not isinstance(report, Mapping):
        return None
    checks = [
        {"name": str(check.get("name")), "status": str(check.get("status"))}
        for check in report.get("checks") or ()
        if isinstance(check, Mapping)
    ]
    return {
        "schema_version": report.get("schema_version"),
        "status": report.get("status"),
        "score": report.get("score"),
        "artifact_sha256": report.get("artifact_sha256"),
        "checks": checks,
        "subject_similarity": report.get("subject_similarity") or {},
    }


def candidate_qa_enforcement_for_request(
    request: DeliverableRequest,
    *,
    mode: str,
    tenant_ids: str,
    agent_ids: str,
) -> str:
    """Only allowlisted tenants/Agents leave shadow mode."""

    if str(mode or "").strip().lower() != "enforcing":
        return "shadow"

    def parse(raw: str) -> set[str]:
        return {item.strip() for item in str(raw or "").split(",") if item.strip()}

    if str(request.tenant_id) in parse(tenant_ids) or str(request.agent_id) in parse(agent_ids):
        return "enforcing"
    return "shadow"


def _candidate_artifact_refs(execution: AgentToolExecution) -> tuple[str, ...]:
    metadata = execution.result_metadata
    refs = metadata.get("artifact_refs") if isinstance(metadata, Mapping) else None
    if not isinstance(refs, list):
        return ()
    return tuple(ref for ref in refs if isinstance(ref, str) and ref)


def _candidate_workspace_path(reference: str, request: DeliverableRequest) -> str | None:
    try:
        parsed = urlsplit(reference)
        raw_path = unquote(parsed.path).replace("\\", "/").lstrip("/")
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "workspace" or parsed.netloc != str(request.agent_id):
        return None
    unit_key = poster_v2_candidate_unit_key(raw_path)
    if unit_key is None or str(request.id) not in raw_path:
        return None
    return raw_path


async def evaluate_poster_v2_candidates(
    db: AsyncSession,
    *,
    request: DeliverableRequest,
    run_id: uuid.UUID,
    storage=None,
) -> tuple[CandidateQaReport, ...]:
    """Evaluate every v2 candidate Tool output and bind reports to units.

    Shadow mode (the default) only records reports; ``enforcing`` additionally
    fails the candidate_qa unit so the candidate cannot be registered as the
    final artifact.
    """

    if request.workflow_id != "builtin.poster.v2" or request.current_execution_id is None:
        return ()
    settings = get_settings()
    enforcement = candidate_qa_enforcement_for_request(
        request,
        mode=settings.DELIVERABLE_CREATIVE_QA_ENFORCEMENT,
        tenant_ids=settings.DELIVERABLE_CREATIVE_QA_TENANT_IDS,
        agent_ids=settings.DELIVERABLE_CREATIVE_QA_AGENT_IDS,
    )
    execution_result = await db.execute(
        select(DeliverableExecution).where(
            DeliverableExecution.tenant_id == request.tenant_id,
            DeliverableExecution.id == request.current_execution_id,
        )
    )
    execution = execution_result.scalar_one_or_none()
    if execution is None:
        return ()
    unit_result = await db.execute(
        select(DeliverableExecutionUnit).where(
            DeliverableExecutionUnit.tenant_id == request.tenant_id,
            DeliverableExecutionUnit.execution_id == execution.id,
            DeliverableExecutionUnit.unit_key.like("candidate-%"),
        )
    )
    units = {(unit.stage_key, unit.unit_key): unit for unit in unit_result.scalars().all()}

    tool_result = await db.execute(
        select(AgentToolExecution).where(
            AgentToolExecution.tenant_id == request.tenant_id,
            AgentToolExecution.run_id == run_id,
            AgentToolExecution.tool_name.in_(_QA_IMAGE_TOOLS),
            AgentToolExecution.status == "succeeded",
        )
    )
    tool_executions = tuple(tool_result.scalars().all())

    spec = request.spec if isinstance(request.spec, Mapping) else {}
    try:
        expected_copy_texts = tuple(
            block["text"] for block in poster_exact_copy_blocks(spec)
        )
    except MediaContractError:
        expected_copy_texts = ()
    raw_prohibitions = spec.get("prohibitions")
    if isinstance(raw_prohibitions, str):
        prohibited_terms = tuple(
            line.strip() for line in raw_prohibitions.splitlines() if line.strip()
        )
    elif isinstance(raw_prohibitions, Sequence):
        prohibited_terms = tuple(
            str(item).strip() for item in raw_prohibitions if str(item).strip()
        )
    else:
        prohibited_terms = ()
    expected_ratio = str(spec.get("aspect_ratio") or "").strip() or None

    storage_backend = storage or get_storage_backend()
    now = datetime.now(UTC)
    reports: list[CandidateQaReport] = []
    seen_units: set[str] = set()
    for tool_execution in tool_executions:
        for reference in _candidate_artifact_refs(tool_execution):
            workspace_path = _candidate_workspace_path(reference, request)
            if workspace_path is None:
                continue
            unit_key = poster_v2_candidate_unit_key(workspace_path)
            if unit_key is None or unit_key in seen_units:
                continue
            seen_units.add(unit_key)
            generate_unit = units.get(("candidate_generate", unit_key))
            qa_unit = units.get(("candidate_qa", unit_key))
            try:
                data = await storage_backend.read_bytes(
                    agent_storage_key(request.agent_id, workspace_path)
                )
            except Exception:
                report = CandidateQaReport(
                    unit_key=unit_key,
                    artifact_path=workspace_path,
                    artifact_sha256="0" * 64,
                    status="failed",
                    score=0,
                    checks=(
                        CandidateQaCheck(
                            name="artifact_decodable",
                            status="failed",
                            evidence=("candidate bytes unavailable in storage",),
                        ),
                    ),
                )
            else:
                report = evaluate_image_candidate(
                    data=data,
                    unit_key=unit_key,
                    artifact_path=workspace_path,
                    expected_aspect_ratio=expected_ratio,
                    expected_copy_texts=expected_copy_texts,
                    prohibited_terms=prohibited_terms,
                )
            reports.append(report)
            if generate_unit is not None and generate_unit.status in {"pending", "running"}:
                generate_unit.status = "succeeded"
                generate_unit.completed_at = now
                generate_unit.result_snapshot = {
                    **(generate_unit.result_snapshot or {}),
                    "candidate_artifact_path": workspace_path,
                    "artifact_sha256": report.artifact_sha256,
                }
            if qa_unit is not None:
                qa_unit.quality_evaluation = {
                    "candidate_qa": report.model_dump(mode="json"),
                    "enforcement": enforcement,
                    "evaluated_at": now.isoformat(),
                }
                if enforcement == "enforcing":
                    if report.status == "failed" and qa_unit.status != "failed":
                        qa_unit.status = "failed"
                        qa_unit.last_error_code = "candidate_qa_failed"
                        qa_unit.completed_at = now
                    elif report.status == "passed" and qa_unit.status in {"pending", "running"}:
                        qa_unit.status = "succeeded"
                        qa_unit.completed_at = now
    await db.flush()
    return tuple(reports)


__all__ = [
    "QA_SCHEMA_VERSION",
    "CandidateQaCheck",
    "CandidateQaReport",
    "candidate_qa_enforcement_for_request",
    "evaluate_image_candidate",
    "evaluate_poster_v2_candidates",
    "image_average_hash",
    "qa_summary_from_evaluation",
    "subject_similarity",
]
