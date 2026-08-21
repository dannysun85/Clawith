import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.api import saas as saas_api
from app.api import subscription as subscription_api
from app.models.audit import AuditLog
from app.services import credit_service
from app.services import quota_guard
from app.services.llm import caller as llm_caller
from app.models.agent import Agent
from app.models.subscription import (
    CreditBalance,
    CreditPack,
    CreditReservation,
    CreditTransaction,
    PaymentOrder,
    Plan,
    Subscription,
)
from app.models.user import User
from app.schemas.saas import (
    AssignSubscriptionIn,
    BillingRuleCreateIn,
    GrantCreditsIn,
    ManualOrderDecisionIn,
)
from app.schemas.schemas import AgentCreate
from app.schemas.subscription import AssignPlanIn, CheckoutSubscribeIn, CheckoutTopupIn
from app.services.entitlements import Entitlements
from app.services.billing_provider import BillingProviderReadiness
from app.services.token_tracker import TokenUsage


class DummyResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return [] if self._scalar is None else [self._scalar]


class DummyManyResult:
    def __init__(self, values):
        self._values = values

    def scalar_one_or_none(self):
        if not self._values:
            return None
        return self._values[0]

    def scalars(self):
        return self

    def all(self):
        return self._values


class MockDB:
    def __init__(self, *, get_map=None, execute_results=None):
        self.get_map = get_map or {}
        self.execute_results = list(execute_results or [])
        self.added = []
        self.committed = False
        self.refreshed = []
        self.statements = []

    async def get(self, model, key, *args, **kwargs):
        value = self.get_map.get((model, key))
        if value is not None:
            return value
        return self.get_map.get(model)

    async def execute(self, _stmt=None, _params=None):
        self.statements.append(_stmt)
        if self.execute_results:
            return self.execute_results.pop(0)
        return DummyResult()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


async def _streaming_text(response) -> str:
    body = ""
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            body += chunk.decode("utf-8")
        else:
            body += chunk
    return body


def _admin_user():
    return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None)


def _manual_order_decision(
    order: PaymentOrder,
    *,
    disposition: str = "mark_paid",
    expected_status: str = "pending",
    rollback_of_decision_id: uuid.UUID | None = None,
) -> ManualOrderDecisionIn:
    return ManualOrderDecisionIn(
        expected_tenant_id=order.tenant_id,
        expected_status=expected_status,
        disposition=disposition,
        evidence_ref="finance-ticket-20260821",
        reason="Finance confirmed the exact manual order disposition.",
        rollback_of_decision_id=rollback_of_decision_id,
    )


def _hydrate_payment_order_defaults(order: PaymentOrder) -> PaymentOrder:
    order.created_at = order.created_at or datetime.now(timezone.utc)
    order.refunded_amount_cents = int(order.refunded_amount_cents or 0)
    order.refunded_credits = int(order.refunded_credits or 0)
    order.paid_effects_status = order.paid_effects_status or "not_applicable"
    order.paid_effects_attempts = int(order.paid_effects_attempts or 0)
    order.provider_close_status = order.provider_close_status or "not_requested"
    order.provider_close_attempts = int(order.provider_close_attempts or 0)
    return order


def _ent(modalities=None, tiers=None):
    return Entitlements(
        plan_id=uuid.uuid4(),
        plan_code="free",
        max_agents=2,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=1000,
        allowed_modalities=modalities if modalities is not None else ["text"],
        allowed_tiers=tiers if tiers is not None else ["lite"],
    )


@pytest.mark.asyncio
async def test_assign_subscription_plan_change_grants_period_credits():
    tenant_id = uuid.uuid4()
    old_plan_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, credits_per_period=50_000)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=old_plan_id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    db = MockDB(get_map={Plan: plan}, execute_results=[DummyResult(sub)])

    with (
        patch.object(subscription_api, "grant_credits_in_session", AsyncMock()) as grant,
        patch.object(subscription_api, "reconcile_tenant_agent_plan_selections", AsyncMock()),
        patch("app.services.subscription_lifecycle.restore_stopped_agents", AsyncMock()),
        patch("app.services.subscription_lifecycle.enforce_agent_limit", AsyncMock()),
    ):
        out = await subscription_api.assign_subscription(
            AssignPlanIn(tenant_id=tenant_id, plan_id=plan.id),
            current_user=_admin_user(),
            db=db,
        )

    assert out.plan_code == "pro"
    assert sub.plan_id == plan.id
    grant.assert_awaited_once()
    assert grant.await_args.kwargs["tenant_id"] == tenant_id
    assert grant.await_args.kwargs["amount"] == 50_000
    assert grant.await_args.kwargs["reason"] == "subscribe"
    assert grant.await_args.kwargs["ref_type"] == "subscription_plan_change"
    assert grant.await_args.kwargs["ref_id"] != sub.id


@pytest.mark.asyncio
async def test_assign_subscription_same_plan_does_not_duplicate_grant():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, credits_per_period=50_000)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    db = MockDB(get_map={Plan: plan}, execute_results=[DummyResult(sub)])

    with (
        patch.object(subscription_api, "grant_credits_in_session", AsyncMock()) as grant,
        patch.object(subscription_api, "reconcile_tenant_agent_plan_selections", AsyncMock()),
        patch("app.services.subscription_lifecycle.restore_stopped_agents", AsyncMock()),
        patch("app.services.subscription_lifecycle.enforce_agent_limit", AsyncMock()),
    ):
        await subscription_api.assign_subscription(
            AssignPlanIn(tenant_id=tenant_id, plan_id=plan.id),
            current_user=_admin_user(),
            db=db,
        )

    grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_saas_bulk_assign_existing_subscription_grants_plan_credits():
    tenant_id = uuid.uuid4()
    old_plan_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, credits_per_period=50_000)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=old_plan_id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    db = MockDB(get_map={Plan: plan}, execute_results=[DummyResult(sub)])

    with (
        patch.object(saas_api, "grant_credits_in_session", AsyncMock()) as grant,
        patch.object(saas_api, "reconcile_tenant_agent_plan_selections", AsyncMock()),
        patch.object(saas_api, "restore_stopped_agents", AsyncMock()),
        patch.object(saas_api, "enforce_agent_limit", AsyncMock()),
    ):
        result = await saas_api.assign_subscriptions(
            AssignSubscriptionIn(tenant_ids=[tenant_id], plan_id=plan.id),
            current_user=_admin_user(),
            db=db,
        )

    assert result == {"updated": 1}
    assert sub.plan_id == plan.id
    grant.assert_awaited_once()
    assert grant.await_args.kwargs["tenant_id"] == tenant_id
    assert grant.await_args.kwargs["amount"] == 50_000
    assert grant.await_args.kwargs["reason"] == "subscribe"
    assert grant.await_args.kwargs["ref_type"] == "subscription_plan_change"
    assert grant.await_args.kwargs["ref_id"] != sub.id


@pytest.mark.asyncio
async def test_saas_bulk_credit_grant_requires_confirmation_for_multiple_tenants():
    tenant_ids = [uuid.uuid4(), uuid.uuid4()]
    db = MockDB()

    with pytest.raises(HTTPException) as exc:
        await saas_api.grant_credits_bulk(
            GrantCreditsIn(tenant_ids=tenant_ids, amount=1000, reason="manual_adjustment"),
            current_user=_admin_user(),
            db=db,
        )

    assert exc.value.status_code == 400
    assert "confirmation" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_saas_credit_grant_writes_admin_audit_log_in_same_session():
    tenant_id = uuid.uuid4()
    admin = _admin_user()
    db = MockDB()

    with patch.object(saas_api, "grant_credits_in_session", AsyncMock()) as grant:
        result = await saas_api.grant_credits_bulk(
            GrantCreditsIn(
                tenant_ids=[tenant_id],
                amount=1000,
                reason="manual_adjustment",
                confirm=True,
                audit_reason="QA topup",
            ),
            current_user=admin,
            db=db,
        )

    assert result == {"granted_to": 1, "amount": 1000}
    grant.assert_awaited_once()
    assert grant.await_args.args[0] is db
    audit = next(item for item in db.added if isinstance(item, AuditLog))
    assert audit.user_id == admin.id
    assert audit.action == "saas_credits_grant"
    assert audit.details["tenant_ids"] == [str(tenant_id)]
    assert audit.details["amount"] == 1000
    assert audit.details["reason"] == "manual_adjustment"
    assert audit.details["audit_reason"] == "QA topup"


