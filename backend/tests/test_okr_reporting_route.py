import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import okr_reporting
from app.services.llm import RouteMeta


@pytest.mark.asyncio
async def test_okr_reports_use_unified_agent_route_and_failover(monkeypatch):
    agent_id = uuid.uuid4()
    route_meta = RouteMeta(saas_tier="lite", modality="text", action="chat")
    primary = SimpleNamespace(model="MiniMax-M2.1", provider="minimax")
    fallback = SimpleNamespace(model="MiniMax-M2", provider="minimax")
    resolved = okr_reporting.ResolvedReportModels(
        primary=primary,
        fallback=fallback,
        okr_agent_id=agent_id,
        agent=SimpleNamespace(id=agent_id, name="OKR Agent", role_description="Summarize OKRs"),
        route_meta=route_meta,
    )
    captured = {}

    async def fake_resolve(_tenant_id):
        return resolved

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return "# Company Daily Report\nDate: 2026-07-13\n\n## Executive Summary\n- 正常"

    monkeypatch.setattr(okr_reporting, "_resolve_report_models", fake_resolve)
    monkeypatch.setattr(okr_reporting, "call_llm_with_failover", fake_call)

    result = await okr_reporting._generate_llm_report_content(
        uuid.uuid4(),
        "daily",
        date(2026, 7, 13),
        date(2026, 7, 13),
        {"submitted": 1},
        fallback_content="fallback",
    )

    assert "正常" in result
    assert captured["primary_model"] is primary
    assert captured["fallback_model"] is fallback
    assert captured["agent_id"] == agent_id
    assert captured["route_meta"] is route_meta
    assert captured["skip_tools"] is True


@pytest.mark.asyncio
async def test_okr_unified_route_error_returns_deterministic_fallback(monkeypatch):
    agent_id = uuid.uuid4()
    resolved = okr_reporting.ResolvedReportModels(
        primary=SimpleNamespace(model="MiniMax-M2.1", provider="minimax"),
        fallback=None,
        okr_agent_id=agent_id,
        agent=SimpleNamespace(id=agent_id, name="OKR Agent", role_description=""),
        route_meta=RouteMeta(saas_tier="pro", modality="text", action="chat"),
    )

    async def fake_resolve(_tenant_id):
        return resolved

    async def fake_call(**_kwargs):
        return "⚠️ 平台账号池暂无可用 API key"

    monkeypatch.setattr(okr_reporting, "_resolve_report_models", fake_resolve)
    monkeypatch.setattr(okr_reporting, "call_llm_with_failover", fake_call)

    result = await okr_reporting._generate_llm_report_content(
        uuid.uuid4(),
        "weekly",
        date(2026, 7, 6),
        date(2026, 7, 12),
        {},
        fallback_content="deterministic fallback",
    )

    assert result == "deterministic fallback"
