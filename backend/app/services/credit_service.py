"""Credit balance, cost rules, and ledger services.

Provides atomic credit charging with SELECT FOR UPDATE and writes an audit
record to credit_transactions for every consume event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.subscription import (
    BillingRule,
    CreditBalance,
    CreditPack,
    CreditReservation,
    CreditTransaction,
    Plan,
    Subscription,
)
from app.services.modalities import canonicalize_modality
from app.services.quota_guard import QuotaExceeded, subscription_action_message

IDEMPOTENT_GRANT_REASONS = {"subscribe", "topup", "refund", "refund_clawback"}
SUBSCRIPTION_PLAN_CHANGE_REF_TYPE = "subscription_plan_change"


def subscription_plan_change_grant_ref_id(
    subscription_id: uuid.UUID,
    previous_plan_id: uuid.UUID,
    next_plan_id: uuid.UUID,
    changed_at: datetime,
) -> uuid.UUID:
    """Build the idempotency key for one subscription plan-change grant.

    The subscription row is retained across plan changes. Reusing only its ID
    would collide with the initial grant and silently suppress every upgrade.
    """

    normalized_at = changed_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return uuid.uuid5(
        subscription_id,
        f"{previous_plan_id}->{next_plan_id}@{normalized_at}",
    )


async def get_credit_balance(tenant_id: uuid.UUID) -> CreditBalance:
    """Get or create a tenant's credit balance row."""
    async with async_session() as db:
        result = await db.execute(select(CreditBalance).where(CreditBalance.tenant_id == tenant_id))
        balance = result.scalar_one_or_none()
        if not balance:
            balance = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
            db.add(balance)
        await ensure_current_subscription_credit_grant_in_session(
            db,
            tenant_id=tenant_id,
            balance_row=balance,
        )
        await db.commit()
        return balance


