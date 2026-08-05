import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api import agents as agents_api


def _capability_rows() -> list[dict[str, object]]:
    return [
        {
            "modality": "video",
            "tool_name": "generate_video_minimax",
            "available": True,
            "allowed_by_plan": True,
            "pool_available": True,
            "tool_enabled": True,
            "reason": None,
            "allowed_tiers": ["lite", "pro", "ultra"],
            "capability_status": "available",
            "available_providers": ["minimax"],
            "route_reason": "minimax_daily_allowance_only",
            "next_action": "火山 Agent Plan 当前为 plan=small，不包含视频资格；当前先使用 MiniMax 每账号每日 3 次 Plan 额度。",
        },
        {
            "modality": "image",
            "tool_name": "generate_image_minimax",
            "available": True,
            "allowed_by_plan": True,
            "pool_available": True,
            "tool_enabled": True,
            "reason": None,
            "allowed_tiers": ["lite", "pro", "ultra"],
            "capability_status": "available",
            "available_providers": ["volcengine_agent_plan", "minimax"],
            "route_reason": None,
            "next_action": "按当前工作合同执行；供应商选择由平台托管。",
        },
    ]


def _user(role: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        identity=SimpleNamespace(is_platform_admin=False),
    )


@pytest.mark.asyncio
async def test_media_capability_endpoint_redacts_provider_diagnostics_for_members():
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    rows = _capability_rows()
    with (
        patch.object(
            agents_api,
            "check_agent_access",
            new=AsyncMock(return_value=(agent, "use")),
        ),
        patch.object(
            agents_api,
            "get_tenant_entitlements",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.media_capabilities.get_agent_media_capabilities",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await agents_api.get_media_capabilities(
            agent_id=agent.id,
            tier="pro",
            current_user=_user("member"),
            db=object(),
        )

    by_modality = {row["modality"]: row for row in result["capabilities"]}
    assert by_modality["video"]["available"] is True
    assert by_modality["video"]["capability_status"] == "available"
    assert by_modality["video"]["available_providers"] == []
    assert by_modality["video"]["route_reason"] is None
    assert by_modality["video"]["next_action"] == "按当前工作合同执行；供应商选择由平台托管。"
    assert "plan=small" not in json.dumps(result, ensure_ascii=False)
    assert "minimax" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_media_capability_endpoint_keeps_diagnostics_for_platform_admin():
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    rows = _capability_rows()
    with (
        patch.object(
            agents_api,
            "check_agent_access",
            new=AsyncMock(return_value=(agent, "manage")),
        ),
        patch.object(
            agents_api,
            "get_tenant_entitlements",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.media_capabilities.get_agent_media_capabilities",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await agents_api.get_media_capabilities(
            agent_id=agent.id,
            tier="pro",
            current_user=_user("platform_admin"),
            db=object(),
        )

    by_modality = {row["modality"]: row for row in result["capabilities"]}
    assert by_modality["video"]["available_providers"] == ["minimax"]
    assert by_modality["video"]["route_reason"] == "minimax_daily_allowance_only"
    assert "plan=small" in by_modality["video"]["next_action"]
