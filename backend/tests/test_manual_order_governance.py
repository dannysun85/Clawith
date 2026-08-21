from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.models.subscription import PaymentOrder, PaymentOrderOperatorDecision
from app.schemas.saas import ManualOrderDecisionIn
from app.services import manual_order_governance as governance


class Result:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class Session:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(self.results.pop(0) if self.results else None)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


def _order(**overrides) -> PaymentOrder:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "type": "topup",
        "credits": 1000,
        "amount_cents": 100,
        "currency": "CNY",
        "provider": "manual",
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "refunded_amount_cents": 0,
        "refunded_credits": 0,
    }
    values.update(overrides)
    return PaymentOrder(**values)


def _kwargs(order: PaymentOrder, **overrides):
    values = {
        "order_id": order.id,
        "expected_tenant_id": order.tenant_id,
        "expected_status": "pending",
        "disposition": "keep_pending",
        "evidence_ref": "ticket-finance-20260821",
        "reason": "Operator reviewed the exact historical order.",
        "rollback_of_decision_id": None,
        "actor_user_id": uuid.uuid4(),
        "idempotency_key": "manual-order-idempotency-0001",
    }
    values.update(overrides)
    return values


def test_manual_order_input_fails_closed_for_invalid_restore_shape():
    tenant_id = uuid.uuid4()
    with pytest.raises(ValueError, match="rollback_of_decision_id"):
        ManualOrderDecisionIn(
            expected_tenant_id=tenant_id,
            expected_status="canceled",
            disposition="restore_pending",
            evidence_ref="ticket-12345678",
            reason="Undo the reviewed cancellation.",
        )

    with pytest.raises(ValueError, match="only valid"):
        ManualOrderDecisionIn(
            expected_tenant_id=tenant_id,
            expected_status="pending",
            disposition="keep_pending",
            evidence_ref="ticket-12345678",
            reason="Keep this order pending for review.",
            rollback_of_decision_id=uuid.uuid4(),
        )


def test_idempotency_hash_is_stable_and_never_contains_the_raw_key():
    raw = "manual-order-secret-looking-key"
    digest = governance.hash_manual_order_idempotency_key(raw)
    assert len(digest) == 64
    assert raw not in digest
    assert digest == governance.hash_manual_order_idempotency_key(raw)

    with pytest.raises(governance.ManualOrderGovernanceError) as exc:
        governance.hash_manual_order_idempotency_key("short")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_keep_pending_writes_one_receipt_and_exact_replay_does_not_write_again():
    order = _order()
    first_session = Session(order, None)
    first = await governance.apply_manual_order_decision_in_session(
        first_session,
        **_kwargs(order),
    )

    assert first.replayed is False
    assert first.order.status == "pending"
    assert first.decision.previous_status == "pending"
    assert first.decision.resulting_status == "pending"
    assert first.decision.idempotency_key_hash != "manual-order-idempotency-0001"
    assert first_session.added == [first.decision]

    replay_session = Session(order, first.decision)
    replay = await governance.apply_manual_order_decision_in_session(
        replay_session,
        **_kwargs(order),
    )
    assert replay.replayed is True
    assert replay.decision.id == first.decision.id
    assert replay_session.added == []


@pytest.mark.asyncio
async def test_reusing_idempotency_key_with_different_request_is_rejected():
    order = _order()
    first_session = Session(order, None)
    first = await governance.apply_manual_order_decision_in_session(
        first_session,
        **_kwargs(order),
    )

    replay_session = Session(order, first.decision)
    with pytest.raises(governance.ManualOrderGovernanceError, match="different"):
        await governance.apply_manual_order_decision_in_session(
            replay_session,
            **_kwargs(order, reason="A different operator decision reason."),
        )


