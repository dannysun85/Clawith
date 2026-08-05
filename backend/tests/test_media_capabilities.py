import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.entitlements import Entitlements
from app.services.media_capabilities import (
    _credential_media_modalities,
    _ordered_available_providers,
    evaluate_media_capabilities,
    get_agent_media_capabilities,
    get_platform_media_generation_receipts,
    get_platform_media_provider_state,
    get_platform_media_provider_modalities,
    media_route_capability_status,
    PlatformMediaProviderState,
)
from app.services.media_provider_routing import (
    media_provider_order_for_modality,
    media_provider_order_for_image_strategy,
    media_provider_order_for_voice_id,
    validate_media_route_policy,
)


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


def _ent() -> Entitlements:
    return Entitlements(
        plan_id=uuid.uuid4(),
        plan_code="free",
        max_agents=1,
        max_llm_calls_per_day=1000,
        message_limit=50,
        message_period="permanent",
        max_triggers=20,
        credits_per_period=5000,
        allowed_modalities=["text"],
        allowed_tiers=["lite"],
        generation_modalities=["image", "audio", "music", "video"],
        generation_tiers=["lite"],
    )


def test_media_capabilities_require_plan_tool_and_pool():
    rows = evaluate_media_capabilities(
        _ent(),
        tier="lite",
        enabled_tools={"generate_image_minimax", "generate_speech_minimax"},
        pool_modalities={"image", "voice"},
    )
    by_modality = {row["modality"]: row for row in rows}
    assert by_modality["image"]["available"] is True
    assert by_modality["audio"]["available"] is True
    assert by_modality["music"]["reason"] == "agent_tool_disabled"
    assert by_modality["video"]["reason"] == "agent_tool_disabled"


def test_media_capabilities_report_tier_and_pool_failures_without_model_ids():
    rows = evaluate_media_capabilities(
        _ent(),
        tier="ultra",
        enabled_tools={
            "generate_image_minimax",
            "generate_speech_minimax",
            "generate_music_minimax",
            "generate_video_minimax",
        },
        pool_modalities={"image", "audio", "music"},
    )
    by_modality = {row["modality"]: row for row in rows}
    assert by_modality["image"]["reason"] == "plan_denied"
    assert by_modality["video"]["pool_available"] is False
    assert all("model_id" not in row and "credential_id" not in row for row in rows)


@pytest.mark.asyncio
async def test_media_capability_pool_ignores_tenant_private_credentials():
    db = _RecordingDB()

    await get_agent_media_capabilities(
        db,
        agent_id=uuid.uuid4(),
        entitlements=_ent(),
        tier="lite",
    )

    credential_query = str(db.statements[-1].compile())
    assert "llm_credentials.tenant_id IS NULL" in credential_query


def test_media_capability_view_matches_pool_semantics_for_empty_capabilities():
    empty = type("Credential", (), {"capabilities": []})()
    legacy = type("Credential", (), {"capabilities": None})()
    multimodal = type(
        "Credential",
        (),
        {"capabilities": ["multimodal"]},
    )()

    assert _credential_media_modalities(empty) == set()
    assert _credential_media_modalities(legacy) == {
        "image",
        "audio",
        "music",
        "video",
    }
    assert _credential_media_modalities(multimodal) == {
        "image",
        "audio",
        "music",
        "video",
    }


def test_media_provider_order_matches_implemented_runtime_modalities():
    automatic = ("volcengine_agent_plan", "minimax")
    assert media_provider_order_for_modality("image") == automatic
    assert media_provider_order_for_modality("audio") == automatic
    assert media_provider_order_for_modality("video") == (
        "minimax",
        "volcengine_agent_plan",
    )
    assert media_provider_order_for_modality("music") == ("minimax",)
    assert media_provider_order_for_modality("unknown") == ()
    assert media_provider_order_for_image_strategy("commercial_quality") == automatic
    assert media_provider_order_for_image_strategy("creative_exploration") == (
        "minimax",
        "volcengine_agent_plan",
    )
    with pytest.raises(ValueError, match="execution_strategy"):
        media_provider_order_for_image_strategy("pick_a_vendor")


def test_media_route_policy_matches_reviewed_provider_and_model_contract():
    assert validate_media_route_policy() == ()


