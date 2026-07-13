"""Billing reconciliation and credit-ledger integrity checks."""

from __future__ import annotations

import uuid
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.subscription import CreditBalance, CreditReservation, CreditTransaction, PaymentOrder
from app.models.media_generation import MediaGenerationTask
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    release_reserved_credits_in_session,
)


@dataclass(slots=True)
class LedgerIntegrityIssue:
    code: str
    tenant_id: uuid.UUID
    expected: int
    actual: int
    message: str


@dataclass(slots=True)
class LedgerIntegrityReport:
    checked_tenants: int = 0
    issues: list[LedgerIntegrityIssue] = field(default_factory=list)


@dataclass(slots=True)
class PaymentReconciliationIssue:
    code: str
    order_id: uuid.UUID
    tenant_id: uuid.UUID
    provider: str
    status: str
    message: str


@dataclass(slots=True)
class PaymentReconciliationReport:
    checked_orders: int = 0
    issues: list[PaymentReconciliationIssue] = field(default_factory=list)


async def check_credit_ledger_integrity(
    db: AsyncSession | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
) -> LedgerIntegrityReport:
    """Compare credit balance rows against ledger and open reservations."""
    if db is None:
        async with async_session() as session:
            return await check_credit_ledger_integrity(session, tenant_id=tenant_id)

    balance_stmt = select(CreditBalance)
    if tenant_id:
        balance_stmt = balance_stmt.where(CreditBalance.tenant_id == tenant_id)
    balances_result = await db.execute(balance_stmt)
    balances = list(balances_result.scalars().all())

    tx_stmt = select(CreditTransaction)
    if tenant_id:
        tx_stmt = tx_stmt.where(CreditTransaction.tenant_id == tenant_id)
    tx_result = await db.execute(tx_stmt)
    transactions = list(tx_result.scalars().all())

    reservation_stmt = select(CreditReservation).where(
        CreditReservation.status.in_(("reserved", "settlement_ready"))
    )
    if tenant_id:
        reservation_stmt = reservation_stmt.where(CreditReservation.tenant_id == tenant_id)
    reservation_result = await db.execute(reservation_stmt)
    reservations = list(reservation_result.scalars().all())

    tx_totals: dict[uuid.UUID, int] = {}
    for tx in transactions:
        tx_totals[tx.tenant_id] = tx_totals.get(tx.tenant_id, 0) + tx.delta

    reserved_totals: dict[uuid.UUID, int] = {}
    for reservation in reservations:
        reserved_totals[reservation.tenant_id] = reserved_totals.get(reservation.tenant_id, 0) + reservation.amount

    report = LedgerIntegrityReport(checked_tenants=len(balances))
    for balance in balances:
        expected_balance = tx_totals.get(balance.tenant_id, 0)
        if balance.balance != expected_balance:
            report.issues.append(LedgerIntegrityIssue(
                code="balance_drift",
                tenant_id=balance.tenant_id,
                expected=expected_balance,
                actual=balance.balance,
                message="credit_balances.balance does not equal credit_transactions delta sum",
            ))
        expected_reserved = reserved_totals.get(balance.tenant_id, 0)
        actual_reserved = balance.reserved or 0
        if actual_reserved != expected_reserved:
            report.issues.append(LedgerIntegrityIssue(
                code="reserved_drift",
                tenant_id=balance.tenant_id,
                expected=expected_reserved,
                actual=actual_reserved,
                message="credit_balances.reserved does not equal open credit reservation amount",
            ))

    if report.issues:
        logger.warning("[billing] ledger integrity issues detected: {}", [asdict(issue) for issue in report.issues])
    return report


