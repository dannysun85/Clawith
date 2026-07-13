"""Unit tests for SaaS admin operations."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import admin as admin_api
from app.api import saas as saas_api
from app.schemas.saas import InitializeFreeSubscriptionsIn, MediaRouteUpdateIn


class DummyResult:
    def __init__(self, *, scalar=None, values=None):
        self._scalar = scalar
        self._values = list(values or [])

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.added = []
        self.committed = False
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class AdminCompanyCreateDB:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        from datetime import datetime, timezone

        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()
            if hasattr(value, "is_active") and value.is_active is None:
                value.is_active = True
            if hasattr(value, "sso_enabled") and value.sso_enabled is None:
                value.sso_enabled = False
            if hasattr(value, "created_at") and value.created_at is None:
                value.created_at = datetime.now(timezone.utc)


def _admin_user(email="admin@reeftotem.ai", role="platform_admin", identity_is_platform_admin=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        email=email,
        tenant_id=uuid.uuid4(),
        identity=SimpleNamespace(is_platform_admin=identity_is_platform_admin),
    )


@pytest.mark.asyncio
async def test_saas_admin_owner_email_is_allowed():
    user = _admin_user(email="ADMIN@REEFTOTEM.AI")

    result = await saas_api.get_saas_admin(user)

    assert result is user


@pytest.mark.asyncio
async def test_saas_admin_rejects_other_platform_admin_email():
    user = _admin_user(email="other@example.com")

    with pytest.raises(HTTPException) as exc:
        await saas_api.get_saas_admin(user)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_saas_admin_rejects_owner_email_without_platform_admin_role():
    user = _admin_user(email="admin@reeftotem.ai", role="org_admin")

    with pytest.raises(HTTPException) as exc:
        await saas_api.get_saas_admin(user)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_initialize_free_subscriptions_creates_only_missing_tenants():
    tenant_missing = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    tenant_existing = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    free_plan = SimpleNamespace(id=uuid.uuid4(), code="free", is_active=True)
    existing_sub = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_existing.id)
    created_sub = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_missing.id)
    db = RecordingDB([
        DummyResult(scalar=free_plan),
        DummyResult(values=[tenant_missing, tenant_existing]),
        DummyResult(),
        DummyResult(scalar=existing_sub),
    ])

    with patch.object(
        saas_api,
        "ensure_free_subscription_for_tenant",
        AsyncMock(return_value=created_sub),
    ) as ensure_free:
        with patch.object(saas_api, "reconcile_tenant_agent_plan_selections", AsyncMock()):
            with patch.object(saas_api, "restore_stopped_agents", AsyncMock()) as restore:
                with patch.object(saas_api, "enforce_agent_limit", AsyncMock()) as enforce:
                    result = await saas_api.initialize_free_subscriptions(
                        InitializeFreeSubscriptionsIn(confirm=True),
                        current_user=_admin_user(),
                        db=db,
                    )

    assert result.total_candidates == 2
    assert result.created == 1
    assert result.skipped_existing == 1
    assert result.tenant_ids == [tenant_missing.id]
    assert db.committed is True
    ensure_free.assert_awaited_once()
    assert ensure_free.await_args.args[1] == tenant_missing.id
    restore.assert_awaited_once_with(tenant_missing.id)
    enforce.assert_awaited_once_with(tenant_missing.id)


@pytest.mark.asyncio
async def test_legacy_admin_company_creation_initializes_free_subscription():
    db = AdminCompanyCreateDB()
    current_user = _admin_user()

    with patch.object(
        admin_api,
        "ensure_free_subscription_for_tenant",
        AsyncMock(),
    ) as ensure_free:
        result = await admin_api.create_company(
            admin_api.CompanyCreateRequest(name="Release Gate Tenant"),
            current_user=current_user,
            db=db,
        )

    assert result.company.name == "Release Gate Tenant"
    ensure_free.assert_awaited_once_with(
        db,
        result.company.id,
        granted_by=current_user.id,
    )


def test_final_catalog_migration_preserves_one_agent_free_limit():
    migration = (
        __import__("pathlib").Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "073_seed_saas_mvp_catalog.py"
    ).read_text(encoding="utf-8")

    assert "1, 1000, 50, 'permanent'" in migration
    assert "5, 1000, 50, 'permanent'" not in migration


@pytest.mark.asyncio
async def test_media_routes_expose_complete_matrix_without_credentials():
    tools = [
        SimpleNamespace(name=name, config={}, enabled=True)
        for name in saas_api.MINIMAX_MEDIA_TOOL_NAMES.values()
    ]
    credential = SimpleNamespace(
        capabilities=["text", "image", "audio", "music", "video"],
        api_key="must-not-leak",
    )
    db = RecordingDB([
        DummyResult(values=tools),
        DummyResult(values=[credential]),
    ])

    routes = await saas_api.list_media_routes(current_user=_admin_user(), db=db)

    assert len(routes) == 12
    assert {(route.modality, route.tier) for route in routes} == {
        (modality, tier)
        for modality in ("image", "audio", "music", "video")
        for tier in ("lite", "pro", "ultra")
    }
    assert all(route.available for route in routes)
    serialized = " ".join(str(route.model_dump()) for route in routes)
    assert "must-not-leak" not in serialized
    assert "api_key" not in serialized
    statements = "\n".join(str(statement) for statement in db.statements)
    assert "tools.tenant_id IS NULL" in statements
    assert "llm_credentials.tenant_id IS NULL" in statements


@pytest.mark.asyncio
async def test_media_route_update_writes_only_tier_scoped_platform_config():
    tool = SimpleNamespace(name="generate_video_minimax", config={"voice_id": "unchanged"}, enabled=True)
    credential = SimpleNamespace(capabilities=["video"])
    db = RecordingDB([
        DummyResult(scalar=tool),
        DummyResult(values=[credential]),
    ])

    result = await saas_api.update_media_route(
        "video",
        "ultra",
        MediaRouteUpdateIn(
            model="MiniMax-Hailuo-2.3",
            duration=6,
            resolution="1080p",
            enabled=True,
        ),
        current_user=_admin_user(),
        db=db,
    )

    assert db.committed is True
    assert tool.config["voice_id"] == "unchanged"
    assert tool.config["ultra_model"] == "MiniMax-Hailuo-2.3"
    assert tool.config["ultra_duration"] == 6
    assert tool.config["ultra_resolution"] == "1080P"
    assert result.available is True
    assert result.source == "override"
    assert any(getattr(item, "action", None) == "saas_media_route_update" for item in db.added)


@pytest.mark.asyncio
async def test_media_route_rejects_provider_invalid_quality_pair():
    tool = SimpleNamespace(name="generate_video_minimax", config={}, enabled=True)
    db = RecordingDB([DummyResult(scalar=tool)])

    with pytest.raises(HTTPException) as exc:
        await saas_api.update_media_route(
            "video",
            "lite",
            MediaRouteUpdateIn(duration=10, resolution="1080P"),
            current_user=_admin_user(),
            db=db,
        )

    assert exc.value.status_code == 400
    assert "Unsupported lite video" in exc.value.detail
    assert db.committed is False


def test_text_model_route_cannot_claim_media_generation_capability():
    model = SimpleNamespace(
        label="MiniMax-M2.7 Pro (Platform)",
        modality="text",
        modalities=["text"],
        supports_vision=False,
    )

    assert saas_api._validate_model_route(model, "text") == "text"
    with pytest.raises(HTTPException, match="media routes"):
        saas_api._validate_model_route(model, "video")
    with pytest.raises(HTTPException, match="does not support"):
        saas_api._validate_model_route(model, "image")


def test_media_route_migration_seeds_explicit_matrix_and_disables_legacy_tts():
    migration = (
        __import__("pathlib").Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "088_seed_minimax_media_routes.py"
    ).read_text(encoding="utf-8")

    assert "generate_image_minimax" in migration
    assert "generate_speech_minimax" in migration
    assert "generate_music_minimax" in migration
    assert "generate_video_minimax" in migration
    assert "lite_model" in migration and "pro_model" in migration and "ultra_model" in migration
    assert "action = 'tts' OR modality = 'tts'" in migration