def test_media_capability_display_order_matches_runtime_route_order():
    providers = {"minimax", "volcengine_agent_plan"}
    assert _ordered_available_providers("image", providers) == [
        "volcengine_agent_plan",
        "minimax",
    ]
    assert _ordered_available_providers("video", providers) == [
        "minimax",
        "volcengine_agent_plan",
    ]
    assert _ordered_available_providers("music", providers) == [
        "minimax",
        "volcengine_agent_plan",
    ]
    assert _ordered_available_providers("unknown", providers) == [
        "minimax",
        "volcengine_agent_plan",
    ]


def test_media_route_status_treats_each_image_strategy_as_callable():
    assert media_route_capability_status("image", {"minimax"})[:2] == (
        "available",
        None,
    )
    assert media_route_capability_status("video", {"minimax"})[:2] == (
        "available",
        "minimax_daily_allowance_only",
    )
    assert media_route_capability_status("audio", {"minimax"})[0] == "available"
    assert media_route_capability_status("music", {"minimax"})[0] == "available"
    assert media_route_capability_status("image", set())[0] == "unavailable"


def test_video_allowance_status_explains_agent_plan_tier_without_granting_access():
    status, reason, next_action = media_route_capability_status(
        "video",
        {"minimax"},
        provider_plan_tiers={"volcengine_agent_plan": {"small"}},
    )

    assert (status, reason) == ("available", "minimax_daily_allowance_only")
    assert "plan=small" in next_action
    assert "不包含视频资格" in next_action
    assert "MiniMax 每账号每日 3 次" in next_action


def test_explicit_voice_identity_never_silently_crosses_provider_namespaces():
    automatic = ("volcengine_agent_plan", "minimax")
    assert media_provider_order_for_voice_id(None) == automatic
    assert media_provider_order_for_voice_id("") == automatic
    assert media_provider_order_for_voice_id("auto") == automatic
    assert media_provider_order_for_voice_id("zh_female_shuangkuaisisi_moon_bigtts") == (
        "volcengine_agent_plan",
    )
    assert media_provider_order_for_voice_id("Chinese (Mandarin)_Warm_Bestie") == (
        "minimax",
    )


@pytest.mark.asyncio
async def test_platform_media_pool_reports_provider_specific_plan_capabilities():
    def credential(provider, plan_tier, capabilities):
        credential_id = uuid.uuid4()
        verified_at = datetime.now(timezone.utc)
        return type(
            "Credential",
            (),
            {
                "id": credential_id,
                "provider": provider,
                "plan_tier": plan_tier,
                "capabilities": capabilities,
                "modality_status": {},
                "enabled": True,
                "status": "healthy",
                "daily_quota": None,
                "used_today": 0,
                "last_verification_at": verified_at,
                "verification_receipt": {
                    "kind": "credential_auth_probe",
                    "credential_id": str(credential_id),
                    "provider": provider,
                    "checked_at": verified_at.isoformat(),
                    "ok": True,
                },
            },
        )()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [
                credential(
                    "volcengine_agent_plan",
                    "small",
                    ["image", "audio", "video"],
                ),
                credential(
                    "minimax",
                    None,
                    ["image", "audio", "music", "video"],
                ),
            ]

    class _DB:
        async def execute(self, statement):
            return _Result()

    state = await get_platform_media_provider_state(_DB())
    provider_modalities = state.verified_modalities

    assert provider_modalities["volcengine_agent_plan"] == {"image", "audio"}
    assert provider_modalities["minimax"] == {"image", "audio", "music", "video"}
    assert state.provider_plan_tiers["volcengine_agent_plan"] == {"small"}


@pytest.mark.asyncio
async def test_platform_media_pool_does_not_treat_healthy_legacy_row_as_verified():
    credential = type(
        "Credential",
        (),
        {
            "id": uuid.uuid4(),
            "provider": "minimax",
            "plan_tier": None,
            "capabilities": ["image", "video"],
            "modality_status": {},
            "enabled": True,
            "status": "healthy",
            "daily_quota": None,
            "used_today": 0,
            "last_verification_at": None,
            "verification_receipt": None,
        },
    )()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [credential]

    class _DB:
        async def execute(self, statement):
            return _Result()

    provider_modalities = await get_platform_media_provider_modalities(_DB())

    assert provider_modalities["minimax"] == set()


