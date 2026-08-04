"""Immutable Runtime model-route snapshot tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.models.agent import Agent
from app.services.agent_runtime.model_route import (
    RuntimeModelRouteError,
    resolve_runtime_model_route,
)
from app.services.llm.caller import RouteMeta
from app.services.quota_guard import QuotaExceeded


def _agent() -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Route Agent",
        role_description="Test route snapshots",
        status="idle",
        is_expired=False,
    )


@pytest.mark.asyncio
async def test_legacy_route_pins_primary_and_distinct_fallback() -> None:
    agent = _agent()
    agent.tenant_id = None
    agent.primary_model_id = uuid.uuid4()
    agent.fallback_model_id = uuid.uuid4()

    route = await resolve_runtime_model_route(agent)

    assert route.model_id == agent.primary_model_id
    assert route.fallback_model_id == agent.fallback_model_id
    assert route.saas_tier == ""
    assert route.modality == "text"


@pytest.mark.asyncio
async def test_tenant_legacy_row_without_stored_tier_uses_entitlement_route() -> None:
    agent = _agent()
    agent.preferred_tier = None
    agent.primary_model_id = None
    agent.fallback_model_id = None
    primary = SimpleNamespace(id=uuid.uuid4())
    resolver = AsyncMock(
        return_value=(
            primary,
            None,
            RouteMeta(saas_tier="lite", modality="text"),
        )
    )

    with patch(
        "app.services.llm.caller.resolve_agent_model",
        new=resolver,
    ):
        route = await resolve_runtime_model_route(agent)

    resolver.assert_awaited_once_with(agent)
    assert route.model_id == primary.id
    assert route.fallback_model_id is None
    assert route.saas_tier == "lite"
    assert route.modality == "text"


@pytest.mark.asyncio
async def test_tenant_route_entitlement_failure_is_a_runtime_route_error() -> None:
    agent = _agent()
    agent.preferred_tier = None

    with (
        patch(
            "app.services.llm.caller.resolve_agent_model",
            new=AsyncMock(
                side_effect=QuotaExceeded(
                    "No model route configured for lite/text.",
                    quota_type="no_route",
                )
            ),
        ),
        pytest.raises(RuntimeModelRouteError, match="no available model route"),
    ):
        await resolve_runtime_model_route(agent)


@pytest.mark.asyncio
async def test_saas_route_pins_resolved_primary_fallback_and_metadata() -> None:
    agent = _agent()
    agent.preferred_tier = "ultra"
    agent.preferred_modality = "video"
    primary = SimpleNamespace(id=uuid.uuid4())
    fallback = SimpleNamespace(id=uuid.uuid4())

    with patch(
        "app.services.llm.caller.resolve_agent_model",
        new=AsyncMock(
            return_value=(
                primary,
                fallback,
                RouteMeta(saas_tier="ultra", modality="video"),
            )
        ),
    ):
        route = await resolve_runtime_model_route(agent)

    assert route.model_id == primary.id
    assert route.fallback_model_id == fallback.id
    assert route.saas_tier == "ultra"
    assert route.modality == "video"


@pytest.mark.asyncio
async def test_fallback_only_route_is_promoted_without_self_failover() -> None:
    agent = _agent()
    agent.preferred_tier = "lite"
    fallback = SimpleNamespace(id=uuid.uuid4())

    with patch(
        "app.services.llm.caller.resolve_agent_model",
        new=AsyncMock(
            return_value=(
                None,
                fallback,
                RouteMeta(saas_tier="lite", modality="text"),
            )
        ),
    ):
        route = await resolve_runtime_model_route(agent)

    assert route.model_id == fallback.id
    assert route.fallback_model_id is None
