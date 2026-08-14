"""Regression coverage for public URL resolution and generated setup links."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api import gateway as gateway_api
from app.services.platform_service import platform_service


class _ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SettingsDB:
    def __init__(self, setting=None):
        self.setting = setting
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _ScalarResult(self.setting)


def _request(host: str = "tenant.example.com") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": (host, 443),
            "path": "/",
            "root_path": "",
            "headers": [(b"host", host.encode())],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_public_base_url_prefers_environment_without_querying_db(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://env.example.com/")
    db = _SettingsDB(
        SimpleNamespace(value={"public_base_url": "https://stored.example.com"})
    )

    result = await platform_service.get_public_base_url(db, _request())

    assert result == "https://env.example.com"
    assert db.execute_calls == 0


@pytest.mark.asyncio
async def test_public_base_url_uses_persisted_platform_setting(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    db = _SettingsDB(
        SimpleNamespace(value={"public_base_url": "https://stored.example.com/app/"})
    )

    result = await platform_service.get_public_base_url(db, _request())

    assert result == "https://stored.example.com/app"


@pytest.mark.asyncio
async def test_public_base_url_uses_request_and_fails_closed_without_context(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    assert await platform_service.get_public_base_url(request=_request()) == "https://tenant.example.com"
    with pytest.raises(RuntimeError, match="Public base URL is not configured"):
        await platform_service.get_public_base_url()


@pytest.mark.asyncio
async def test_gateway_setup_guide_uses_resolved_deployment_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    agent_id = uuid.uuid4()
    db = _SettingsDB()
    monkeypatch.setattr(
        gateway_api,
        "_get_agent_by_key",
        AsyncMock(return_value=SimpleNamespace(id=agent_id)),
    )
    monkeypatch.setattr(
        platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://astra.customer.example"),
    )

    result = await gateway_api.get_setup_guide(
        agent_id=agent_id,
        request=_request(),
        x_api_key="secret-agent-key",
        db=db,
    )

    assert "https://astra.customer.example/api/gateway/poll" in result["skill_content"]
    assert "https://try.astra.ai" not in result["skill_content"]
    assert "Sync with Astra platform" in result["skill_content"]
    assert "Clawith platform" not in result["skill_content"]
    assert "Clawith inbox" not in result["skill_content"]
    assert result["skill_filename"] == "clawith_sync.md"

    result_zh = await gateway_api.get_setup_guide(
        agent_id=agent_id,
        request=_request(),
        x_api_key="secret-agent-key",
        accept_language="zh-CN",
        db=db,
    )

    assert "检查 Astra inbox" in result_zh["skill_content"]
    assert "Astra 平台" in result_zh["skill_content"]
    assert "Clawith platform" not in result_zh["skill_content"]
    assert "Clawith inbox" not in result_zh["skill_content"]