@pytest.mark.asyncio
async def test_saas_create_billing_rule_writes_admin_audit_log():
    admin = _admin_user()
    db = MockDB()

    rule = await saas_api.create_billing_rule(
        BillingRuleCreateIn(action="chat", modality="text", tier="lite", credit_cost=1),
        current_user=admin,
        db=db,
    )

    assert rule.action == "chat"
    audit = next(item for item in db.added if isinstance(item, AuditLog))
    assert audit.user_id == admin.id
    assert audit.action == "saas_billing_rule_create"
    assert audit.details["after"]["action"] == "chat"
    assert audit.details["after"]["tier"] == "lite"


@pytest.mark.asyncio
async def test_saas_mark_order_paid_grants_subscription_credits_in_same_session():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, credits_per_period=50_000)
    order = _hydrate_payment_order_defaults(PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=plan.id,
        amount_cents=16000,
        currency="USD",
        provider="manual",
        status="pending",
    ))
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=uuid.uuid4(),
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyResult(order), DummyResult(None), DummyResult(sub)],
    )

    with (
        patch("app.services.billing_events.grant_credits_in_session", AsyncMock()) as grant_in_session,
        patch.object(saas_api, "apply_paid_subscribe_effects", AsyncMock()) as effects,
    ):
        result = await saas_api.mark_order_paid(
            order.id,
            _manual_order_decision(order),
            idempotency_key="manual-order-test-0001",
            current_user=_admin_user(),
            db=db,
        )

    assert result.order.status == "paid"
    assert result.decision.disposition == "mark_paid"
    assert result.replayed is False
    grant_in_session.assert_awaited_once()
    assert grant_in_session.await_args.args[0] is db
    assert grant_in_session.await_args.kwargs["tenant_id"] == tenant_id
    assert grant_in_session.await_args.kwargs["amount"] == 50_000
    assert grant_in_session.await_args.kwargs["reason"] == "subscribe"
    assert grant_in_session.await_args.kwargs["ref_type"] == "order"
    assert grant_in_session.await_args.kwargs["ref_id"] == order.id
    effects.assert_awaited_once_with(order)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["wechat", "stripe"])
async def test_saas_cannot_manually_finalize_provider_backed_order(provider):
    order = _hydrate_payment_order_defaults(PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider=provider,
        status="pending",
    ))
    db = MockDB(execute_results=[DummyResult(order), DummyResult(None)])

    with pytest.raises(HTTPException) as exc_info:
        await saas_api.mark_order_paid(
            order.id,
            _manual_order_decision(order),
            idempotency_key="manual-order-test-provider",
            current_user=_admin_user(),
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert "verified provider event" in str(exc_info.value.detail)
    assert order.status == "pending"


@pytest.mark.asyncio
async def test_saas_cannot_reopen_canceled_manual_order_as_paid():
    order = _hydrate_payment_order_defaults(PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="manual",
        status="canceled",
    ))
    db = MockDB(execute_results=[DummyResult(order), DummyResult(None)])

    with pytest.raises(HTTPException) as exc_info:
        await saas_api.mark_order_paid(
            order.id,
            _manual_order_decision(order),
            idempotency_key="manual-order-test-canceled",
            current_user=_admin_user(),
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert "found canceled" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_credit_ledger_integrity_detects_balance_and_reserved_drift():
    from app.services.billing_reconciliation import check_credit_ledger_integrity

    tenant_id = uuid.uuid4()
    balance = CreditBalance(tenant_id=tenant_id, balance=900, reserved=200)
    txs = [
        CreditTransaction(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delta=1000,
            balance_after=1000,
            reason="subscribe",
            created_at=datetime.now(timezone.utc),
        ),
        CreditTransaction(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delta=-50,
            balance_after=950,
            reason="consume",
            created_at=datetime.now(timezone.utc),
        ),
    ]
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=150,
        status="reserved",
        action="video",
    )
    db = MockDB(
        execute_results=[
            DummyManyResult(
                [
                    SimpleNamespace(
                        tenant_id=tenant_id,
                        actual_balance=balance.balance,
                        actual_reserved=balance.reserved,
                        expected_balance=sum(tx.delta for tx in txs),
                        expected_reserved=reservation.amount,
                    )
                ]
            )
        ]
    )

    report = await check_credit_ledger_integrity(db, tenant_id=tenant_id)

    assert report.checked_tenants == 1
    assert {issue.code for issue in report.issues} == {"balance_drift", "reserved_drift"}
    assert report.issues[0].tenant_id == tenant_id


@pytest.mark.asyncio
async def test_credit_ledger_integrity_detects_transaction_tenant_without_balance():
    from app.services.billing_reconciliation import check_credit_ledger_integrity

    tenant_id = uuid.uuid4()
    transaction = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=1000,
        balance_after=1000,
        reason="subscribe",
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB(
        execute_results=[
            DummyManyResult(
                [
                    SimpleNamespace(
                        tenant_id=tenant_id,
                        actual_balance=None,
                        actual_reserved=None,
                        expected_balance=transaction.delta,
                        expected_reserved=0,
                    )
                ]
            )
        ]
    )

    report = await check_credit_ledger_integrity(db, tenant_id=tenant_id)

    assert report.checked_tenants == 1
    assert len(report.issues) == 1
    assert report.issues[0].code == "missing_credit_balance"
    assert report.issues[0].tenant_id == tenant_id
    assert report.issues[0].expected == 1
    assert report.issues[0].actual == 0


@pytest.mark.asyncio
async def test_credit_ledger_integrity_detects_reservation_tenant_without_balance():
    from app.services.billing_reconciliation import check_credit_ledger_integrity

    tenant_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=25,
        status="provider_inflight",
        action="video",
    )
    db = MockDB(
        execute_results=[
            DummyManyResult(
                [
                    SimpleNamespace(
                        tenant_id=tenant_id,
                        actual_balance=None,
                        actual_reserved=None,
                        expected_balance=0,
                        expected_reserved=reservation.amount,
                    )
                ]
            )
        ]
    )

    report = await check_credit_ledger_integrity(db, tenant_id=tenant_id)

    assert report.checked_tenants == 1
    assert len(report.issues) == 1
    assert report.issues[0].code == "missing_credit_balance"
    assert report.issues[0].tenant_id == tenant_id


@pytest.mark.asyncio
async def test_payment_reconciliation_log_excludes_order_and_tenant_details():
    from app.services.billing_reconciliation import reconcile_pending_payment_orders

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        amount_cents=16000,
        currency="USD",
        provider="stripe",
        provider_session_id=None,
        status="pending",
    )
    db = MockDB(execute_results=[DummyManyResult([order])])

    with patch("app.services.billing_reconciliation.logger.warning") as warning:
        report = await reconcile_pending_payment_orders(db)

    assert report.checked_orders == 1
    assert [issue.code for issue in report.issues] == ["missing_provider_session"]
    warning.assert_called_once_with(
        "[billing] payment reconciliation issues detected issue_count={} code_counts={}",
        1,
        {"missing_provider_session": 1},
    )
    serialized_log_args = repr(warning.call_args)
    assert str(order.id) not in serialized_log_args
    assert str(order.tenant_id) not in serialized_log_args
    assert order.provider not in serialized_log_args


@pytest.mark.asyncio
async def test_payment_reconciliation_applies_subscribe_effects_after_recovery():
    from app.services.billing_reconciliation import reconcile_pending_payment_orders

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        amount_cents=16000,
        currency="CNY",
        provider="wechat",
        provider_session_id="session",
        status="paid",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    db = MockDB(
        execute_results=[DummyManyResult([order])],
        get_map={(PaymentOrder, order.id): order},
    )

    async def _mark_paid(_db, synced):
        synced.status = "paid"
        return True

    effect_saw_commit = {"value": False}

    async def _effects(_order):
        effect_saw_commit["value"] = db.committed

    with (
        patch(
            "app.services.billing_events.sync_pending_order_from_provider",
            new=AsyncMock(side_effect=_mark_paid),
        ),
        patch(
            "app.services.subscription_lifecycle.apply_paid_subscribe_effects",
            new=AsyncMock(side_effect=_effects),
        ) as effects,
    ):
        report = await reconcile_pending_payment_orders(db)

    assert [issue.code for issue in report.issues] == ["recovered_via_provider_query"]
    effects.assert_awaited_once_with(order)
    assert effect_saw_commit["value"] is True


