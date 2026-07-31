import uuid
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api import agents as agents_api
from app.api import saas as saas_api
from app.api import subscription as subscription_api
from app.models.subscription import CreditBalance, Plan, Subscription
from app.schemas.schemas import AgentCreate
from app.services import credit_service, quota_guard
from app.services.entitlements import Entitlements
from app.services.quota_guard import QuotaExceeded


def _session_with_execute(results):
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(side_effect=list(results))
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    return patch.object(quota_guard, "async_session", return_value=fake_session)


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars_result(values):
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _row_result(value):
    result = MagicMock()
    result.one_or_none.return_value = value
    return result


def _no_returning_row_result():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    return result


def _many_result(values):
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


@pytest.mark.asyncio
async def test_employee_seat_count_excludes_only_onboarding_linked_assistants():
    tenant_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_scalars_result([SimpleNamespace(id=uuid.uuid4())]))

    assert await quota_guard._count_active_tenant_agents(tenant_id, db) == 1

    statement = str(db.execute.await_args.args[0])
    assert "user_tenant_onboarding" in statement
    assert "personal_assistant_agent_id" in statement
    assert "agents.role_description" not in statement.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_agent_creation_quota_blocks_org_admin_when_tenant_limit_is_full():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, role="org_admin", tenant_id=tenant_id)
    existing_agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        status="running",
        is_expired=False,
    )

    with (
        _session_with_execute([_scalar_result(user), _scalars_result([existing_agent])]),
        patch.object(quota_guard, "_tenant_max_agents", AsyncMock(return_value=1)),
    ):
        with pytest.raises(QuotaExceeded) as exc:
            await quota_guard.check_agent_creation_quota(user_id)

    assert exc.value.quota_type == "max_agents"
    assert "1/1" in exc.value.message
    assert "升级套餐" in exc.value.message


@pytest.mark.asyncio
async def test_create_agent_api_returns_payment_required_with_upgrade_url_at_plan_limit():
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        quota_agent_ttl_hours=0,
    )

    with patch.object(
        agents_api,
        "check_agent_creation_quota",
        AsyncMock(side_effect=QuotaExceeded("Agent 创建数量已达当前套餐上限（1/1）。请前往「套餐详情」升级套餐后继续。", "max_agents")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.create_agent(
                AgentCreate(name="Extra Agent"),
                background_tasks=BackgroundTasks(),
                current_user=user,
                db=MagicMock(),
            )

    assert exc.value.status_code == 402
    assert exc.value.detail["error"] == "QUOTA_EXCEEDED"
    assert exc.value.detail["quota_type"] == "max_agents"
    assert exc.value.detail["action"] == "upgrade"
    assert exc.value.detail["details"]["upgrade_url"] == "/account/subscription"


@pytest.mark.asyncio
async def test_subscription_seats_reports_agent_plan_usage_not_user_count():
    tenant_id = uuid.uuid4()
    entitlements = Entitlements(
        plan_id=uuid.uuid4(),
        plan_code="free",
        max_agents=1,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=1000,
        allowed_modalities=["text"],
        allowed_tiers=["lite"],
    )

    with (
        patch.object(subscription_api, "get_active_subscription", AsyncMock(return_value=SimpleNamespace(seats=99))),
        patch.object(subscription_api, "get_tenant_entitlements", AsyncMock(return_value=entitlements)),
        patch("app.services.quota_guard._count_active_tenant_agents", AsyncMock(return_value=1)),
    ):
        result = await subscription_api.get_seats(
            current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id),
            db=MagicMock(),
        )

    assert result.seats_total == 1
    assert result.seats_used == 1
    assert result.pending_invites == 0


@pytest.mark.asyncio
async def test_saas_tenants_reports_agent_plan_seats_not_user_count():
    tenant_id = uuid.uuid4()
    plan = Plan(id=uuid.uuid4(), code="free", name="Free", is_active=True, max_agents=1)
    sub = Subscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        period_start=datetime.now(timezone.utc),
        auto_renew=True,
        seats=99,
    )
    entitlements = Entitlements(
        plan_id=plan.id,
        plan_code="free",
        max_agents=1,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=1000,
        allowed_modalities=["text"],
        allowed_tiers=["lite"],
    )
    tenant = SimpleNamespace(id=tenant_id, name="Acme")
    human_users = [
        SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, is_active=True),
        SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, is_active=True),
    ]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _many_result([tenant]),
        _many_result(human_users),
    ])
    db.get = AsyncMock(side_effect=lambda model, key: {
        (Plan, plan.id): plan,
        (CreditBalance, tenant_id): CreditBalance(tenant_id=tenant_id, balance=1000, reserved=0),
    }.get((model, key)))

    with (
        patch.object(saas_api, "get_active_subscription", AsyncMock(return_value=sub)),
        patch.object(saas_api, "get_tenant_entitlements", AsyncMock(return_value=entitlements), create=True),
        patch("app.services.quota_guard._count_active_tenant_agents", AsyncMock(return_value=1)),
    ):
        tenants = await saas_api.list_tenants(current_user=SimpleNamespace(role="platform_admin"), db=db)

    assert tenants[0].seats_total == 1
    assert tenants[0].seats_used == 1


