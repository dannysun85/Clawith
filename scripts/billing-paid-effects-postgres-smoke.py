#!/usr/bin/env python3
"""Prove paid-subscription effects leases and fencing on PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent  # noqa: F401 - registers billing FK target metadata
from app.models.llm import LLMModel  # noqa: F401 - registers tenant FK target metadata
from app.models.subscription import PaymentOrder
from app.models.tenant import Tenant
from app.models.user import User  # noqa: F401 - registers tenant FK target metadata
from app.services.subscription_lifecycle import (
    _claim_paid_subscribe_effects,
    _finish_paid_subscribe_effects,
)


async def main() -> None:
    tenant_id = uuid.uuid4()
    order_id = uuid.uuid4()
    async with async_session() as db:
        tenant = Tenant(
            id=tenant_id,
            name="Paid Effects Lease PostgreSQL Smoke",
            slug=f"paid-effects-{tenant_id.hex[:12]}",
            im_provider="web_only",
            is_active=True,
        )
        db.add(tenant)
        await db.flush()
        db.add(
            PaymentOrder(
                id=order_id,
                tenant_id=tenant_id,
                type="subscribe",
                amount_cents=100,
                currency="CNY",
                provider="wechat",
                status="paid",
                paid_effects_status="pending",
            )
        )
        await db.commit()

    claims = await asyncio.gather(
        _claim_paid_subscribe_effects(order_id),
        _claim_paid_subscribe_effects(order_id),
    )
    winners = [claim for claim in claims if claim is not None]
    assert winners == [(tenant_id, 1)], claims

    async with async_session() as db:
        order = await db.get(PaymentOrder, order_id)
        assert order is not None
        order.paid_effects_started_at = datetime.now(timezone.utc) - timedelta(
            minutes=11
        )
        await db.commit()

    reclaimed = await _claim_paid_subscribe_effects(order_id)
    assert reclaimed == (tenant_id, 2)
    assert (
        await _finish_paid_subscribe_effects(
            order_id,
            attempt=1,
            applied=False,
            error=RuntimeError("stale worker"),
        )
        is False
    )
    assert (
        await _finish_paid_subscribe_effects(
            order_id,
            attempt=2,
            applied=True,
        )
        is True
    )

    async with async_session() as db:
        order = (
            await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
        ).scalar_one()
        assert order.paid_effects_status == "applied"
        assert order.paid_effects_attempts == 2
        assert order.paid_effects_error is None
        await db.execute(delete(PaymentOrder).where(PaymentOrder.id == order_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()

    print("billing_paid_effects_postgres_smoke=ok")


async def _run() -> None:
    try:
        await main()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