@pytest.mark.asyncio
async def test_payment_reconciliation_closes_expired_wechat_order_without_paid_effects():
    from app.services.billing_reconciliation import reconcile_pending_payment_orders

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        provider_session_id="session",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    db = MockDB(
        execute_results=[DummyManyResult([order])],
        get_map={(PaymentOrder, order.id): order},
    )

    async def _close(_db, expired_order):
        expired_order.status = "canceled"
        return True

    with (
        patch(
            "app.services.billing_events.sync_pending_order_from_provider",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.services.billing_events.close_expired_pending_order",
            new=AsyncMock(side_effect=_close),
        ) as close_order,
        patch(
            "app.services.billing_reconciliation.get_settings",
            return_value=SimpleNamespace(WECHAT_PAY_ORDER_EXPIRE_MINUTES=120),
        ),
        patch(
            "app.services.subscription_lifecycle.apply_paid_subscribe_effects",
            new=AsyncMock(),
        ) as effects,
    ):
        report = await reconcile_pending_payment_orders(db)

    assert order.status == "canceled"
    assert [issue.code for issue in report.issues] == ["expired_order_closed"]
    close_order.assert_awaited_once_with(db, order)
    effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_effects_reconciliation_retries_incomplete_receipts():
    from app.services.billing_reconciliation import reconcile_paid_subscribe_effects

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_effects_status="failed",
        paid_at=datetime.now(timezone.utc),
    )
    db = MockDB(
        execute_results=[DummyManyResult([order.id])],
        get_map={(PaymentOrder, order.id): order},
    )
    with patch(
        "app.services.subscription_lifecycle.apply_paid_subscribe_effects",
        new=AsyncMock(return_value=True),
    ) as effects:
        report = await reconcile_paid_subscribe_effects(db)

    assert report.checked_orders == 1
    assert report.applied_orders == 1
    assert report.failed_orders == 0
    effects.assert_awaited_once_with(order)


@pytest.mark.asyncio
async def test_paid_subscribe_effect_failure_leaves_retryable_receipt():
    from app.services import subscription_lifecycle

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
    )
    failure = RuntimeError("restore failed")
    with (
        patch.object(
            subscription_lifecycle,
            "_claim_paid_subscribe_effects",
            new=AsyncMock(return_value=(order.tenant_id, 2)),
        ),
        patch.object(
            subscription_lifecycle,
            "_finish_paid_subscribe_effects",
            new=AsyncMock(),
        ) as finish,
        patch(
            "app.services.agent_plan_selection.reconcile_tenant_agent_plan_selections",
            new=AsyncMock(side_effect=failure),
        ),
        patch.object(subscription_lifecycle, "restore_stopped_agents", new=AsyncMock()),
        patch.object(subscription_lifecycle, "enforce_agent_limit", new=AsyncMock()),
        patch(
            "app.services.production_issue_monitor.record_production_issue",
            new=AsyncMock(),
        ) as issue,
    ):
        with pytest.raises(RuntimeError, match="restore failed"):
            await subscription_lifecycle.apply_paid_subscribe_effects(order)

    finish.assert_awaited_once_with(
        order.id,
        attempt=2,
        applied=False,
        error=failure,
    )
    issue.assert_awaited_once()
    assert issue.await_args.kwargs["error_code"] == "paid_effects_incomplete"


@pytest.mark.asyncio
async def test_paid_effects_finish_is_fenced_by_claim_attempt():
    from app.services import subscription_lifecycle

    class _SessionContext:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return None

    db = MockDB(execute_results=[DummyResult(None)])
    order_id = uuid.uuid4()
    with patch.object(
        subscription_lifecycle,
        "async_session",
        return_value=_SessionContext(db),
    ):
        updated = await subscription_lifecycle._finish_paid_subscribe_effects(
            order_id,
            attempt=1,
            applied=False,
            error=RuntimeError("stale worker"),
        )

    assert updated is False
    assert db.committed is False
    statement = str(db.statements[0])
    assert "payment_orders.paid_effects_status" in statement
    assert "payment_orders.paid_effects_attempts" in statement


@pytest.mark.asyncio
async def test_provider_close_failure_is_pending_retry_with_operator_issue():
    from app.services.billing_events import close_expired_pending_order

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    db = MockDB(execute_results=[DummyResult(order)])
    provider = SimpleNamespace(close_order=AsyncMock(return_value=False))
    with (
        patch("app.services.billing_provider.get_billing_provider", return_value=provider),
        patch(
            "app.services.production_issue_monitor.record_production_issue",
            new=AsyncMock(),
        ) as issue,
    ):
        changed = await close_expired_pending_order(db, order)

    assert changed is False
    assert order.status == "pending"
    assert order.provider_close_status == "retry_wait"
    assert order.provider_close_attempts == 1
    assert order.provider_close_next_retry_at is not None
    assert "ValueError" in (order.provider_close_error or "")
    issue.assert_awaited_once()
    assert issue.await_args.kwargs["error_code"] == "provider_close_retry_required"


@pytest.mark.asyncio
async def test_provider_close_success_records_terminal_receipt():
    from app.services.billing_events import close_expired_pending_order

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
        provider_close_status="retry_wait",
        provider_close_attempts=1,
    )
    db = MockDB(execute_results=[DummyResult(order)])
    provider = SimpleNamespace(close_order=AsyncMock(return_value=True))
    with patch(
        "app.services.billing_provider.get_billing_provider",
        return_value=provider,
    ):
        changed = await close_expired_pending_order(db, order)

    assert changed is True
    assert order.status == "canceled"
    assert order.provider_close_status == "closed"
    assert order.provider_close_attempts == 2
    assert order.provider_close_error is None
    assert order.provider_close_next_retry_at is None


@pytest.mark.asyncio
async def test_provider_close_fifth_failure_escalates_to_operator_review():
    from app.services.billing_events import close_expired_pending_order

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="pending",
        provider_close_attempts=4,
    )
    db = MockDB(execute_results=[DummyResult(order)])
    provider = SimpleNamespace(close_order=AsyncMock(side_effect=RuntimeError("unavailable")))
    with (
        patch("app.services.billing_provider.get_billing_provider", return_value=provider),
        patch(
            "app.services.production_issue_monitor.record_production_issue",
            new=AsyncMock(),
        ) as issue,
    ):
        changed = await close_expired_pending_order(db, order)

    assert changed is False
    assert order.status == "pending"
    assert order.provider_close_status == "operator_review"
    assert order.provider_close_attempts == 5
    assert issue.await_args.kwargs["severity"] == "critical"


@pytest.mark.asyncio
async def test_saas_reconciliation_endpoint_returns_jsonable_report():
    tenant_id = uuid.uuid4()
    balance = CreditBalance(tenant_id=tenant_id, balance=900, reserved=0)
    tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=1000,
        balance_after=1000,
        reason="subscribe",
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB(
        execute_results=[
            DummyManyResult(
                [
                    SimpleNamespace(
                        tenant_id=tenant_id,
                        actual_balance=balance.balance,
                        actual_reserved=balance.reserved,
                        expected_balance=tx.delta,
                        expected_reserved=0,
                    )
                ]
            )
        ]
    )

    report = await saas_api.get_ledger_reconciliation(
        tenant_id=tenant_id,
        current_user=_admin_user(),
        db=db,
    )

    assert report["checked_tenants"] == 1
    assert report["issues"][0]["code"] == "balance_drift"
    assert report["issues"][0]["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_saas_expire_stale_reservations_writes_admin_audit_log():
    admin = _admin_user()
    db = MockDB()

    with patch.object(saas_api, "expire_stale_credit_reservations", AsyncMock(return_value=2)):
        result = await saas_api.expire_stale_reservations(current_user=admin, db=db)

    assert result == {"expired": 2}
    audit = next(item for item in db.added if isinstance(item, AuditLog))
    assert audit.user_id == admin.id
    assert audit.action == "saas_reservations_expire_stale"
    assert audit.details["expired"] == 2
    assert db.committed is True


@pytest.mark.asyncio
async def test_saas_export_orders_csv_contains_payment_rows():
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=1500,
        currency="USD",
        provider="manual",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    db = MockDB(execute_results=[DummyManyResult([order])])

    response = await saas_api.export_orders_csv(current_user=_admin_user(), db=db)
    text = await _streaming_text(response)

    assert "id,tenant_id,type,plan_id,credits" in text
    assert str(order.id) in text
    assert "topup" in text
    assert "paid" in text


@pytest.mark.asyncio
async def test_saas_export_credit_transactions_csv_contains_ledger_rows():
    tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        delta=-5,
        balance_after=995,
        reason="consume",
        action="chat",
        modality="text",
        tier="lite",
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB(execute_results=[DummyManyResult([tx])])

    response = await saas_api.export_credit_transactions_csv(current_user=_admin_user(), db=db)
    text = await _streaming_text(response)

    assert "id,tenant_id,delta,balance_after,reason" in text
    assert str(tx.id) in text
    assert "consume" in text
    assert "chat" in text


@pytest.mark.asyncio
async def test_get_usage_falls_back_to_current_entitlement_limits():
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    balance = CreditBalance(tenant_id=tenant_id, balance=998, reserved=0)
    db = MockDB(get_map={CreditBalance: balance}, execute_results=[DummyResult(None)])

    with (
        patch("app.services.quota_guard._today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 8))),
        patch.object(subscription_api, "get_tenant_entitlements", AsyncMock(return_value=_ent())),
        patch.object(subscription_api, "get_credit_balance", AsyncMock(return_value=balance)),
    ):
        usage = await subscription_api.get_my_usage(current_user=user, db=db)

    assert usage.llm_calls_used == 0
    assert usage.llm_calls_limit == 1000
    assert usage.messages_used == 0
    assert usage.messages_limit == 50
    assert usage.credits_balance == 998


@pytest.mark.asyncio
async def test_get_usage_reconciles_current_subscription_credit_balance():
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    repaired_balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=0)
    db = MockDB(execute_results=[DummyResult(None)])

    with (
        patch("app.services.quota_guard._today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 8))),
        patch.object(subscription_api, "get_tenant_entitlements", AsyncMock(return_value=_ent())),
        patch.object(subscription_api, "get_credit_balance", AsyncMock(return_value=repaired_balance)) as get_balance,
    ):
        usage = await subscription_api.get_my_usage(current_user=user, db=db)

    get_balance.assert_awaited_once_with(tenant_id)
    assert usage.credits_balance == 1000


@pytest.mark.asyncio
async def test_reconcile_subscription_credit_grant_repairs_existing_subscription_without_subscribe_tx():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="free", name="Free", is_active=True, credits_per_period=1000)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyResult(sub), DummyResult(None)],
    )

    tx = await credit_service.ensure_current_subscription_credit_grant_in_session(
        db,
        tenant_id=tenant_id,
        balance_row=balance,
        granted_by=None,
    )

    assert balance.balance == 1000
    assert tx is not None
    assert tx.tenant_id == tenant_id
    assert tx.delta == 1000
    assert tx.balance_after == 1000
    assert tx.reason == "subscribe"
    assert tx.ref_type == "subscription"
    assert tx.ref_id == sub.id
    assert db.added == [tx]