async def ensure_current_subscription_credit_grant_in_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    balance_row: CreditBalance | None = None,
    granted_by: uuid.UUID | None = None,
) -> CreditTransaction | None:
    """Repair a missing current-subscription credit grant exactly once.

    Older migrations could create an active Free subscription and a 0 balance
    without the matching subscribe ledger entry. This reconciles that state
    before balance reads or charges, while staying idempotent via the
    subscription-scoped subscribe transaction.
    """
    subscription_result = await db.execute(
        select(Subscription)
        .where(
            Subscription.tenant_id == tenant_id,
            Subscription.status.in_(("active", "trialing", "canceled", "past_due")),
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = subscription_result.scalar_one_or_none()
    if not subscription:
        return None

    plan = await db.get(Plan, subscription.plan_id)
    credits_per_period = plan.credits_per_period if plan else 0
    if credits_per_period <= 0:
        return None

    existing_tx_result = await db.execute(
        select(CreditTransaction.id)
        .where(
            CreditTransaction.tenant_id == tenant_id,
            CreditTransaction.reason == "subscribe",
            CreditTransaction.ref_type == "subscription",
            CreditTransaction.ref_id == subscription.id,
        )
        .limit(1)
    )
    if existing_tx_result.scalar_one_or_none():
        return None

    if balance_row is None:
        balance_result = await db.execute(
            select(CreditBalance)
            .where(CreditBalance.tenant_id == tenant_id)
            .with_for_update()
        )
        balance_row = balance_result.scalar_one_or_none()
        if not balance_row:
            balance_row = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
            db.add(balance_row)

    balance_row.balance += credits_per_period
    balance_row.updated_at = datetime.now(timezone.utc)

    tx = CreditTransaction(
        tenant_id=tenant_id,
        delta=credits_per_period,
        balance_after=balance_row.balance,
        reason="subscribe",
        ref_type="subscription",
        ref_id=subscription.id,
        user_id=granted_by,
    )
    db.add(tx)
    return tx


async def get_credit_cost(action: str, modality: str | None, saas_tier: str | None) -> int:
    """Look up the credit cost for an action/modality/tier combination.

    Falls back through specificity levels until a rule is found:
    1. exact (action, modality, tier)
    2. (action, modality, tier=None)
    3. (action, modality=None, tier)
    4. (action, modality=None, tier=None)
    5. 0 if no rule matches
    """
    modality = canonicalize_modality(modality)
    tier = (saas_tier or "").lower() or None

    async with async_session() as db:
        result = await db.execute(
            select(BillingRule).where(
                BillingRule.action == action,
                BillingRule.enabled == True,  # noqa: E712
            )
        )
        rules = result.scalars().all()

    candidates = []
    for rule in rules:
        rule_modality = canonicalize_modality(rule.modality)
        rule_tier = (rule.tier or "").lower() or None
        if rule_modality == modality and rule_tier == tier:
            candidates.append((3, rule))
        elif rule_modality == modality and rule_tier is None:
            candidates.append((2, rule))
        elif rule_modality is None and rule_tier == tier:
            candidates.append((1, rule))
        elif rule_modality is None and rule_tier is None:
            candidates.append((0, rule))

    if not candidates:
        return 0

    # Highest specificity, then highest priority
    candidates.sort(key=lambda x: (x[0], x[1].priority), reverse=True)
    return candidates[0][1].credit_cost


async def check_credit_balance(
    tenant_id: uuid.UUID,
    action: str,
    modality: str | None,
    saas_tier: str | None,
) -> None:
    """Raise QuotaExceeded if the tenant cannot afford the action."""
    cost = await get_credit_cost(action, modality, saas_tier)
    if cost <= 0:
        return

    balance_row = await get_credit_balance(tenant_id)
    available = balance_row.balance - (balance_row.reserved or 0)
    if available < cost:
        raise QuotaExceeded(
            subscription_action_message("Credits 不足，无法继续执行。"),
            quota_type="insufficient_credits",
        )


async def check_credit_amount(tenant_id: uuid.UUID, amount: int) -> None:
    """Raise QuotaExceeded if the tenant cannot afford a concrete credit amount."""
    if amount <= 0:
        return

    balance_row = await get_credit_balance(tenant_id)
    available = balance_row.balance - (balance_row.reserved or 0)
    if available < amount:
        raise QuotaExceeded(
            subscription_action_message("Credits 不足，无法继续执行。"),
            quota_type="insufficient_credits",
        )


async def charge_credits(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    action: str,
    modality: str | None,
    saas_tier: str | None,
    provider: str | None,
    model: str | None,
    delta: int | None = None,
) -> CreditTransaction:
    """Atomically deduct credits and write a ledger row.

    If delta is not provided, it is looked up from billing_rules.
    Raises QuotaExceeded on insufficient balance.
    """
    async with async_session() as db:
        tx = await charge_credits_in_session(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action=action,
            modality=modality,
            saas_tier=saas_tier,
            provider=provider,
            model=model,
            delta=delta,
        )
        await db.commit()
        return tx


async def charge_credits_in_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    action: str,
    modality: str | None,
    saas_tier: str | None,
    provider: str | None,
    model: str | None,
    delta: int | None = None,
) -> CreditTransaction:
    """Deduct credits and write a ledger row using the caller's transaction."""
    if delta is None:
        delta = await get_credit_cost(action, modality, saas_tier)
    if delta <= 0:
        # Still write a zero-cost ledger row for audit completeness
        delta = 0

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if not balance_row:
        balance_row = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
        db.add(balance_row)

    await ensure_current_subscription_credit_grant_in_session(
        db,
        tenant_id=tenant_id,
        balance_row=balance_row,
    )

    available = balance_row.balance - (balance_row.reserved or 0)
    if available < delta:
        raise QuotaExceeded(
            subscription_action_message("Credits 不足，无法继续执行。"),
            quota_type="insufficient_credits",
        )

    balance_row.balance -= delta
    balance_row.updated_at = datetime.now(timezone.utc)

    tx = CreditTransaction(
        tenant_id=tenant_id,
        delta=-delta,
        balance_after=balance_row.balance,
        reason="consume",
        ref_type="agent" if agent_id else None,
        ref_id=agent_id,
        user_id=user_id,
        agent_id=agent_id,
        action=action,
        modality=canonicalize_modality(modality),
        tier=(saas_tier or "").lower() or None,
        provider=provider,
        model=model,
    )
    db.add(tx)
    return tx


async def reserve_credits(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    action: str,
    modality: str | None,
    saas_tier: str | None,
    provider: str | None,
    model: str | None,
    amount: int | None = None,
    ref_type: str | None = None,
    ref_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    initial_status: str = "reserved",
) -> CreditReservation:
    """Hold credits for an asynchronous operation."""
    async with async_session() as db:
        reservation = await reserve_credits_in_session(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action=action,
            modality=modality,
            saas_tier=saas_tier,
            provider=provider,
            model=model,
            amount=amount,
            ref_type=ref_type,
            ref_id=ref_id,
            expires_at=expires_at,
            initial_status=initial_status,
        )
        await db.commit()
        return reservation


async def reserve_credits_in_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    action: str,
    modality: str | None,
    saas_tier: str | None,
    provider: str | None,
    model: str | None,
    amount: int | None = None,
    ref_type: str | None = None,
    ref_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    initial_status: str = "reserved",
) -> CreditReservation:
    """Reserve credits using the caller's transaction/session."""
    if initial_status not in {"reserved", "provider_inflight"}:
        raise ValueError(f"Unsupported initial credit reservation status: {initial_status}")
    if amount is None:
        amount = await get_credit_cost(action, modality, saas_tier)
    if amount <= 0:
        amount = 0

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if not balance_row:
        balance_row = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
        db.add(balance_row)

    await ensure_current_subscription_credit_grant_in_session(
        db,
        tenant_id=tenant_id,
        balance_row=balance_row,
    )

    available = balance_row.balance - (balance_row.reserved or 0)
    if available < amount:
        raise QuotaExceeded(
            subscription_action_message("Credits 不足，无法继续执行。"),
            quota_type="insufficient_credits",
        )

    balance_row.reserved = (balance_row.reserved or 0) + amount
    balance_row.updated_at = datetime.now(timezone.utc)
    reservation = CreditReservation(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        action=action,
        modality=canonicalize_modality(modality),
        tier=(saas_tier or "").lower() or None,
        provider=provider,
        model=model,
        amount=amount,
        status=initial_status,
        ref_type=ref_type,
        ref_id=ref_id,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=24)),
    )
    db.add(reservation)
    return reservation