async def expire_stale_credit_reservations(
    db: AsyncSession | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Recover expired reservations without discarding completed provider debt."""
    if db is None:
        # Import recoverable legacy tasks before an expiry sweep can release
        # their reserved Credits during the first startup after migration.
        from app.services.media_generation import backfill_legacy_minimax_video_tasks

        await backfill_legacy_minimax_video_tasks()
        async with async_session() as session:
            count = await expire_stale_credit_reservations(session, now=now)
            await session.commit()
            return count

    now = now or datetime.now(timezone.utc)
    active_media_reservations = select(MediaGenerationTask.reservation_id).where(
        MediaGenerationTask.status.in_(("submitting", "submitted", "processing", "retrying", "downloading")),
        MediaGenerationTask.reservation_id.is_not(None),
    )
    result = await db.execute(
        select(CreditReservation).where(
            CreditReservation.status.in_(("reserved", "settlement_ready")),
            CreditReservation.expires_at.is_not(None),
            CreditReservation.expires_at <= now,
            CreditReservation.id.not_in(active_media_reservations),
        )
    )
    reservations = list(result.scalars().all())
    recovered = 0
    for reservation in reservations:
        if reservation.status == "settlement_ready":
            try:
                await finalize_reserved_credits_in_session(db, reservation.id)
            except Exception as exc:
                # The durable debt stays held for the next sweep (or a top-up).
                logger.error(
                    "[billing] settlement-ready reservation recovery failed "
                    "reservation_id={} error_type={}",
                    reservation.id,
                    type(exc).__name__,
                )
                continue
        else:
            await release_reserved_credits_in_session(db, reservation.id, status="expired")
        recovered += 1
    if reservations:
        logger.info(
            "[billing] stale reservation sweep candidates={} recovered={}",
            len(reservations),
            recovered,
        )
    return recovered


async def reconcile_pending_payment_orders(
    db: AsyncSession | None = None,
) -> PaymentReconciliationReport:
    """Identify pending non-manual orders that likely missed a provider webhook."""
    if db is None:
        async with async_session() as session:
            return await reconcile_pending_payment_orders(session)

    result = await db.execute(
        select(PaymentOrder).where(
            PaymentOrder.status == "pending",
            PaymentOrder.provider != "manual",
        )
    )
    orders = list(result.scalars().all())
    report = PaymentReconciliationReport(checked_orders=len(orders))
    for order in orders:
        if not order.provider_session_id:
            report.issues.append(PaymentReconciliationIssue(
                code="missing_provider_session",
                order_id=order.id,
                tenant_id=order.tenant_id,
                provider=order.provider,
                status=order.status,
                message="non-manual pending order has no provider_session_id",
            ))
    if report.issues:
        logger.warning("[billing] payment reconciliation issues detected: {}", [asdict(issue) for issue in report.issues])
    return report


async def start_billing_reconciliation_daemon() -> None:
    """Run periodic billing integrity checks and stale reservation expiry."""
    settings = get_settings()
    ledger_interval = max(int(settings.BILLING_RECONCILIATION_INTERVAL_SECONDS), 300)
    reservation_interval = max(int(settings.BILLING_RESERVATION_EXPIRY_SWEEP_SECONDS), 60)
    next_ledger_check = 0.0

    logger.info(
        "[billing] reconciliation daemon started ledger_interval={}s reservation_interval={}s",
        ledger_interval,
        reservation_interval,
    )
    while True:
        loop_time = asyncio.get_running_loop().time()
        try:
            expired = await expire_stale_credit_reservations()
            if expired:
                logger.info("[billing] reservation sweep expired={}", expired)

            if loop_time >= next_ledger_check:
                ledger_report = await check_credit_ledger_integrity()
                payment_report = await reconcile_pending_payment_orders()
                logger.info(
                    "[billing] reconciliation complete checked_tenants={} ledger_issues={} checked_orders={} payment_issues={}",
                    ledger_report.checked_tenants,
                    len(ledger_report.issues),
                    payment_report.checked_orders,
                    len(payment_report.issues),
                )
                next_ledger_check = loop_time + ledger_interval
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[billing] reconciliation daemon iteration failed")

        await asyncio.sleep(reservation_interval)