@pytest.mark.asyncio
async def test_reconcile_subscription_credit_grant_does_not_duplicate_existing_subscribe_tx():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="free", name="Free", is_active=True, credits_per_period=1000)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=1,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=0, reserved=0)
    existing_tx_id = uuid.uuid4()
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyResult(sub), DummyResult(existing_tx_id)],
    )

    tx = await credit_service.ensure_current_subscription_credit_grant_in_session(
        db,
        tenant_id=tenant_id,
        balance_row=balance,
        granted_by=None,
    )

    assert tx is None
    assert balance.balance == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_charge_credits_in_session_deducts_balance_and_writes_ledger_without_commit():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    balance = CreditBalance(tenant_id=tenant_id, balance=10, reserved=0)
    db = MockDB(execute_results=[DummyResult(balance)])

    with patch.object(
        credit_service,
        "ensure_current_subscription_credit_grant_in_session",
        AsyncMock(return_value=None),
    ):
        tx = await credit_service.charge_credits_in_session(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="chat",
            modality="text",
            saas_tier="lite",
            provider="minimax",
            model="MiniMax-M2.7",
            delta=3,
        )

    assert balance.balance == 7
    assert tx.delta == -3
    assert tx.balance_after == 7
    assert tx.reason == "consume"
    assert tx.tenant_id == tenant_id
    assert tx.user_id == user_id
    assert tx.agent_id == agent_id
    assert tx.action == "chat"
    assert tx.modality == "text"
    assert tx.tier == "lite"
    assert tx.provider == "minimax"
    assert tx.model == "MiniMax-M2.7"
    assert db.added == [tx]
    assert db.committed is False


@pytest.mark.asyncio
async def test_grant_credits_in_session_is_idempotent_for_order_reference():
    tenant_id = uuid.uuid4()
    order_id = uuid.uuid4()
    existing_tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=10_000,
        balance_after=10_000,
        reason="topup",
        ref_type="order",
        ref_id=order_id,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=10_000, reserved=0)
    db = MockDB(execute_results=[DummyResult(existing_tx), DummyResult(balance)])

    tx = await credit_service.grant_credits_in_session(
        db,
        tenant_id=tenant_id,
        amount=10_000,
        reason="topup",
        granted_by=uuid.uuid4(),
        ref_type="order",
        ref_id=order_id,
    )

    assert tx is existing_tx
    assert balance.balance == 10_000
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_grant_credits_in_session_is_idempotent_for_incident_refund():
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    existing_tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=456,
        balance_after=1456,
        reason="refund",
        ref_type="product_incident",
        ref_id=incident_id,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1456, reserved=0)
    db = MockDB(execute_results=[DummyResult(existing_tx), DummyResult(balance)])

    tx = await credit_service.grant_credits_in_session(
        db,
        tenant_id=tenant_id,
        amount=456,
        reason="refund",
        ref_type="product_incident",
        ref_id=incident_id,
    )

    assert tx is existing_tx
    assert balance.balance == 1456
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_reserve_credits_in_session_holds_available_balance_without_commit():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=100)
    db = MockDB(execute_results=[DummyResult(balance)])

    with patch.object(
        credit_service,
        "ensure_current_subscription_credit_grant_in_session",
        AsyncMock(return_value=None),
    ):
        reservation = await credit_service.reserve_credits_in_session(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="video",
            modality="video",
            saas_tier="pro",
            provider="minimax",
            model="MiniMax-Hailuo-2.3",
            amount=490,
            ref_type="minimax_task",
            ref_id=uuid.uuid4(),
        )

    assert balance.balance == 1000
    assert balance.reserved == 590
    assert reservation.status == "reserved"
    assert reservation.amount == 490
    assert reservation.action == "video"
    assert reservation.modality == "video"
    assert reservation.tier == "pro"
    assert db.added == [reservation]
    assert db.committed is False


@pytest.mark.asyncio
async def test_finalize_reserved_credits_in_session_consumes_once_and_writes_ledger():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        action="video",
        modality="video",
        tier="pro",
        provider="minimax",
        model="MiniMax-Hailuo-2.3",
        amount=490,
        status="reserved",
        ref_type="minimax_task",
        ref_id=uuid.uuid4(),
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=490)
    db = MockDB(
        get_map={(CreditReservation, reservation.id): reservation},
        execute_results=[DummyResult(None), DummyResult(balance)],
    )

    tx = await credit_service.finalize_reserved_credits_in_session(db, reservation.id)

    assert reservation.status == "finalized"
    assert reservation.finalized_at is not None
    assert balance.balance == 510
    assert balance.reserved == 0
    assert tx.delta == -490
    assert tx.balance_after == 510
    assert tx.reason == "consume"
    assert tx.ref_type == "reservation"
    assert tx.ref_id == reservation.id
    assert tx.user_id == user_id
    assert tx.agent_id == agent_id
    assert tx.action == "video"
    assert db.added == [tx]
    assert db.committed is False

    db.execute_results = [DummyResult(tx)]
    second_tx = await credit_service.finalize_reserved_credits_in_session(db, reservation.id)

    assert second_tx is tx
    assert balance.balance == 510
    assert balance.reserved == 0
    assert db.added == [tx]


@pytest.mark.asyncio
async def test_release_reserved_credits_in_session_releases_without_ledger():
    tenant_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=490,
        status="reserved",
        action="video",
        modality="video",
        tier="pro",
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=490)
    db = MockDB(
        get_map={(CreditReservation, reservation.id): reservation},
        execute_results=[DummyResult(balance)],
    )

    out = await credit_service.release_reserved_credits_in_session(db, reservation.id)

    assert out is reservation
    assert reservation.status == "released"
    assert balance.balance == 1000
    assert balance.reserved == 0
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_mark_credit_reservation_settlement_ready_resizes_durable_hold():
    tenant_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=3,
        status="provider_inflight",
        action="chat",
        modality="text",
        tier="lite",
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=100, reserved=8)
    db = MockDB(
        get_map={(CreditReservation, reservation.id): reservation},
        execute_results=[DummyResult(balance)],
    )

    out = await credit_service.mark_credit_reservation_settlement_ready_in_session(
        db,
        reservation.id,
        amount=5,
    )

    assert out is reservation
    assert reservation.status == "settlement_ready"
    assert reservation.amount == 5
    assert balance.reserved == 10


