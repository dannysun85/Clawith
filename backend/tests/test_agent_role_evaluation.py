from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from app.api.agent_workforce import AgentTemplateEnableBatchIn
from app.models.agent import AgentTemplateEvaluation
from app.services.agent_candidate_templates import load_candidate_template_seeds
from app.services.agent_role_evaluation import (
    EVALUATOR_VERSION,
    METRIC_NAMES,
    activation_gate_reasons,
    evaluate_candidate_metrics,
    expected_role_family,
    load_evaluation_fixtures,
    validate_fixture_results,
)


def _metrics(**overrides):
    metrics = {
        "task_success_rate": 0.8,
        "first_effective_output_seconds": 100,
        "clarification_turns": 2,
        "tool_success_rate": 0.9,
        "human_edit_ratio": 0.3,
        "elapsed_seconds": 300,
        "tokens_used": 1000,
    }
    metrics.update(overrides)
    return metrics


def test_fixture_set_is_versioned_anonymized_and_covers_five_families() -> None:
    payload = load_evaluation_fixtures()

    assert payload["fixture_set_version"] == "agent-role-ab-v1"
    assert payload["privacy"] == "synthetic_anonymized_no_customer_data"
    assert set(payload["families"]) == {
        "engineering",
        "content",
        "coordination",
        "marketing",
        "sales",
    }
    assert len(payload["fixtures"]) == 10
    assert all(item["forbidden_actions"] for item in payload["fixtures"])


def test_candidate_passes_only_when_safety_capability_and_metrics_hold() -> None:
    decision = evaluate_candidate_metrics(
        baseline=_metrics(),
        candidate=_metrics(
            task_success_rate=0.9,
            first_effective_output_seconds=80,
            clarification_turns=1,
            tool_success_rate=0.95,
            human_edit_ratio=0.2,
            elapsed_seconds=250,
            tokens_used=950,
        ),
        safety_pass=True,
        capability_pass=True,
    )

    assert EVALUATOR_VERSION == "agent-role-gate-v1"
    assert decision.status == "passed"
    assert decision.reasons == ()


def test_candidate_fails_on_quality_cost_or_safety_regression() -> None:
    decision = evaluate_candidate_metrics(
        baseline=_metrics(),
        candidate=_metrics(
            task_success_rate=0.7,
            human_edit_ratio=0.5,
            tokens_used=1200,
        ),
        safety_pass=False,
        capability_pass=True,
    )

    assert decision.status == "failed"
    assert {
        "safety_gate_failed",
        "task_success_regressed",
        "task_success_below_0_75",
        "human_edit_ratio_regressed",
        "tokens_used_regressed_over_10_percent",
    } <= set(decision.reasons)


def test_metric_contract_rejects_missing_or_out_of_range_values() -> None:
    missing = _metrics()
    missing.pop("tokens_used")
    with pytest.raises(ValueError, match="metric contract mismatch"):
        evaluate_candidate_metrics(
            baseline=missing,
            candidate=_metrics(),
            safety_pass=True,
            capability_pass=True,
        )
    with pytest.raises(ValueError, match="between 0 and 1"):
        evaluate_candidate_metrics(
            baseline=_metrics(),
            candidate=_metrics(tool_success_rate=1.1),
            safety_pass=True,
            capability_pass=True,
        )
    assert set(METRIC_NAMES) == set(_metrics())


def test_fixture_results_require_exact_family_cases_and_evidence() -> None:
    results = [
        {
            "fixture_id": "engineering-api-regression",
            "status": "completed",
            "evidence_refs": ["artifact://eval/1"],
        },
        {
            "fixture_id": "engineering-review-risk",
            "status": "completed",
            "evidence_refs": ["artifact://eval/2"],
        },
    ]
    validate_fixture_results("engineering", results)

    with pytest.raises(ValueError, match="fixture results mismatch"):
        validate_fixture_results("sales", results)


def test_every_candidate_pack_maps_to_a_fixed_evaluation_family() -> None:
    expected = {
        "engineering-candidates": "engineering",
        "marketing-candidates": "marketing",
        "sales-candidates": "sales",
        "project-candidates": "coordination",
        "specialized-candidates": "content",
    }
    assert {pack: expected_role_family(pack) for pack in expected} == expected
    mapped = {
        expected_role_family(template["workforce_pack"])
        for template in load_candidate_template_seeds()
    }
    assert mapped == {
        "engineering",
        "content",
        "coordination",
        "marketing",
        "sales",
    }


def test_evaluation_model_is_revision_bound_and_auditable() -> None:
    columns = AgentTemplateEvaluation.__table__.columns

    assert {
        "template_id",
        "role_revision",
        "fixture_set_version",
        "baseline_metrics",
        "candidate_metrics",
        "gate_status",
        "gate_reasons",
        "promoted_at",
        "rolled_back_at",
    } <= set(columns.keys())


def test_batch_activation_is_fail_closed_and_limited_to_ten() -> None:
    template = SimpleNamespace(
        lifecycle_status="candidate_disabled",
        workforce_decision="add_candidate",
    )
    passed = SimpleNamespace(
        gate_status="passed",
        safety_pass=True,
        capability_pass=True,
        rolled_back_at=None,
    )

    assert activation_gate_reasons(template, passed, contract_ready=True) == ()
    assert activation_gate_reasons(template, None, contract_ready=True) == (
        "evaluation_missing",
    )
    assert activation_gate_reasons(template, passed, contract_ready=False) == (
        "capability_contract_not_ready",
    )
    with pytest.raises(ValidationError):
        AgentTemplateEnableBatchIn(template_ids=[uuid.uuid4() for _ in range(11)])
