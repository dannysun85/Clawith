"""Billing reconciliation and credit-ledger integrity checks."""

from __future__ import annotations

import uuid
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.subscription import CreditBalance, CreditReservation, CreditTransaction, PaymentOrder
from app.models.media_generation import MediaGenerationTask
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    release_reserved_credits_in_session,
)
from app.services.media_generation import UNRESOLVED_MEDIA_STATUSES


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

    open_reservation_statuses = ("reserved", "provider_inflight", "settlement_ready")
    audited_tenants = union(
        select(CreditBalance.tenant_id.label("tenant_id")),
        select(CreditTransaction.tenant_id.label("tenant_id")),
        select(CreditReservation.tenant_id.label("tenant_id")).where(
            CreditReservation.status.in_(open_reservation_statuses)
        ),
    ).subquery("audited_credit_tenants")
    transaction_totals = (
        select(
            CreditTransaction.tenant_id.label("tenant_id"),
            func.sum(CreditTransaction.delta).label("expected_balance"),
        )
        .group_by(CreditTransaction.tenant_id)
        .subquery("credit_transaction_totals")
    )
    reservation_totals = (
        select(
            CreditReservation.tenant_id.label("tenant_id"),
            func.sum(CreditReservation.amount).label("expected_reserved"),
        )
        .where(CreditReservation.status.in_(open_reservation_statuses))
        .group_by(CreditReservation.tenant_id)
        .subquery("open_credit_reservation_totals")
    )
    integrity_stmt = (
        select(
            audited_tenants.c.tenant_id,
            CreditBalance.balance.label("actual_balance"),
            CreditBalance.reserved.label("actual_reserved"),
            func.coalesce(transaction_totals.c.expected_balance, 0).label("expected_balance"),
            func.coalesce(reservation_totals.c.expected_reserved, 0).label("expected_reserved"),
        )
        .select_from(audited_tenants)
        .outerjoin(
            CreditBalance,
            CreditBalance.tenant_id == audited_tenants.c.tenant_id,
        )
        .outerjoin(
            transaction_totals,
            transaction_totals.c.tenant_id == audited_tenants.c.tenant_id,
        )
        .outerjoin(
            reservation_totals,
            reservation_totals.c.tenant_id == audited_tenants.c.tenant_id,
        )
        .order_by(audited_tenants.c.tenant_id)
    )
    if tenant_id:
        integrity_stmt = integrity_stmt.where(audited_tenants.c.tenant_id == tenant_id)
    integrity_result = await db.execute(integrity_stmt)
    rows = list(integrity_result.all())

    report = LedgerIntegrityReport(checked_tenants=len(rows))
    for row in rows:
        audited_tenant_id = row.tenant_id
        if row.actual_balance is None:
            report.issues.append(
                LedgerIntegrityIssue(
                    code="missing_credit_balance",
                    tenant_id=audited_tenant_id,
                    expected=1,
                    actual=0,
                    message="credit activity exists without a credit_balances row",
                )
            )
            continue

        expected_balance = int(row.expected_balance)
        if row.actual_balance != expected_balance:
            report.issues.append(
                LedgerIntegrityIssue(
                    code="balance_drift",
                    tenant_id=audited_tenant_id,
                    expected=expected_balance,
                    actual=row.actual_balance,
                    message="credit_balances.balance does not equal credit_transactions delta sum",
                )
            )
        expected_reserved = int(row.expected_reserved)
        actual_reserved = row.actual_reserved or 0
        if actual_reserved != expected_reserved:
            report.issues.append(
                LedgerIntegrityIssue(
                    code="reserved_drift",
                    tenant_id=audited_tenant_id,
                    expected=expected_reserved,
                    actual=actual_reserved,
                    message="credit_balances.reserved does not equal open credit reservation amount",
                )
            )

    if report.issues:
        code_counts: dict[str, int] = {}
        for issue in report.issues:
            code_counts[issue.code] = code_counts.get(issue.code, 0) + 1
        logger.warning(
            "[billing] ledger integrity issues detected issue_count={} code_counts={}",
            len(report.issues),
            code_counts,
        )
    return report