@pytest.mark.asyncio
async def test_provider_inflight_hold_requires_explicit_provider_failure_to_release():
    tenant_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=5,
        status="provider_inflight",
        action="chat",
        modality="text",
        tier="lite",
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=100, reserved=5)
    db = MockDB(
        get_map={(CreditReservation, reservation.id): reservation},
        execute_results=[DummyResult(balance)],
    )

    retained = await credit_service.release_reserved_credits_in_session(db, reservation.id)
    assert retained.status == "provider_inflight"
    assert balance.reserved == 5

    released = await credit_service.release_reserved_credits_in_session(
        db,
        reservation.id,
        release_provider_inflight=True,
    )
    assert released.status == "released"
    assert balance.reserved == 0


@pytest.mark.asyncio
async def test_settlement_ready_reservation_cannot_be_released_and_finalizes_once():
    tenant_id = uuid.uuid4()
    reservation = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=5,
        status="settlement_ready",
        action="chat",
        modality="text",
        tier="lite",
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=100, reserved=5)
    db = MockDB(
        get_map={(CreditReservation, reservation.id): reservation},
        execute_results=[DummyResult(None), DummyResult(balance)],
    )

    released = await credit_service.release_reserved_credits_in_session(db, reservation.id)
    assert released.status == "settlement_ready"
    assert balance.reserved == 5

    tx = await credit_service.finalize_reserved_credits_in_session(db, reservation.id)
    assert reservation.status == "finalized"
    assert balance.balance == 95
    assert balance.reserved == 0
    assert tx.delta == -5

    db.execute_results = [DummyResult(tx)]
    duplicate = await credit_service.finalize_reserved_credits_in_session(db, reservation.id)
    assert duplicate is tx
    assert balance.balance == 95


@pytest.mark.asyncio
async def test_stale_sweep_finalizes_provider_debt_and_releases_plain_hold():
    from app.services import billing_reconciliation

    now = datetime.now(timezone.utc)
    settlement = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        amount=5,
        status="settlement_ready",
        action="chat",
        expires_at=now,
    )
    plain = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        amount=5,
        status="reserved",
        action="chat",
        expires_at=now,
    )
    provider_inflight = CreditReservation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        amount=8,
        status="provider_inflight",
        action="chat",
        expires_at=now,
    )
    db = MockDB(
        execute_results=[
            DummyManyResult([settlement.id, plain.id, provider_inflight.id]),
            DummyResult(settlement),
            DummyResult(plain),
            DummyResult(provider_inflight),
        ]
    )

    with (
        patch.object(
            billing_reconciliation,
            "finalize_reserved_credits_in_session",
            AsyncMock(),
        ) as finalize,
        patch.object(
            billing_reconciliation,
            "release_reserved_credits_in_session",
            AsyncMock(),
        ) as release,
        patch(
            "app.services.production_issue_monitor.record_production_issue",
            AsyncMock(),
        ) as monitor,
        patch(
            "app.services.media_generation.backfill_legacy_minimax_video_tasks",
            AsyncMock(return_value=0),
        ) as backfill,
    ):
        recovered = await billing_reconciliation.expire_stale_credit_reservations(db, now=now)

    assert recovered == 2
    backfill.assert_awaited_once()
    finalize.assert_awaited_once_with(db, settlement.id)
    release.assert_awaited_once_with(db, plain.id, status="expired")
    monitor.assert_awaited_once()
    assert monitor.await_args.kwargs["metadata"] == {"reservation_id": str(provider_inflight.id)}
    assert provider_inflight.status == "provider_inflight"
    assert provider_inflight.expires_at > now


@pytest.mark.asyncio
async def test_stale_sweep_protects_every_unresolved_media_status():
    from app.services import billing_reconciliation, media_generation

    class RecordingSweepDB(MockDB):
        def __init__(self):
            super().__init__(execute_results=[DummyManyResult([])])
            self.statements = []

        async def execute(self, statement=None, _params=None):
            self.statements.append(statement)
            return await super().execute(statement, _params)

    db = RecordingSweepDB()
    with patch(
        "app.services.media_generation.backfill_legacy_minimax_video_tasks",
        AsyncMock(return_value=0),
    ):
        assert await billing_reconciliation.expire_stale_credit_reservations(db) == 0

    assert db.statements
    compiled = db.statements[0].compile()
    sequence_params = [tuple(value) for value in compiled.params.values() if isinstance(value, (list, tuple))]
    assert media_generation.UNRESOLVED_MEDIA_STATUSES in sequence_params


@pytest.mark.asyncio
async def test_get_my_orders_returns_current_tenant_orders_for_client_history():
    tenant_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    orders = [
        PaymentOrder(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            type="subscribe",
            plan_id=plan_id,
            amount_cents=19200,
            currency="USD",
            provider="manual",
            status="pending",
        ),
        PaymentOrder(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            type="topup",
            credits=10_000,
            amount_cents=1500,
            currency="USD",
            provider="manual",
            status="paid",
        ),
    ]
    db = MockDB(execute_results=[DummyManyResult(orders)])

    result = await subscription_api.get_my_orders(page=1, limit=10, current_user=user, db=db)

    assert [order.id for order in result] == [order.id for order in orders]
    assert result[0].type == "subscribe"
    assert result[1].credits == 10_000


