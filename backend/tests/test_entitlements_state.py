"""Regression tests for get_tenant_entitlements state machine (entitlements.py).

Mock-based (no DB). Covers the period_end=None bug: a permanent subscription
(e.g. free plan, period_end=None) must NOT be treated as expired.
"""

import uuid
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import entitlements


def _sub(status="active", period_end=None):
    return SimpleNamespace(
        plan_id=uuid.uuid4(),
        status=status,
        period_end=period_end,
        created_at=None,
    )


def _plan():
    p = MagicMock()
    p.id = uuid.uuid4()
    p.code = "free"
    p.max_agents = 2
    p.max_llm_calls_per_day = 1000
    p.message_limit = 50
    p.message_period = "permanent"
    p.max_triggers = 20
    p.credits_per_period = 0
    p.allowed_modalities = ["text"]
    p.allowed_tiers = ["standard"]
    p.features = {
        "generation_modalities": ["image", "audio", "music", "video"],
        "generation_tiers": ["lite"],
    }
    return p


def _patch_env(sub=None, plan=None):
    """Patch get_active_subscription (→sub) and async_session (→ plan)."""
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = plan
    fake_db.execute = AsyncMock(return_value=fake_result)
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    return (
        patch.object(entitlements, "get_active_subscription", AsyncMock(return_value=sub)),
        patch.object(entitlements, "async_session", return_value=fake_session),
    )


@pytest.mark.asyncio
async def test_permanent_subscription_not_expired():
    """free plan (period_end=None) must return entitlements, NOT None. (regression)"""
    sp, pp = _patch_env(sub=_sub(status="active", period_end=None), plan=_plan())
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is not None
    assert ent.allowed_modalities == ["text"]
    assert ent.allowed_tiers == ["standard"]
    assert ent.generation_modalities == ["image", "audio", "music", "video"]
    assert ent.generation_tiers == ["lite"]


@pytest.mark.asyncio
async def test_legacy_plan_generation_falls_back_to_media_allowed_modalities():
    plan = _plan()
    plan.features = {}
    plan.allowed_modalities = ["text", "vision", "voice", "video"]
    plan.allowed_tiers = ["pro"]
    sp, pp = _patch_env(sub=_sub(status="active", period_end=None), plan=plan)
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent.generation_modalities == ["image", "audio", "video"]
    assert ent.generation_tiers == ["pro"]


@pytest.mark.asyncio
async def test_active_future_period_end_valid():
    from datetime import datetime
    future = datetime.now(timezone.utc) + timedelta(days=10)
    sp, pp = _patch_env(sub=_sub(status="active", period_end=future), plan=_plan())
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is not None


@pytest.mark.asyncio
async def test_active_past_period_end_expired():
    from datetime import datetime
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sp, pp = _patch_env(sub=_sub(status="active", period_end=past), plan=_plan())
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is None


@pytest.mark.asyncio
async def test_canceled_retains_until_period_end():
    from datetime import datetime
    future = datetime.now(timezone.utc) + timedelta(days=5)
    sp, pp = _patch_env(sub=_sub(status="canceled", period_end=future), plan=_plan())
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is not None  # canceled but still within period


@pytest.mark.asyncio
async def test_canceled_past_period_end_expired():
    from datetime import datetime
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sp, pp = _patch_env(sub=_sub(status="canceled", period_end=past), plan=_plan())
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is None


@pytest.mark.asyncio
async def test_no_subscription_returns_none():
    sp, pp = _patch_env(sub=None, plan=None)
    with sp, pp:
        ent = await entitlements.get_tenant_entitlements(uuid.uuid4())
    assert ent is None
