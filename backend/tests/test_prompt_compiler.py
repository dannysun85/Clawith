"""FR-I2/FR-I5 contracts for the image prompt compiler and candidate QA."""

from __future__ import annotations

import hashlib
import io
import shutil
import uuid

import pytest

from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.services.candidate_qa import (
    candidate_qa_enforcement_for_request,
    evaluate_image_candidate,
    image_average_hash,
    qa_summary_from_evaluation,
    subject_similarity,
)
from app.services.creative_briefs import compile_creative_brief
from app.services.media_assets import MediaContractError
from app.services.prompt_compiler import (
    COMPILER_VERSION,
    PosterV2CandidateBinding,
    compile_image_prompt,
    compiled_prompt_workspace_path,
    poster_v2_candidate_unit_key,
    resolve_poster_v2_candidate_unit,
)


def _brief(tier: str = "pro"):
    brief, missing = compile_creative_brief(
        "为新款极光保温杯制作抖音投放海报",
        {
            "channel": "social",
            "aspect_ratio": "3:4",
            "style": "commercial",
            "audience": "25-35 岁都市白领",
            "exact_copy_blocks": [{"role": "title", "text": "极光保温杯"}],
            "prohibitions": "禁止出现竞品 logo",
        },
        [],
        tier=tier,
    )
    assert missing == ()
    assert brief is not None
    return brief


def test_compilation_is_reproducible_for_same_brief_and_version() -> None:
    brief = _brief()
    first = compile_image_prompt(
        brief, provider_target="volcengine_agent_plan", candidate_index=1, quality_size="3K",
    )
    second = compile_image_prompt(
        brief, provider_target="volcengine_agent_plan", candidate_index=1, quality_size="3K",
    )
    assert first == second
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.compiler_version == COMPILER_VERSION

    other_candidate = compile_image_prompt(
        brief, provider_target="volcengine_agent_plan", candidate_index=2, quality_size="3K",
    )
    assert other_candidate.prompt_sha256 != first.prompt_sha256
    assert other_candidate.neutral["composition"] != first.neutral["composition"]


def test_compiler_never_passes_through_raw_goal_text() -> None:
    brief = _brief()
    compiled = compile_image_prompt(brief, provider_target="minimax", candidate_index=1)
    # The raw goal carried an instruction that is not part of the brief's
    # structured purpose; it must not leak into any compiled layer.
    smuggled = "顺便帮我写一首诗"
    brief_with_clean_purpose = brief.model_copy(
        update={"purpose": "新款保温杯的抖音投放主视觉"}
    )
    compiled_clean = compile_image_prompt(
        brief_with_clean_purpose, provider_target="minimax", candidate_index=1,
    )
    assert smuggled not in compiled_clean.neutral_prompt
    assert smuggled not in str(compiled_clean.provider_payload)
    # Structured prohibitions do reach the negative constraints.
    assert any("竞品 logo" in item for item in compiled.neutral["negative_constraints"])
    # The no-generated-text policy is always present.
    assert "Do not render any words" in compiled.neutral_prompt


def test_provider_payload_boundaries_are_reused() -> None:
    from app.services.volcengine_agent_plan import image_size_for_aspect_ratio

    brief = _brief()
    volc = compile_image_prompt(
        brief, provider_target="volcengine_agent_plan", candidate_index=1, quality_size="2K",
    )
    assert volc.provider_payload["size"] == image_size_for_aspect_ratio("2K", "3:4")
    assert volc.provider_payload["prompt"] == volc.neutral_prompt
    assert volc.provider_payload["watermark"] is False

    minimax = compile_image_prompt(brief, provider_target="minimax", candidate_index=1)
    assert minimax.provider_payload == {
        "model": "image-01",
        "prompt": minimax.neutral_prompt,
        "aspect_ratio": "3:4",
        "response_format": "url",
    }

    with pytest.raises(ValueError, match="aspect_ratio"):
        compile_image_prompt(
            brief.model_copy(update={"aspect_ratio": "21:9"}),
            provider_target="volcengine_agent_plan",
            candidate_index=1,
            quality_size="2K",
        )
    with pytest.raises(ValueError, match="Unsupported image provider target"):
        compile_image_prompt(brief, provider_target="midjourney", candidate_index=1)