@pytest.mark.asyncio
async def test_checkout_subscribe_returns_provider_session_url():
    tenant_id = uuid.uuid4()
    plan = Plan(
        id=uuid.uuid4(),
        code="pro",
        name="Pro",
        is_active=True,
        price_cents=20_00,
        currency="USD",
        stripe_price_id="price_pro_monthly",
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    checkout = SimpleNamespace(
        provider="stripe",
        session_id="cs_test_123",
        session_url="https://checkout.stripe.test/session/cs_test_123",
        payment_id=None,
    )
    provider = SimpleNamespace(create_subscription_checkout=AsyncMock(return_value=checkout))
    db = MockDB(get_map={(Plan, plan.id): plan})

    with patch.object(subscription_api, "get_billing_provider", return_value=provider, create=True):
        order = await subscription_api.checkout_subscribe(
            CheckoutSubscribeIn(plan_id=plan.id, period="monthly", seats=1),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )

    assert order.provider == "stripe"
    assert order.provider_session_id == "cs_test_123"
    assert getattr(order, "session_url") == "https://checkout.stripe.test/session/cs_test_123"
    provider.create_subscription_checkout.assert_awaited_once()
    assert provider.create_subscription_checkout.await_args.kwargs["order"] is order
    assert provider.create_subscription_checkout.await_args.kwargs["plan"] is plan


@pytest.mark.asyncio
async def test_checkout_fails_before_order_creation_when_provider_is_not_ready():
    readiness = BillingProviderReadiness(
        provider="wechat",
        status="misconfigured",
        checkout_enabled=False,
        native_payment_enabled=False,
        webhook_ready=False,
        missing_config=("WECHAT_PAY_PLATFORM_SERIAL_NO",),
        issues=("wechat_notify_url_must_be_public_https",),
        next_action="configure provider",
    )
    db = MockDB()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    with patch.object(subscription_api, "billing_provider_readiness", return_value=readiness):
        with pytest.raises(HTTPException) as exc_info:
            await subscription_api.checkout_subscribe(
                CheckoutSubscribeIn(plan_id=uuid.uuid4()),
                SimpleNamespace(headers={"host": "localhost"}),
                current_user=user,
                db=db,
            )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "BILLING_PROVIDER_NOT_READY"
    assert exc_info.value.detail["provider"] == "wechat"
    assert db.added == []


@pytest.mark.asyncio
async def test_billing_config_exposes_manual_mode_without_claiming_native_payment():
    settings = SimpleNamespace(
        BILLING_PROVIDER="manual",
        BILLING_USD_CNY_RATE=7.0,
        PAYMENT_BASE_URL="",
        PUBLIC_BASE_URL="",
    )
    with patch.object(subscription_api, "get_settings", return_value=settings):
        result = await subscription_api.get_billing_config(current_user=SimpleNamespace())

    assert result.provider == "manual"
    assert result.status == "manual"
    assert result.checkout_enabled is True
    assert result.native_payment_enabled is False
    assert result.webhook_ready is False
    assert "不会生成微信支付二维码" in result.next_action


@pytest.mark.asyncio
async def test_checkout_topup_returns_provider_session_url():
    tenant_id = uuid.uuid4()
    pack = SimpleNamespace(
        id=uuid.uuid4(),
        code="boost_10k",
        name="Boost 10k",
        credits=10_000,
        price_cents=1500,
        currency="USD",
        is_active=True,
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    checkout = SimpleNamespace(
        provider="stripe",
        session_id="cs_test_topup",
        session_url="https://checkout.stripe.test/session/cs_test_topup",
        payment_id=None,
    )
    provider = SimpleNamespace(create_topup_checkout=AsyncMock(return_value=checkout))
    db = MockDB(get_map={(CreditPack, pack.id): pack})

    with patch.object(subscription_api, "get_billing_provider", return_value=provider, create=True):
        order = await subscription_api.checkout_topup(
            CheckoutTopupIn(credit_pack_id=pack.id),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )

    assert order.provider == "stripe"
    assert order.provider_session_id == "cs_test_topup"
    assert getattr(order, "session_url") == "https://checkout.stripe.test/session/cs_test_topup"
    provider.create_topup_checkout.assert_awaited_once()
    assert provider.create_topup_checkout.await_args.kwargs["order"] is order
    assert provider.create_topup_checkout.await_args.kwargs["pack"] is pack


@pytest.mark.asyncio
async def test_billing_webhook_duplicate_event_does_not_finalize_twice():
    from app.services.billing_events import PaymentProviderEventState, process_billing_webhook_event

    tenant_id = uuid.uuid4()
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="topup",
        credits=10_000,
        amount_cents=1500,
        currency="USD",
        provider="stripe",
        provider_session_id="cs_test_topup",
        status="pending",
    )
    state = PaymentProviderEventState(
        provider="stripe",
        event_id="evt_1",
        event_type="checkout.session.completed",
        order_id=order.id,
        status="paid",
        provider_session_id="cs_test_topup",
        provider_payment_id="pi_1",
    )
    provider = SimpleNamespace(
        verify_webhook=AsyncMock(return_value={"id": "evt_1", "type": "checkout.session.completed"}),
        load_remote_event_state=AsyncMock(return_value=state),
    )
    db = MockDB(get_map={(PaymentOrder, order.id): order})

    with patch("app.services.billing_events.finalize_order_in_session", AsyncMock(return_value=order)) as finalize:
        first = await process_billing_webhook_event(
            db,
            provider_name="stripe",
            payload=b"{}",
            signature="sig",
            provider=provider,
        )
        second = await process_billing_webhook_event(
            db,
            provider_name="stripe",
            payload=b"{}",
            signature="sig",
            provider=provider,
        )

    assert first["status"] == "processed"
    assert second["status"] == "duplicate"
    assert second["order_id"] == str(order.id)
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_processed_webhook_replay_reenters_paid_subscribe_effects():
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
    )

    class RequestStub:
        headers = {}

        async def body(self):
            return b"{}"

    db = MockDB(get_map={(PaymentOrder, order.id): order})
    with (
        patch.object(subscription_api, "get_billing_provider", return_value=SimpleNamespace()),
        patch.object(
            subscription_api,
            "process_billing_webhook_event",
            new=AsyncMock(
                return_value={
                    "status": "duplicate",
                    "event_id": "evt_paid_retry",
                    "order_id": str(order.id),
                }
            ),
        ),
        patch.object(
            subscription_api,
            "_apply_paid_subscribe_effects",
            new=AsyncMock(),
        ) as effects,
    ):
        result = await subscription_api.billing_webhook(
            "wechat",
            RequestStub(),
            stripe_signature=None,
            db=db,
        )

    assert result == {"code": "SUCCESS", "message": "成功"}
    effects.assert_awaited_once_with(order)


@pytest.mark.asyncio
async def test_billing_webhook_preserves_remote_state_error_before_event_exists():
    from app.services.billing_events import process_billing_webhook_event

    provider = SimpleNamespace(
        verify_webhook=AsyncMock(return_value={"id": "evt_remote_failure", "type": "TRANSACTION.SUCCESS"}),
        load_remote_event_state=AsyncMock(side_effect=ValueError("remote state unavailable")),
    )
    db = MockDB()

    with pytest.raises(ValueError, match="remote state unavailable"):
        await process_billing_webhook_event(
            db,
            provider_name="wechat",
            payload=b"{}",
            signature=None,
            provider=provider,
        )

    assert db.added == []


@pytest.mark.asyncio
async def test_wechat_webhook_api_forwards_all_signature_headers():
    from app.services.billing_events import PaymentProviderEventState

    provider = SimpleNamespace(
        verify_webhook=AsyncMock(return_value={"id": "evt_headers", "type": "TRANSACTION.SUCCESS"}),
        load_remote_event_state=AsyncMock(
            return_value=PaymentProviderEventState(
                provider="wechat",
                event_id="evt_headers",
                event_type="TRANSACTION.SUCCESS",
                order_id=None,
                status="ignored",
            )
        ),
        validate_event_state=lambda _order, _state: None,
    )

    class RequestStub:
        headers = {
            "Wechatpay-Signature": "signature",
            "Wechatpay-Timestamp": "1700000000",
            "Wechatpay-Nonce": "nonce",
            "Wechatpay-Serial": "serial",
        }

        async def body(self):
            return b"{}"

    db = MockDB()
    with patch.object(subscription_api, "get_billing_provider", return_value=provider):
        result = await subscription_api.billing_webhook(
            "wechat",
            RequestStub(),
            stripe_signature=None,
            db=db,
        )

    assert result == {"code": "SUCCESS", "message": "成功"}
    assert provider.verify_webhook.await_args.kwargs["headers"] == RequestStub.headers


@pytest.mark.asyncio
async def test_refund_event_marks_paid_order_without_regranting_credits():
    from app.services.billing_events import PaymentProviderEventState, process_billing_webhook_event

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=10_000,
        amount_cents=1500,
        currency="CNY",
        provider="wechat",
        provider_session_id="session",
        status="paid",
    )
    state = PaymentProviderEventState(
        provider="wechat",
        event_id="evt_refund",
        event_type="REFUND.SUCCESS",
        order_id=order.id,
        status="refunded",
        provider_session_id="session",
    )
    provider = SimpleNamespace(
        verify_webhook=AsyncMock(return_value={"id": "evt_refund", "type": "REFUND.SUCCESS"}),
        load_remote_event_state=AsyncMock(return_value=state),
        validate_event_state=lambda _order, _state: None,
    )
    db = MockDB(get_map={(PaymentOrder, order.id): order})

    with patch("app.services.billing_events.finalize_order_in_session", AsyncMock()) as finalize:
        result = await process_billing_webhook_event(
            db,
            provider_name="wechat",
            payload=b"{}",
            signature=None,
            provider=provider,
        )

    assert result["status"] == "processed"
    assert order.status == "refunded"
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_claws_back_only_unreserved_available_credits_exactly_once():
    from app.services.billing_events import refund_order_in_session

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    original = CreditTransaction(
        tenant_id=order.tenant_id,
        delta=1000,
        balance_after=1000,
        reason="topup",
        ref_type="order",
        ref_id=order.id,
    )
    balance = CreditBalance(tenant_id=order.tenant_id, balance=700, reserved=200)
    db = MockDB(
        execute_results=[
            DummyResult(original),
            DummyResult(None),
            DummyResult(balance),
        ]
    )

    await refund_order_in_session(db, order)
    await refund_order_in_session(db, order)

    clawbacks = [item for item in db.added if isinstance(item, CreditTransaction) and item.reason == "refund_clawback"]
    assert order.status == "refunded"
    assert balance.balance == 200
    assert balance.reserved == 200
    assert len(clawbacks) == 1
    assert clawbacks[0].delta == -500
    assert clawbacks[0].balance_after == 200


@pytest.mark.asyncio
async def test_partial_topup_refunds_are_proportional_and_keep_order_nonterminal():
    from app.services.billing_events import refund_order_in_session

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=1000,
        amount_cents=100,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    original = CreditTransaction(
        tenant_id=order.tenant_id,
        delta=1000,
        balance_after=1000,
        reason="topup",
        ref_type="order",
        ref_id=order.id,
    )
    balance = CreditBalance(tenant_id=order.tenant_id, balance=1000, reserved=300)
    db = MockDB(
        execute_results=[
            DummyResult(original),
            DummyResult(0),
            DummyResult(balance),
            DummyResult(original),
            DummyResult(-250),
            DummyResult(balance),
        ]
    )

    await refund_order_in_session(db, order, refund_amount_cents=25)
    assert order.status == "partially_refunded"
    assert order.refunded_amount_cents == 25
    assert order.refunded_credits == 250
    assert balance.balance == 750
    assert balance.reserved == 300

    await refund_order_in_session(db, order, refund_amount_cents=25)
    assert order.status == "partially_refunded"
    assert order.refunded_amount_cents == 50
    assert order.refunded_credits == 500
    assert balance.balance == 500
    assert balance.reserved == 300


