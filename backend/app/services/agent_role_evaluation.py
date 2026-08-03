"""Offline A/B metric contract and activation gate for Agent roles."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "agent_role_evaluation_fixtures.v1.json"
)
EVALUATOR_VERSION = "agent-role-gate-v1"
METRIC_NAMES = (
    "task_success_rate",
    "first_effective_output_seconds",
    "clarification_turns",
    "tool_success_rate",
    "human_edit_ratio",
    "elapsed_seconds",
    "tokens_used",
)
RATE_METRICS = {"task_success_rate", "tool_success_rate", "human_edit_ratio"}
PACK_ROLE_FAMILIES = {
    "engineering-candidates": "engineering",
    "design-candidates": "engineering",
    "quality-candidates": "engineering",
    "marketing-candidates": "marketing",
    "sales-candidates": "sales",
    "product-candidates": "coordination",
    "project-candidates": "coordination",
    "people-candidates": "coordination",
    "support-candidates": "coordination",
    "finance-candidates": "coordination",
    "specialized-candidates": "content",
}


@dataclass(frozen=True)
class EvaluationDecision:
    status: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def load_evaluation_fixtures() -> dict:
    """Load and validate the synthetic, versioned evaluation set."""
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures") or []
    families = set(payload.get("families") or [])
    fixture_ids = {item.get("id") for item in fixtures}
    if payload.get("privacy") != "synthetic_anonymized_no_customer_data":
        raise ValueError("evaluation fixtures must be synthetic and anonymized")
    if len(fixtures) != 10 or len(fixture_ids) != 10:
        raise ValueError("evaluation fixture set must contain 10 unique cases")
    if {item.get("family") for item in fixtures} != families:
        raise ValueError("every declared evaluation family must have fixtures")
    if any(not item.get("required_evidence") for item in fixtures):
        raise ValueError("every fixture must define required evidence")
    return payload


def _validated_metrics(metrics: Mapping[str, float], label: str) -> dict[str, float]:
    if set(metrics) != set(METRIC_NAMES):
        missing = sorted(set(METRIC_NAMES) - set(metrics))
        extra = sorted(set(metrics) - set(METRIC_NAMES))
        raise ValueError(f"{label} metric contract mismatch missing={missing} extra={extra}")
    normalized = {name: float(metrics[name]) for name in METRIC_NAMES}
    for name, value in normalized.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label}.{name} must be finite and non-negative")
        if name in RATE_METRICS and value > 1:
            raise ValueError(f"{label}.{name} must be between 0 and 1")
    return normalized


def evaluate_candidate_metrics(
    *,
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    safety_pass: bool,
    capability_pass: bool,
) -> EvaluationDecision:
    """Return a deterministic gate result without invoking any Provider."""
    old = _validated_metrics(baseline, "baseline")
    new = _validated_metrics(candidate, "candidate")
    reasons: list[str] = []
    if not safety_pass:
        reasons.append("safety_gate_failed")
    if not capability_pass:
        reasons.append("capability_gate_failed")
    if new["task_success_rate"] < old["task_success_rate"]:
        reasons.append("task_success_regressed")
    if new["task_success_rate"] < 0.75:
        reasons.append("task_success_below_0_75")
    if new["tool_success_rate"] < old["tool_success_rate"]:
        reasons.append("tool_success_regressed")
    if new["human_edit_ratio"] > old["human_edit_ratio"]:
        reasons.append("human_edit_ratio_regressed")
    if new["clarification_turns"] > old["clarification_turns"] + 0.5:
        reasons.append("clarification_turns_regressed")
    for metric in ("first_effective_output_seconds", "elapsed_seconds", "tokens_used"):
        tolerance = old[metric] * 1.10 if old[metric] else 0
        if new[metric] > tolerance:
            reasons.append(f"{metric}_regressed_over_10_percent")
    return EvaluationDecision(
        status="passed" if not reasons else "failed",
        reasons=tuple(reasons),
    )


def validate_fixture_results(role_family: str, fixture_results: list[dict]) -> None:
    """Require complete evidence from the two fixtures for one role family."""
    payload = load_evaluation_fixtures()
    required = {
        item["id"] for item in payload["fixtures"] if item["family"] == role_family
    }
    if not required:
        raise ValueError(f"unknown role family: {role_family}")
    supplied = {str(item.get("fixture_id") or "") for item in fixture_results}
    if supplied != required:
        raise ValueError(
            f"fixture results mismatch required={sorted(required)} supplied={sorted(supplied)}"
        )
    if any(item.get("status") != "completed" for item in fixture_results):
        raise ValueError("all fixture results must be completed")
    if any(not item.get("evidence_refs") for item in fixture_results):
        raise ValueError("every fixture result must include evidence_refs")


def expected_role_family(workforce_pack: str | None) -> str:
    try:
        return PACK_ROLE_FAMILIES[str(workforce_pack)]
    except KeyError as exc:
        raise ValueError(f"workforce pack has no evaluation family: {workforce_pack}") from exc


def activation_gate_reasons(
    template: object | None,
    evaluation: object | None,
    *,
    contract_ready: bool,
) -> tuple[str, ...]:
    """Return deterministic fail-closed reasons for one batch promotion item."""
    if template is None:
        return ("template_not_found",)
    reasons: list[str] = []
    if getattr(template, "lifecycle_status", None) != "candidate_disabled":
        reasons.append("template_not_candidate_disabled")
    if getattr(template, "workforce_decision", None) != "add_candidate":
        reasons.append("template_not_add_candidate")
    if not contract_ready:
        reasons.append("capability_contract_not_ready")
    if evaluation is None:
        reasons.append("evaluation_missing")
    elif getattr(evaluation, "gate_status", None) != "passed":
        reasons.append("evaluation_gate_failed")
    elif not getattr(evaluation, "safety_pass", False) or not getattr(
        evaluation, "capability_pass", False
    ):
        reasons.append("evaluation_safety_or_capability_failed")
    elif getattr(evaluation, "rolled_back_at", None) is not None:
        reasons.append("evaluation_was_rolled_back")
    return tuple(reasons)


__all__ = [
    "EVALUATOR_VERSION",
    "METRIC_NAMES",
    "activation_gate_reasons",
    "evaluate_candidate_metrics",
    "expected_role_family",
    "load_evaluation_fixtures",
    "validate_fixture_results",
]
