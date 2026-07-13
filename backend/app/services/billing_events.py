"""Billing order state-machine and webhook idempotency."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import BillingWebhookEvent, PaymentOrder, Plan, Subscription
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
            period_end = now + timedelta(days=30)
            if existing_sub:
                existing_sub.plan_id = order.plan_id
                existing_sub.status = "active"
                existing_sub.period_end = period_end
                existing_sub.cancel_at_period_end = False
                sub = existing_sub
            else:
                sub = Subscription(
                    tenant_id=order.tenant_id,
                    plan_id=order.plan_id,
                    status="active",
                    period_start=now,
                    period_end=period_end,
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


async def process_billing_webhook_event(
    db: AsyncSession,
    *,
    provider_name: str,
    payload: bytes,
    signature: str | None,
    provider: Any,
) -> dict[str, Any]:
    """Verify, store, and apply a provider webhook exactly once."""
    event = await provider.verify_webhook(payload, signature)
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id or not event_type:
        raise ValueError("Billing webhook event must include id and type")

    existing_result = await db.execute(
        select(BillingWebhookEvent)
        .where(
            BillingWebhookEvent.provider == provider_name,
            BillingWebhookEvent.event_id == event_id,
        )
        .limit(1)
    )
    webhook_event = existing_result.scalar_one_or_none() or _find_added_webhook_event(db, provider_name, event_id)
    if webhook_event and webhook_event.status == "processed":
        return {"status": "duplicate", "event_id": event_id}

    if not webhook_event:
        webhook_event = BillingWebhookEvent(
            provider=provider_name,
            event_id=event_id,
            event_type=event_type,
            raw=event,
            status="processing",
        )
        db.add(webhook_event)

    try:
        state = await provider.load_remote_event_state(event)
        if not state.order_id:
            webhook_event.status = "processed"
            webhook_event.processed_at = _utcnow()
            return {"status": "ignored", "event_id": event_id}

        order = await db.get(PaymentOrder, state.order_id, with_for_update=True)
        if not order:
            raise ValueError(f"Payment order not found for webhook event {event_id}")
        if order.provider != provider_name:
            raise ValueError(f"Payment order provider mismatch: expected {order.provider}, got {provider_name}")
        if state.provider_session_id and order.provider_session_id and state.provider_session_id != order.provider_session_id:
            raise ValueError("Payment order provider session mismatch")

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

        webhook_event.status = "processed"
        webhook_event.processed_at = _utcnow()
        return {"status": "processed", "event_id": event_id, "order_id": str(state.order_id)}
    except Exception as exc:
        webhook_event.status = "failed"
        webhook_event.error = str(exc)[:500]
        raise