@pytest.mark.asyncio
async def test_partial_subscription_refund_does_not_cancel_current_plan():
    from app.services.billing_events import refund_order_in_session

    tenant_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=plan_id,
        change_kind="new",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    original = CreditTransaction(
        tenant_id=tenant_id,
        delta=1000,
        balance_after=1000,
        reason="subscribe",
        ref_type="order",
        ref_id=order.id,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=0)
    subscription = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan_id,
        status="active",
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db = MockDB(
        execute_results=[
            DummyResult(original),
            DummyResult(0),
            DummyResult(balance),
        ]
    )

    await refund_order_in_session(db, order, refund_amount_cents=500)

    assert order.status == "partially_refunded"
    assert order.refunded_amount_cents == 500
    assert order.refunded_credits == 250
    assert subscription.status == "active"


@pytest.mark.asyncio
async def test_full_historical_subscription_refund_does_not_cancel_newer_plan():
    from app.services.billing_events import refund_order_in_session

    tenant_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=plan_id,
        change_kind="new",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    original = CreditTransaction(
        tenant_id=tenant_id,
        delta=1000,
        balance_after=1000,
        reason="subscribe",
        ref_type="order",
        ref_id=order.id,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=0)
    newer_order_id = uuid.uuid4()
    db = MockDB(
        execute_results=[
            DummyResult(original),
            DummyResult(0),
            DummyResult(balance),
            DummyResult(newer_order_id),
        ]
    )

    await refund_order_in_session(db, order)

    assert order.status == "refunded"
    assert order.refunded_amount_cents == order.amount_cents
    assert db.execute_results == []


@pytest.mark.asyncio
async def test_latest_refunded_subscription_is_canceled_after_credit_clawback():
    from app.services.billing_events import refund_order_in_session

    tenant_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=plan_id,
        change_kind="new",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    original = CreditTransaction(
        tenant_id=tenant_id,
        delta=1000,
        balance_after=1000,
        reason="subscribe",
        ref_type="order",
        ref_id=order.id,
    )
    balance = CreditBalance(tenant_id=tenant_id, balance=1000, reserved=0)
    subscription = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan_id,
        status="active",
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc) + timedelta(days=30),
        auto_renew=True,
    )
    db = MockDB(
        execute_results=[
            DummyResult(original),
            DummyResult(None),
            DummyResult(balance),
            DummyResult(None),
            DummyResult(subscription),
        ]
    )

    await refund_order_in_session(db, order)

    assert order.status == "refunded"
    assert balance.balance == 0
    assert subscription.status == "canceled"
    assert subscription.auto_renew is False
    assert subscription.period_end <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_subscription_summary_uses_server_balance_reservations_and_ledger_totals():
    tenant_id = uuid.uuid4()
    plan = Plan(
        id=uuid.uuid4(),
        code="free",
        name="Free",
        is_active=True,
        credits_per_period=1000,
        max_agents=1,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        period_end=None,
        seats=1,
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    balance = CreditBalance(tenant_id=tenant_id, balance=903, reserved=100)
    transactions = [
        CreditTransaction(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delta=1000,
            balance_after=1000,
            reason="subscribe",
            created_at=datetime.now(timezone.utc),
        ),
        CreditTransaction(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delta=50,
            balance_after=1050,
            reason="topup",
            created_at=datetime.now(timezone.utc),
        ),
        CreditTransaction(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            delta=-147,
            balance_after=903,
            reason="consume",
            created_at=datetime.now(timezone.utc),
        ),
    ]
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyManyResult(transactions)],
    )

    with (
        patch.object(subscription_api, "get_active_subscription", AsyncMock(return_value=sub)),
        patch.object(subscription_api, "get_credit_balance", AsyncMock(return_value=balance)),
        patch.object(subscription_api, "get_tenant_entitlements", AsyncMock(return_value=_ent())),
        patch("app.services.quota_guard._count_active_tenant_agents", AsyncMock(return_value=1)),
    ):
        summary = await subscription_api.get_subscription_summary(current_user=user, db=db)

    assert summary.plan_code == "free"
    assert summary.period_grant == 1000
    assert summary.topup_grants == 50
    assert summary.consumed_credits == 147
    assert summary.refunded_credits == 0
    assert summary.total_granted == 1050
    assert summary.balance == 903
    assert summary.reserved == 100
    assert summary.available_balance == 803
    assert summary.seats_used == 1
    assert summary.seats_total == 2


@pytest.mark.asyncio
async def test_get_credit_transactions_adds_client_labels_for_consumer_and_actor():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id)
    tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=-3,
        balance_after=997,
        reason="consume",
        user_id=user_id,
        agent_id=agent_id,
        action="heartbeat",
        modality="text",
        tier="lite",
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB(
        get_map={
            (Agent, agent_id): Agent(id=agent_id, name="Clawiee", creator_id=user_id, tenant_id=tenant_id),
            (User, user_id): User(id=user_id, display_name="tyree sun", tenant_id=tenant_id),
        }
    )

    with patch.object(subscription_api, "list_credit_transactions", AsyncMock(return_value=([tx], 1))):
        result = await subscription_api.get_credit_transactions(
            page=1,
            limit=30,
            current_user=current_user,
            db=db,
        )

    assert result[0].consumer_label == "Clawiee"
    assert result[0].actor_label == "tyree sun"


@pytest.mark.asyncio
async def test_get_credit_transactions_falls_back_to_current_user_label():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    current_user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        display_name="",
        username="qa.user",
        email="qa.user@example.com",
    )
    tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=-1,
        balance_after=999,
        reason="consume",
        user_id=user_id,
        action="chat",
        modality="text",
        tier="lite",
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB()

    with patch.object(subscription_api, "list_credit_transactions", AsyncMock(return_value=([tx], 1))):
        result = await subscription_api.get_credit_transactions(
            page=1,
            limit=30,
            current_user=current_user,
            db=db,
        )

    assert result[0].actor_label == "qa.user"


@pytest.mark.asyncio
async def test_incident_refund_has_explicit_platform_labels_without_ledger_mutation():
    tenant_id = uuid.uuid4()
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    tx = CreditTransaction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        delta=490,
        balance_after=1490,
        reason="refund",
        ref_type="product_incident",
        ref_id=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
    )
    db = MockDB()

    with patch.object(subscription_api, "list_credit_transactions", AsyncMock(return_value=([tx], 1))):
        result = await subscription_api.get_credit_transactions(
            page=1,
            limit=30,
            current_user=current_user,
            db=db,
        )

    assert result[0].consumer_label == "平台事故补偿"
    assert result[0].actor_label == "系统管理员"
    assert tx.reason == "refund"
    assert tx.ref_type == "product_incident"


def test_agent_plan_selection_defaults_to_first_allowed_tier_and_modality():
    tier, modality = agents_api._resolve_agent_plan_selection(_ent(), None, None)

    assert tier == "lite"
    assert modality == "text"


def test_agent_plan_selection_rejects_disallowed_tier():
    with pytest.raises(Exception) as exc:
        agents_api._resolve_agent_plan_selection(_ent(), "pro", "text")

    assert getattr(exc.value, "status_code", None) == 403
    assert "Tier 'pro'" in str(exc.value.detail)


def test_agent_plan_selection_rejects_disallowed_modality():
    with pytest.raises(Exception) as exc:
        agents_api._resolve_agent_plan_selection(_ent(), "lite", "image")

    assert getattr(exc.value, "status_code", None) == 403
    assert "Modality 'image'" in str(exc.value.detail)


def test_agent_plan_selection_allows_legacy_tenants_without_entitlements():
    data = AgentCreate(name="Valid Agent", preferred_tier="pro", preferred_modality="image")

    tier, modality = agents_api._resolve_agent_plan_selection(None, data.preferred_tier, data.preferred_modality)

    assert tier == "pro"
    assert modality == "image"


@pytest.mark.asyncio
async def test_llm_credit_settlement_does_not_recount_tenant_tokens():
    model = SimpleNamespace(provider="minimax", model="MiniMax-M2.7", modality="text", tier="basic")
    usage = TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20)

    with (
        patch.object(llm_caller, "consume_agent_llm_quota", AsyncMock()) as consume_quota,
        patch.object(llm_caller, "record_tenant_tokens", AsyncMock(), create=True) as record_tokens,
        patch.object(llm_caller, "charge_credits", AsyncMock()) as charge,
    ):
        await llm_caller._record_llm_usage_and_charge(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            model=model,
            usage=usage,
            route_meta=llm_caller.RouteMeta(saas_tier="lite", modality="text"),
        )

    consume_quota.assert_awaited_once()
    assert consume_quota.await_args.kwargs["model_tier"] == "lite"
    record_tokens.assert_not_awaited()
    charge.assert_awaited_once()
    assert charge.await_args.kwargs["delta"] == 1