async def expire_stale_credit_reservations(
    db: AsyncSession | None = None,
    *,
    now: datetime | None = None,
    _legacy_backfill_done: bool = False,
) -> int:
    """Recover expired reservations without discarding completed provider debt."""
    if not _legacy_backfill_done:
        # Do this for both scheduled and explicit SaaS-owner sweeps. Passing an
        # existing DB session must not bypass legacy video debt import.
        from app.services.media_generation import backfill_legacy_minimax_video_tasks

        await backfill_legacy_minimax_video_tasks()
    if db is None:
        async with async_session() as session:
            count = await expire_stale_credit_reservations(
                session,
                now=now,
                _legacy_backfill_done=True,
            )
            await session.commit()
            return count

    now = now or datetime.now(timezone.utc)
    active_media_reservations = select(MediaGenerationTask.reservation_id).where(
        MediaGenerationTask.status.in_(UNRESOLVED_MEDIA_STATUSES),
        MediaGenerationTask.reservation_id.is_not(None),
    )
    result = await db.execute(
        select(CreditReservation.id).where(
            CreditReservation.status.in_(("reserved", "provider_inflight", "settlement_ready")),
            CreditReservation.expires_at.is_not(None),
            CreditReservation.expires_at <= now,
            CreditReservation.id.not_in(active_media_reservations),
        )
    )
    candidate_ids = list(result.scalars().all())
    recovered = 0
    for reservation_id in candidate_ids:
        # Re-read every candidate under a row lock and repeat every eligibility
        # predicate. The initial query intentionally loads IDs only so an ORM
        # identity-map snapshot cannot survive a concurrent sweeper's commit.
        locked_result = await db.execute(
            select(CreditReservation)
            .where(
                CreditReservation.id == reservation_id,
                CreditReservation.status.in_(("reserved", "provider_inflight", "settlement_ready")),
                CreditReservation.expires_at.is_not(None),
                CreditReservation.expires_at <= now,
                CreditReservation.id.not_in(active_media_reservations),
            )
            .with_for_update(skip_locked=True)
        )
        reservation = locked_result.scalar_one_or_none()
        if reservation is None:
            continue
        if reservation.status == "settlement_ready":
            try:
                await finalize_reserved_credits_in_session(db, reservation.id)
            except Exception as exc:
                # The durable debt stays held for the next sweep (or a top-up).
                logger.error(
                    "[billing] settlement-ready reservation recovery failed reservation_id={} error_type={}",
                    reservation.id,
                    type(exc).__name__,
                )
                continue
        elif reservation.status == "provider_inflight":
            # The process may have lost the exact provider response before it
            # could durably transition to settlement_ready. Never guess by
            # charging the conservative hold or releasing possible provider
            # debt. Escalate it for operator reconciliation and re-alert daily.
            from app.services.production_issue_monitor import record_production_issue

            await record_production_issue(
                source="billing_reconciliation",
                category="billing_settlement",
                summary="Stale LLM provider-inflight Credits hold requires reconciliation",
                severity="critical",
                error_code="stale_provider_inflight",
                operation=reservation.action,
                tenant_id=reservation.tenant_id,
                user_id=reservation.user_id,
                agent_id=reservation.agent_id,
                metadata={"reservation_id": str(reservation.id)},
            )
            reservation.expires_at = now + timedelta(hours=24)
            logger.error(
                "[billing] stale provider-inflight reservation retained reservation_id={}",
                reservation.id,
            )
            continue
        else:
            await release_reserved_credits_in_session(db, reservation.id, status="expired")
        recovered += 1
    if candidate_ids:
        logger.info(
            "[billing] stale reservation sweep candidates={} recovered={}",
            len(candidate_ids),
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
            report.issues.append(
                PaymentReconciliationIssue(
                    code="missing_provider_session",
                    order_id=order.id,
                    tenant_id=order.tenant_id,
                    provider=order.provider,
                    status=order.status,
                    message="non-manual pending order has no provider_session_id",
                )
            )
    if report.issues:
        code_counts: dict[str, int] = {}
        for issue in report.issues:
            code_counts[issue.code] = code_counts.get(issue.code, 0) + 1
        logger.warning(
            "[billing] payment reconciliation issues detected issue_count={} code_counts={}",
            len(report.issues),
            code_counts,
        )
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
