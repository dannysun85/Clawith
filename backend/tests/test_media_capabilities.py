import uuid

import pytest

from app.services.entitlements import Entitlements
from app.services.media_capabilities import (
    _credential_media_modalities,
    evaluate_media_capabilities,
    get_agent_media_capabilities,
    get_platform_media_provider_modalities,
)
from app.services.media_provider_routing import (
    media_provider_order_for_modality,
    media_provider_order_for_voice_id,
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
    assert media_provider_order_for_modality("video") == automatic
    assert media_provider_order_for_modality("music") == ("minimax",)
    assert media_provider_order_for_modality("unknown") == ()


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
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [
                type(
                    "Credential",
                    (),
                    {
                        "provider": "volcengine_agent_plan",
                        "plan_tier": "small",
                        "capabilities": ["image", "audio", "video"],
                        "modality_status": {},
                    },
                )(),
                type(
                    "Credential",
                    (),
                    {
                        "provider": "minimax",
                        "plan_tier": None,
                        "capabilities": ["image", "audio", "music", "video"],
                        "modality_status": {},
                    },
                )(),
            ]

    class _DB:
        async def execute(self, statement):
            return _Result()

    provider_modalities = await get_platform_media_provider_modalities(_DB())

    assert provider_modalities["volcengine_agent_plan"] == {"image", "audio"}
    assert provider_modalities["minimax"] == {"image", "audio", "music", "video"}
