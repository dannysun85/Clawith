"""Pure deterministic checks for a confirmed Work result contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re

from app.services.agent_runtime.state import RuntimeGraphState
from app.services.product_information_architecture import (
    evaluate_product_navigation_claims,
)


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class WorkAcceptanceEvaluation:
    required: bool
    valid: bool
    passed: bool
    details: dict
    repair_reason: str | None = None


def _sequence_of_text(value: object) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return [str(item).strip() for item in value]


def _work_statement_from_state(state: RuntimeGraphState) -> object:
    snapshots = state.get("snapshots")
    initial_input = getattr(snapshots, "initial_input", None)
    if not isinstance(initial_input, Mapping):
        return None
    return initial_input.get("work_statement")


def _contract_from_state(state: RuntimeGraphState) -> object:
    work_statement = _work_statement_from_state(state)
    return (
        work_statement.get("acceptance_contract")
        if isinstance(work_statement, Mapping)
        else None
    )


def _result_length(candidate: str, unit: str) -> int:
    if unit == "characters":
        return len(candidate.strip())
    if unit == "cjk_characters":
        return len(_CJK_RE.findall(candidate))
    if unit == "words":
        return len(_WORD_RE.findall(candidate))
    raise ValueError("unsupported result length unit")


def evaluate_work_acceptance(
    state: RuntimeGraphState,
    candidate: str,
    *,
    evidence_refs: Sequence[str] = (),
) -> WorkAcceptanceEvaluation:
    """Evaluate only server-understood checks; semantic criteria stay for owner review."""

    raw_contract = _contract_from_state(state)
    if raw_contract is None:
        return WorkAcceptanceEvaluation(
            required=False,
            valid=True,
            passed=True,
            details={"code": "work_acceptance_not_required"},
        )
    if not isinstance(raw_contract, Mapping) or raw_contract.get("version") != 1:
        return WorkAcceptanceEvaluation(
            required=True,
            valid=False,
            passed=False,
            details={"code": "invalid_work_acceptance_contract"},
        )

    criteria = _sequence_of_text(raw_contract.get("criteria"))
    required_sections = _sequence_of_text(raw_contract.get("required_sections", []))
    forbidden_terms = _sequence_of_text(raw_contract.get("forbidden_terms", []))
    result_language = raw_contract.get("result_language", "auto")
    evidence_required = raw_contract.get("evidence_required", False)
    owner_review_required = raw_contract.get("owner_review_required", True)
    if (
        not criteria
        or required_sections is None
        or forbidden_terms is None
        or result_language not in {"auto", "zh-CN", "en"}
        or not isinstance(evidence_required, bool)
        or not isinstance(owner_review_required, bool)
    ):
        return WorkAcceptanceEvaluation(
            required=True,
            valid=False,
            passed=False,
            details={"code": "invalid_work_acceptance_contract"},
        )

    violations: list[dict] = []
    checks: list[dict] = []
    normalized_candidate = candidate.casefold()

    missing_sections = [
        section for section in required_sections if section.casefold() not in normalized_candidate
    ]
    checks.append(
        {
            "kind": "required_sections",
            "passed": not missing_sections,
            "expected": required_sections,
            "missing": missing_sections,
        }
    )
    if missing_sections:
        violations.append({"kind": "required_sections", "missing": missing_sections})

    present_forbidden_terms = [
        term for term in forbidden_terms if term.casefold() in normalized_candidate
    ]
    checks.append(
        {
            "kind": "forbidden_terms",
            "passed": not present_forbidden_terms,
            "present": present_forbidden_terms,
        }
    )
    if present_forbidden_terms:
        violations.append(
            {"kind": "forbidden_terms", "present": present_forbidden_terms}
        )

    raw_length = raw_contract.get("length")
    if raw_length is not None:
        if not isinstance(raw_length, Mapping):
            return WorkAcceptanceEvaluation(
                required=True,
                valid=False,
                passed=False,
                details={"code": "invalid_work_acceptance_contract"},
            )
        unit = raw_length.get("unit", "characters")
        minimum = raw_length.get("minimum")
        maximum = raw_length.get("maximum")
        if (
            unit not in {"characters", "cjk_characters", "words"}
            or (minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int)))
            or (maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int)))
            or (minimum is None and maximum is None)
        ):
            return WorkAcceptanceEvaluation(
                required=True,
                valid=False,
                passed=False,
                details={"code": "invalid_work_acceptance_contract"},
            )
        actual = _result_length(candidate, str(unit))
        length_passed = (minimum is None or actual >= minimum) and (
            maximum is None or actual <= maximum
        )
        length_check = {
            "kind": "length",
            "passed": length_passed,
            "unit": unit,
            "minimum": minimum,
            "maximum": maximum,
            "actual": actual,
        }
        checks.append(length_check)
        if not length_passed:
            violations.append(length_check)

    language_passed = True
    if result_language == "zh-CN":
        language_passed = bool(_CJK_RE.search(candidate))
    elif result_language == "en":
        language_passed = bool(_WORD_RE.search(candidate))
    checks.append(
        {
            "kind": "result_language",
            "passed": language_passed,
            "expected": result_language,
        }
    )
    if not language_passed:
        violations.append({"kind": "result_language", "expected": result_language})

    evidence_passed = not evidence_required or bool(evidence_refs)
    checks.append(
        {
            "kind": "evidence",
            "passed": evidence_passed,
            "required": evidence_required,
            "reference_count": len(evidence_refs),
        }
    )
    if not evidence_passed:
        violations.append({"kind": "evidence", "required": True})

    work_statement = _work_statement_from_state(state)
    product_navigation = evaluate_product_navigation_claims(
        (
            work_statement.get("product_information_architecture")
            if isinstance(work_statement, Mapping)
            else None
        ),
        candidate,
    )
    if not product_navigation.valid:
        return WorkAcceptanceEvaluation(
            required=True,
            valid=False,
            passed=False,
            details=product_navigation.details,
        )
    checks.append(
        {
            "kind": "product_navigation",
            "passed": product_navigation.passed,
            **product_navigation.details,
        }
    )
    if not product_navigation.passed:
        violations.append(
            {
                "kind": "product_navigation",
                **product_navigation.details,
            }
        )

    passed = not violations
    details = {
        "code": "work_acceptance_passed" if passed else "work_acceptance_failed",
        "contract_version": 1,
        "checks": checks,
        "violations": violations,
        "criteria": criteria,
        "owner_review_required": owner_review_required,
        "semantic_criteria_status": (
            "pending_owner_review" if owner_review_required else "not_required"
        ),
    }
    repair_reason = None
    if not passed:
        repair_reason = product_navigation.repair_reason or (
            "The result does not satisfy the confirmed Work acceptance contract. "
            f"Repair every deterministic violation and return one complete final result: {violations}"
        )
    return WorkAcceptanceEvaluation(
        required=True,
        valid=True,
        passed=passed,
        details=details,
        repair_reason=repair_reason,
    )


__all__ = ["WorkAcceptanceEvaluation", "evaluate_work_acceptance"]
