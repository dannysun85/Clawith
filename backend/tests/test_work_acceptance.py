from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.work import WorkAcceptanceContract, WorkResultLengthContract
from app.services.agent_runtime.node_executor import DeterministicRuntimeVerifier
from app.services.agent_runtime.state import RunInputSnapshots
from app.services.agent_runtime.work_acceptance import evaluate_work_acceptance
from app.services.product_information_architecture import (
    product_information_architecture_snapshot,
)


def _state(contract: dict, *, product_ia: dict | None = None) -> dict:
    work_statement = {
        "version": 2,
        "acceptance_contract": contract,
    }
    if product_ia is not None:
        work_statement["product_information_architecture"] = product_ia
    return {
        "snapshots": RunInputSnapshots(
            session_context={},
            session_context_version=1,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={
                "work_statement": work_statement
            },
        ),
        "messages": [],
        "lifecycle": {"status": "verifying", "next_route": "verify"},
    }


def _contract(**overrides) -> dict:
    value = {
        "version": 1,
        "criteria": ["给出可执行方案"],
        "required_sections": ["结论", "行动计划"],
        "forbidden_terms": ["propose_experience_draft"],
        "result_language": "zh-CN",
        "length": {"unit": "cjk_characters", "minimum": 12, "maximum": 80},
        "evidence_required": False,
        "owner_review_required": True,
    }
    value.update(overrides)
    return value


def test_work_acceptance_passes_only_when_all_deterministic_checks_pass() -> None:
    candidate = "结论：建议先小范围上线。行动计划：第一周验证真实用户反馈并复盘。"

    result = evaluate_work_acceptance(_state(_contract()), candidate)  # type: ignore[arg-type]

    assert result.valid is True
    assert result.passed is True
    assert result.details["semantic_criteria_status"] == "pending_owner_review"


def test_work_acceptance_reports_length_sections_forbidden_terms_and_evidence() -> None:
    result = evaluate_work_acceptance(
        _state(_contract(evidence_required=True)),  # type: ignore[arg-type]
        "结论：propose_experience_draft",
    )

    assert result.valid is True
    assert result.passed is False
    assert {item["kind"] for item in result.details["violations"]} == {
        "required_sections",
        "forbidden_terms",
        "length",
        "evidence",
    }


def test_work_acceptance_repairs_an_invented_product_breadcrumb() -> None:
    candidate = "结论：进入工作台 → 报告中心。行动计划：按页面提示生成周报并提交审核。"

    result = evaluate_work_acceptance(
        _state(
            _contract(),
            product_ia=product_information_architecture_snapshot(),
        ),  # type: ignore[arg-type]
        candidate,
    )

    assert result.valid is True
    assert result.passed is False
    violation = next(
        item for item in result.details["violations"] if item["kind"] == "product_navigation"
    )
    assert violation["invalid_claims"] == [["工作台", "报告中心"]]
    assert "does not exist" in str(result.repair_reason)


@pytest.mark.asyncio
async def test_runtime_verifier_repairs_a_noncompliant_work_result() -> None:
    result = await DeterministicRuntimeVerifier().verify(
        _state(_contract()),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        "只有结论",
    )

    assert result.outcome == "repair"
    assert result.details["code"] == "work_acceptance_failed"


def test_inline_length_contract_rejects_requests_that_need_a_formal_deliverable() -> None:
    with pytest.raises(ValidationError, match="formal Deliverable"):
        WorkResultLengthContract(
            unit="cjk_characters",
            minimum=650,
            maximum=3000,
        )


def test_acceptance_contract_normalizes_and_requires_real_criteria() -> None:
    contract = WorkAcceptanceContract(criteria=["  可执行  ", "可执行"])
    assert contract.criteria == ["可执行"]

    with pytest.raises(ValidationError, match="acceptance criterion"):
        WorkAcceptanceContract(criteria=["   "])
