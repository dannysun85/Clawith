#!/usr/bin/env python3
"""PostgreSQL smoke for manual-order receipts, replay, rollback, and audit."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.api.saas import _apply_manual_order_decision
from app.database import async_session
from app.models.audit import AuditLog
from app.models.subscription import PaymentOrder, PaymentOrderOperatorDecision
from app.models.user import User
from app.schemas.saas import ManualOrderDecisionIn
from app.services.manual_order_governance import apply_manual_order_decision_in_session


def _input(
    order: PaymentOrder,
    disposition: str,
    *,
    rollback_of_decision_id: uuid.UUID | None = None,
) -> ManualOrderDecisionIn:
    return ManualOrderDecisionIn(
        expected_tenant_id=order.tenant_id,
        expected_status="canceled" if disposition == "restore_pending" else "pending",
        disposition=disposition,
        evidence_ref=f"migration-smoke:{disposition}:20260821",
        reason=f"PostgreSQL migration smoke validates {disposition} safely.",
        rollback_of_decision_id=rollback_of_decision_id,
    )


async def _api_decision(
    *,
    order_id: uuid.UUID,
    data: ManualOrderDecisionIn,
    key: str,
    actor: User,
):
    async with async_session() as db:
        return await _apply_manual_order_decision(
            order_id=order_id,
            data=data,
            idempotency_key=key,
            current_user=actor,
            db=db,
        )


async def _main() -> None:
    async with async_session() as db:
        actor = (
            await db.execute(
                select(User).where(User.tenant_id.is_not(None)).limit(1)
            )
        ).scalar_one_or_none()
        if actor is None or actor.tenant_id is None:
            raise AssertionError("migration smoke requires one tenant-scoped user fixture")
        order = PaymentOrder(
            id=uuid.uuid4(),
            tenant_id=actor.tenant_id,
            type="topup",
            credits=100,
            amount_cents=100,
            currency="CNY",
            provider="manual",
            status="pending",
        )
        concurrent_order = PaymentOrder(
            id=uuid.uuid4(),
            tenant_id=actor.tenant_id,
            type="topup",
            credits=100,
            amount_cents=100,
            currency="CNY",
            provider="manual",
            status="pending",
        )
        db.add(order)
        db.add(concurrent_order)
        await db.commit()
        await db.refresh(order)
        await db.refresh(concurrent_order)
        actor_id = actor.id
        order_id = order.id
        concurrent_order_id = concurrent_order.id

    keep_key = "manual-order-postgres-keep-0001"
    kept = await _api_decision(
        order_id=order_id,
        data=_input(order, "keep_pending"),
        key=keep_key,
        actor=actor,
    )
    replayed = await _api_decision(
        order_id=order_id,
        data=_input(order, "keep_pending"),
        key=keep_key,
        actor=actor,
    )
    if kept.replayed or not replayed.replayed or kept.decision.id != replayed.decision.id:
        raise AssertionError("exact idempotent replay did not return the original receipt")

    cancelled = await _api_decision(
        order_id=order_id,
        data=_input(order, "cancel_test"),
        key="manual-order-postgres-cancel-0001",
        actor=actor,
    )
    if cancelled.order.status != "canceled":
        raise AssertionError("cancel decision did not transition pending to canceled")
    restored = await _api_decision(
        order_id=order_id,
        data=_input(
            cancelled.order,
            "restore_pending",
            rollback_of_decision_id=cancelled.decision.id,
        ),
        key="manual-order-postgres-restore-0001",
        actor=actor,
    )
    if restored.order.status != "pending":
        raise AssertionError("restore decision did not roll the exact cancellation back")

    async def concurrent_keep() -> bool:
        async with async_session() as db:
            result = await apply_manual_order_decision_in_session(
                db,
                order_id=concurrent_order_id,
                expected_tenant_id=concurrent_order.tenant_id,
                expected_status="pending",
                disposition="keep_pending",
                evidence_ref="migration-smoke:concurrent:20260821",
                reason="Two workers replay the exact same operator request.",
                rollback_of_decision_id=None,
                actor_user_id=actor_id,
                idempotency_key="manual-order-postgres-concurrent-0001",
            )
            await db.commit()
            return result.replayed

    replay_flags = sorted(await asyncio.gather(concurrent_keep(), concurrent_keep()))
    if replay_flags != [False, True]:
        raise AssertionError(f"concurrent replay flags were not exactly-once: {replay_flags}")

    async with async_session() as db:
        receipt_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PaymentOrderOperatorDecision)
                    .where(PaymentOrderOperatorDecision.order_id == order_id)
                )
            ).scalar_one()
        )
        audit_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action == "saas_manual_order_decision",
                        AuditLog.details["order_id"].as_string() == str(order_id),
                    )
                )
            ).scalar_one()
        )
        concurrent_receipts = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PaymentOrderOperatorDecision)
                    .where(
                        PaymentOrderOperatorDecision.order_id
                        == concurrent_order_id
                    )
                )
            ).scalar_one()
        )
        raw_key_leaks = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PaymentOrderOperatorDecision)
                    .where(
                        PaymentOrderOperatorDecision.idempotency_key_hash.in_(
                            (
                                keep_key,
                                "manual-order-postgres-cancel-0001",
                                "manual-order-postgres-restore-0001",
                            )
                        )
                    )
                )
            ).scalar_one()
        )
        await db.rollback()

    if receipt_count != 3 or audit_count != 3:
        raise AssertionError(
            f"expected three decisions/audits, got receipts={receipt_count} audits={audit_count}"
        )
    if concurrent_receipts != 1:
        raise AssertionError("concurrent same-key decision created duplicate receipts")
    if raw_key_leaks:
        raise AssertionError("raw idempotency keys were persisted")

    print(
        "manual_order_governance_postgres_smoke_ok "
        "receipts=3 audits=3 concurrent_receipts=1"
    )


if __name__ == "__main__":
    asyncio.run(_main())
