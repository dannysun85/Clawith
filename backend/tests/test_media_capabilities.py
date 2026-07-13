import uuid

from app.services.entitlements import Entitlements
from app.services.media_capabilities import evaluate_media_capabilities


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
