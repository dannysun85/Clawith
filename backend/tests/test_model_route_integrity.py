from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import enterprise as enterprise_api
from app.api import saas as saas_api
from app.schemas.schemas import LLMModelUpdate
from app.schemas.saas import ModelRouteCreateIn, ModelRouteUpdateIn
from app.services import model_router
from app.services.modalities import model_supports_modality
from app.services.quota_guard import QuotaExceeded


def _session_with_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    return session, db


@pytest.mark.asyncio
async def test_route_selection_has_stable_tie_breakers():
    route = SimpleNamespace(id=uuid.uuid4())
    session, db = _session_with_result(route)

    with patch.object(model_router, "async_session", return_value=session):
        assert await model_router._pick_route("ultra", "text") is route

    query = str(db.execute.await_args.args[0])
    assert "model_routes.priority DESC" in query
    assert "model_routes.created_at ASC" in query
    assert "model_routes.id ASC" in query
    assert "JOIN llm_models" in query
    assert "llm_models.tenant_id IS NULL" in query
    assert "llm_models.enabled = true" in query.lower()
    assert "jsonb_exists" in query
    assert "jsonb_array_length" in query


@pytest.mark.asyncio
async def test_fallback_loader_requires_enabled_route():
    session, db = _session_with_result(None)

    with patch.object(model_router, "async_session", return_value=session):
        await model_router._pick_route_by_id(uuid.uuid4())

    query = str(db.execute.await_args.args[0])
    assert "model_routes.enabled = true" in query.lower()
    assert "JOIN llm_models" in query
    assert "llm_models.tenant_id IS NULL" in query
    assert "jsonb_exists" in query
    assert "jsonb_array_length" in query


@pytest.mark.asyncio
async def test_model_loader_rejects_tenant_owned_rows_at_query_boundary():
    session, db = _session_with_result(None)

    with patch.object(model_router, "async_session", return_value=session):
        await model_router._load_model(uuid.uuid4(), enabled_only=True)

    query = str(db.execute.await_args.args[0])
    assert "llm_models.tenant_id IS NULL" in query
    assert "llm_models.enabled = true" in query.lower()


@pytest.mark.asyncio
async def test_disabled_primary_model_fails_closed_before_provider_use(monkeypatch):
    route = SimpleNamespace(llm_model_id=uuid.uuid4(), fallback_route_id=None)
    monkeypatch.setattr(model_router, "_check_tier_entitlement", AsyncMock())
    monkeypatch.setattr(model_router, "_pick_route", AsyncMock(return_value=route))
    load_model = AsyncMock(return_value=None)
    monkeypatch.setattr(model_router, "_load_model", load_model)

    with pytest.raises(QuotaExceeded, match="missing or disabled"):
        await model_router.resolve_route(uuid.uuid4(), "ultra", "text")

    load_model.assert_awaited_once_with(route.llm_model_id, enabled_only=True)


@pytest.mark.asyncio
async def test_text_route_keeps_minimax_m3_primary_and_agent_plan_fallback(monkeypatch):
    primary_route = SimpleNamespace(
        llm_model_id=uuid.uuid4(),
        fallback_route_id=uuid.uuid4(),
    )
    fallback_route = SimpleNamespace(
        saas_tier="pro",
        modality="text",
        llm_model_id=uuid.uuid4(),
    )
    primary_model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    fallback_model = SimpleNamespace(
        provider="volcengine_agent_plan",
        model="doubao-seed-2.1-turbo",
    )
    monkeypatch.setattr(model_router, "_check_tier_entitlement", AsyncMock())
    monkeypatch.setattr(model_router, "_pick_route", AsyncMock(return_value=primary_route))
    monkeypatch.setattr(
        model_router,
        "_pick_route_by_id",
        AsyncMock(return_value=fallback_route),
    )
    load_model = AsyncMock(side_effect=[primary_model, fallback_model])
    monkeypatch.setattr(model_router, "_load_model", load_model)

    resolved = await model_router.resolve_route(uuid.uuid4(), "pro", "text")

    assert resolved.model is primary_model
    assert resolved.fallback_model is fallback_model
    assert resolved.provider == "minimax"
    assert resolved.model_name == "MiniMax-M3"
    assert load_model.await_args_list == [
        call(primary_route.llm_model_id, enabled_only=True),
        call(fallback_route.llm_model_id, enabled_only=True),
    ]


@pytest.mark.asyncio
async def test_cross_modality_fallback_is_not_used(monkeypatch):
    primary = SimpleNamespace(
        llm_model_id=uuid.uuid4(),
        fallback_route_id=uuid.uuid4(),
    )
    fallback_route = SimpleNamespace(
        saas_tier="ultra",
        modality="image",
        llm_model_id=uuid.uuid4(),
    )
    model = SimpleNamespace(provider="minimax", model="MiniMax-M3")
    monkeypatch.setattr(model_router, "_check_tier_entitlement", AsyncMock())
    monkeypatch.setattr(model_router, "_pick_route", AsyncMock(return_value=primary))
    monkeypatch.setattr(model_router, "_pick_route_by_id", AsyncMock(return_value=fallback_route))
    load_model = AsyncMock(return_value=model)
    monkeypatch.setattr(model_router, "_load_model", load_model)

    resolved = await model_router.resolve_route(uuid.uuid4(), "ultra", "text")

    assert resolved.fallback_model is None
    load_model.assert_awaited_once_with(primary.llm_model_id, enabled_only=True)


class _FallbackDb:
    def __init__(self, objects):
        self.objects = objects

    async def get(self, _model, object_id):
        return self.objects.get(object_id)


