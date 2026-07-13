"""Unit tests for the plan inference capability gate.

These tests verify the branching logic (not the DB). The DB session and
entitlements lookup are mocked so the five decision branches are exercised
directly. A regression in the branching (e.g. denying when allowed, or vice
versa) makes these tests fail.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import quota_guard
from app.services.entitlements import Entitlements
from app.services.quota_guard import (
    QuotaExceeded,
    check_plan_generation_entitlement,
    check_plan_inference_entitlement,
)


def _ent(modalities=None, tiers=None, generation_modalities=None, generation_tiers=None):
    return Entitlements(
        plan_id=uuid.uuid4(),
        plan_code="free",
        max_agents=2,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=0,
        allowed_modalities=modalities if modalities is not None else [],
        allowed_tiers=tiers if tiers is not None else [],
        generation_modalities=generation_modalities if generation_modalities is not None else [],
        generation_tiers=generation_tiers if generation_tiers is not None else [],
    )


@pytest.mark.asyncio
async def test_free_plan_can_generate_media_on_lite_without_widening_chat_modalities():
    ent = _ent(
        modalities=["text"],
        tiers=["lite"],
        generation_modalities=["image", "audio", "music", "video"],
        generation_tiers=["lite"],
    )
    session_patch, ent_patch = _patch_env(tenant_id="t1", ent=ent)
    with session_patch, ent_patch:
        await check_plan_generation_entitlement(
            uuid.uuid4(),
            modality="video",
            saas_tier="lite",
        )
        with pytest.raises(QuotaExceeded) as chat_exc:
            await check_plan_inference_entitlement(
                uuid.uuid4(),
                modality="video",
                saas_tier="lite",
            )
        assert chat_exc.value.quota_type == "model_modality"


@pytest.mark.asyncio
async def test_free_plan_generation_rejects_ultra_tier():
    ent = _ent(
        modalities=["text"],
        tiers=["lite"],
        generation_modalities=["image", "audio", "music", "video"],
        generation_tiers=["lite"],
    )
    session_patch, ent_patch = _patch_env(tenant_id="t1", ent=ent)
    with session_patch, ent_patch:
        with pytest.raises(QuotaExceeded) as exc:
            await check_plan_generation_entitlement(
                uuid.uuid4(),
                modality="image",
                saas_tier="ultra",
            )
        assert exc.value.quota_type == "generation_tier"


def _patch_env(tenant_id="t1", ent=None, status="active"):
    """Patch async_session (returns (tenant_id, status) row) and get_tenant_entitlements (→ent)."""
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_result.one_or_none.return_value = (tenant_id, status)
    fake_db.execute = AsyncMock(return_value=fake_result)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    session_patch = patch.object(quota_guard, "async_session", return_value=fake_session)
    ent_patch = patch.object(
        quota_guard, "get_tenant_entitlements", AsyncMock(return_value=ent)
    )
    return session_patch, ent_patch


@pytest.mark.asyncio
async def test_no_agent_id_allows():
    """No agent context → cannot determine tenant → allow (skip check)."""
    with patch.object(quota_guard, "async_session") as sess:
        await check_plan_inference_entitlement(None, modality="vision", saas_tier="ultra")
        sess.assert_not_called()  # must short-circuit before any DB hit


@pytest.mark.asyncio
async def test_no_tenant_allows():
    """Agent exists but has no tenant → allow, and skip entitlements lookup."""
    session_patch, ent_patch = _patch_env(tenant_id=None, ent=None)
    with session_patch, ent_patch as ent_mock:
        await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="pro")
        ent_mock.assert_not_called()


@pytest.mark.asyncio
async def test_no_subscription_allows():
    """Tenant has no active subscription → backward-compat fallback, no restriction."""
    session_patch, ent_patch = _patch_env(tenant_id="t1", ent=None)
    with session_patch, ent_patch:
        # vision + premium would be denied IF a plan restricted it; no plan → allow
        await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="ultra")


@pytest.mark.asyncio
async def test_allowed_modality_and_tier_allows():
    """Model within plan's allowed sets → allow."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text", "vision"], tiers=["pro"])
    )
    with session_patch, ent_patch:
        await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="pro")


@pytest.mark.asyncio
async def test_image_plan_allows_legacy_vision_model():
    """New image modality plans remain compatible with existing vision-tagged models."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text", "image"], tiers=["pro"])
    )
    with session_patch, ent_patch:
        await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="pro")


@pytest.mark.asyncio
async def test_legacy_vision_plan_allows_image_model():
    """Existing vision modality plans remain compatible with new image-tagged models."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text", "vision"], tiers=["pro"])
    )
    with session_patch, ent_patch:
        await check_plan_inference_entitlement(uuid.uuid4(), modality="image", saas_tier="pro")


@pytest.mark.asyncio
async def test_empty_allowed_sets_mean_no_restriction():
    """Plan with empty allowed_modalities/tiers → no restriction (allow anything)."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=[], tiers=[])
    )
    with session_patch, ent_patch:
        await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="ultra")


@pytest.mark.asyncio
async def test_disallowed_modality_denies():
    """Free plan text-only + vision model → deny with model_modality."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text"], tiers=["pro", "ultra"])
    )
    with session_patch, ent_patch:
        with pytest.raises(QuotaExceeded) as exc:
            await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="pro")
        assert exc.value.quota_type == "model_modality"


@pytest.mark.asyncio
async def test_disallowed_tier_denies():
    """Free plan standard-only + premium model → deny with model_tier."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text", "vision"], tiers=["pro"])
    )
    with session_patch, ent_patch:
        with pytest.raises(QuotaExceeded) as exc:
            await check_plan_inference_entitlement(uuid.uuid4(), modality="text", saas_tier="ultra")
        assert exc.value.quota_type == "model_tier"


@pytest.mark.asyncio
async def test_modality_checked_before_tier():
    """If both modality and tier are disallowed, modality is reported first."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text"], tiers=["pro"])
    )
    with session_patch, ent_patch:
        with pytest.raises(QuotaExceeded) as exc:
            await check_plan_inference_entitlement(uuid.uuid4(), modality="vision", saas_tier="ultra")
        assert exc.value.quota_type == "model_modality"


@pytest.mark.asyncio
async def test_stopped_agent_denied():
    """Stopped agent (subscription downgrade/expiry) → denied with agent_stopped, before model check."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text", "vision"], tiers=["pro", "ultra"]), status="stopped"
    )
    with session_patch, ent_patch:
        with pytest.raises(QuotaExceeded) as exc:
            # even an allowed model is rejected because the agent is stopped
            await check_plan_inference_entitlement(uuid.uuid4(), modality="text", saas_tier="pro")
        assert exc.value.quota_type == "agent_stopped"


@pytest.mark.asyncio
async def test_saas_tier_is_checked_without_concrete_model_object():
    """A routed call is authorized by SaaS capability, not an LLMModel row/tier."""
    session_patch, ent_patch = _patch_env(
        tenant_id="t1", ent=_ent(modalities=["text"], tiers=["lite"])
    )
    with session_patch, ent_patch:
        await check_plan_inference_entitlement(
            uuid.uuid4(),
            modality="text",
            saas_tier="lite",
        )
