"""Evidence-backed, replay-safe governance for manual payment orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import PaymentOrder, PaymentOrderOperatorDecision
from app.services.billing_events import finalize_order_in_session


CANCEL_DISPOSITIONS = frozenset(
    {"cancel_expired", "cancel_test", "cancel_invalid"}
)
MANUAL_ORDER_DISPOSITIONS = frozenset(
    {
        "keep_pending",
        "mark_paid",
        *CANCEL_DISPOSITIONS,
        "restore_pending",
    }
)


class ManualOrderGovernanceError(ValueError):
    """A fail-closed operator error with an API-safe HTTP status."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ManualOrderDecisionResult:
    order: PaymentOrder
    decision: PaymentOrderOperatorDecision
    replayed: bool


def hash_manual_order_idempotency_key(raw_key: str | None) -> str:
    """Validate and hash the caller key without retaining the raw value."""

    normalized = str(raw_key or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise ManualOrderGovernanceError(
            "Idempotency-Key must contain between 8 and 128 non-whitespace characters",
            status_code=400,
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def manual_order_request_fingerprint(
    *,
    order_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    expected_status: str,
    disposition: str,
    evidence_ref: str,
    reason: str,
    rollback_of_decision_id: uuid.UUID | None,
) -> str:
    payload = {
        "disposition": disposition,
        "evidence_ref": evidence_ref,
        "expected_status": expected_status,
        "expected_tenant_id": str(expected_tenant_id),
        "order_id": str(order_id),
        "reason": reason,
        "rollback_of_decision_id": (
            str(rollback_of_decision_id)
            if rollback_of_decision_id is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_transition_shape(
    *,
    disposition: str,
    expected_status: str,
    rollback_of_decision_id: uuid.UUID | None,
) -> None:
    if disposition not in MANUAL_ORDER_DISPOSITIONS:
        raise ManualOrderGovernanceError("Unsupported manual-order disposition", status_code=422)
    if disposition == "restore_pending":
        if expected_status != "canceled" or rollback_of_decision_id is None:
            raise ManualOrderGovernanceError(
                "restore_pending requires expected_status=canceled and rollback_of_decision_id",
                status_code=422,
            )
        return
    if expected_status != "pending" or rollback_of_decision_id is not None:
        raise ManualOrderGovernanceError(
            f"{disposition} requires expected_status=pending without rollback_of_decision_id",
            status_code=422,
        )


async def _locked_order(db: AsyncSession, order_id: uuid.UUID) -> PaymentOrder:
    result = await db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.id == order_id)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise ManualOrderGovernanceError("Order not found", status_code=404)
    return order


async def _existing_receipt(
    db: AsyncSession,
    *,
    order_id: uuid.UUID,
    idempotency_key_hash: str,
) -> PaymentOrderOperatorDecision | None:
    result = await db.execute(
        select(PaymentOrderOperatorDecision).where(
            PaymentOrderOperatorDecision.order_id == order_id,
            PaymentOrderOperatorDecision.idempotency_key_hash
            == idempotency_key_hash,
        )
    )
    return result.scalar_one_or_none()


async def _validate_restore(
    db: AsyncSession,
    *,
    order: PaymentOrder,
    rollback_of_decision_id: uuid.UUID,
) -> PaymentOrderOperatorDecision:
    result = await db.execute(
        select(PaymentOrderOperatorDecision).where(
            PaymentOrderOperatorDecision.id == rollback_of_decision_id,
            PaymentOrderOperatorDecision.order_id == order.id,
            PaymentOrderOperatorDecision.tenant_id == order.tenant_id,
        )
    )
    prior = result.scalar_one_or_none()
    if prior is None or prior.disposition not in CANCEL_DISPOSITIONS:
        raise ManualOrderGovernanceError(
            "rollback_of_decision_id must reference this order's cancellation receipt"
        )
    rolled_back_result = await db.execute(
        select(PaymentOrderOperatorDecision.id).where(
            PaymentOrderOperatorDecision.rollback_of_decision_id == prior.id
        )
    )
    if rolled_back_result.scalar_one_or_none() is not None:
        raise ManualOrderGovernanceError("Cancellation receipt was already rolled back")
    latest_result = await db.execute(
        select(PaymentOrderOperatorDecision.id)
        .where(PaymentOrderOperatorDecision.order_id == order.id)
        .order_by(
            PaymentOrderOperatorDecision.created_at.desc(),
            PaymentOrderOperatorDecision.id.desc(),
        )
        .limit(1)
    )
    if latest_result.scalar_one_or_none() != prior.id:
        raise ManualOrderGovernanceError(
            "Only the latest operator cancellation can be restored"
        )
    if any(
        (
            order.paid_at is not None,
            bool(order.provider_payment_id),
            int(order.refunded_amount_cents or 0) > 0,
            int(order.refunded_credits or 0) > 0,
        )
    ):
        raise ManualOrderGovernanceError(
            "Order has payment or refund evidence and cannot be restored to pending"
        )
    return prior


async def apply_manual_order_decision_in_session(
    db: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    expected_status: str,
    disposition: str,
    evidence_ref: str,
    reason: str,
    rollback_of_decision_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    idempotency_key: str | None,
) -> ManualOrderDecisionResult:
    """Apply exactly one tenant-fenced decision in the caller transaction."""

    _validate_transition_shape(
        disposition=disposition,
        expected_status=expected_status,
        rollback_of_decision_id=rollback_of_decision_id,
    )
    idempotency_key_hash = hash_manual_order_idempotency_key(idempotency_key)
    request_fingerprint = manual_order_request_fingerprint(
        order_id=order_id,
        expected_tenant_id=expected_tenant_id,
        expected_status=expected_status,
        disposition=disposition,
        evidence_ref=evidence_ref,
        reason=reason,
        rollback_of_decision_id=rollback_of_decision_id,
    )

    # The order lock serializes every decision for this order.  Looking up the
    # receipt after taking the lock makes concurrent repeats observe the first
    # transaction's committed receipt without relying on exception recovery.
    order = await _locked_order(db, order_id)
    existing = await _existing_receipt(
        db,
        order_id=order_id,
        idempotency_key_hash=idempotency_key_hash,
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise ManualOrderGovernanceError(
                "Idempotency-Key was already used with a different manual-order decision"
            )
        return ManualOrderDecisionResult(
            order=order,
            decision=existing,
            replayed=True,
        )

    if order.tenant_id != expected_tenant_id:
        raise ManualOrderGovernanceError("Order tenant does not match expected_tenant_id")
    if order.provider != "manual":
        raise ManualOrderGovernanceError(
            "Provider-backed orders can only be finalized by a verified provider event or reconciliation"
        )
    if order.status != expected_status:
        raise ManualOrderGovernanceError(
            f"Order status changed: expected {expected_status}, found {order.status}"
        )

    previous_status = order.status
    if disposition == "keep_pending":
        resulting_status = "pending"
    elif disposition == "mark_paid":
        await finalize_order_in_session(db, order, actor_user_id=actor_user_id)
        resulting_status = "paid"
    elif disposition in CANCEL_DISPOSITIONS:
        if order.paid_at is not None or order.provider_payment_id:
            raise ManualOrderGovernanceError(
                "Order has payment evidence and cannot be canceled as pending"
            )
        order.status = "canceled"
        resulting_status = "canceled"
    else:
        assert disposition == "restore_pending"
        assert rollback_of_decision_id is not None
        await _validate_restore(
            db,
            order=order,
            rollback_of_decision_id=rollback_of_decision_id,
        )
        order.status = "pending"
        resulting_status = "pending"

    decision = PaymentOrderOperatorDecision(
        id=uuid.uuid4(),
        order_id=order.id,
        tenant_id=order.tenant_id,
        actor_user_id=actor_user_id,
        idempotency_key_hash=idempotency_key_hash,
        request_fingerprint=request_fingerprint,
        disposition=disposition,
        evidence_ref=evidence_ref,
        reason=reason,
        previous_status=previous_status,
        resulting_status=resulting_status,
        rollback_of_decision_id=rollback_of_decision_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(decision)
    await db.flush()
    return ManualOrderDecisionResult(
        order=order,
        decision=decision,
        replayed=False,
    )


__all__ = [
    "CANCEL_DISPOSITIONS",
    "MANUAL_ORDER_DISPOSITIONS",
    "ManualOrderDecisionResult",
    "ManualOrderGovernanceError",
    "apply_manual_order_decision_in_session",
    "hash_manual_order_idempotency_key",
    "manual_order_request_fingerprint",
]