@pytest.mark.asyncio
async def test_admin_rejects_disabled_and_self_fallbacks():
    route_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    fallback = SimpleNamespace(
        id=fallback_id,
        enabled=False,
        saas_tier="ultra",
        modality="text",
        llm_model_id=uuid.uuid4(),
        fallback_route_id=None,
    )
    db = _FallbackDb({fallback_id: fallback})

    with pytest.raises(HTTPException, match="must be enabled"):
        await saas_api._validate_fallback_route(
            db,
            fallback_route_id=fallback_id,
            route_id=route_id,
            saas_tier="ultra",
            modality="text",
        )
    with pytest.raises(HTTPException, match="cannot fall back to itself"):
        await saas_api._validate_fallback_route(
            db,
            fallback_route_id=route_id,
            route_id=route_id,
            saas_tier="ultra",
            modality="text",
        )


def test_saas_routes_reject_tenant_owned_model_credentials():
    model = SimpleNamespace(tenant_id=uuid.uuid4())

    with pytest.raises(HTTPException, match="platform-owned"):
        saas_api._validate_platform_route_model(model)


def test_enterprise_model_editor_cannot_break_live_route():
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        base_url=None,
        enabled=True,
        modality="text",
        modalities=["text", "image", "video"],
        supports_vision=True,
    )
    route = SimpleNamespace(saas_tier="ultra", modality="video")

    with pytest.raises(HTTPException, match="Disable every active") as exc:
        enterprise_api._validate_routed_model_update(
            model,
            LLMModelUpdate(enabled=False),
            [route],
        )
    assert exc.value.status_code == 409

    legacy_image_model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        base_url=None,
        enabled=True,
        modality="text",
        modalities=None,
        supports_vision=True,
    )
    with pytest.raises(HTTPException, match="would break active") as exc:
        enterprise_api._validate_routed_model_update(
            legacy_image_model,
            LLMModelUpdate(supports_vision=False),
            [SimpleNamespace(saas_tier="lite", modality="image")],
        )
    assert exc.value.status_code == 409


def test_enterprise_model_editor_cannot_change_live_route_connection_identity():
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        base_url="https://api.minimax.io/v1",
        enabled=True,
        modality="text",
        modalities=["text", "image", "video"],
        supports_vision=True,
    )
    route = SimpleNamespace(saas_tier="ultra", modality="text")

    for update in (
        LLMModelUpdate(provider="openai"),
        LLMModelUpdate(model="MiniMax-M2.5"),
        LLMModelUpdate(base_url="https://example.invalid/v1"),
    ):
        with pytest.raises(HTTPException, match="connection identity") as exc:
            enterprise_api._validate_routed_model_update(model, update, [route])
        assert exc.value.status_code == 409


def test_nonempty_modalities_are_authoritative_with_legacy_alias_support():
    assert not model_supports_modality(
        "text",
        model_modality="text",
        model_modalities=["video"],
        supports_vision=False,
    )
    assert model_supports_modality(
        "image",
        model_modality="text",
        model_modalities=["vision"],
        supports_vision=False,
    )
    assert model_supports_modality(
        "text",
        model_modality="text",
        model_modalities=[],
        supports_vision=False,
    )


def test_active_inbound_fallback_target_cannot_be_invalidated():
    dependant = SimpleNamespace(
        id=uuid.uuid4(),
        saas_tier="ultra",
        modality="video",
    )

    for prospective in (
        {"enabled": False, "saas_tier": "ultra", "modality": "video"},
        {"enabled": True, "saas_tier": "pro", "modality": "video"},
        {"enabled": True, "saas_tier": "ultra", "modality": "text"},
    ):
        with pytest.raises(HTTPException, match="active fallback target") as exc:
            saas_api._validate_inbound_fallback_continuity(
                [dependant],
                **prospective,
            )
        assert exc.value.status_code == 409

    saas_api._validate_inbound_fallback_continuity(
        [dependant],
        enabled=True,
        saas_tier="ultra",
        modality="video",
    )


@pytest.mark.asyncio
async def test_enterprise_model_delete_rejects_any_saas_route(monkeypatch):
    model_id = uuid.uuid4()
    route_id = uuid.uuid4()
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(id=model_id)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    routes = AsyncMock(return_value=[SimpleNamespace(id=route_id)])
    monkeypatch.setattr(enterprise_api, "_model_routes", routes)
    actor = SimpleNamespace(role="platform_admin", identity=None)

    with pytest.raises(HTTPException, match="every SaaS model route") as exc:
        await enterprise_api.remove_llm_model(
            model_id,
            force=True,
            current_user=actor,
            db=db,
        )

    assert exc.value.status_code == 409
    routes.assert_awaited_once_with(db, model_id, enabled_only=False)


def test_model_route_schema_normalizes_and_bounds_tier_modality_priority():
    model_id = uuid.uuid4()
    route = ModelRouteCreateIn(
        saas_tier=" Ultra ",
        modality=" VIDEO ",
        llm_model_id=model_id,
        priority=930,
    )
    assert route.saas_tier == "ultra"
    assert route.modality == "video"

    for payload in (
        {"saas_tier": "enterprise", "modality": "text", "llm_model_id": model_id},
        {"saas_tier": "lite", "modality": "audio", "llm_model_id": model_id},
        {
            "saas_tier": "lite",
            "modality": "text",
            "llm_model_id": model_id,
            "priority": 1_000_001,
        },
    ):
        with pytest.raises(ValidationError):
            ModelRouteCreateIn(**payload)


def test_model_route_update_rejects_null_for_nonnullable_columns():
    assert ModelRouteUpdateIn(fallback_route_id=None).model_dump(exclude_unset=True) == {
        "fallback_route_id": None,
    }
    for field in ("saas_tier", "modality", "llm_model_id", "priority", "enabled"):
        with pytest.raises(ValidationError, match="explicit null"):
            ModelRouteUpdateIn.model_validate({field: None})