@pytest.mark.asyncio
async def test_platform_media_pool_prefers_successful_receipt_over_newer_failed_probe():
    def credential(*, verified_at, ok, status="healthy"):
        credential_id = uuid.uuid4()
        return type(
            "Credential",
            (),
            {
                "id": credential_id,
                "provider": "minimax",
                "plan_tier": None,
                "capabilities": ["video"],
                "modality_status": {},
                "enabled": True,
                "status": status,
                "daily_quota": None,
                "used_today": 0,
                "last_verification_at": verified_at,
                "verification_receipt": {
                    "kind": "credential_auth_probe",
                    "credential_id": str(credential_id),
                    "provider": "minimax",
                    "checked_at": verified_at.isoformat(),
                    "ok": ok,
                    "provider_status": 200 if ok else 401,
                },
            },
        )()

    successful_at = datetime(2026, 8, 1, 17, 54, 46, tzinfo=timezone.utc)
    failed_at = datetime(2026, 8, 1, 17, 54, 47, tzinfo=timezone.utc)
    successful = credential(verified_at=successful_at, ok=True)
    newer_failed = credential(
        verified_at=failed_at,
        ok=False,
        status="unverified",
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [successful, newer_failed]

    class _DB:
        async def execute(self, statement):
            return _Result()

    state = await get_platform_media_provider_state(_DB())

    assert state.verified_modalities["minimax"] == {"video"}
    assert state.account_receipts[("minimax", "video")]["ok"] is True
    assert state.account_receipts[("minimax", "video")]["provider_status"] == 200


@pytest.mark.asyncio
async def test_generation_receipt_requires_task_provider_to_match_verified_credential():
    credential_id = uuid.uuid4()
    verified_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    credential = type(
        "Credential",
        (),
        {
            "id": credential_id,
            "provider": "minimax",
            "last_verification_at": verified_at,
            "verification_receipt": {
                "kind": "credential_auth_probe",
                "credential_id": str(credential_id),
                "provider": "minimax",
                "checked_at": verified_at.isoformat(),
                "ok": True,
            },
        },
    )()
    task = type(
        "Task",
        (),
        {
            "id": uuid.uuid4(),
            "credential_id": credential_id,
            # Deliberately mismatched with the credential/provider binding.
            "provider": "volcengine_agent_plan",
            "modality": "image",
            "status": "succeeded",
            "provider_task_id": "stale-or-misbound-task",
            "output_size": 4096,
            "completed_at": verified_at,
            "model": "doubao-seedream-5.0-lite",
        },
    )()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [task]

    class _DB:
        async def execute(self, statement):
            return _Result()

    state = PlatformMediaProviderState(
        configured_modalities={"volcengine_agent_plan": {"image"}, "minimax": {"image"}},
        verified_modalities={"volcengine_agent_plan": {"image"}, "minimax": {"image"}},
        plan_tiers={},
        account_receipts={},
        verified_credentials={credential_id: credential},
    )

    assert await get_platform_media_generation_receipts(_DB(), state) == {}


@pytest.mark.asyncio
async def test_generation_receipt_ignores_output_completed_before_current_verification():
    credential_id = uuid.uuid4()
    verified_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    credential = type(
        "Credential",
        (),
        {
            "id": credential_id,
            "provider": "minimax",
            "last_verification_at": verified_at,
            "verification_receipt": {
                "kind": "credential_auth_probe",
                "credential_id": str(credential_id),
                "provider": "minimax",
                "checked_at": verified_at.isoformat(),
                "ok": True,
            },
        },
    )()
    task = type(
        "Task",
        (),
        {
            "id": uuid.uuid4(),
            "credential_id": credential_id,
            "provider": "minimax",
            "modality": "image",
            "status": "succeeded",
            "provider_task_id": "pre-verification-task",
            "output_size": 4096,
            "completed_at": verified_at - timedelta(seconds=1),
            "model": "image-01",
        },
    )()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [task]

    class _DB:
        async def execute(self, statement):
            return _Result()

    state = PlatformMediaProviderState(
        configured_modalities={"volcengine_agent_plan": set(), "minimax": {"image"}},
        verified_modalities={"volcengine_agent_plan": set(), "minimax": {"image"}},
        plan_tiers={},
        account_receipts={},
        verified_credentials={credential_id: credential},
    )

    assert await get_platform_media_generation_receipts(_DB(), state) == {}