@pytest.mark.asyncio
async def test_cancel_then_restore_requires_exact_latest_cancellation_receipt():
    order = _order()
    cancel_session = Session(order, None)
    cancelled = await governance.apply_manual_order_decision_in_session(
        cancel_session,
        **_kwargs(order, disposition="cancel_test"),
    )
    assert order.status == "canceled"

    restore_session = Session(
        order,
        None,
        cancelled.decision,
        None,
        cancelled.decision.id,
    )
    restored = await governance.apply_manual_order_decision_in_session(
        restore_session,
        **_kwargs(
            order,
            expected_status="canceled",
            disposition="restore_pending",
            rollback_of_decision_id=cancelled.decision.id,
            idempotency_key="manual-order-idempotency-restore",
        ),
    )
    assert order.status == "pending"
    assert restored.decision.rollback_of_decision_id == cancelled.decision.id
    assert restored.decision.previous_status == "canceled"
    assert restored.decision.resulting_status == "pending"


@pytest.mark.asyncio
async def test_restore_rejects_paid_or_refunded_evidence():
    order = _order(status="canceled", provider_payment_id="payment-evidence")
    prior = PaymentOrderOperatorDecision(
        id=uuid.uuid4(),
        order_id=order.id,
        tenant_id=order.tenant_id,
        actor_user_id=uuid.uuid4(),
        idempotency_key_hash="a" * 64,
        request_fingerprint="b" * 64,
        disposition="cancel_invalid",
        evidence_ref="ticket-12345678",
        reason="Reviewed invalid historical order.",
        previous_status="pending",
        resulting_status="canceled",
        created_at=datetime.now(timezone.utc),
    )
    session = Session(order, None, prior, None, prior.id)
    with pytest.raises(governance.ManualOrderGovernanceError, match="payment or refund"):
        await governance.apply_manual_order_decision_in_session(
            session,
            **_kwargs(
                order,
                expected_status="canceled",
                disposition="restore_pending",
                rollback_of_decision_id=prior.id,
                idempotency_key="manual-order-idempotency-restore-paid",
            ),
        )


@pytest.mark.asyncio
async def test_tenant_fence_and_provider_authority_fail_closed():
    order = _order()
    with pytest.raises(governance.ManualOrderGovernanceError, match="tenant"):
        await governance.apply_manual_order_decision_in_session(
            Session(order, None),
            **_kwargs(order, expected_tenant_id=uuid.uuid4()),
        )

    provider_order = _order(provider="wechat")
    with pytest.raises(governance.ManualOrderGovernanceError, match="provider event"):
        await governance.apply_manual_order_decision_in_session(
            Session(provider_order, None),
            **_kwargs(provider_order),
        )


@pytest.mark.asyncio
async def test_mark_paid_calls_financial_finalizer_once_and_receipts_result():
    order = _order()
    session = Session(order, None)

    async def finalize(_db, target, *, actor_user_id):
        assert actor_user_id is not None
        target.status = "paid"
        target.paid_at = datetime.now(timezone.utc)
        return target

    with patch.object(
        governance,
        "finalize_order_in_session",
        AsyncMock(side_effect=finalize),
    ) as finalizer:
        result = await governance.apply_manual_order_decision_in_session(
            session,
            **_kwargs(order, disposition="mark_paid"),
        )

    finalizer.assert_awaited_once()
    assert result.order.status == "paid"
    assert result.decision.resulting_status == "paid"
    assert result.replayed is False


def test_receipt_model_has_durable_idempotency_and_rollback_constraints():
    table = PaymentOrderOperatorDecision.__table__
    assert {column.name for column in table.primary_key.columns} == {"id"}
    constraint_names = {constraint.name for constraint in table.constraints}
    assert "uq_payment_order_operator_decision_idempotency" in constraint_names
    assert "uq_payment_order_operator_decision_rollback" in constraint_names
    assert "ck_payment_order_operator_decision_rollback_shape" in constraint_names
    assert PaymentOrderOperatorDecision.__tablename__ == "payment_order_operator_decisions"
