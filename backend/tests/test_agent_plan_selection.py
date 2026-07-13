import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.api import onboarding as onboarding_api
from app.schemas.schemas import AgentUpdate
from app.services.agent_plan_selection import (
    InvalidAgentPlanSelection,
    resolve_agent_plan_selection,
)
from app.services.entitlements import Entitlements
from app.services.llm import caller as llm_caller


def _ent(*, tiers=None, modalities=None) -> Entitlements:
    return Entitlements(
        plan_id=uuid.uuid4(),
        plan_code="free",
        max_agents=1,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=1000,
        allowed_modalities=modalities if modalities is not None else ["text"],
        allowed_tiers=tiers if tiers is not None else ["lite"],
    )


def test_legacy_agent_tier_is_clamped_to_active_plan_default():
    assert resolve_agent_plan_selection(_ent(), "pro", "text", strict=False) == ("lite", "text")


def test_unknown_tier_is_rejected_even_without_subscription_restrictions():
    with pytest.raises(InvalidAgentPlanSelection) as exc:
        resolve_agent_plan_selection(None, "enterprise-secret", "text")

    assert exc.value.quota_type == "model_tier"


@pytest.mark.asyncio
async def test_onboarding_personal_assistant_uses_active_plan_default_tier():
    with patch.object(
        onboarding_api,
        "get_tenant_entitlements",
        AsyncMock(return_value=_ent()),
    ):
        assert await onboarding_api._tenant_plan_selection(uuid.uuid4()) == ("lite", "text")


@pytest.mark.asyncio
async def test_agent_update_rejects_disallowed_tier_for_agents_tenant():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        tenant_id=tenant_id,
        agent_type="native",
        preferred_tier="lite",
        preferred_modality="text",
    )
    current_user = SimpleNamespace(id=user_id, role="org_admin", tenant_id=tenant_id)

    with (
        patch.object(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage"))),
        patch.object(agents_api, "get_tenant_entitlements", AsyncMock(return_value=_ent())) as get_ent,
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.update_agent(
                agent.id,
                AgentUpdate(preferred_tier="pro"),
                current_user=current_user,
                db=MagicMock(),
            )

    assert exc.value.status_code == 403
    assert "not included" in str(exc.value.detail)
    get_ent.assert_awaited_once_with(tenant_id)


@pytest.mark.asyncio
async def test_runtime_clamps_stored_tier_but_keeps_shared_route_resolution():
    tenant_id = uuid.uuid4()
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="shared-lite")
    route = SimpleNamespace(
        model=model,
        fallback_model=None,
        saas_tier="lite",
        modality="text",
    )
    agent = SimpleNamespace(
        tenant_id=tenant_id,
        preferred_tier="pro",
        preferred_modality="text",
        primary_model_id=uuid.uuid4(),
        fallback_model_id=None,
    )

    with (
        patch("app.services.entitlements.get_tenant_entitlements", AsyncMock(return_value=_ent())),
        patch.object(llm_caller, "resolve_route", AsyncMock(return_value=route)) as resolve_route,
    ):
        primary, fallback, meta = await llm_caller.resolve_agent_model(agent)

    assert primary is model
    assert fallback is None
    assert meta and meta.saas_tier == "lite"
    resolve_route.assert_awaited_once_with(tenant_id, "lite", "text", allow_fallback=True)