async def finalize_reserved_credits(reservation_id: uuid.UUID) -> CreditTransaction:
    """Finalize a reservation in its own transaction."""
    async with async_session() as db:
        tx = await finalize_reserved_credits_in_session(db, reservation_id)
        await db.commit()
        return tx


async def mark_credit_reservation_settlement_ready(
    reservation_id: uuid.UUID,
    *,
    amount: int,
) -> CreditReservation:
    """Persist the exact provider debt before a result or side effect is released.

    The reservation itself is the durable settlement outbox.  LLM requests are
    created as ``provider_inflight`` before network I/O; ``settlement_ready``
    means the provider has completed and the exact amount must never be
    released by an expiry/error path.  The balance hold is resized under the
    same row locks, so concurrent calls cannot all pass a read-only preflight.
    """
    async with async_session() as db:
        reservation = await mark_credit_reservation_settlement_ready_in_session(
            db,
            reservation_id,
            amount=amount,
        )
        await db.commit()
        return reservation


async def mark_credit_reservation_settlement_ready_in_session(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    amount: int,
) -> CreditReservation:
    """Resize and mark a reservation as an irrevocable provider debt."""
    exact_amount = max(int(amount or 0), 0)
    reservation = await db.get(
        CreditReservation,
        reservation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if not reservation:
        raise ValueError("Credit reservation not found")
    if reservation.status == "finalized":
        return reservation
    if reservation.status == "settlement_ready":
        if reservation.amount != exact_amount:
            raise ValueError("Credit reservation already has a different settlement amount")
        return reservation
    if reservation.status not in {"reserved", "provider_inflight"}:
        raise ValueError(
            f"Credit reservation is not settlement-ready eligible (status={reservation.status})"
        )

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == reservation.tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if not balance_row:
        raise ValueError("Credit balance not found for reservation")

    # A provider response has already incurred this debt.  If the conservative
    # pre-hold underestimated it, keep the full amount reserved even when it
    # temporarily exceeds the current balance.  Future calls remain blocked;
    # reconciliation finalizes after a top-up instead of losing the debt.
    balance_row.reserved = max(
        (balance_row.reserved or 0) + exact_amount - reservation.amount,
        0,
    )
    balance_row.updated_at = datetime.now(timezone.utc)
    reservation.amount = exact_amount
    reservation.status = "settlement_ready"
    return reservation


async def finalize_reserved_credits_in_session(
    db: AsyncSession,
    reservation_id: uuid.UUID,
) -> CreditTransaction:
    """Consume a reserved amount exactly once and write the ledger row."""
    reservation = await db.get(
        CreditReservation,
        reservation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if not reservation:
        raise ValueError("Credit reservation not found")

    existing_tx_result = await db.execute(
        select(CreditTransaction)
        .where(
            CreditTransaction.tenant_id == reservation.tenant_id,
            CreditTransaction.reason == "consume",
            CreditTransaction.ref_type == "reservation",
            CreditTransaction.ref_id == reservation.id,
        )
        .limit(1)
    )
    existing_tx = existing_tx_result.scalar_one_or_none()
    if existing_tx:
        reservation.status = "finalized"
        reservation.finalized_at = reservation.finalized_at or datetime.now(timezone.utc)
        return existing_tx

    if reservation.status not in {"reserved", "settlement_ready"}:
        raise ValueError(f"Credit reservation is not finalizable (status={reservation.status})")

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == reservation.tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if not balance_row:
        raise ValueError("Credit balance not found for reservation")
    if balance_row.balance < reservation.amount:
        raise QuotaExceeded(
            subscription_action_message("Credits 不足，无法完成结算。"),
            quota_type="insufficient_credits",
        )

    balance_row.balance -= reservation.amount
    balance_row.reserved = max((balance_row.reserved or 0) - reservation.amount, 0)
    balance_row.updated_at = datetime.now(timezone.utc)
    reservation.status = "finalized"
    reservation.finalized_at = datetime.now(timezone.utc)

    tx = CreditTransaction(
        tenant_id=reservation.tenant_id,
        delta=-reservation.amount,
        balance_after=balance_row.balance,
        reason="consume",
        ref_type="reservation",
        ref_id=reservation.id,
        user_id=reservation.user_id,
        agent_id=reservation.agent_id,
        action=reservation.action,
        modality=reservation.modality,
        tier=reservation.tier,
        provider=reservation.provider,
        model=reservation.model,
    )
    db.add(tx)
    return tx


async def release_reserved_credits(
    reservation_id: uuid.UUID,
    status: str = "released",
    *,
    release_provider_inflight: bool = False,
) -> CreditReservation:
    """Release a reservation in its own transaction."""
    async with async_session() as db:
        reservation = await release_reserved_credits_in_session(
            db,
            reservation_id,
            status=status,
            release_provider_inflight=release_provider_inflight,
        )
        await db.commit()
        return reservation


async def release_reserved_credits_in_session(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    status: str = "released",
    release_provider_inflight: bool = False,
) -> CreditReservation:
    """Release held credits without writing a consume ledger row."""
    reservation = await db.get(
        CreditReservation,
        reservation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if not reservation:
        raise ValueError("Credit reservation not found")
    releasable_statuses = {"reserved"}
    if release_provider_inflight:
        releasable_statuses.add("provider_inflight")
    if reservation.status not in releasable_statuses:
        return reservation

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == reservation.tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if balance_row:
        balance_row.reserved = max((balance_row.reserved or 0) - reservation.amount, 0)
        balance_row.updated_at = datetime.now(timezone.utc)
    reservation.status = status
    return reservation


async def grant_credits(
    tenant_id: uuid.UUID,
    amount: int,
    reason: str,
    granted_by: uuid.UUID | None = None,
    ref_type: str | None = None,
    ref_id: uuid.UUID | None = None,
) -> CreditTransaction:
    """Add credits to a tenant (e.g. admin grant, subscription award, top-up)."""
    async with async_session() as db:
        tx = await grant_credits_in_session(
            db,
            tenant_id=tenant_id,
            amount=amount,
            reason=reason,
            granted_by=granted_by,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        await db.commit()
        return tx


async def grant_credits_in_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    amount: int,
    reason: str,
    granted_by: uuid.UUID | None = None,
    ref_type: str | None = None,
    ref_id: uuid.UUID | None = None,
) -> CreditTransaction:
    """Add credits using the caller's transaction/session."""
    if amount <= 0:
        raise ValueError("grant amount must be positive")

    if reason in IDEMPOTENT_GRANT_REASONS and ref_type and ref_id:
        existing_tx_result = await db.execute(
            select(CreditTransaction)
            .where(
                CreditTransaction.tenant_id == tenant_id,
                CreditTransaction.reason == reason,
                CreditTransaction.ref_type == ref_type,
                CreditTransaction.ref_id == ref_id,
            )
            .limit(1)
        )
        existing_tx = existing_tx_result.scalar_one_or_none()
        if existing_tx:
            return existing_tx

    result = await db.execute(
        select(CreditBalance)
        .where(CreditBalance.tenant_id == tenant_id)
        .with_for_update()
    )
    balance_row = result.scalar_one_or_none()
    if not balance_row:
        balance_row = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
        db.add(balance_row)

    balance_row.balance += amount
    balance_row.updated_at = datetime.now(timezone.utc)

    tx = CreditTransaction(
        tenant_id=tenant_id,
        delta=amount,
        balance_after=balance_row.balance,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        user_id=granted_by,
    )
    db.add(tx)
    return tx


async def list_credit_transactions(
    tenant_id: uuid.UUID,
    page: int = 1,
    limit: int = 50,
    action: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> tuple[list[CreditTransaction], int]:
    """Return paginated credit transactions for a tenant and total count."""
    async with async_session() as db:
        stmt = select(CreditTransaction).where(CreditTransaction.tenant_id == tenant_id)
        if action:
            stmt = stmt.where(CreditTransaction.action == action)
        if from_date:
            stmt = stmt.where(CreditTransaction.created_at >= from_date)
        if to_date:
            stmt = stmt.where(CreditTransaction.created_at <= to_date)

        count_result = await db.execute(
            select(CreditTransaction).where(CreditTransaction.tenant_id == tenant_id)
        )
        total = len(count_result.scalars().all())

        result = await db.execute(
            stmt.order_by(CreditTransaction.created_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list(result.scalars().all()), total


async def get_credit_packs(active_only: bool = True) -> list[CreditPack]:
    """Return credit packs, optionally filtering to active ones."""
    async with async_session() as db:
        stmt = select(CreditPack)
        if active_only:
            stmt = stmt.where(CreditPack.is_active == True)  # noqa: E712
        stmt = stmt.order_by(CreditPack.sort_order)
        result = await db.execute(stmt)
        return list(result.scalars().all())