def test_candidate_workspace_paths_and_unit_key_parsing() -> None:
    request_id = uuid.uuid4()
    path = compiled_prompt_workspace_path(request_id, "candidate-01")
    assert path == f"workspace/deliverables/{request_id}/prompts/candidate-01.txt"
    assert (
        poster_v2_candidate_unit_key(
            f"workspace/deliverables/{request_id}/candidates/candidate-02.png"
        )
        == "candidate-02"
    )
    # Versioned durable outputs keep the unit binding.
    assert (
        poster_v2_candidate_unit_key(
            f"workspace/deliverables/{request_id}/candidates/candidate-02_ab12cd34ef56.png"
        )
        == "candidate-02"
    )
    assert poster_v2_candidate_unit_key(f"workspace/deliverables/{request_id}/final.png") is None
    assert poster_v2_candidate_unit_key("workspace/other/candidate-01.png") is None


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _statement):
        return _Result(self._results.pop(0))


class _FakeStorage:
    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs

    async def read_bytes(self, key):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        return self.blobs[key]


def _v2_request() -> DeliverableRequest:
    return DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        work_type="poster",
        workflow_id="builtin.poster.v2",
        workflow_version="2.0.0",
        goal="goal",
        inputs=[],
        spec={},
        tier="pro",
        approval_policy=["composition", "final"],
        output_contract=["png"],
        status="running",
        current_stage="running",
        version=1,
        contract_revision=1,
    )


def _execution_for(request: DeliverableRequest) -> DeliverableExecution:
    return DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=1,
        kind="initial",
        status="running",
        current_stage="running",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=request.client_request_id,
        request_fingerprint="b" * 64,
    )


def _candidate_unit(request, execution, unit_key="candidate-01") -> DeliverableExecutionUnit:
    return DeliverableExecutionUnit(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        stage_key="candidate_generate",
        unit_key=unit_key,
        status="pending",
        dependency_hash="c" * 64,
        attempt_count=0,
        input_snapshot={},
        result_snapshot={},
        quality_evaluation={},
    )


@pytest.mark.asyncio
async def test_unit_binding_is_none_for_non_v2_requests(monkeypatch) -> None:
    request = _v2_request()
    request.workflow_id = "builtin.poster.v1"
    monkeypatch.setattr(
        "app.services.prompt_compiler.async_session",
        lambda: _FakeSession([request]),
    )
    binding = await resolve_poster_v2_candidate_unit(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        request_id=request.id,
        save_path=f"workspace/deliverables/{request.id}/final.png",
        prompt="anything",
    )
    assert binding is None
    # And no request context at all short-circuits before any DB work.
    assert (
        await resolve_poster_v2_candidate_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=None,
            save_path="workspace/x.png",
            prompt="anything",
        )
        is None
    )


@pytest.mark.asyncio
async def test_unit_binding_rejects_non_candidate_paths(monkeypatch) -> None:
    request = _v2_request()
    monkeypatch.setattr(
        "app.services.prompt_compiler.async_session",
        lambda: _FakeSession([request]),
    )
    with pytest.raises(MediaContractError, match="candidates/candidate-NN"):
        await resolve_poster_v2_candidate_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=f"workspace/deliverables/{request.id}/final.png",
            prompt="anything",
        )


@pytest.mark.asyncio
async def test_unit_binding_enforces_the_verbatim_compiled_prompt(monkeypatch) -> None:
    request = _v2_request()
    execution = _execution_for(request)
    request.current_execution_id = execution.id
    unit = _candidate_unit(request, execution)
    compilation = DeliverablePromptCompilation(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        unit_id=unit.id,
        compiler_version=COMPILER_VERSION,
        brief_sha256="d" * 64,
        compiled_prompt_sha256="e" * 64,
        compiled_prompt_path=compiled_prompt_workspace_path(request.id, "candidate-01"),
        provider_target="volcengine_agent_plan",
    )
    monkeypatch.setattr(
        "app.services.prompt_compiler.async_session",
        lambda: _FakeSession([request, execution, unit, compilation]),
    )
    compiled_text = "compiled provider prompt body"
    from app.services.storage import agent_storage_key

    monkeypatch.setattr(
        "app.services.prompt_compiler.get_storage_backend",
        lambda: _FakeStorage(
            {agent_storage_key(request.agent_id, compilation.compiled_prompt_path): compiled_text.encode()}
        ),
    )

    with pytest.raises(MediaContractError, match="verbatim"):
        await resolve_poster_v2_candidate_unit(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            request_id=request.id,
            save_path=f"workspace/deliverables/{request.id}/candidates/candidate-01.png",
            prompt="rewritten by the agent",
        )

    binding = await resolve_poster_v2_candidate_unit(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        request_id=request.id,
        save_path=f"workspace/deliverables/{request.id}/candidates/candidate-01.png",
        prompt=compiled_text,
    )
    assert isinstance(binding, PosterV2CandidateBinding)
    assert binding.unit_id == unit.id
    assert binding.execution_id == execution.id


