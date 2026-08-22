"""Regression coverage for routed billing on autonomous LLM entry points."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import caller as llm_caller
from app.services.token_tracker import TokenUsage
from app.services.quota_guard import QuotaExceeded


class FakeLLMClient:
    def __init__(self, *responses, failure_may_have_been_accepted=False):
        self._responses = list(responses)
        self.failure_may_have_been_accepted = failure_may_have_been_accepted
        self.provider_request_started = False
        self.close = AsyncMock()

    async def stream(self, **_kwargs):
        self.provider_request_started = False
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            self.provider_request_started = self.failure_may_have_been_accepted
            raise response
        self.provider_request_started = True
        return response


class FakeCompleteLLMClient:
    def __init__(self, response):
        self._response = response
        self.close = AsyncMock()

    async def complete(self, **_kwargs):
        return self._response


class FakeAmbiguousCompleteFailureClient:
    def __init__(self, error):
        self._error = error
        self.provider_request_started = True
        self.close = AsyncMock()

    async def complete(self, **_kwargs):
        raise self._error


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSessionContext:
    def __init__(self, value):
        self._value = value
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return FakeScalarResult(self._value)


class FakeQueryResult:
    def __init__(self, *, scalar=None, values=None):
        self._scalar = scalar
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._values


class FakeSequenceSessionContext:
    def __init__(self, *results):
        self._results = list(results)
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_prepare_agent_llm_invocation_uses_platform_route_and_billing_preflight():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        preferred_tier=None,
        preferred_modality=None,
        primary_model_id=uuid.uuid4(),
        fallback_model_id=None,
    )
    routed_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        enabled=True,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="ultra", modality="text")

    with (
        patch.object(
            llm_caller,
            "resolve_agent_model",
            AsyncMock(return_value=(routed_model, None, route_meta)),
        ) as resolve_route,
        patch.object(
            llm_caller,
            "_prepare_llm_billing_context",
            AsyncMock(return_value=tenant_id),
        ) as billing_preflight,
        patch.object(
            llm_caller,
            "resolve_model_key",
            AsyncMock(return_value=("pool-key", "https://api.example.test", credential_id)),
        ) as resolve_key,
    ):
        invocation = await llm_caller.prepare_agent_llm_invocation(agent, action="heartbeat")

    assert invocation is not None
    assert invocation.model is routed_model
    assert invocation.route_meta is not route_meta
    assert invocation.route_meta.action == "heartbeat"
    assert invocation.tenant_id == tenant_id
    assert invocation.api_key == "pool-key"
    assert invocation.credential_id == credential_id
    resolve_route.assert_awaited_once_with(agent)
    billing_preflight.assert_awaited_once_with(agent_id, routed_model, invocation.route_meta)
    resolve_key.assert_awaited_once_with(
        routed_model,
        capability_modality="text",
    )


@pytest.mark.asyncio
async def test_settle_agent_llm_invocation_records_pool_usage_without_double_charging():
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text", action="heartbeat")
    invocation = llm_caller.AgentLLMInvocation(
        model=model,
        fallback_model=None,
        route_meta=route_meta,
        tenant_id=tenant_id,
        api_key="pool-key",
        base_url="https://api.example.test",
        credential_id=credential_id,
    )
    usage = TokenUsage(total_tokens=1234, input_tokens=1000, output_tokens=234)

    with (
        patch.object(llm_caller, "record_credential_call", AsyncMock()) as record_pool_usage,
        patch.object(llm_caller, "_record_llm_usage_and_charge", AsyncMock()) as settle_credits,
    ):
        await llm_caller.settle_agent_llm_invocation(
            invocation,
            agent_id=agent_id,
            user_id=user_id,
            usage=usage,
        )

    record_pool_usage.assert_awaited_once_with(credential_id, tokens_used=1234)
    settle_credits.assert_awaited_once_with(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=tenant_id,
        model=model,
        usage=usage,
        route_meta=route_meta,
        charge_credits_enabled=False,
    )


@pytest.mark.asyncio
async def test_heartbeat_aggregate_settlement_only_consumes_agent_quota():
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
    )
    route_meta = llm_caller.RouteMeta(
        saas_tier="lite",
        modality="text",
        action="heartbeat",
    )
    invocation = llm_caller.AgentLLMInvocation(
        model=model,
        fallback_model=None,
        route_meta=route_meta,
        tenant_id=tenant_id,
        api_key="pool-key",
        base_url=None,
        credential_id=None,
    )
    usage = TokenUsage(total_tokens=1200, input_tokens=1000, output_tokens=200)

    with (
        patch.object(llm_caller, "consume_agent_llm_quota", AsyncMock()) as consume_quota,
        patch.object(llm_caller, "charge_credits", AsyncMock()) as charge_credits,
    ):
        await llm_caller.settle_agent_llm_invocation(
            invocation,
            agent_id=agent_id,
            user_id=user_id,
            usage=usage,
        )

    consume_quota.assert_awaited_once_with(agent_id, model_tier="lite")
    charge_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_round_reserves_conservative_max_output_cost_before_provider_call():
    reservation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    route_meta = llm_caller.RouteMeta(saas_tier="ultra", modality="text")

    with (
        patch.object(llm_caller, "get_credit_cost", AsyncMock(return_value=1)),
        patch.object(
            llm_caller,
            "reserve_credits",
            AsyncMock(return_value=SimpleNamespace(id=reservation_id)),
        ) as reserve,
    ):
        result = await llm_caller.reserve_llm_round_credits(
            tenant_id=tenant_id,
            user_id=None,
            agent_id=agent_id,
            model=model,
            route_meta=route_meta,
            messages=[llm_caller.LLMMessage(role="user", content="hello")],
            tools=None,
            max_tokens=8192,
            ref_id=run_id,
        )

    assert result == reservation_id
    assert reserve.await_args.kwargs["amount"] >= 10
    assert reserve.await_args.kwargs["action"] == "chat"
    assert reserve.await_args.kwargs["ref_type"] == "llm_round"
    assert reserve.await_args.kwargs["ref_id"] == run_id
    assert reserve.await_args.kwargs["initial_status"] == "provider_inflight"


@pytest.mark.asyncio
async def test_llm_round_persists_exact_debt_before_final_ledger_charge():
    events: list[str] = []
    reservation_id = uuid.uuid4()
    route_meta = llm_caller.RouteMeta(saas_tier="ultra", modality="text")
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    usage = TokenUsage(total_tokens=120, input_tokens=100, output_tokens=20)

    async def mark_ready(*_args, **kwargs):
        events.append(f"ready:{kwargs['amount']}")

    async def finalize(*_args, **_kwargs):
        events.append("finalize")

    with (
        patch.object(llm_caller, "get_credit_cost", AsyncMock(return_value=5)),
        patch.object(llm_caller, "mark_credit_reservation_settlement_ready", mark_ready),
        patch.object(llm_caller, "finalize_reserved_credits", finalize),
    ):
        await llm_caller.settle_llm_round_credits(
            reservation_id,
            usage=usage,
            model=model,
            route_meta=route_meta,
            agent_id=uuid.uuid4(),
            user_id=None,
            tenant_id=uuid.uuid4(),
        )

    assert events == ["ready:5", "finalize"]


@pytest.mark.asyncio
async def test_llm_round_uses_dynamic_cost_when_it_exceeds_tier_minimum():
    reservation_id = uuid.uuid4()
    route_meta = llm_caller.RouteMeta(saas_tier="ultra", modality="text")
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        capabilities={"service_tier": "priority"},
    )
    mark_ready = AsyncMock()

    with (
        patch.object(llm_caller, "get_credit_cost", AsyncMock(return_value=5)),
        patch.object(llm_caller, "provider_text_credits", return_value=9) as provider_price,
        patch.object(llm_caller, "mark_credit_reservation_settlement_ready", mark_ready),
        patch.object(llm_caller, "finalize_reserved_credits", AsyncMock()),
    ):
        await llm_caller.settle_llm_round_credits(
            reservation_id,
            usage=TokenUsage(total_tokens=9000, input_tokens=8000, output_tokens=1000),
            model=model,
            route_meta=route_meta,
            agent_id=uuid.uuid4(),
            user_id=None,
            tenant_id=uuid.uuid4(),
        )

    mark_ready.assert_awaited_once_with(reservation_id, amount=9)
    assert provider_price.call_args.kwargs["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_llm_round_outbox_failure_records_recoverable_debt_context():
    reservation_id = uuid.uuid4()
    route_meta = llm_caller.RouteMeta(saas_tier="ultra", modality="text")
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    monitor = AsyncMock()

    with (
        patch.object(llm_caller, "get_credit_cost", AsyncMock(return_value=5)),
        patch.object(
            llm_caller,
            "mark_credit_reservation_settlement_ready",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(llm_caller, "_record_llm_settlement_failure", monitor),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await llm_caller.settle_llm_round_credits(
                reservation_id,
                usage=TokenUsage(total_tokens=120, input_tokens=100, output_tokens=20),
                model=model,
                route_meta=route_meta,
                agent_id=uuid.uuid4(),
                user_id=None,
                tenant_id=uuid.uuid4(),
            )

    assert monitor.await_args.kwargs["stage"] == "credits_outbox"
    assert monitor.await_args.kwargs["reservation_id"] == reservation_id
    assert monitor.await_args.kwargs["settlement_credits"] == 5


@pytest.mark.asyncio
async def test_call_llm_auto_resolves_missing_route_metadata_before_provider_use():
    agent_id = uuid.uuid4()
    legacy_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="standard",
    )
    routed_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(5, None))),
        patch.object(
            llm_caller,
            "ensure_agent_billing_route",
            AsyncMock(return_value=(routed_model, route_meta)),
        ) as ensure_route,
        patch.object(
            llm_caller,
            "_prepare_llm_billing_context",
            AsyncMock(side_effect=QuotaExceeded("Credits unavailable", quota_type="insufficient_credits")),
        ) as billing_preflight,
    ):
        result = await llm_caller.call_llm(
            model=legacy_model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
        )

    assert result == "⚠️ Credits unavailable"
    ensure_route.assert_awaited_once_with(agent_id, legacy_model, None)
    billing_preflight.assert_awaited_once_with(agent_id, routed_model, route_meta)


@pytest.mark.asyncio
async def test_persisted_agent_model_without_route_fails_before_provider_resolution():
    agent_id = uuid.uuid4()
    persisted_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="standard",
    )
    resolve_key = AsyncMock()

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(5, None))),
        patch.object(
            llm_caller,
            "ensure_agent_billing_route",
            AsyncMock(return_value=(persisted_model, None)),
        ),
        patch.object(llm_caller, "resolve_model_key", resolve_key),
    ):
        result = await llm_caller.call_llm(
            model=persisted_model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
        )

    assert result == "⚠️ Agent billing route is unavailable."
    resolve_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_agent_cannot_recover_to_an_unbilled_persisted_model():
    persisted_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="standard",
    )
    db = FakeSessionContext(None)

    with patch.object(llm_caller, "async_session", return_value=db):
        with pytest.raises(QuotaExceeded, match="billing context is unavailable"):
            await llm_caller.ensure_agent_billing_route(
                uuid.uuid4(),
                persisted_model,
                None,
            )


@pytest.mark.asyncio
async def test_call_llm_settles_each_provider_round_even_at_tool_round_limit():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    response = SimpleNamespace(
        content="",
        tool_calls=[],
        reasoning_content=None,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )
    client = FakeLLMClient(response)
    reservation_id = uuid.uuid4()

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(1, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, credential_id))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=[])),
        patch.object(
            llm_caller,
            "get_provider_spec",
            return_value=SimpleNamespace(accepts_plain_text_final=False, requires_api_key=True),
        ),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(llm_caller, "record_token_usage", AsyncMock()) as record_tokens,
        patch.object(llm_caller, "record_credential_call", AsyncMock()) as record_pool_usage,
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(return_value=reservation_id),
        ) as reserve_round,
        patch.object(llm_caller, "settle_llm_round_credits", AsyncMock()) as settle_round,
        patch.object(llm_caller, "_record_llm_usage_and_charge", AsyncMock()) as settle_quota,
    ):
        result = await llm_caller.call_llm(
            model=model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            route_meta=route_meta,
        )

    assert result == "[Error] Too many tool call rounds"
    record_tokens.assert_awaited_once()
    record_pool_usage.assert_awaited_once_with(credential_id, tokens_used=120)
    reserve_round.assert_awaited_once()
    settle_round.assert_awaited_once()
    assert settle_round.await_args.args == (reservation_id,)
    assert settle_round.await_args.kwargs["usage"].total_tokens == 120
    settle_quota.assert_awaited_once()
    assert settle_quota.await_args.kwargs["charge_credits_enabled"] is False
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_credit_reservation_failure_never_calls_or_degrades_provider():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    client = SimpleNamespace(stream=AsyncMock(), close=AsyncMock())

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(1, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, credential_id))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=[])),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(llm_caller, "_record_llm_settlement_failure", AsyncMock()) as monitor,
        patch.object(llm_caller, "_apply_credential_failure_policy", AsyncMock()) as degrade,
        patch.object(llm_caller, "_record_llm_product_issue", AsyncMock()) as provider_issue,
    ):
        result = await llm_caller.call_llm(
            model=model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            route_meta=route_meta,
        )

    assert result == "⚠️ Credits 预留暂时不可用，模型尚未调用，请稍后重试。"
    client.stream.assert_not_awaited()
    client.close.assert_awaited_once()
    degrade.assert_not_awaited()
    provider_issue.assert_not_awaited()
    assert monitor.await_args.kwargs["stage"] == "credits_reserve"


@pytest.mark.asyncio
async def test_completed_response_survives_credits_settlement_failure_and_is_monitored():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    user_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    response = SimpleNamespace(
        content="completed answer",
        tool_calls=[],
        reasoning_content=None,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )
    client = FakeLLMClient(response)
    reservation_id = uuid.uuid4()

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(1, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, credential_id))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=[])),
        patch.object(
            llm_caller,
            "get_provider_spec",
            return_value=SimpleNamespace(accepts_plain_text_final=True, requires_api_key=True),
        ),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(llm_caller, "record_token_usage", AsyncMock()),
        patch.object(llm_caller, "record_credential_call", AsyncMock()),
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(return_value=reservation_id),
        ),
        patch.object(llm_caller, "get_credit_cost", AsyncMock(return_value=1)),
        patch.object(
            llm_caller,
            "mark_credit_reservation_settlement_ready",
            AsyncMock(),
        ) as mark_ready,
        patch.object(
            llm_caller,
            "finalize_reserved_credits",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(
            llm_caller,
            "_record_llm_usage_and_charge",
            AsyncMock(),
        ),
        patch.object(llm_caller, "_record_llm_settlement_failure", AsyncMock()) as monitor,
    ):
        result = await llm_caller.call_llm(
            model=model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
            user_id=user_id,
            route_meta=route_meta,
        )

    assert result == "completed answer"
    client.close.assert_awaited_once()
    mark_ready.assert_awaited_once_with(reservation_id, amount=1)
    monitor.assert_awaited_once()
    assert monitor.await_args.kwargs["stage"] == "credits_finalize"


@pytest.mark.asyncio
async def test_completed_provider_response_settlement_error_never_releases_round_hold():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    response = SimpleNamespace(
        content="completed answer",
        tool_calls=[],
        reasoning_content=None,
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )
    client = FakeLLMClient(response)

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(1, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, None))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=[])),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(llm_caller, "record_token_usage", AsyncMock()),
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(return_value=reservation_id),
        ),
        patch.object(
            llm_caller,
            "settle_llm_round_credits",
            AsyncMock(side_effect=RuntimeError("outbox unavailable")),
        ),
        patch.object(llm_caller, "release_llm_round_credits", AsyncMock()) as release_round,
        patch.object(llm_caller, "_record_llm_usage_and_charge", AsyncMock()),
    ):
        result = await llm_caller.call_llm(
            model=model,
            messages=[{"role": "user", "content": "run"}],
            agent_name="Autonomous agent",
            role_description="worker",
            agent_id=agent_id,
            user_id=user_id,
            route_meta=route_meta,
        )

    assert result == "⚠️ Credits 结算暂时不可用，本轮结果未执行，请稍后重试。"
    release_round.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_financial_charge_precedes_secondary_agent_usage_counter():
    events: list[str] = []
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3", tier="basic")
    usage = TokenUsage(total_tokens=100, input_tokens=80, output_tokens=20)

    async def charge(**_kwargs):
        events.append("credits")

    async def consume(*_args, **_kwargs):
        events.append("agent_quota")

    with (
        patch.object(llm_caller, "charge_credits", charge),
        patch.object(llm_caller, "consume_agent_llm_quota", consume),
    ):
        await llm_caller._record_llm_usage_and_charge(
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            model=model,
            usage=usage,
            route_meta=route_meta,
        )

    assert events == ["credits", "agent_quota"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_may_have_accepted", "provider_failed"),
    [(True, False), (False, True)],
)
async def test_call_llm_settles_completed_rounds_when_cancelled(
    provider_may_have_accepted,
    provider_failed,
):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    response = SimpleNamespace(
        content="",
        tool_calls=[],
        reasoning_content=None,
        usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    )
    client = FakeLLMClient(
        response,
        asyncio.CancelledError(),
        failure_may_have_been_accepted=provider_may_have_accepted,
    )
    first_reservation_id = uuid.uuid4()
    second_reservation_id = uuid.uuid4()
    failover_guard = llm_caller.FailoverGuard()

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(2, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, None))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=[])),
        patch.object(
            llm_caller,
            "get_provider_spec",
            return_value=SimpleNamespace(accepts_plain_text_final=False, requires_api_key=True),
        ),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(llm_caller, "record_token_usage", AsyncMock()),
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(side_effect=[first_reservation_id, second_reservation_id]),
        ),
        patch.object(llm_caller, "settle_llm_round_credits", AsyncMock()) as settle_round,
        patch.object(llm_caller, "release_llm_round_credits", AsyncMock()) as release_round,
        patch.object(llm_caller, "_record_llm_usage_and_charge", AsyncMock()) as settle_quota,
    ):
        with pytest.raises(asyncio.CancelledError):
            await llm_caller.call_llm(
                model=model,
                messages=[{"role": "user", "content": "run"}],
                agent_name="Autonomous agent",
                role_description="worker",
                agent_id=agent_id,
                user_id=uuid.uuid4(),
                route_meta=route_meta,
                failover_guard=failover_guard,
            )

    settle_round.assert_awaited_once()
    assert settle_round.await_args.args == (first_reservation_id,)
    release_round.assert_awaited_once()
    assert release_round.await_args.args == (second_reservation_id,)
    assert release_round.await_args.kwargs["provider_failed"] is provider_failed
    assert failover_guard.provider_work_started is True
    assert (
        failover_guard.provider_outcome_ambiguous
        is provider_may_have_accepted
    )
    assert failover_guard.can_failover() is False
    settle_quota.assert_awaited_once()
    assert settle_quota.await_args.kwargs["charge_credits_enabled"] is False
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_llm_settles_completed_round_when_tool_execution_is_cancelled():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    response = SimpleNamespace(
        content="",
        tool_calls=[{
            "id": "tool-1",
            "type": "function",
            "function": {"name": "test_tool", "arguments": "{}"},
        }],
        reasoning_content=None,
        usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    )
    client = FakeLLMClient(response)
    reservation_id = uuid.uuid4()
    tools = [{
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": "Test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    with (
        patch.object(llm_caller, "_get_agent_config", AsyncMock(return_value=(2, None))),
        patch.object(llm_caller, "_get_user_name", AsyncMock(return_value=None)),
        patch.object(llm_caller, "_prepare_llm_billing_context", AsyncMock(return_value=tenant_id)),
        patch.object(llm_caller, "resolve_model_key", AsyncMock(return_value=("key", None, None))),
        patch.object(llm_caller, "create_llm_client", return_value=client),
        patch.object(llm_caller, "get_agent_tools_for_llm", AsyncMock(return_value=tools)),
        patch.object(
            llm_caller,
            "get_provider_spec",
            return_value=SimpleNamespace(accepts_plain_text_final=False, requires_api_key=True),
        ),
        patch("app.services.agent_context.build_agent_context", AsyncMock(return_value=("system", "dynamic"))),
        patch.object(llm_caller, "record_token_usage", AsyncMock()),
        patch.object(llm_caller, "execute_tool", AsyncMock(side_effect=asyncio.CancelledError())),
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(return_value=reservation_id),
        ),
        patch.object(llm_caller, "settle_llm_round_credits", AsyncMock()) as settle_round,
        patch.object(llm_caller, "_record_llm_usage_and_charge", AsyncMock()) as settle_quota,
    ):
        with pytest.raises(asyncio.CancelledError):
            await llm_caller.call_llm(
                model=model,
                messages=[{"role": "user", "content": "run"}],
                agent_name="Autonomous agent",
                role_description="worker",
                agent_id=agent_id,
                user_id=uuid.uuid4(),
                route_meta=route_meta,
            )

    settle_round.assert_awaited_once()
    assert settle_round.await_args.args == (reservation_id,)
    settle_quota.assert_awaited_once()
    assert settle_quota.await_args.kwargs["charge_credits_enabled"] is False
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_oneshot_finally_settles_usage_when_tool_execution_is_cancelled():
    """The entrypoint only registers work; provider billing lives in Runtime."""
    from app.services import heartbeat

    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Autonomous agent",
        creator_id=uuid.uuid4(),
    )
    run_id = uuid.uuid4()
    session = FakeSessionContext(agent)
    enqueue = AsyncMock(return_value=SimpleNamespace(run_id=run_id))
    with (
        patch("app.database.async_session", return_value=session),
        patch.object(heartbeat, "enqueue_oneshot_runtime", enqueue),
    ):
        result = await heartbeat.run_agent_oneshot(agent_id, "run", max_rounds=2)

    assert result == str(run_id)
    session.commit.assert_awaited_once()
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_finally_settles_usage_when_tool_execution_is_cancelled():
    """Runtime settles the provider call before a later tool step can cancel."""
    from app.services.llm import single_step
    from app.services.llm.client import LLMResponse, LLMMessage

    agent_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        base_url=None,
        temperature=0.2,
        max_output_tokens=256,
        request_timeout=120,
    )
    invocation = llm_caller.AgentLLMInvocation(
        model=model,
        fallback_model=None,
        route_meta=llm_caller.RouteMeta(
            saas_tier="lite",
            modality="text",
            action="heartbeat",
        ),
        tenant_id=tenant_id,
        api_key="pool-key",
        base_url=None,
        credential_id=None,
    )
    response = LLMResponse(
        content="",
        tool_calls=[{
            "id": "tool-1",
            "type": "function",
            "function": {"name": "test_tool", "arguments": "{}"},
        }],
        reasoning_content=None,
        usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    )
    client = FakeCompleteLLMClient(response)
    with (
        patch.object(single_step, "create_llm_client", return_value=client),
        patch.object(single_step, "get_max_tokens", return_value=256),
        patch.object(
            single_step,
            "reserve_llm_round_credits",
            AsyncMock(return_value=uuid.uuid4()),
        ) as reserve_round,
        patch.object(
            single_step,
            "settle_llm_round_credits",
            AsyncMock(),
        ) as settle_round,
        patch.object(single_step, "record_token_usage", AsyncMock()) as record_tokens,
        patch.object(
            single_step,
            "settle_agent_llm_invocation",
            AsyncMock(),
        ) as settle_credits,
    ):
        run_id = uuid.uuid4()
        completion = await single_step.complete_llm_once(
            model,
            [LLMMessage(role="user", content="run")],
            agent_id=agent_id,
            user_id=creator_id,
            invocation=invocation,
            billing_ref_id=run_id,
        )

    assert completion.usage.total_tokens == 100
    record_tokens.assert_awaited_once()
    assert record_tokens.await_args.args[1].total_tokens == 100
    settle_credits.assert_awaited_once()
    assert settle_credits.await_args.kwargs["agent_id"] == agent_id
    assert settle_credits.await_args.kwargs["user_id"] == creator_id
    assert settle_credits.await_args.kwargs["usage"].total_tokens == 100
    settle_round.assert_awaited_once()
    assert reserve_round.await_args.kwargs["ref_id"] == run_id
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_llm_blocks_fallback_when_primary_outcome_is_ambiguous():
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Autonomous agent",
        creator_id=uuid.uuid4(),
    )
    primary_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
        request_timeout=120,
    )
    fallback_model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        modality="text",
        tier="basic",
        temperature=0.2,
        max_output_tokens=256,
        request_timeout=120,
    )
    route_meta = llm_caller.RouteMeta(saas_tier="lite", modality="text")
    client = FakeAmbiguousCompleteFailureClient(llm_caller.LLMError("timeout"))
    db = FakeSessionContext(agent)

    with (
        patch.object(
            llm_caller,
            "resolve_agent_model",
            AsyncMock(return_value=(primary_model, fallback_model, route_meta)),
        ),
        patch.object(
            llm_caller,
            "get_agent_tools_for_llm",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            llm_caller,
            "_prepare_llm_billing_context",
            AsyncMock(return_value=tenant_id),
        ),
        patch.object(
            llm_caller,
            "resolve_model_key",
            AsyncMock(return_value=("key", None, credential_id)),
        ),
        patch.object(llm_caller, "create_llm_client", return_value=client) as create_client,
        patch.object(
            llm_caller,
            "reserve_llm_round_credits",
            AsyncMock(return_value=uuid.uuid4()),
        ),
        patch.object(
            llm_caller,
            "release_llm_round_credits",
            AsyncMock(),
        ) as release_round,
        patch.object(llm_caller, "_record_llm_product_issue", AsyncMock()),
        patch.object(llm_caller, "_apply_credential_failure_policy", AsyncMock()),
    ):
        result = await llm_caller.call_agent_llm_with_tools(
            db,
            agent_id,
            "system",
            "run",
            max_rounds=1,
        )

    assert result == "[LLM Error] timeout"
    create_client.assert_called_once()
    assert release_round.await_args.kwargs["provider_failed"] is False
    client.close.assert_awaited_once()


def test_autonomous_entrypoints_keep_routing_and_settlement_hooks():
    from app.services import heartbeat
    from app.services.agent_runtime import model_step_service
    from app.services.llm import single_step

    oneshot_source = inspect.getsource(heartbeat.run_agent_oneshot)
    model_step_source = inspect.getsource(model_step_service.RuntimeModelStepService._call_prepared)
    provider_source = inspect.getsource(single_step.complete_llm_once)

    assert not hasattr(heartbeat, "_execute_heartbeat")
    assert "enqueue_oneshot_runtime" in oneshot_source
    assert "prepare_agent_llm_invocation" not in oneshot_source
    assert "complete_llm_once" in model_step_source
    assert "prepare_pinned_agent_llm_invocation" in model_step_source
    assert "reserve_llm_round_credits" in provider_source
    assert "settle_llm_round_credits" in provider_source
    assert "settle_agent_llm_invocation" in provider_source
    assert "record_token_usage" in provider_source
    assert "llm_provider_may_have_accepted" in provider_source
