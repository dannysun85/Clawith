"""Unit tests for subscription_lifecycle (§3.6 兜底): expire + enforce/restore agent limit.

Mock-based (no DB). Verifies the stop-excess / restore / expire logic.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import subscription_lifecycle
from app.services.entitlements import Entitlements


def _ent(max_agents=2):
    return Entitlements(
        plan_id=uuid.uuid4(), plan_code="free", max_agents=max_agents,
        max_llm_calls_per_day=1000, message_limit=50, message_period="permanent",
        max_triggers=20, credits_per_period=0,
        allowed_modalities=["text"], allowed_tiers=["standard"],
    )


def _agents(n, status="running"):
    return [
        SimpleNamespace(
            id=uuid.uuid4(),
            status=status,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
            is_expired=False,
        )
        for i in range(n)
    ]


def _session_with_execute(results):
    """Fake async_session whose db.execute returns `results` (list, consumed in order)."""
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(side_effect=results)
    fake_db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    return patch.object(subscription_lifecycle, "async_session", return_value=fake_session), fake_db


def _scalars_result(items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = items
    return r


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


class RecordingDB:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0

    async def execute(self, _stmt):
        if self.results:
            return self.results.pop(0)
        return _scalar_result(None)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


# ── default subscription provisioning ─────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_free_subscription_grants_initial_credits():
    tenant_id = uuid.uuid4()
    granted_by = uuid.uuid4()
    plan = SimpleNamespace(id=uuid.uuid4(), code="free", credits_per_period=1000)
    db = RecordingDB([_scalar_result(plan), _scalar_result(None)])

    with patch.object(subscription_lifecycle, "grant_credits_in_session", AsyncMock()) as grant:
        sub = await subscription_lifecycle.ensure_free_subscription_for_tenant(
            db,
            tenant_id,
            granted_by=granted_by,
        )

    assert sub.tenant_id == tenant_id
    assert sub.plan_id == plan.id
    assert sub.period_end is None
    assert db.added == [sub]
    assert db.flush_count == 1
    grant.assert_awaited_once()
    assert grant.await_args.kwargs["tenant_id"] == tenant_id
    assert grant.await_args.kwargs["amount"] == 1000
    assert grant.await_args.kwargs["reason"] == "subscribe"
    assert grant.await_args.kwargs["granted_by"] == granted_by
    assert grant.await_args.kwargs["ref_type"] == "subscription"
    assert grant.await_args.kwargs["ref_id"] == sub.id


@pytest.mark.asyncio
async def test_ensure_free_subscription_does_not_duplicate_existing_subscription():
    tenant_id = uuid.uuid4()
    plan = SimpleNamespace(id=uuid.uuid4(), code="free", credits_per_period=1000)
    existing = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, plan_id=plan.id)
    db = RecordingDB([_scalar_result(plan), _scalar_result(existing)])

    with patch.object(subscription_lifecycle, "grant_credits_in_session", AsyncMock()) as grant:
        sub = await subscription_lifecycle.ensure_free_subscription_for_tenant(db, tenant_id)

    assert sub is existing
    assert db.added == []
    assert db.flush_count == 0
    grant.assert_not_awaited()


# ── enforce_agent_limit ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enforce_stops_excess_agents_keeps_oldest():
    """4 active agents, max_agents=2 → stop 2 newest, keep 2 oldest."""
    agents = _agents(4)  # created_at ascending
    sess, _ = _session_with_execute([_scalars_result(agents)])
    with sess, patch.object(subscription_lifecycle, "get_tenant_entitlements", AsyncMock(return_value=_ent(2))):
        stopped = await subscription_lifecycle.enforce_agent_limit(uuid.uuid4())
    assert stopped == 2
    assert agents[0].status == "running"  # oldest kept
    assert agents[1].status == "running"
    assert agents[2].status == "stopped"  # newest excess
    assert agents[3].status == "stopped"


@pytest.mark.asyncio
async def test_enforce_noop_when_under_limit():
    """2 active agents, max_agents=5 → no stop, no commit."""
    agents = _agents(2)
    sess, fake_db = _session_with_execute([_scalars_result(agents)])
    with sess, patch.object(subscription_lifecycle, "get_tenant_entitlements", AsyncMock(return_value=_ent(5))):
        stopped = await subscription_lifecycle.enforce_agent_limit(uuid.uuid4())
    assert stopped == 0
    assert all(a.status == "running" for a in agents)
    fake_db.commit.assert_not_called()


# ── restore_stopped_agents ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_revives_stopped_upto_slots():
    """2 active + 3 stopped, max_agents=5 → restore all 3 stopped to idle."""
    active = _agents(2, status="running")
    stopped = _agents(3, status="stopped")
    sess, _ = _session_with_execute([_scalars_result(active), _scalars_result(stopped)])
    with sess, patch.object(subscription_lifecycle, "get_tenant_entitlements", AsyncMock(return_value=_ent(5))):
        restored = await subscription_lifecycle.restore_stopped_agents(uuid.uuid4())
    assert restored == 3
    assert all(a.status == "idle" for a in stopped)


@pytest.mark.asyncio
async def test_restore_noop_when_no_slots():
    """5 active (max_agents=5) → slots=0 → no restore, no stopped query."""
    active = _agents(5, status="running")
    sess, fake_db = _session_with_execute([_scalars_result(active)])
    with sess, patch.object(subscription_lifecycle, "get_tenant_entitlements", AsyncMock(return_value=_ent(5))):
        restored = await subscription_lifecycle.restore_stopped_agents(uuid.uuid4())
    assert restored == 0
    fake_db.commit.assert_not_called()


# ── expire_subscriptions ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expire_marks_past_period_end_and_enforces():
    """active sub past period_end → marked expired + enforce_agent_limit called."""
    now = datetime.now(timezone.utc)
    sub_expired = SimpleNamespace(tenant_id=uuid.uuid4(), status="active", period_end=now - timedelta(days=1))
    # First execute → active/trialing/canceled past period_end; second → past_due (empty)
    sess, fake_db = _session_with_execute([_scalars_result([sub_expired]), _scalars_result([])])
    with sess, patch.object(subscription_lifecycle, "enforce_agent_limit", AsyncMock()) as enforce_mock:
        count = await subscription_lifecycle.expire_subscriptions()
    assert count == 1
    assert sub_expired.status == "expired"
    assert fake_db.commit.await_count == 1
    enforce_mock.assert_called_once_with(sub_expired.tenant_id)


@pytest.mark.asyncio
async def test_expire_noop_when_none_past_cutoff():
    """No subscriptions past cutoff → 0 expired, no commit, no enforce."""
    sess, fake_db = _session_with_execute([_scalars_result([]), _scalars_result([])])
    with sess, patch.object(subscription_lifecycle, "enforce_agent_limit", AsyncMock()) as enforce_mock:
        count = await subscription_lifecycle.expire_subscriptions()
    assert count == 0
    fake_db.commit.assert_not_called()
    enforce_mock.assert_not_called()
