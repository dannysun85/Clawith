import uuid

import pytest

from app.services.entitlements import Entitlements
from app.services.media_capabilities import (
    evaluate_media_capabilities,
    get_agent_media_capabilities,
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
