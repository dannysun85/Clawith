"""Subscription plan-change semantics: change_kind classification, downgrade
scheduling at finalize, and lifecycle-daemon application at period_end."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import subscription as subscription_api
from app.models.subscription import PaymentOrder, Plan, Subscription
from app.services import subscription_lifecycle


class DummyResult:
    def __init__(self, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars or [])


class MockDB:
    def __init__(self, *, get_map=None, execute_results=None):
        self.get_map = get_map or {}
        self.execute_results = list(execute_results or [])
        self.added = []

    async def get(self, model, key, *args, **kwargs):
        value = self.get_map.get((model, key))
        if value is not None:
            return value
        return self.get_map.get(model)

    async def execute(self, _stmt=None, _params=None):
        if self.execute_results:
            return self.execute_results.pop(0)
        return DummyResult()

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        pass

    async def commit(self):
        pass


def _plan(code: str, tier: int, credits: int = 100) -> Plan:
    return Plan(
        id=uuid.uuid4(),
        code=code,
        name=code.capitalize(),
        tier=tier,
        price_cents=1000 * (tier + 1),
        credits_per_period=credits,
        is_active=True,
    )


def _sub(tenant_id, plan, *, status="active", days_left=10) -> Subscription:
    now = datetime.now(timezone.utc)
    return Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status=status,
        period_start=now - timedelta(days=30 - days_left),
        period_end=now + timedelta(days=days_left),
    )


# ── _classify_plan_change ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_new_without_active_subscription():
    tenant_id = uuid.uuid4()
    plan = _plan("pro", 2)
    db = MockDB(execute_results=[DummyResult(scalar=None)])
    assert await subscription_api._classify_plan_change(db, tenant_id, plan, "monthly") == "new"


@pytest.mark.asyncio
async def test_classify_upgrade_for_higher_tier():
    tenant_id = uuid.uuid4()
    current = _plan("starter", 1)
    target = _plan("pro", 2)
    sub = _sub(tenant_id, current)
    db = MockDB(get_map={(Plan, current.id): current}, execute_results=[DummyResult(scalar=sub)])
    assert await subscription_api._classify_plan_change(db, tenant_id, target, "monthly") == "upgrade"


@pytest.mark.asyncio
async def test_classify_downgrade_for_lower_tier():
    tenant_id = uuid.uuid4()
    current = _plan("scale", 3)
    target = _plan("pro", 2)
    sub = _sub(tenant_id, current)
    db = MockDB(get_map={(Plan, current.id): current}, execute_results=[DummyResult(scalar=sub)])
    assert await subscription_api._classify_plan_change(db, tenant_id, target, "monthly") == "downgrade"


@pytest.mark.asyncio
async def test_classify_renew_same_plan_same_period():
    tenant_id = uuid.uuid4()
    plan = _plan("pro", 2)
    sub = _sub(tenant_id, plan)
    last_order = SimpleNamespace(period="monthly")
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyResult(scalar=sub), DummyResult(scalar=last_order)],
    )
    assert await subscription_api._classify_plan_change(db, tenant_id, plan, "monthly") == "renew"


@pytest.mark.asyncio
async def test_classify_period_switch_same_plan():
    tenant_id = uuid.uuid4()
    plan = _plan("pro", 2)
    sub = _sub(tenant_id, plan)
    last_order = SimpleNamespace(period="monthly")
    db = MockDB(
        get_map={(Plan, plan.id): plan},
        execute_results=[DummyResult(scalar=sub), DummyResult(scalar=last_order)],
    )
    assert await subscription_api._classify_plan_change(db, tenant_id, plan, "yearly") == "period_switch"


# ── finalize: downgrade schedules instead of discarding paid time ────────


@pytest.mark.asyncio
async def test_finalize_downgrade_schedules_without_switching():
    from app.services.billing_events import finalize_order_in_session

    tenant_id = uuid.uuid4()
    current = _plan("scale", 3, credits=1000)
    target = _plan("pro", 2, credits=500)
    sub = _sub(tenant_id, current, days_left=12)
    original_end = sub.period_end
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=target.id,
        period="monthly",
        change_kind="downgrade",
        amount_cents=2000,
        currency="CNY",
        status="pending",
    )
    db = MockDB(
        get_map={(Plan, target.id): target},
        execute_results=[DummyResult(scalar=sub)],
    )

    with patch("app.services.billing_events.grant_credits_in_session", AsyncMock()) as grant:
        await finalize_order_in_session(db, order)

    assert order.status == "paid"
    assert sub.plan_id == current.id  # current plan kept
    assert sub.period_end == original_end  # paid time untouched
    assert sub.scheduled_plan_id == target.id
    assert sub.scheduled_period == "monthly"
    grant.assert_not_called()  # credits arrive at activation


@pytest.mark.asyncio
async def test_finalize_upgrade_applies_immediately_with_credits():
    from app.services.billing_events import finalize_order_in_session

    tenant_id = uuid.uuid4()
    current = _plan("starter", 1)
    target = _plan("pro", 2, credits=500)
    sub = _sub(tenant_id, current, days_left=5)
    order = PaymentOrder(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        type="subscribe",
        plan_id=target.id,
        period="monthly",
        change_kind="upgrade",
        amount_cents=3000,
        currency="CNY",
        status="pending",
    )
    db = MockDB(
        get_map={(Plan, target.id): target},
        execute_results=[DummyResult(scalar=sub)],
    )

    with patch("app.services.billing_events.grant_credits_in_session", AsyncMock()) as grant:
        await finalize_order_in_session(db, order)

    assert sub.plan_id == target.id
    assert sub.scheduled_plan_id is None
    # stacks on the 5 remaining days: old_end + 30
    assert sub.period_end > datetime.now(timezone.utc) + timedelta(days=34)
    grant.assert_awaited_once()


# ── daemon applies the scheduled downgrade at period_end ─────────────────


@pytest.mark.asyncio
async def test_expire_subscriptions_applies_scheduled_downgrade():
    tenant_id = uuid.uuid4()
    current = _plan("scale", 3)
    target = _plan("pro", 2, credits=500)
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=current.id,
        status="active",
        period_start=now - timedelta(days=30),
        period_end=now - timedelta(days=1),  # past cutoff
        scheduled_plan_id=target.id,
        scheduled_period="yearly",
    )
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sub])),  # r1
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),  # r2 past_due
        ]
    )
    fake_db.get = AsyncMock(return_value=target)
    fake_db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(subscription_lifecycle, "async_session", return_value=fake_session),
        patch.object(subscription_lifecycle, "grant_credits_in_session", AsyncMock()) as grant,
        patch.object(subscription_lifecycle, "enforce_agent_limit", AsyncMock()),
    ):
        await subscription_lifecycle.expire_subscriptions()

    assert sub.status == "active"
    assert sub.plan_id == target.id
    assert sub.scheduled_plan_id is None
    assert sub.period_end > now + timedelta(days=360)  # yearly from old period_end
    grant.assert_awaited_once()


@pytest.mark.asyncio
async def test_expire_subscriptions_expires_without_scheduled_plan():
    tenant_id = uuid.uuid4()
    plan = _plan("pro", 2)
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=now - timedelta(days=30),
        period_end=now - timedelta(days=1),
    )
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sub])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])),
        ]
    )
    fake_db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(subscription_lifecycle, "async_session", return_value=fake_session),
        patch.object(subscription_lifecycle, "enforce_agent_limit", AsyncMock()),
    ):
        await subscription_lifecycle.expire_subscriptions()

    assert sub.status == "expired"