@pytest.mark.asyncio
async def test_credit_balance_error_guides_user_to_subscription_or_boost():
    with (
        patch.object(credit_service, "get_credit_cost", AsyncMock(return_value=5)),
        patch.object(
            credit_service,
            "get_credit_balance",
            AsyncMock(return_value=SimpleNamespace(balance=1, reserved=0)),
        ),
    ):
        with pytest.raises(QuotaExceeded) as exc:
            await credit_service.check_credit_balance(
                uuid.uuid4(),
                action="chat",
                modality="text",
                saas_tier="lite",
            )

    assert exc.value.quota_type == "insufficient_credits"
    assert "套餐详情" in exc.value.message
    assert "Boost" in exc.value.message


@pytest.mark.asyncio
async def test_increment_agent_llm_usage_raises_when_atomic_consume_returns_no_row():
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        primary_model_id=None,
        llm_calls_today=0,
        llm_calls_reset_at=None,
    )
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(side_effect=[
        _scalar_result(agent),
        _no_returning_row_result(),
    ])
    fake_db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(quota_guard, "async_session", return_value=fake_session),
        patch.object(quota_guard, "_today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 9))),
        patch.object(quota_guard, "_tenant_llm_limit", AsyncMock(return_value=1)),
    ):
        with pytest.raises(QuotaExceeded) as exc:
            await quota_guard.increment_agent_llm_usage(agent.id, model_tier="lite")

    assert exc.value.quota_type == "tenant_llm"
    assert agent.llm_calls_today == 0
    fake_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_increment_conversation_usage_raises_when_atomic_consume_returns_no_row():
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    lookup_db = MagicMock()
    lookup_db.execute = AsyncMock(return_value=_scalar_result(user))
    consume_db = MagicMock()
    consume_db.execute = AsyncMock(return_value=_no_returning_row_result())
    consume_db.commit = AsyncMock()

    lookup_session = MagicMock()
    lookup_session.__aenter__ = AsyncMock(return_value=lookup_db)
    lookup_session.__aexit__ = AsyncMock(return_value=None)
    consume_session = MagicMock()
    consume_session.__aenter__ = AsyncMock(return_value=consume_db)
    consume_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(quota_guard, "async_session", side_effect=[lookup_session, consume_session]),
        patch.object(quota_guard, "_today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 9))),
        patch.object(quota_guard, "_tenant_message_limit", AsyncMock(return_value=(1, "daily"))),
    ):
        with pytest.raises(QuotaExceeded) as exc:
            await quota_guard.increment_conversation_usage(user.id)

    assert exc.value.quota_type == "conversation"
    consume_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_agent_llm_quota_allows_only_one_of_two_racing_calls():
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        primary_model_id=None,
        llm_calls_today=0,
        llm_calls_reset_at=None,
    )

    success_db = MagicMock()
    success_db.execute = AsyncMock(side_effect=[
        _scalar_result(agent),
        _scalar_result(1),
    ])
    success_db.commit = AsyncMock()
    success_session = MagicMock()
    success_session.__aenter__ = AsyncMock(return_value=success_db)
    success_session.__aexit__ = AsyncMock(return_value=None)

    rejected_db = MagicMock()
    rejected_db.execute = AsyncMock(side_effect=[
        _scalar_result(agent),
        _no_returning_row_result(),
    ])
    rejected_db.commit = AsyncMock()
    rejected_session = MagicMock()
    rejected_session.__aenter__ = AsyncMock(return_value=rejected_db)
    rejected_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(quota_guard, "async_session", side_effect=[success_session, rejected_session]),
        patch.object(quota_guard, "_today_in_tenant_tz", AsyncMock(return_value=date(2026, 7, 9))),
        patch.object(quota_guard, "_tenant_llm_limit", AsyncMock(return_value=1)),
    ):
        results = await asyncio.gather(
            quota_guard.consume_agent_llm_quota(agent.id, model_tier="lite"),
            quota_guard.consume_agent_llm_quota(agent.id, model_tier="lite"),
            return_exceptions=True,
        )

    assert sum(result is None for result in results) == 1
    quota_errors = [result for result in results if isinstance(result, QuotaExceeded)]
    assert len(quota_errors) == 1
    assert quota_errors[0].quota_type == "tenant_llm"
    assert agent.llm_calls_today == 1
    success_db.commit.assert_awaited_once()
    rejected_db.commit.assert_not_awaited()
