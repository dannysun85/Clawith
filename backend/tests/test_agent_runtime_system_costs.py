"""Unit contracts for Planning's platform system-cost authority."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from app.models.agent_run import LLMSystemCostReceipt
from app.services.agent_runtime.system_costs import (
    PlanningCostAccountingError,
    PlanningSystemCostService,
    planning_budget_reservation_credits,
    planning_request_token_upper_bound,
)
from app.services.token_tracker import TokenUsage


class _ReceiptSession:
    def __init__(self, receipt: LLMSystemCostReceipt) -> None:
        self.receipt = receipt
        self.commits = 0

    async def get(self, _model, receipt_id, **_kwargs):
        return self.receipt if receipt_id == self.receipt.id else None

    async def commit(self) -> None:
        self.commits += 1


def _service(receipt: LLMSystemCostReceipt):
    session = _ReceiptSession(receipt)

    @asynccontextmanager
    async def factory():
        yield session

    return PlanningSystemCostService(factory), session  # type: ignore[arg-type]


def _inflight_receipt() -> LLMSystemCostReceipt:
    return LLMSystemCostReceipt(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        call_index=1,
        operation="group_planning",
        model_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-M3",
        provider_service_tier="standard",
        request_fingerprint="a" * 64,
        status="provider_inflight",
        provider_outcome="pending",
        usage_source="pending",
        cost_status="pending",
    )


@pytest.mark.asyncio
async def test_finalized_receipt_fingerprint_includes_usage_and_rejects_drift() -> None:
    receipt = _inflight_receipt()
    service, session = _service(receipt)
    first_usage = TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120)

    await service.finalize(
        receipt.id,
        content="plan",
        tool_calls=(),
        usage=first_usage,
        finish_reason="stop",
    )
    await service.finalize(
        receipt.id,
        content="plan",
        tool_calls=(),
        usage=first_usage,
        finish_reason="stop",
    )

    with pytest.raises(PlanningCostAccountingError) as exc:
        await service.finalize(
            receipt.id,
            content="plan",
            tool_calls=(),
            usage=TokenUsage(input_tokens=101, output_tokens=20, total_tokens=121),
            finish_reason="stop",
        )

    assert exc.value.code == "planning_cost_idempotency_conflict"
    assert receipt.status == "finalized"
    assert receipt.input_tokens == 100
    assert receipt.system_cost_credits == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_and_never_priced_as_exact_zero() -> None:
    receipt = _inflight_receipt()
    service, _session = _service(receipt)

    await service.finalize(
        receipt.id,
        content="plan",
        tool_calls=(),
        usage=TokenUsage(),
        finish_reason="stop",
    )

    assert receipt.status == "finalized"
    assert receipt.provider_outcome == "accepted"
    assert receipt.usage_source == "unknown"
    assert receipt.cost_status == "unpriced"
    assert receipt.system_cost_credits is None


@pytest.mark.asyncio
async def test_response_snapshot_is_json_safe_before_the_provider_debt_commit() -> None:
    receipt = _inflight_receipt()
    service, _session = _service(receipt)
    nested_id = uuid.uuid4()

    await service.finalize(
        receipt.id,
        content="plan",
        tool_calls=({"id": nested_id, "arguments": {"target": nested_id}},),
        usage=TokenUsage(estimated_tokens=12, total_tokens=12),
        finish_reason="tool_calls",
    )

    assert receipt.response_snapshot == {
        "content": "plan",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {"arguments": {"target": str(nested_id)}, "id": str(nested_id)}
        ],
    }
    assert receipt.usage_source == "estimated"


def test_invalid_cost_context_identifiers_fail_closed() -> None:
    context = SimpleNamespace(tenant_id=None, run_id="bad", session_id="")

    with pytest.raises(PlanningCostAccountingError) as exc:
        PlanningSystemCostService._ids(context)

    assert exc.value.code == "planning_cost_context_invalid"


def test_request_bound_is_conservative_and_does_not_return_business_text() -> None:
    business_text = "机密商业计划：明天发布新品，预算 500 万元"

    upper_bound = planning_request_token_upper_bound([business_text])

    assert isinstance(upper_bound, int)
    assert upper_bound >= len(business_text.encode("utf-8"))
    assert business_text not in str(upper_bound)


def test_budget_reservation_uses_exact_provider_quote_or_fail_safe_fallback() -> None:
    minimax = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        capabilities={"service_tier": "standard"},
    )
    unsupported = SimpleNamespace(
        provider="unpriced-provider",
        model="unknown-model",
        capabilities={},
    )

    assert planning_budget_reservation_credits(
        model=minimax,
        request_input_token_upper_bound=4_000,
        request_max_output_tokens=2_048,
        fallback_credits=777,
    ) == 4
    assert planning_budget_reservation_credits(
        model=unsupported,
        request_input_token_upper_bound=4_000,
        request_max_output_tokens=2_048,
        fallback_credits=777,
    ) == 777
