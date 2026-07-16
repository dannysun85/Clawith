#!/usr/bin/env python3
"""Prove concurrent stale-reservation sweeps recover Credits exactly once."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, update

from app.database import async_session, engine
from app.models.agent import Agent  # noqa: F401 - registers FK target metadata
from app.models.subscription import CreditBalance, CreditReservation, CreditTransaction
from app.models.tenant import Tenant
from app.models.user import User  # noqa: F401 - registers FK target metadata
from app.services.billing_reconciliation import (
    check_credit_ledger_integrity,
    expire_stale_credit_reservations,
)


class _TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrived = 0
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived == 2:
            self._ready.set()
        await self._ready.wait()


class _CandidateReadBarrierSession:
    """Pause each sweeper after its unlocked candidate snapshot is loaded."""

    def __init__(self, db, barrier: _TwoPartyBarrier) -> None:
        self._db = db
        self._barrier = barrier
        self._first_execute = True

    async def execute(self, *args, **kwargs):
        result = await self._db.execute(*args, **kwargs)
        if self._first_execute:
            self._first_execute = False
            await self._barrier.wait()
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


class _MutationAfterFirstExecuteSession:
    """Commit a concurrent ledger update after the reconciliation query returns."""

    def __init__(self, db, mutation) -> None:
        self._db = db
        self._mutation = mutation
        self._mutated = False

    async def execute(self, *args, **kwargs):
        result = await self._db.execute(*args, **kwargs)
        if not self._mutated:
            self._mutated = True
            await self._mutation()
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


async def main() -> None:
    tenant_id = uuid.uuid4()
    transaction_only_tenant_id = uuid.uuid4()
    reservation_only_tenant_id = uuid.uuid4()
    concurrent_tenant_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    active_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        db.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="Billing Reconciliation PostgreSQL Smoke",
                    slug=f"billing-reconciliation-{tenant_id.hex[:12]}",
                    im_provider="web_only",
                    is_active=True,
                ),
                Tenant(
                    id=transaction_only_tenant_id,
                    name="Billing Reconciliation Transaction Without Balance",
                    slug=f"billing-transaction-only-{transaction_only_tenant_id.hex[:12]}",
                    im_provider="web_only",
                    is_active=True,
                ),
                Tenant(
                    id=reservation_only_tenant_id,
                    name="Billing Reconciliation Reservation Without Balance",
                    slug=f"billing-reservation-only-{reservation_only_tenant_id.hex[:12]}",
                    im_provider="web_only",
                    is_active=True,
                ),
                Tenant(
                    id=concurrent_tenant_id,
                    name="Billing Reconciliation Concurrent Snapshot",
                    slug=f"billing-concurrent-{concurrent_tenant_id.hex[:12]}",
                    im_provider="web_only",
                    is_active=True,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                CreditBalance(tenant_id=tenant_id, balance=1000, reserved=80),
                CreditBalance(
                    tenant_id=concurrent_tenant_id,
                    balance=1000,
                    reserved=0,
                ),
            ]
        )
        db.add_all(
            [
                CreditTransaction(
                    tenant_id=transaction_only_tenant_id,
                    delta=100,
                    balance_after=100,
                    reason="adjust",
                ),
                CreditTransaction(
                    tenant_id=concurrent_tenant_id,
                    delta=1000,
                    balance_after=1000,
                    reason="adjust",
                ),
                CreditReservation(
                    tenant_id=reservation_only_tenant_id,
                    action="video",
                    modality="video",
                    tier="pro",
                    provider="minimax",
                    model="MiniMax-Hailuo-02",
                    amount=25,
                    status="provider_inflight",
                    expires_at=now + timedelta(hours=1),
                ),
                CreditReservation(
                    id=expired_id,
                    tenant_id=tenant_id,
                    action="chat",
                    modality="text",
                    tier="pro",
                    provider="minimax",
                    model="MiniMax-M3",
                    amount=30,
                    status="reserved",
                    expires_at=now - timedelta(minutes=1),
                ),
                CreditReservation(
                    id=active_id,
                    tenant_id=tenant_id,
                    action="chat",
                    modality="text",
                    tier="pro",
                    provider="minimax",
                    model="MiniMax-M3",
                    amount=50,
                    status="reserved",
                    expires_at=now + timedelta(hours=1),
                ),
            ]
        )
        await db.commit()

    barrier = _TwoPartyBarrier()

    async def sweep_once() -> int:
        async with async_session() as db:
            count = await expire_stale_credit_reservations(
                _CandidateReadBarrierSession(db, barrier),
                now=now,
                _legacy_backfill_done=True,
            )
            await db.commit()
            return count

    try:
        counts = await asyncio.gather(sweep_once(), sweep_once())
        assert sum(counts) == 1

        async with async_session() as db:
            balance = await db.get(CreditBalance, tenant_id)
            expired = await db.get(CreditReservation, expired_id)
            active = await db.get(CreditReservation, active_id)
            assert balance is not None
            assert balance.balance == 1000
            assert balance.reserved == 50
            assert expired is not None and expired.status == "expired"
            assert active is not None and active.status == "reserved"

            transaction_only = await check_credit_ledger_integrity(
                db,
                tenant_id=transaction_only_tenant_id,
            )
            assert transaction_only.checked_tenants == 1
            assert [issue.code for issue in transaction_only.issues] == [
                "missing_credit_balance"
            ]

            reservation_only = await check_credit_ledger_integrity(
                db,
                tenant_id=reservation_only_tenant_id,
            )
            assert reservation_only.checked_tenants == 1
            assert [issue.code for issue in reservation_only.issues] == [
                "missing_credit_balance"
            ]

        mutation_committed = False

        async def commit_concurrent_ledger_update() -> None:
            nonlocal mutation_committed
            async with async_session() as update_db:
                await update_db.execute(
                    update(CreditBalance)
                    .where(CreditBalance.tenant_id == concurrent_tenant_id)
                    .values(balance=900)
                )
                update_db.add(
                    CreditTransaction(
                        tenant_id=concurrent_tenant_id,
                        delta=-100,
                        balance_after=900,
                        reason="consume",
                    )
                )
                await update_db.commit()
            mutation_committed = True

        async with async_session() as db:
            concurrent_snapshot = await check_credit_ledger_integrity(
                _MutationAfterFirstExecuteSession(
                    db,
                    commit_concurrent_ledger_update,
                ),
                tenant_id=concurrent_tenant_id,
            )
        assert mutation_committed is True
        assert concurrent_snapshot.checked_tenants == 1
        assert concurrent_snapshot.issues == []

        async with async_session() as db:
            committed_snapshot = await check_credit_ledger_integrity(
                db,
                tenant_id=concurrent_tenant_id,
            )
        assert committed_snapshot.checked_tenants == 1
        assert committed_snapshot.issues == []
        print("billing_reconciliation_postgres_smoke=ok")
    finally:
        async with async_session() as db:
            await db.execute(
                delete(CreditReservation).where(
                    CreditReservation.tenant_id.in_(
                        (
                            tenant_id,
                            transaction_only_tenant_id,
                            reservation_only_tenant_id,
                            concurrent_tenant_id,
                        )
                    )
                )
            )
            await db.execute(
                delete(CreditTransaction).where(
                    CreditTransaction.tenant_id.in_(
                        (transaction_only_tenant_id, concurrent_tenant_id)
                    )
                )
            )
            await db.execute(
                delete(CreditBalance).where(
                    CreditBalance.tenant_id.in_((tenant_id, concurrent_tenant_id))
                )
            )
            await db.execute(
                delete(Tenant).where(
                    Tenant.id.in_(
                        (
                            tenant_id,
                            transaction_only_tenant_id,
                            reservation_only_tenant_id,
                            concurrent_tenant_id,
                        )
                    )
                )
            )
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
