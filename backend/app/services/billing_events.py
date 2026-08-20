"""Billing order state-machine and webhook idempotency."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import (
    BillingWebhookEvent,
    CreditBalance,
    CreditTransaction,
    PaymentOrder,
    Plan,
    Subscription,
)
from app.services.credit_service import grant_credits_in_session


@dataclass(slots=True)
class PaymentProviderEventState:
    provider: str
    event_id: str
    event_type: str
    order_id: uuid.UUID | None
    status: str
    provider_session_id: str | None = None
    provider_payment_id: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    merchant_id: str | None = None
    app_id: str | None = None
    trade_type: str | None = None
    tenant_id: uuid.UUID | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _find_added_webhook_event(db: AsyncSession, provider: str, event_id: str) -> BillingWebhookEvent | None:
    """Support direct-call unit tests with lightweight fake sessions."""
    for item in getattr(db, "added", []):
        if isinstance(item, BillingWebhookEvent) and item.provider == provider and item.event_id == event_id:
            return item
    return None


async def finalize_order_in_session(
    db: AsyncSession,
    order: PaymentOrder,
    *,
    actor_user_id: uuid.UUID | None = None,
    provider_payment_id: str | None = None,
    paid_at: datetime | None = None,
) -> PaymentOrder:
    """Mark an order paid and apply the subscription/topup credit effects once."""
    if order.status == "paid":
        return order

    order.status = "paid"
    order.paid_at = paid_at or _utcnow()
    if provider_payment_id:
        order.provider_payment_id = provider_payment_id

    if order.type == "subscribe" and order.plan_id:
        plan = await db.get(Plan, order.plan_id)
        if plan:
            existing = await db.execute(
                select(Subscription).where(
                    Subscription.tenant_id == order.tenant_id,
                    Subscription.status.in_(("active", "trialing")),
                )
            )
            existing_sub = existing.scalar_one_or_none()
            now = order.paid_at or _utcnow()
            period_days = 365 if (order.period or "monthly") == "yearly" else 30
            if order.change_kind == "downgrade" and existing_sub:
                # Downgrades never discard paid higher-tier time: the new plan
                # is scheduled and applied by the lifecycle daemon at period_end.
                # Credits are granted at activation, not at payment.
                existing_sub.scheduled_plan_id = order.plan_id
                existing_sub.scheduled_period = order.period or "monthly"
                return order
            if existing_sub:
                existing_sub.plan_id = order.plan_id
                existing_sub.status = "active"
                existing_sub.cancel_at_period_end = False
                existing_sub.scheduled_plan_id = None
                existing_sub.scheduled_period = None
                # Renewals stack on unexpired time instead of restarting from now.
                base = now
                if existing_sub.period_end and existing_sub.period_end > base:
                    base = existing_sub.period_end
                existing_sub.period_end = base + timedelta(days=period_days)
                sub = existing_sub
            else:
                sub = Subscription(
                    tenant_id=order.tenant_id,
                    plan_id=order.plan_id,
                    status="active",
                    period_start=now,
                    period_end=now + timedelta(days=period_days),
                )
                db.add(sub)
            if plan.credits_per_period:
                await grant_credits_in_session(
                    db,
                    tenant_id=order.tenant_id,
                    amount=plan.credits_per_period,
                    reason="subscribe",
                    granted_by=actor_user_id,
                    ref_type="order",
                    ref_id=order.id,
                )

    elif order.type == "topup" and order.credits:
        await grant_credits_in_session(
            db,
            tenant_id=order.tenant_id,
            amount=order.credits,
            reason="topup",
            granted_by=actor_user_id,
            ref_type="order",
            ref_id=order.id,
        )

    return order


async def refund_order_in_session(
    db: AsyncSession,
    order: PaymentOrder,
    *,
    provider_payment_id: str | None = None,
) -> PaymentOrder:
    """Record a provider refund and claw back only still-available granted Credits.

    The caller holds the payment-order row lock.  In-flight reservations remain
    protected, consumed Credits are never made negative, and the order-scoped
    ``refund_clawback`` ledger row makes retries idempotent.
    """
    if order.status == "refunded":
        return order
    if provider_payment_id:
        order.provider_payment_id = provider_payment_id
    if order.status != "paid":
        order.status = "refunded"
        return order

    grant_reason = "subscribe" if order.type == "subscribe" else "topup"
    original_grant_result = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.tenant_id == order.tenant_id,
            CreditTransaction.reason == grant_reason,
            CreditTransaction.ref_type == "order",
            CreditTransaction.ref_id == order.id,
        )
        .limit(1)
    )
    original_grant = original_grant_result.scalar_one_or_none()
    clawback_result = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.tenant_id == order.tenant_id,
            CreditTransaction.reason == "refund_clawback",
            CreditTransaction.ref_type == "order",
            CreditTransaction.ref_id == order.id,
        )
        .limit(1)
    )
    existing_clawback = clawback_result.scalar_one_or_none()
    if original_grant is not None and existing_clawback is None:
        balance_result = await db.execute(
            select(CreditBalance)
            .where(CreditBalance.tenant_id == order.tenant_id)
            .with_for_update()
        )
        balance = balance_result.scalar_one_or_none()
        if balance is None:
            raise ValueError("Credit balance missing for refunded payment order")
        available = max(int(balance.balance or 0) - int(balance.reserved or 0), 0)
        clawback = min(max(int(original_grant.delta or 0), 0), available)
        balance.balance -= clawback
        balance.updated_at = _utcnow()
        db.add(
            CreditTransaction(
                tenant_id=order.tenant_id,
                delta=-clawback,
                balance_after=balance.balance,
                reason="refund_clawback",
                ref_type="order",
                ref_id=order.id,
            )
        )

    if order.type == "subscribe" and order.plan_id and order.change_kind != "downgrade":
        paid_at = order.paid_at or order.created_at or _utcnow()
        newer_order_result = await db.execute(
            select(PaymentOrder.id)
            .where(
                PaymentOrder.tenant_id == order.tenant_id,
                PaymentOrder.type == "subscribe",
                PaymentOrder.status == "paid",
                PaymentOrder.id != order.id,
                PaymentOrder.paid_at > paid_at,
            )
            .limit(1)
        )
        if newer_order_result.scalar_one_or_none() is None:
            subscription_result = await db.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == order.tenant_id,
                    Subscription.plan_id == order.plan_id,
                    Subscription.status.in_(("active", "trialing")),
                )
                .with_for_update()
            )
            subscription = subscription_result.scalar_one_or_none()
            if subscription is not None:
                subscription.status = "canceled"
                subscription.period_end = _utcnow()
                subscription.auto_renew = False
                subscription.cancel_at_period_end = False
                subscription.scheduled_plan_id = None
                subscription.scheduled_period = None

    order.status = "refunded"
    return order


async def process_billing_webhook_event(
    db: AsyncSession,
    *,
    provider_name: str,
    payload: bytes,
    signature: str | None,
    provider: Any,
    signature_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify, store, and apply a provider webhook exactly once."""
    event = await provider.verify_webhook(payload, signature, headers=signature_headers)
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id or not event_type:
        raise ValueError("Billing webhook event must include id and type")

    async def find_webhook_event() -> BillingWebhookEvent | None:
        result = await db.execute(
            select(BillingWebhookEvent)
            .where(
                BillingWebhookEvent.provider == provider_name,
                BillingWebhookEvent.event_id == event_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() or _find_added_webhook_event(db, provider_name, event_id)

    webhook_event = await find_webhook_event()
    if webhook_event is not None and webhook_event.status == "processed":
        return {"status": "duplicate", "event_id": event_id}

    try:
        state = await provider.load_remote_event_state(event)
        order = None
        if state.order_id:
            # The order lock is also the financial idempotency fence.  After a
            # concurrent callback waits here, re-read the event row so it sees
            # the first transaction's processed marker instead of inserting a
            # conflicting duplicate event.
            order = await db.get(PaymentOrder, state.order_id, with_for_update=True)
            webhook_event = await find_webhook_event()
            if webhook_event is not None and webhook_event.status == "processed":
                return {"status": "duplicate", "event_id": event_id}

        if webhook_event is None:
            webhook_event = BillingWebhookEvent(
                provider=provider_name,
                event_id=event_id,
                event_type=event_type,
                raw=event,
                status="processing",
            )
            db.add(webhook_event)

        if not state.order_id:
            webhook_event.status = "processed"
            webhook_event.processed_at = _utcnow()
            return {"status": "ignored", "event_id": event_id}

        if not order:
            raise ValueError(f"Payment order not found for webhook event {event_id}")
        if order.provider != provider_name:
            raise ValueError(f"Payment order provider mismatch: expected {order.provider}, got {provider_name}")
        if state.provider_session_id and order.provider_session_id and state.provider_session_id != order.provider_session_id:
            raise ValueError("Payment order provider session mismatch")
        validate_state = getattr(provider, "validate_event_state", None)
        if validate_state:
            validate_state(order, state)

        if state.status == "paid":
            await finalize_order_in_session(
                db,
                order,
                actor_user_id=None,
                provider_payment_id=state.provider_payment_id,
                paid_at=_utcnow(),
            )
        elif state.status in {"failed", "canceled"} and order.status == "pending":
            order.status = "failed" if state.status == "failed" else "canceled"
            if state.provider_payment_id:
                order.provider_payment_id = state.provider_payment_id
        elif state.status == "refunded" and order.status in {"pending", "paid"}:
            await refund_order_in_session(
                db,
                order,
                provider_payment_id=state.provider_payment_id,
            )

        webhook_event.status = "processed"
        webhook_event.processed_at = _utcnow()
        return {"status": "processed", "event_id": event_id, "order_id": str(state.order_id)}
    except Exception as exc:
        if webhook_event is not None:
            webhook_event.status = "failed"
            webhook_event.error = str(exc)[:500]
        raise


async def sync_pending_order_from_provider(db: AsyncSession, order: PaymentOrder) -> bool:
    """Pull a pending order's state from the provider server-to-server.

    Recovers orders whose webhook was missed (notify_url unreachable, dropped
    callback) by querying the provider directly and applying the same state
    machine as ``process_billing_webhook_event``. Returns True when the order
    changed; the caller owns the commit.
    """
    from app.services.billing_provider import get_billing_provider

    if order.status != "pending" or not order.provider or order.provider == "manual":
        return False
    try:
        provider = get_billing_provider(order.provider)
    except ValueError:
        return False
    state = await provider.query_order_state(order)
    if state is None or state.order_id != order.id:
        return False

    locked_result = await db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order.id).with_for_update(skip_locked=True)
    )
    locked = locked_result.scalar_one_or_none()
    if locked is None or locked.status != "pending":
        return False
    validate_state = getattr(provider, "validate_event_state", None)
    if validate_state:
        validate_state(locked, state)

    if state.status == "paid":
        await finalize_order_in_session(
            db,
            locked,
            actor_user_id=None,
            provider_payment_id=state.provider_payment_id,
            paid_at=_utcnow(),
        )
        return True
    if state.status in {"failed", "canceled"}:
        locked.status = "failed" if state.status == "failed" else "canceled"
        if state.provider_payment_id:
            locked.provider_payment_id = state.provider_payment_id
        return True
    if state.status == "refunded":
        await refund_order_in_session(
            db,
            locked,
            provider_payment_id=state.provider_payment_id,
        )
        return True
    return False


async def close_expired_pending_order(db: AsyncSession, order: PaymentOrder) -> bool:
    """Close an expired pending order at the provider before canceling locally."""
    from app.services.billing_provider import get_billing_provider

    if order.status != "pending" or not order.provider or order.provider == "manual":
        return False
    provider = get_billing_provider(order.provider)
    locked_result = await db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order.id).with_for_update(skip_locked=True)
    )
    locked = locked_result.scalar_one_or_none()
    if locked is None or locked.status != "pending":
        return False
    if not await provider.close_order(locked):
        return False
    locked.status = "canceled"
    return True
