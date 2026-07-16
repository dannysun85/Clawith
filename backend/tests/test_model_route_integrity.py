from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import saas as saas_api
from app.schemas.saas import ModelRouteCreateIn, ModelRouteUpdateIn
from app.services import model_router
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


@pytest.mark.asyncio
async def test_fallback_loader_requires_enabled_route():
    session, db = _session_with_result(None)

    with patch.object(model_router, "async_session", return_value=session):
        await model_router._pick_route_by_id(uuid.uuid4())

    query = str(db.execute.await_args.args[0])
    assert "model_routes.enabled = true" in query.lower()


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
