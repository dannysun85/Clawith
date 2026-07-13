import uuid
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.services import agent_tools
from app.services.feishu_service import feishu_service


class _Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class _CalendarClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/events"):
            return _Response({
                "code": 0,
                "data": {"event": {"event_id": "event-1"}},
            })
        return _Response({"code": 123, "msg": "attendee forbidden"})


def test_iso_to_ts_applies_explicit_timezone_to_naive_input():
    timestamp = agent_tools._iso_to_ts("2026-06-05T14:00:00", "Asia/Shanghai")
    expected = datetime(2026, 6, 5, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert timestamp == expected


@pytest.mark.asyncio
async def test_calendar_create_rejects_invalid_time_before_external_calls(monkeypatch):
    credentials = AsyncMock(return_value=("app-id", "secret"))
    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", credentials)

    result = await agent_tools._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Planning",
            "start_time": "tomorrow afternoon",
            "end_time": "later",
        },
    )

    assert result.startswith("❌ Invalid calendar time:")
    credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_create_reports_partial_attendee_failure(monkeypatch):
    _CalendarClient.calls = []
    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", AsyncMock(return_value=("app-id", "secret")))
    monkeypatch.setattr(feishu_service, "get_tenant_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(agent_tools, "_get_agent_calendar_id", AsyncMock(return_value=("cal-1", None)))
    monkeypatch.setattr("httpx.AsyncClient", _CalendarClient)

    result = await agent_tools._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Planning",
            "start_time": "2026-06-05T14:00:00",
            "end_time": "2026-06-05T14:30:00",
            "timezone": "Asia/Shanghai",
            "attendee_open_ids": ["ou_attendee"],
        },
    )

    assert result.startswith("✅ 日历事件已创建！")
    assert "1 个邀请发送失败" in result
    event_payload = _CalendarClient.calls[0][1]["json"]
    assert event_payload["start_time"] == {
        "timestamp": str(int(datetime(2026, 6, 5, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())),
        "timezone": "Asia/Shanghai",
    }


@pytest.mark.asyncio
async def test_calendar_create_exposes_authentication_failure(monkeypatch):
    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", AsyncMock(return_value=("app-id", "secret")))
    monkeypatch.setattr(
        feishu_service,
        "get_tenant_access_token",
        AsyncMock(side_effect=RuntimeError("token endpoint unavailable")),
    )

    result = await agent_tools._feishu_calendar_create(
        uuid.uuid4(),
        {
            "summary": "Planning",
            "start_time": "2026-06-05T14:00:00+08:00",
            "end_time": "2026-06-05T14:30:00+08:00",
        },
    )

    assert result == "❌ Failed to authenticate with Feishu Calendar: token endpoint unavailable"