def _png_bytes(width: int = 768, height: int = 1024, text: str | None = None) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), (240, 244, 248))
    if text:
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, height // 2 - 90, width, height // 2 + 90], fill=(255, 255, 255))
        draw.text(
            (40, height // 2 - 50),
            text,
            fill=(10, 10, 10),
            font=ImageFont.load_default(size=64),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_candidate_qa_report_is_bound_to_artifact_sha256() -> None:
    data = _png_bytes()
    expected = hashlib.sha256(data).hexdigest()
    report = evaluate_image_candidate(
        data=data,
        unit_key="candidate-01",
        artifact_path="workspace/deliverables/x/candidates/candidate-01.png",
        expected_aspect_ratio="3:4",
        expected_copy_texts=("极光保温杯",),
    )
    assert report.artifact_sha256 == expected
    assert report.status == "passed"
    checks = {check.name: check.status for check in report.checks}
    assert checks["artifact_decodable"] == "passed"
    assert checks["aspect_ratio_match"] == "passed"
    # OCR checks are real when tesseract is installed and explicitly
    # unavailable otherwise; neither may be silently skipped.
    assert checks["no_pseudo_text"] in {"passed", "unavailable"}
    summary = qa_summary_from_evaluation({"candidate_qa": report.model_dump(mode="json")})
    assert summary is not None
    assert summary["artifact_sha256"] == expected
    assert summary["status"] == "passed"
    assert qa_summary_from_evaluation({}) is None


def test_candidate_qa_rejects_wrong_ratio_and_undecodable_bytes() -> None:
    bad_ratio = _png_bytes(width=1024, height=1024)
    report = evaluate_image_candidate(
        data=bad_ratio,
        unit_key="candidate-02",
        artifact_path="workspace/deliverables/x/candidates/candidate-02.png",
        expected_aspect_ratio="3:4",
    )
    assert report.status == "failed"
    checks = {check.name: check.status for check in report.checks}
    assert checks["aspect_ratio_match"] == "failed"

    corrupt = evaluate_image_candidate(
        data=b"not an image",
        unit_key="candidate-03",
        artifact_path="workspace/deliverables/x/candidates/candidate-03.png",
    )
    assert corrupt.status == "failed"
    assert corrupt.score == 0
    assert corrupt.artifact_sha256 == hashlib.sha256(b"not an image").hexdigest()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_candidate_qa_ocr_catches_pseudo_text_and_prohibited_terms() -> None:
    data = _png_bytes(text="SUPER SALE 90% OFF")
    report = evaluate_image_candidate(
        data=data,
        unit_key="candidate-01",
        artifact_path="workspace/deliverables/x/candidates/candidate-01.png",
        expected_aspect_ratio="3:4",
        expected_copy_texts=("极光保温杯",),
    )
    checks = {check.name: check.status for check in report.checks}
    assert checks["no_pseudo_text"] == "failed"
    assert report.status == "failed"

    clean = evaluate_image_candidate(
        data=data,
        unit_key="candidate-01",
        artifact_path="workspace/deliverables/x/candidates/candidate-01.png",
        expected_aspect_ratio="3:4",
        expected_copy_texts=("SUPER SALE 90% OFF",),
    )
    assert {check.name: check.status for check in clean.checks}["no_pseudo_text"] == "passed"

    watermarked = evaluate_image_candidate(
        data=data,
        unit_key="candidate-01",
        artifact_path="workspace/deliverables/x/candidates/candidate-01.png",
        expected_aspect_ratio="3:4",
        expected_copy_texts=("SUPER SALE 90% OFF",),
        prohibited_terms=("SALE",),
    )
    assert {
        check.name: check.status for check in watermarked.checks
    }["no_prohibited_terms"] == "failed"


def test_subject_similarity_placeholder_contract() -> None:
    data = _png_bytes()
    ahash = image_average_hash(data)
    assert len(ahash) == 16
    identical = subject_similarity(reference_ahash=ahash, candidate_ahash=ahash)
    assert identical["placeholder"] is True
    assert identical["score"] == 1.0
    missing_reference = subject_similarity(reference_ahash=None, candidate_ahash=ahash)
    assert missing_reference["status"] == "unavailable"
    assert missing_reference["score"] is None


def test_qa_enforcement_defaults_to_shadow() -> None:
    request = _v2_request()
    assert (
        candidate_qa_enforcement_for_request(
            request, mode="shadow", tenant_ids=str(request.tenant_id), agent_ids="",
        )
        == "shadow"
    )
    assert (
        candidate_qa_enforcement_for_request(
            request, mode="enforcing", tenant_ids="", agent_ids="",
        )
        == "shadow"
    )
    assert (
        candidate_qa_enforcement_for_request(
            request, mode="enforcing", tenant_ids=str(request.tenant_id), agent_ids="",
        )
        == "enforcing"
    )
