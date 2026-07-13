"""Subscription APIs (阶段0: plans, entitlements, subscriptions, admin assign, usage).

No payment in 阶段0 — admin assigns plans manually. Payment integration in 阶段2.
See SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §3 / §9.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user, get_saas_admin
from app.database import get_db
from app.models.agent import Agent
from app.models.subscription import (
    BillingProfile,
    CreditPack,
    CreditTransaction,
    PaymentOrder,
    Plan,
    Subscription,
    TenantUsage,
)
from app.models.user import User
from app.schemas.subscription import (
    AssignPlanIn,
    BillingProfileIn,
    BillingProfileOut,
    CheckoutSubscribeIn,
    CheckoutTopupIn,
    CreditBalanceOut,
    CreditPackOut,
    CreditTransactionOut,
    EntitlementsOut,
    PaymentOrderOut,
    PlanCreateIn,
    PlanOut,
    PlanUpdateIn,
    SeatUsageOut,
    SubscriptionOut,
    SubscriptionSummaryOut,
    UsageOut,
)
from app.services.billing_events import process_billing_webhook_event
from app.services.billing_provider import get_billing_provider
from app.services.credit_service import (
    get_credit_balance,
    get_credit_packs,
    grant_credits_in_session,
    list_credit_transactions,
)
from app.services.entitlements import get_active_subscription, get_tenant_entitlements
from app.services.agent_plan_selection import reconcile_tenant_agent_plan_selections

router = APIRouter(prefix="/subscription", tags=["subscription"])


def _client_user_label(user: object | None) -> str | None:
    """Return a readable user label for client ledgers without exposing UUIDs."""
    if not user:
        return None
    for attr in ("display_name", "username", "email"):
        try:
            value = getattr(user, attr, None)
        except Exception:
            value = None
        if value:
            return str(value)
    return None


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all active plans (public to logged-in users)."""
    result = await db.execute(
        select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)  # noqa: E712
    )
    return result.scalars().all()


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    data: PlanCreateIn,
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a plan (admin only). code is immutable after creation."""
    existing = await db.execute(select(Plan).where(Plan.code == data.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Plan code '{data.code}' already exists")
    plan = Plan(
        code=data.code,
        name=data.name,
        tier=data.tier,
        period=data.period,
        price_cents=data.price_cents,
        currency=data.currency,
        max_agents=data.max_agents,
        max_llm_calls_per_day=data.max_llm_calls_per_day,
        message_limit=data.message_limit,
        message_period=data.message_period,
        max_triggers=data.max_triggers,
        credits_per_period=data.credits_per_period,
        allowed_modalities=data.allowed_modalities,
        allowed_tiers=data.allowed_tiers,
        features=data.features,
        sort_order=data.sort_order,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    data: PlanUpdateIn,
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a plan (admin only). code is immutable."""
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(plan, k, v)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(
    plan_id: uuid.UUID,
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a plan (is_active=false). The free plan is protected.

    Existing subscriptions keep resolving (they reference plan_id directly);
    deactivated plans just drop out of the public list and can't be newly assigned.
    """
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.code == "free":
        raise HTTPException(status_code=400, detail="Cannot delete the free plan")
    plan.is_active = False
    await db.commit()


@router.get("/my-entitlements", response_model=EntitlementsOut | None)
async def get_my_entitlements(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's entitlements + subscription state (for subscription tab)."""
    if not current_user.tenant_id:
        return None
    ent = await get_tenant_entitlements(current_user.tenant_id)
    sub = await get_active_subscription(current_user.tenant_id)
    if not ent and not sub:
        # No subscription → return defaults (frontend shows free fallback)
        return EntitlementsOut(subscription_status=sub.status if sub else None)
    return EntitlementsOut(
        plan_id=ent.plan_id if ent else sub.plan_id,
        plan_code=ent.plan_code if ent else None,
        max_agents=ent.max_agents if ent else 0,
        max_llm_calls_per_day=ent.max_llm_calls_per_day if ent else 0,
        message_limit=ent.message_limit if ent else 0,
        message_period=ent.message_period if ent else "permanent",
        max_triggers=ent.max_triggers if ent else 0,
        credits_per_period=ent.credits_per_period if ent else 0,
        allowed_modalities=ent.allowed_modalities if ent else [],
        allowed_tiers=ent.allowed_tiers if ent else [],
        generation_modalities=ent.generation_modalities if ent else [],
        generation_tiers=ent.generation_tiers if ent else [],
        subscription_status=sub.status if sub else None,
        period_end=sub.period_end if sub else None,
    )


@router.get("/subscriptions", response_model=SubscriptionOut | None)
async def get_my_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's subscription."""
    if not current_user.tenant_id:
        return None
    sub = await get_active_subscription(current_user.tenant_id)
    if not sub:
        return None
    plan = await db.get(Plan, sub.plan_id)
    out = SubscriptionOut.model_validate(sub)
    out.plan_code = plan.code if plan else None
    return out


@router.post("/subscriptions/assign", response_model=SubscriptionOut)
async def assign_subscription(
    data: AssignPlanIn,
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    """Assign a plan to a tenant (admin only, no payment — 阶段0).

    State machine (3.6): upgrade applies immediately; downgrade at period end
    (simplified in 阶段0: admin reassign takes effect immediately).
    """
    plan = await db.get(Plan, data.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(status_code=404, detail="Plan not found")

    existing = await get_active_subscription(data.tenant_id)
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=data.period_days) if data.period_days else None

    if existing:
        if hasattr(db, "merge"):
            existing = await db.merge(existing)
        plan_changed = existing.plan_id != data.plan_id
        # Upgrade/switch: immediate (3.6)
        existing.plan_id = data.plan_id
        existing.status = "active"
        existing.period_end = period_end
        existing.cancel_at_period_end = False
        sub = existing
    else:
        plan_changed = True
        sub = Subscription(
            tenant_id=data.tenant_id,
            plan_id=data.plan_id,
            status="active",
            period_start=now,
            period_end=period_end,
        )
        db.add(sub)
    await db.flush()
    if plan_changed and plan.credits_per_period > 0:
        await grant_credits_in_session(
            db,
            tenant_id=data.tenant_id,
            amount=plan.credits_per_period,
            reason="subscribe",
            granted_by=current_user.id,
            ref_type="subscription",
            ref_id=sub.id,
        )
    await db.commit()
    await db.refresh(sub)
    # Reconcile agent count to the new plan's max_agents (3.6):
    # upgrade/renew → restore stopped agents; downgrade → stop excess.
    from app.services.subscription_lifecycle import enforce_agent_limit, restore_stopped_agents
    await reconcile_tenant_agent_plan_selections(data.tenant_id)
    await restore_stopped_agents(data.tenant_id)
    await enforce_agent_limit(data.tenant_id)
    out = SubscriptionOut.model_validate(sub)
    out.plan_code = plan.code
    return out


@router.get("/usage", response_model=UsageOut | None)
async def get_my_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's usage today (for subscription tab display)."""
    if not current_user.tenant_id:
        return None
    from app.services.quota_guard import _today_in_tenant_tz

    today = await _today_in_tenant_tz(current_user.tenant_id)
    result = await db.execute(
        select(TenantUsage).where(
            TenantUsage.tenant_id == current_user.tenant_id,
            TenantUsage.period_date == today,
        )
    )
    usage = result.scalar_one_or_none()
    cb = await get_credit_balance(current_user.tenant_id)
    ent = await get_tenant_entitlements(current_user.tenant_id)
    llm_calls_limit = usage.llm_calls_limit if usage and usage.llm_calls_limit else (ent.max_llm_calls_per_day if ent else 0)
    messages_limit = usage.messages_limit if usage and usage.messages_limit else (ent.message_limit if ent else 0)
    return UsageOut(
        period_date=str(today),
        llm_calls_used=usage.llm_calls_used if usage else 0,
        llm_calls_limit=llm_calls_limit,
        messages_used=usage.messages_used if usage else 0,
        messages_limit=messages_limit,
        tokens_used=usage.tokens_used if usage else 0,
        credits_balance=cb.balance if cb else 0,
    )


@router.get("/credits", response_model=CreditBalanceOut)
async def get_credits(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's credit balance."""
    if not current_user.tenant_id:
        return CreditBalanceOut(balance=0, reserved=0)
    balance = await get_credit_balance(current_user.tenant_id)
    return CreditBalanceOut(balance=balance.balance, reserved=balance.reserved)


@router.get("/credit-transactions", response_model=list[CreditTransactionOut])
async def get_credit_transactions(
    page: int = 1,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's credit transaction ledger."""
    if not current_user.tenant_id:
        return []
    transactions, _ = await list_credit_transactions(
        current_user.tenant_id, page=page, limit=limit
    )
    out: list[CreditTransactionOut] = []
    agent_labels: dict[uuid.UUID, str] = {}
    user_labels: dict[uuid.UUID, str] = {}
    for tx in transactions:
        consumer_label = None
        actor_label = None
        if tx.reason == "refund" and tx.ref_type == "product_incident":
            # Historical incident compensation is a platform operation, not an
            # anonymous customer action. Project labels at read time so the
            # immutable ledger and its balance snapshots remain untouched.
            consumer_label = "平台事故补偿"
            actor_label = "系统管理员"
        if tx.agent_id:
            if tx.agent_id not in agent_labels:
                agent = await db.get(Agent, tx.agent_id)
                agent_labels[tx.agent_id] = agent.name if agent else str(tx.agent_id)
            consumer_label = consumer_label or agent_labels[tx.agent_id]
        if tx.user_id:
            if tx.user_id not in user_labels:
                actor = await db.get(User, tx.user_id)
                user_labels[tx.user_id] = (
                    _client_user_label(actor)
                    or (_client_user_label(current_user) if tx.user_id == current_user.id else None)
                    or str(tx.user_id)
                )
            actor_label = actor_label or user_labels[tx.user_id]
        out.append(
            CreditTransactionOut.model_validate(tx).model_copy(
                update={
                    "consumer_label": consumer_label,
                    "actor_label": actor_label,
                }
            )
        )
    return out


@router.get("/orders", response_model=list[PaymentOrderOut])
async def get_my_orders(
    page: int = 1,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's payment/subscription orders for the client order-history tab."""
    if not current_user.tenant_id:
        return []
    safe_page = max(page, 1)
    safe_limit = min(max(limit, 1), 100)
    result = await db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.tenant_id == current_user.tenant_id)
        .order_by(PaymentOrder.created_at.desc())
        .offset((safe_page - 1) * safe_limit)
        .limit(safe_limit)
    )
    return result.scalars().all()


@router.get("/credit-packs", response_model=list[CreditPackOut])
async def list_credit_packs(
    current_user: User = Depends(get_current_user),
):
    """List active credit packs (Boost) for purchase."""
    return await get_credit_packs(active_only=True)


@router.get("/seats", response_model=SeatUsageOut)
async def get_seats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current tenant's Agent seat usage.

    In the client subscription UI, Seats refers to public Agent seats from the
    plan's max_agents entitlement, not human users in the company.
    """
    if not current_user.tenant_id:
        return SeatUsageOut(seats_total=0, seats_used=0, pending_invites=0)

    from app.services.quota_guard import _count_active_tenant_agents

    sub = await get_active_subscription(current_user.tenant_id)
    ent = await get_tenant_entitlements(current_user.tenant_id)
    seats_total = ent.max_agents if ent else (sub.seats if sub else 1)
    seats_used = await _count_active_tenant_agents(current_user.tenant_id, db)

    return SeatUsageOut(
        seats_total=seats_total,
        seats_used=seats_used,
        pending_invites=0,
    )


@router.get("/summary", response_model=SubscriptionSummaryOut)
async def get_subscription_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the server-side billing/usage summary used by client subscription pages."""
    if not current_user.tenant_id:
        return SubscriptionSummaryOut()

    tenant_id = current_user.tenant_id
    sub = await get_active_subscription(tenant_id)
    plan = await db.get(Plan, sub.plan_id) if sub else None
    ent = await get_tenant_entitlements(tenant_id)
    balance = await get_credit_balance(tenant_id)

    result = await db.execute(
        select(CreditTransaction).where(CreditTransaction.tenant_id == tenant_id)
    )
    transactions = list(result.scalars().all())
    period_grant = sum(tx.delta for tx in transactions if tx.reason == "subscribe" and tx.delta > 0)
    topup_grants = sum(tx.delta for tx in transactions if tx.reason == "topup" and tx.delta > 0)
    consumed_credits = abs(sum(tx.delta for tx in transactions if tx.reason == "consume" and tx.delta < 0))
    refunded_credits = sum(tx.delta for tx in transactions if tx.reason == "refund" and tx.delta > 0)
    total_granted = period_grant + topup_grants + refunded_credits

    from app.services.quota_guard import _count_active_tenant_agents

    seats_total = ent.max_agents if ent else (plan.max_agents if plan else (sub.seats if sub else 0))
    seats_used = await _count_active_tenant_agents(tenant_id, db)
    reserved = balance.reserved or 0
    return SubscriptionSummaryOut(
        plan_id=ent.plan_id if ent else (plan.id if plan else None),
        plan_code=ent.plan_code if ent else (plan.code if plan else None),
        subscription_status=sub.status if sub else None,
        period_start=sub.period_start if sub else None,
        period_end=sub.period_end if sub else None,
        period_grant=period_grant,
        topup_grants=topup_grants,
        consumed_credits=consumed_credits,
        refunded_credits=refunded_credits,
        total_granted=total_granted,
        balance=balance.balance,
        reserved=reserved,
        available_balance=max(balance.balance - reserved, 0),
        seats_used=seats_used,
        seats_total=seats_total,
        llm_calls_limit=ent.max_llm_calls_per_day if ent else 0,
        message_limit=ent.message_limit if ent else 0,
        max_triggers=ent.max_triggers if ent else 0,
    )


@router.post("/checkout/subscribe", response_model=PaymentOrderOut, status_code=status.HTTP_201_CREATED)
async def checkout_subscribe(
    data: CheckoutSubscribeIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a subscription checkout order."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    plan = await db.get(Plan, data.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Simple price logic: yearly = 10 months equivalent
    if data.period == "yearly":
        amount_cents = plan.price_cents * 10
    else:
        amount_cents = plan.price_cents

    order = PaymentOrder(
        tenant_id=current_user.tenant_id,
        type="subscribe",
        plan_id=plan.id,
        amount_cents=amount_cents,
        currency=plan.currency,
        status="pending",
    )
    db.add(order)
    await db.flush()
    try:
        provider = get_billing_provider()
        checkout = await provider.create_subscription_checkout(
            order=order,
            plan=plan,
            period=data.period,
            seats=data.seats,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    order.provider = checkout.provider
    order.provider_session_id = checkout.session_id
    order.provider_payment_id = checkout.payment_id
    await db.commit()
    await db.refresh(order)
    setattr(order, "session_url", checkout.session_url)
    return order


@router.post("/checkout/topup", response_model=PaymentOrderOut, status_code=status.HTTP_201_CREATED)
async def checkout_topup(
    data: CheckoutTopupIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a credit top-up checkout order."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    pack = await db.get(CreditPack, data.credit_pack_id)
    if not pack or not pack.is_active:
        raise HTTPException(status_code=404, detail="Credit pack not found")

    order = PaymentOrder(
        tenant_id=current_user.tenant_id,
        type="topup",
        credits=pack.credits,
        amount_cents=pack.price_cents,
        currency=pack.currency,
        status="pending",
    )
    db.add(order)
    await db.flush()
    try:
        provider = get_billing_provider()
        checkout = await provider.create_topup_checkout(order=order, pack=pack)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    order.provider = checkout.provider
    order.provider_session_id = checkout.session_id
    order.provider_payment_id = checkout.payment_id
    await db.commit()
    await db.refresh(order)
    setattr(order, "session_url", checkout.session_url)
    return order


@router.get("/checkout/{order_id}/status", response_model=PaymentOrderOut)
async def get_checkout_status(
    order_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get payment order status."""
    order = await db.get(PaymentOrder, order_id)
    if not order or order.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/billing/webhook/{provider_name}")
async def billing_webhook(
    provider_name: str,
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """Signed billing webhook endpoint. Auth is provider signature, not user JWT."""
    payload = await request.body()
    try:
        provider = get_billing_provider(provider_name)
        result = await process_billing_webhook_event(
            db,
            provider_name=provider_name,
            payload=payload,
            signature=stripe_signature,
            provider=provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    order_id = result.get("order_id")
    if order_id:
        order = await db.get(PaymentOrder, uuid.UUID(str(order_id)))
        if order and order.type == "subscribe" and order.status == "paid":
            from app.services.subscription_lifecycle import enforce_agent_limit, restore_stopped_agents

            await reconcile_tenant_agent_plan_selections(order.tenant_id)
            await restore_stopped_agents(order.tenant_id)
            await enforce_agent_limit(order.tenant_id)
    return result


@router.get("/billing/profile", response_model=BillingProfileOut | None)
async def get_billing_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant billing profile."""
    if not current_user.tenant_id:
        return None
    profile = await db.get(BillingProfile, current_user.tenant_id)
    if not profile:
        return None
    return profile


@router.put("/billing/profile", response_model=BillingProfileOut)
async def update_billing_profile(
    data: BillingProfileIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant billing profile (org_admin+)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant")

    profile = await db.get(BillingProfile, current_user.tenant_id)
    if not profile:
        profile = BillingProfile(tenant_id=current_user.tenant_id)
        db.add(profile)

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(profile, k, v)
    await db.commit()
    await db.refresh(profile)
    return profile
