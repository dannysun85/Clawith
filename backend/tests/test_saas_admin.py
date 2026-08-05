"""Unit tests for SaaS admin operations."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import admin as admin_api
from app.api import saas as saas_api
from app.schemas.saas import (
    InitializeFreeSubscriptionsIn,
    LLMCreditHoldResolutionIn,
    MediaRouteUpdateIn,
)


class DummyResult:
    def __init__(self, *, scalar=None, values=None):
        self._scalar = scalar
        self._values = list(values or [])

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar

    def scalar_one(self):
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


def _verified_credential(
    provider: str,
    *,
    plan_tier: str | None,
    capabilities: list[str],
):
    credential_id = uuid.uuid4()
    verified_at = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=credential_id,
        provider=provider,
        plan_tier=plan_tier,
        capabilities=capabilities,
        modality_status={},
        api_key="must-not-leak",
        enabled=True,
        status="healthy",
        daily_quota=None,
        used_today=0,
        last_verification_at=verified_at,
        verification_receipt={
            "receipt_ref": f"credential-auth:{uuid.uuid4()}",
            "kind": "credential_auth_probe",
            "scope": "account_authentication",
            "credential_id": str(credential_id),
            "provider": provider,
            "checked_at": verified_at.isoformat(),
            "ok": True,
        },
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
async def test_saas_llm_hold_resolution_forwards_exact_scope_and_evidence():
    reservation_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    admin = _admin_user()
    request = LLMCreditHoldResolutionIn(
        reservation_ids=[reservation_id],
        expected_tenant_id=tenant_id,
        incident_key="INC-LLM-001",
        evidence_ref="chat-message:tool-call-returned",
        resolution="provider_completed",
        settlement_amount=2,
        apply=False,
    )
    result = SimpleNamespace(to_dict=lambda: {"applied": False})

    with patch.object(
        saas_api,
        "resolve_llm_credit_holds",
        AsyncMock(return_value=result),
    ) as resolve:
        response = await saas_api.resolve_ambiguous_llm_credit_holds(
            request,
            current_user=admin,
        )

    assert response == {"applied": False}
    resolve.assert_awaited_once_with(
        reservation_ids=(reservation_id,),
        expected_tenant_id=tenant_id,
        incident_key="INC-LLM-001",
        evidence_ref="chat-message:tool-call-returned",
        resolution="provider_completed",
        settlement_amount=2,
        actor_user_id=admin.id,
        apply=False,
    )


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
    agent_plan_credential = _verified_credential(
        "volcengine_agent_plan",
        plan_tier="small",
        capabilities=["image", "audio", "video"],
    )
    agent_plan_credential.api_key = "agent-plan-key-must-not-leak"
    minimax_credential = _verified_credential(
        "minimax",
        plan_tier=None,
        capabilities=["text", "image", "audio", "music", "video"],
    )
    db = RecordingDB([
        DummyResult(values=tools),
        DummyResult(values=[agent_plan_credential, minimax_credential]),
    ])

    routes = await saas_api.list_media_routes(current_user=_admin_user(), db=db)

    assert len(routes) == 12
    assert {(route.modality, route.tier) for route in routes} == {
        (modality, tier)
        for modality in ("image", "audio", "music", "video")
        for tier in ("lite", "pro", "ultra")
    }
    assert all(route.available for route in routes)
    assert all(route.provider == "automatic" for route in routes)
    assert all(route.routing_mode == "automatic_failover" for route in routes)
    by_modality = {
        route.modality: route
        for route in routes
        if route.tier == "lite"
    }
    assert by_modality["image"].provider_order == [
        "volcengine_agent_plan",
        "minimax",
    ]
    assert by_modality["image"].route_semantics == "account_pool_readiness_only"
    assert [item.strategy for item in by_modality["image"].execution_strategies] == [
        "commercial_quality",
        "creative_exploration",
    ]
    commercial, creative = by_modality["image"].execution_strategies
    assert commercial.provider_order == ["volcengine_agent_plan", "minimax"]
    assert creative.provider_order == ["minimax", "volcengine_agent_plan"]
    assert commercial.executable_without_alternate_confirmation is True
    assert creative.executable_without_alternate_confirmation is True
    assert by_modality["audio"].provider_order == [
        "volcengine_agent_plan",
        "minimax",
    ]
    assert by_modality["video"].provider_order == [
        "minimax",
        "volcengine_agent_plan",
    ]
    assert by_modality["music"].provider_order == ["minimax"]
    assert by_modality["image"].available_providers == [
        "volcengine_agent_plan",
        "minimax",
    ]
    assert by_modality["audio"].available_providers == [
        "volcengine_agent_plan",
        "minimax",
    ]
    assert by_modality["music"].available_providers == ["minimax"]
    assert by_modality["video"].available_providers == ["minimax"]
    assert by_modality["image"].capability_status == "available"
    assert by_modality["audio"].capability_status == "available"
    assert by_modality["music"].capability_status == "available"
    assert by_modality["video"].capability_status == "available"
    assert by_modality["video"].reason_code == "minimax_daily_allowance_only"
    assert by_modality["video"].primary_provider == "minimax"
    assert by_modality["video"].degraded_providers == []
    assert by_modality["video"].evaluation_source == "persisted_account_and_generation_receipts"
    assert by_modality["video"].readiness_status == "generation_unverified"
    assert by_modality["video"].quality_evidence_status == "not_reviewed"
    assert all(
        not item.generation_observed
        for item in by_modality["video"].provider_readiness
    )
    assert by_modality["image"].primary_provider == "volcengine_agent_plan"
    assert by_modality["image"].fallback_provider == "minimax"
    assert by_modality["audio"].primary_provider == "volcengine_agent_plan"
    assert by_modality["audio"].fallback_provider == "minimax"
    assert by_modality["music"].primary_provider == "minimax"
    assert by_modality["music"].fallback_provider == ""
    assert by_modality["video"].fallback_provider == "volcengine_agent_plan"
    serialized = " ".join(str(route.model_dump()) for route in routes)
    assert "must-not-leak" not in serialized
    assert "agent-plan-key-must-not-leak" not in serialized
    assert "api_key" not in serialized
    statements = "\n".join(str(statement) for statement in db.statements)
    assert "tools.tenant_id IS NULL" in statements
    assert "llm_credentials.tenant_id IS NULL" in statements


def _video_provider_state(*providers: str) -> saas_api.PlatformMediaProviderState:
    provider_set = set(providers)
    return saas_api.PlatformMediaProviderState(
        configured_modalities={
            "volcengine_agent_plan": {"video"}
            if "volcengine_agent_plan" in provider_set
            else set(),
            "minimax": {"video"} if "minimax" in provider_set else set(),
        },
        verified_modalities={
            "volcengine_agent_plan": {"video"}
            if "volcengine_agent_plan" in provider_set
            else set(),
            "minimax": {"video"} if "minimax" in provider_set else set(),
        },
        plan_tiers={},
        account_receipts={},
        verified_credentials={},
    )


def _video_allowance(*, used: int, remaining: int) -> dict[str, object]:
    return {
        "allowance_date": "2026-08-06",
        "timezone": "Asia/Shanghai",
        "quota": 3,
        "used": used,
        "remaining": remaining,
        "eligible_accounts": 1,
        "excluded_accounts": 0,
        "accounts": [],
    }


def test_video_route_is_unavailable_when_minimax_is_exhausted_and_fire_is_off():
    route = saas_api._media_route_out(
        modality="video",
        tier="lite",
        tool=SimpleNamespace(config={}, enabled=True),
        provider_state=_video_provider_state("minimax"),
        generation_receipts={},
        minimax_allowance=_video_allowance(used=3, remaining=0),
    )

    assert route.available is False
    assert route.pool_available is False
    assert route.available_providers == []
    assert route.capability_status == "unavailable"
    assert route.reason_code == "minimax_daily_allowance_exhausted"


def test_video_route_activates_fire_after_minimax_daily_allowance_is_exhausted():
    route = saas_api._media_route_out(
        modality="video",
        tier="pro",
        tool=SimpleNamespace(config={}, enabled=True),
        provider_state=_video_provider_state("minimax", "volcengine_agent_plan"),
        generation_receipts={},
        minimax_allowance=_video_allowance(used=3, remaining=0),
    )

    assert route.available is True
    assert route.pool_available is True
    assert route.available_providers == ["volcengine_agent_plan"]
    assert route.capability_status == "available"
    assert (
        route.reason_code
        == "minimax_daily_allowance_exhausted_volcengine_active"
    )
    assert "火山 Agent Plan 接管" in route.recommended_action
    strategy = route.execution_strategies[0]
    assert strategy.provider_order == ["minimax", "volcengine_agent_plan"]
    assert strategy.preferred_ready is False
    assert strategy.alternate_provider == "volcengine_agent_plan"
    assert strategy.executable_without_alternate_confirmation is True


@pytest.mark.asyncio
async def test_media_routes_report_generation_evidence_without_claiming_quality():
    tools = [
        SimpleNamespace(name=name, config={}, enabled=True)
        for name in saas_api.MINIMAX_MEDIA_TOOL_NAMES.values()
    ]
    credential = _verified_credential(
        "minimax",
        plan_tier=None,
        capabilities=["image"],
    )
    completed_at = credential.last_verification_at + timedelta(seconds=1)
    task = SimpleNamespace(
        id=uuid.uuid4(),
        credential_id=credential.id,
        provider="minimax",
        modality="image",
        status="succeeded",
        provider_task_id="provider-task-recorded",
        output_size=4096,
        completed_at=completed_at,
        model="image-01",
        request_metadata={
            "quoted_credits": 4,
            "pricing_version": "image-provider-v1",
            "billing_basis": "provider_native",
        },
        last_response={"usage": {"total_tokens": 321}},
    )
    db = RecordingDB(
        [
            DummyResult(values=tools),
            DummyResult(values=[credential]),
            DummyResult(values=[task]),
        ]
    )

    routes = await saas_api.list_media_routes(current_user=_admin_user(), db=db)

    image = next(
        route for route in routes if route.modality == "image" and route.tier == "lite"
    )
    minimax = next(
        item for item in image.provider_readiness if item.provider == "minimax"
    )
    assert image.readiness_status == "generation_observed"
    assert image.quality_evidence_status == "not_reviewed"
    assert image.provider_order == ["volcengine_agent_plan", "minimax"]
    assert image.available_providers == ["minimax"]
    assert image.primary_provider == "volcengine_agent_plan"
    commercial, creative = image.execution_strategies
    assert commercial.preferred_ready is False
    assert commercial.executable_without_alternate_confirmation is False
    assert commercial.alternate_provider == "minimax"
    assert commercial.alternate_confirmation_required is True
    assert creative.preferred_ready is True
    assert creative.executable_without_alternate_confirmation is True
    assert image.fallback_provider == "minimax"
    assert minimax.account_verified is True
    assert minimax.generation_observed is True
    assert minimax.generation_receipt is not None
    assert minimax.generation_receipt["evidence_level"] == "generation_observed"
    assert minimax.generation_receipt["quality_reviewed"] is False
    assert minimax.generation_receipt["quoted_credits"] == 4
    assert minimax.generation_receipt["pricing_version"] == "image-provider-v1"
    assert minimax.generation_receipt["provider_total_tokens"] == 321
    serialized = str(image.model_dump())
    assert "provider-task-recorded" not in serialized
    assert "must-not-leak" not in serialized


@pytest.mark.asyncio
async def test_media_route_update_writes_only_tier_scoped_platform_config():
    tool = SimpleNamespace(name="generate_video_minimax", config={"voice_id": "unchanged"}, enabled=True)
    credential = _verified_credential(
        "minimax",
        plan_tier=None,
        capabilities=["video"],
    )
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
    multimodal_model = SimpleNamespace(
        label="MiniMax-M3 Pro (Platform)",
        modality="multimodal",
        modalities=["text", "image", "video"],
        supports_vision=True,
    )

    assert saas_api._validate_model_route(model, "text") == "text"
    with pytest.raises(HTTPException, match="does not support"):
        saas_api._validate_model_route(model, "image")
    with pytest.raises(HTTPException, match="does not support"):
        saas_api._validate_model_route(model, "video")
    assert saas_api._validate_model_route(multimodal_model, "image") == "image"
    assert saas_api._validate_model_route(multimodal_model, "video") == "video"
    with pytest.raises(HTTPException, match="media routes"):
        saas_api._validate_model_route(multimodal_model, "audio")
    with pytest.raises(HTTPException, match="media routes"):
        saas_api._validate_model_route(multimodal_model, "music")


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