@pytest.mark.asyncio
async def test_plan_credits_are_periodic_balance_grants_not_token_budget():
    tenant_id = uuid.uuid4()
    ent = _ent()
    ent.credits_per_period = 1

    with (
        patch.object(quota_guard, "get_tenant_entitlements", AsyncMock(return_value=ent)),
        patch.object(quota_guard, "_today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 9))),
        patch.object(quota_guard, "_get_tenant_usage", AsyncMock(return_value=(999_999, 0))),
    ):
        await quota_guard.check_tenant_token_credits(tenant_id)


@pytest.mark.asyncio
async def test_finalize_order_yearly_period_grants_365_days():
    from app.services.billing_events import finalize_order_in_session

    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=2000)
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        plan_id=plan.id,
        period="yearly",
        amount_cents=19200,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    db = MockDB(get_map={(Plan, plan.id): plan}, execute_results=[DummyResult(None)])
    now = datetime.now(timezone.utc)

    await finalize_order_in_session(db, order, paid_at=now)

    assert order.status == "paid"
    sub = next(item for item in db.added if isinstance(item, Subscription))
    assert sub.period_end - sub.period_start >= timedelta(days=364)


@pytest.mark.asyncio
async def test_finalize_order_monthly_period_grants_30_days():
    from app.services.billing_events import finalize_order_in_session

    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=2000)
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="subscribe",
        plan_id=plan.id,
        period="monthly",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    db = MockDB(get_map={(Plan, plan.id): plan}, execute_results=[DummyResult(None)])
    now = datetime.now(timezone.utc)

    await finalize_order_in_session(db, order, paid_at=now)

    sub = next(item for item in db.added if isinstance(item, Subscription))
    assert timedelta(days=29) <= sub.period_end - sub.period_start <= timedelta(days=31)


@pytest.mark.asyncio
async def test_finalize_order_renewal_stacks_remaining_time():
    from app.services.billing_events import finalize_order_in_session

    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=2000)
    now = datetime.now(timezone.utc)
    existing = Subscription(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        plan_id=plan.id,
        status="active",
        period_start=now - timedelta(days=25),
        period_end=now + timedelta(days=5),
    )
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=existing.tenant_id,
        type="subscribe",
        plan_id=plan.id,
        period="monthly",
        amount_cents=2000,
        currency="CNY",
        provider="wechat",
        status="pending",
    )
    db = MockDB(get_map={(Plan, plan.id): plan}, execute_results=[DummyResult(existing)])

    await finalize_order_in_session(db, order, paid_at=now)

    # 5 remaining days + 30 new days, not a restart from now.
    assert timedelta(days=34) <= existing.period_end - now <= timedelta(days=36)


@pytest.mark.asyncio
async def test_checkout_subscribe_yearly_uses_feature_price_and_seats():
    tenant_id = uuid.uuid4()
    plan = Plan(
        id=uuid.uuid4(),
        code="pro",
        name="Pro",
        is_active=True,
        price_cents=20_00,
        currency="CNY",
        features={"yearly_price_cents": 192_00},
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    checkout = SimpleNamespace(
        provider="wechat",
        session_id="ordersession",
        session_url="weixin://wxpay/bizpayurl?pr=x",
        payment_id=None,
    )
    provider = SimpleNamespace(create_subscription_checkout=AsyncMock(return_value=checkout))
    db = MockDB(get_map={(Plan, plan.id): plan})

    with patch.object(subscription_api, "get_billing_provider", return_value=provider, create=True):
        order = await subscription_api.checkout_subscribe(
            CheckoutSubscribeIn(plan_id=plan.id, period="yearly", seats=3),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )

    assert order.period == "yearly"
    assert order.amount_cents == 192_00 * 3


@pytest.mark.asyncio
async def test_checkout_subscribe_yearly_defaults_to_twenty_percent_off():
    tenant_id = uuid.uuid4()
    plan = Plan(
        id=uuid.uuid4(),
        code="pro",
        name="Pro",
        is_active=True,
        price_cents=20_00,
        currency="CNY",
        features=None,
    )
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    checkout = SimpleNamespace(
        provider="wechat",
        session_id="ordersession",
        session_url="weixin://wxpay/bizpayurl?pr=x",
        payment_id=None,
    )
    provider = SimpleNamespace(create_subscription_checkout=AsyncMock(return_value=checkout))
    db = MockDB(get_map={(Plan, plan.id): plan})

    with patch.object(subscription_api, "get_billing_provider", return_value=provider, create=True):
        order = await subscription_api.checkout_subscribe(
            CheckoutSubscribeIn(plan_id=plan.id, period="yearly", seats=1),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )

    # Frontend default: 12 months minus the 20% yearly discount.
    assert order.amount_cents == round(20_00 * 12 * 0.8)


@pytest.mark.asyncio
async def test_checkout_subscribe_rejects_invalid_period_and_seats():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=20_00)
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    db = MockDB(get_map={(Plan, plan.id): plan})

    with pytest.raises(HTTPException) as period_exc:
        await subscription_api.checkout_subscribe(
            CheckoutSubscribeIn(plan_id=plan.id, period="weekly", seats=1),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )
    assert period_exc.value.status_code == 400

    with pytest.raises(HTTPException) as seats_exc:
        await subscription_api.checkout_subscribe(
            CheckoutSubscribeIn(plan_id=plan.id, period="monthly", seats=0),
            SimpleNamespace(headers={"host": "localhost"}),
            current_user=user,
            db=db,
        )
    assert seats_exc.value.status_code == 400


@pytest.mark.asyncio
async def test_sync_pending_order_from_provider_finalizes_paid():
    from app.services.billing_events import PaymentProviderEventState, sync_pending_order_from_provider

    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=2000)
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=10_000,
        amount_cents=1500,
        currency="CNY",
        provider="wechat",
        provider_session_id="",
        status="pending",
    )
    state = PaymentProviderEventState(
        provider="wechat",
        event_id=f"query:{order.id}",
        event_type="QUERY",
        order_id=order.id,
        status="paid",
        provider_session_id=order.id.hex,
        provider_payment_id="4200001234",
    )
    provider = SimpleNamespace(query_order_state=AsyncMock(return_value=state))
    db = MockDB(get_map={(Plan, plan.id): plan}, execute_results=[DummyResult(order)])

    with patch("app.services.billing_provider.get_billing_provider", return_value=provider):
        changed = await sync_pending_order_from_provider(db, order)

    assert changed is True
    assert order.status == "paid"
    assert order.provider_payment_id == "4200001234"


@pytest.mark.asyncio
async def test_sync_pending_order_from_provider_ignores_unsupported_provider():
    from app.services.billing_events import sync_pending_order_from_provider

    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=10_000,
        amount_cents=1500,
        currency="CNY",
        provider="manual",
        status="pending",
    )
    db = MockDB()

    assert await sync_pending_order_from_provider(db, order) is False


@pytest.mark.asyncio
async def test_get_checkout_status_syncs_pending_wechat_order():
    from app.services.billing_events import PaymentProviderEventState

    plan = Plan(id=uuid.uuid4(), code="pro", name="Pro", is_active=True, price_cents=2000)
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="topup",
        credits=10_000,
        amount_cents=1500,
        currency="CNY",
        provider="wechat",
        provider_session_id="",
        status="pending",
    )
    state = PaymentProviderEventState(
        provider="wechat",
        event_id=f"query:{order.id}",
        event_type="QUERY",
        order_id=order.id,
        status="paid",
        provider_session_id=order.id.hex,
        provider_payment_id="4200005678",
    )
    provider = SimpleNamespace(query_order_state=AsyncMock(return_value=state))
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=order.tenant_id)
    db = MockDB(
        get_map={(PaymentOrder, order.id): order, (Plan, plan.id): plan},
        execute_results=[DummyResult(order)],
    )

    with (
        patch("app.services.billing_provider.get_billing_provider", return_value=provider),
        patch.object(subscription_api, "_apply_paid_subscribe_effects", new=AsyncMock()) as effects,
    ):
        result = await subscription_api.get_checkout_status(order.id, current_user=user, db=db)

    assert result is order
    assert order.status == "paid"
    assert order.provider_payment_id == "4200005678"
    effects.assert_awaited_once_with(order)
