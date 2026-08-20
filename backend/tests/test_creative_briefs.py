"""FR-I1/FR-I3 contracts for structured creative briefs and the v2 poster seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from pydantic import ValidationError

from app.models.deliverable import (
    DeliverableCreativeBrief,
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverablePromptCompilation,
    DeliverableRequest,
)
from app.services import deliverable_workflows
from app.services.creative_briefs import (
    CREATIVE_BRIEF_SCHEMA_VERSION,
    PRESENTATION_BRIEF_SCHEMA_VERSION,
    VIDEO_BRIEF_SCHEMA_VERSION,
    CreativeBrief,
    brief_sha256,
    candidate_count_for_policy,
    compile_creative_brief,
    poster_v2_rollout_allowed,
)
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    build_deliverable_prompt,
    preflight_workflow,
    prepare_deliverable_launch,
    require_workflow,
    validate_workflow_spec,
)


def test_registered_brief_schema_versions_fit_persisted_column() -> None:
    column = DeliverableCreativeBrief.__table__.c.schema_version
    max_length = column.type.length

    assert max_length is not None
    assert max(
        len(CREATIVE_BRIEF_SCHEMA_VERSION),
        len(VIDEO_BRIEF_SCHEMA_VERSION),
        len(PRESENTATION_BRIEF_SCHEMA_VERSION),
    ) <= max_length


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


def _v2_spec(**overrides) -> dict:
    spec = {
        "channel": "social",
        "aspect_ratio": "3:4",
        "style": "commercial",
        "audience": "25-35 岁都市白领",
        "exact_copy_blocks": [{"role": "title", "text": "极光保温杯"}],
        "fallback_policy": "primary_only",
        "redraw_scope": "full_creative",
    }
    spec.update(overrides)
    return spec


def _v2_request(*, spec: dict | None = None, tier: str = "pro") -> DeliverableRequest:
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
        goal="为新款极光保温杯制作抖音投放海报",
        inputs=[],
        spec=spec if spec is not None else _v2_spec(),
        tier=tier,
        approval_policy=["composition", "final"],
        output_contract=["png"],
        status="ready",
        current_stage="brief_confirmed",
        version=1,
        contract_revision=1,
    )


def test_complete_brief_compiles_with_deterministic_hash() -> None:
    brief, missing = compile_creative_brief(
        "为新款极光保温杯制作抖音投放海报",
        _v2_spec(),
        [],
        tier="pro",
    )
    assert missing == ()
    assert brief is not None
    assert brief.audience == "25-35 岁都市白领"
    assert brief.candidate_policy.effective == 2
    assert brief.exact_copy_blocks[0].role == "title"
    assert brief_sha256(brief) == brief_sha256(brief)
    assert len(brief_sha256(brief)) == 64


def test_missing_elements_are_reported_never_invented() -> None:
    brief, missing = compile_creative_brief(
        "",
        {"style": "commercial"},
        [],
        tier="lite",
    )
    assert brief is None
    assert "purpose" in missing
    assert "channel" in missing
    assert "audience" in missing
    assert "aspect_ratio" in missing


def test_brief_schema_fails_closed_on_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreativeBrief(
            purpose="p",
            channel="social",
            audience="a",
            aspect_ratio="3:4",
            style="commercial",
            candidate_policy={"tier": "pro", "tier_default": 2, "requested": None, "effective": 2},
            provider="minimax",  # type: ignore[call-arg]
        )


def test_candidate_policy_is_tier_bound_and_only_tunable_down() -> None:
    assert candidate_count_for_policy("lite", {}) == 1
    assert candidate_count_for_policy("pro", {}) == 2
    assert candidate_count_for_policy("ultra", {}) == 3
    assert candidate_count_for_policy("pro", {"candidate_count": 4}) == 2
    assert candidate_count_for_policy("ultra", {"candidate_count": 1}) == 1
    assert candidate_count_for_policy("lite", {"candidate_count": 3}) == 1
    assert candidate_count_for_policy("pro", {"candidate_count": True}) == 2


def test_structured_copy_blocks_take_precedence_over_textarea() -> None:
    brief, missing = compile_creative_brief(
        "goal",
        _v2_spec(
            exact_copy="textarea 文案",
            exact_copy_blocks=[{"role": "cta", "text": "立即抢购"}],
        ),
        [],
        tier="pro",
    )
    assert missing == ()
    assert brief is not None
    assert [(block.role, block.text) for block in brief.exact_copy_blocks] == [
        ("cta", "立即抢购")
    ]


def test_reference_asset_kinds_and_invalid_entries() -> None:
    brief, missing = compile_creative_brief(
        "goal",
        _v2_spec(
            reference_assets=[
                {"path": "workspace/uploads/product.png", "kind": "exact_asset"},
                {"path": "workspace/uploads/mood.png", "kind": "creative_reference"},
            ]
        ),
        [{"type": "workspace_file", "path": "workspace/uploads/extra.jpg"}],
        tier="pro",
    )
    assert missing == ()
    assert brief is not None
    kinds = {asset.path: asset.kind for asset in brief.reference_assets}
    assert kinds["workspace/uploads/product.png"] == "exact_asset"
    assert kinds["workspace/uploads/mood.png"] == "creative_reference"
    # Image inputs become creative references without duplicating spec entries.
    assert kinds["workspace/uploads/extra.jpg"] == "creative_reference"

    brief, missing = compile_creative_brief(
        "goal",
        _v2_spec(reference_assets=[{"path": "workspace/uploads/x.png", "kind": "bogus"}]),
        [],
        tier="pro",
    )
    assert brief is None
    assert "reference_assets[0].kind" in missing


def test_rollout_allowlist_defaults_closed() -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assert not poster_v2_rollout_allowed(
        tenant_id=tenant_id, agent_id=agent_id,
        enabled=False, tenant_ids=str(tenant_id), agent_ids=str(agent_id),
    )
    assert not poster_v2_rollout_allowed(
        tenant_id=tenant_id, agent_id=agent_id,
        enabled=True, tenant_ids="", agent_ids="",
    )
    assert poster_v2_rollout_allowed(
        tenant_id=tenant_id, agent_id=uuid.uuid4(),
        enabled=True, tenant_ids=str(tenant_id), agent_ids="",
    )
    assert poster_v2_rollout_allowed(
        tenant_id=uuid.uuid4(), agent_id=agent_id,
        enabled=True, tenant_ids="", agent_ids=f" {agent_id} ,",
    )


def test_v2_manifest_registered_and_v1_stays_default() -> None:
    assert deliverable_workflows.WORKFLOW_BY_TYPE["poster"].workflow_id == "builtin.poster.v1"
    v2 = require_workflow("poster", "builtin.poster.v2", "2.0.0")
    assert v2.workflow_version == "2.0.0"
    v1 = require_workflow("poster", "builtin.poster.v1", "1.0.0")
    assert v1.workflow_id == "builtin.poster.v1"
    with pytest.raises(DeliverableWorkflowError):
        require_workflow("poster", "builtin.poster.v2", "1.0.0")


def test_v2_spec_accepts_structured_json_fields_and_fails_closed() -> None:
    v2 = require_workflow("poster", "builtin.poster.v2", "2.0.0")
    spec = validate_workflow_spec(v2, _v2_spec(candidate_count=4))
    assert spec["candidate_count"] == 4
    assert spec["exact_copy_blocks"] == [{"role": "title", "text": "极光保温杯"}]
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v2, {**_v2_spec(), "exact_copy_blocks": "not-json{"})
    v1 = require_workflow("poster", "builtin.poster.v1", "1.0.0")
    with pytest.raises(DeliverableWorkflowError):
        validate_workflow_spec(v1, {"channel": "social", "candidate_count": 2})


def _mock_preflight_capability(monkeypatch) -> None:
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
                {
                    "modality": "image",
                    "available": True,
                    "reason": None,
                    "available_providers": ["volcengine_agent_plan", "minimax"],
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_incomplete_brief_is_blocked_with_zero_provider_submission(monkeypatch) -> None:
    _mock_preflight_capability(monkeypatch)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.poster_v2_workflow_allowed",
        lambda tenant_id, agent_id: True,
    )
    workflow = require_workflow("poster", "builtin.poster.v2", "2.0.0")
    spec = _v2_spec()
    spec.pop("audience")

    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=spec,
        goal="为新款极光保温杯制作抖音投放海报",
    )

    assert result["launchable"] is False
    assert "brief_missing:audience" in result["reasons"]
    assert result["creative_brief"]["status"] == "clarifying"
    assert result["creative_brief"]["missing_fields"] == ["audience"]

    # Launching a clarifying request must fail before any media task exists.
    from app.services import media_generation

    async def _forbidden_submission(*_args, **_kwargs):
        raise AssertionError("provider submission must never happen for a clarifying brief")

    monkeypatch.setattr(
        media_generation,
        "create_minimax_sync_media_task_record",
        _forbidden_submission,
    )
    request = _v2_request(spec=spec)
    db = _Session(request)
    with pytest.raises(DeliverableWorkflowError, match="brief_missing:audience"):
        await prepare_deliverable_launch(
            db,  # type: ignore[arg-type]
            request_id=request.id,
            tenant_id=request.tenant_id,
            user_id=request.created_by_user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            message_id=uuid.uuid4(),
        )
    assert not any(
        isinstance(item, DeliverablePromptCompilation) for item in db.added
    )


@pytest.mark.asyncio
async def test_confirmed_brief_launch_compiles_candidate_prompts(monkeypatch) -> None:
    _mock_preflight_capability(monkeypatch)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.poster_v2_workflow_allowed",
        lambda tenant_id, agent_id: True,
    )
    writes: dict[str, bytes] = {}

    class _FakeStorage:
        async def write_bytes(self, key, data, content_type=None):
            writes[key] = data

    monkeypatch.setattr(
        "app.services.prompt_compiler.get_storage_backend",
        lambda: _FakeStorage(),
    )

    request = _v2_request()
    execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=1,
        kind="initial",
        status="ready",
        current_stage="brief_confirmed",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=request.client_request_id,
        request_fingerprint="b" * 64,
    )
    request.current_execution_id = execution.id
    units = [
        DeliverableExecutionUnit(
            id=uuid.uuid4(),
            tenant_id=request.tenant_id,
            request_id=request.id,
            execution_id=execution.id,
            stage_key="candidate_generate",
            unit_key=f"candidate-{index:02d}",
            status="pending",
            dependency_hash="c" * 64,
            attempt_count=0,
            input_snapshot={},
            result_snapshot={},
            quality_evaluation={},
        )
        for index in (1, 2)
    ]
    db = _Session(request, execution, units, None)

    prepared = await prepare_deliverable_launch(
        db,  # type: ignore[arg-type]
        request_id=request.id,
        tenant_id=request.tenant_id,
        user_id=request.created_by_user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        message_id=uuid.uuid4(),
    )

    compilations = [
        item for item in db.added if isinstance(item, DeliverablePromptCompilation)
    ]
    assert len(compilations) == 2
    assert {item.compiler_version for item in compilations} == {"image-v1"}
    assert all(len(item.compiled_prompt_sha256) == 64 for item in compilations)
    assert {item.provider_target for item in compilations} == {"volcengine_agent_plan"}
    briefs = [item for item in db.added if isinstance(item, DeliverableCreativeBrief)]
    assert len(briefs) == 1
    assert briefs[0].status == "confirmed"
    assert len(writes) == 2
    assert any("prompts/candidate-01.txt" in key for key in writes)
    prompt_texts = {value.decode("utf-8") for value in writes.values()}
    # Two candidates, two distinct deterministic compositions.
    assert len(prompt_texts) == 2
    assert "prompts/candidate-01.txt" in prepared.prompt
    assert "prompts/candidate-02.txt" in prepared.prompt
    assert "verbatim" in prepared.prompt
    assert request.status == "running"


def test_v1_poster_prompt_contract_is_unchanged_when_rollout_is_off() -> None:
    v1_request = _v2_request()
    v1_request.workflow_id = "builtin.poster.v1"
    v1_request.workflow_version = "1.0.0"
    v1_request.spec = {
        "channel": "social",
        "aspect_ratio": "3:4",
        "style": "commercial",
        "exact_copy": "极光保温杯",
    }
    prompt = build_deliverable_prompt(v1_request)
    assert "Create one polished commercial image" in prompt
    assert "candidates/candidate-" not in prompt
    assert "prompts/candidate-" not in prompt

    v2_prompt = build_deliverable_prompt(_v2_request())
    assert "v2 multi-candidate poster pipeline" in v2_prompt
    assert "candidates/candidate-01.png" in v2_prompt
    assert "candidates/candidate-02.png" in v2_prompt
    assert "never write, rewrite, extend, translate, or summarize" in v2_prompt
    assert "Create one polished commercial image" not in v2_prompt


@pytest.mark.asyncio
async def test_launchable_workflows_follow_the_rollout_allowlist(monkeypatch) -> None:
    from app.services.deliverable_workflows import list_agent_launchable_workflows

    monkeypatch.setattr(
        "app.services.deliverable_workflows.resolve_route",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._agent_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._presentation_tool_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.deliverable_workflows._video_post_production_tools_available",
        AsyncMock(return_value=True),
    )

    allowed = {"value": False}
    monkeypatch.setattr(
        "app.services.deliverable_workflows.poster_v2_workflow_allowed",
        lambda tenant_id, agent_id: allowed["value"],
    )

    default_listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    default_ids = [workflow.workflow_id for workflow in default_listing]
    assert "builtin.poster.v1" in default_ids
    assert "builtin.poster.v2" not in default_ids

    allowed["value"] = True
    canary_listing = await list_agent_launchable_workflows(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tier="pro",
    )
    canary_ids = [workflow.workflow_id for workflow in canary_listing]
    # Exactly one poster workflow is ever listed: v2 replaces v1 for the
    # allowlisted canary and disappears entirely otherwise.
    assert "builtin.poster.v2" in canary_ids
    assert "builtin.poster.v1" not in canary_ids


@pytest.mark.asyncio
async def test_v2_preflight_requires_the_rollout_allowlist(monkeypatch) -> None:
    _mock_preflight_capability(monkeypatch)
    monkeypatch.setattr(
        "app.services.deliverable_workflows.poster_v2_workflow_allowed",
        lambda tenant_id, agent_id: False,
    )
    workflow = require_workflow("poster", "builtin.poster.v2", "2.0.0")
    result = await preflight_workflow(
        SimpleNamespace(),  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow=workflow,
        tier="pro",
        spec=_v2_spec(),
        goal="为新款极光保温杯制作抖音投放海报",
    )
    assert result["launchable"] is False
    assert "deliverable_poster_v2_not_allowlisted" in result["reasons"]
