import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import pages as pages_api
from app.services import agent_tools
from app.services.platform_service import platform_service


class _ScalarResult:
    def __init__(self, *, scalar=None, items=None):
        self.scalar = scalar
        self.items = list(items or [])

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.items


class _RecordingDB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.added = []
        self.committed = False

    async def execute(self, _statement):
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_pages_api_returns_absolute_public_urls(monkeypatch):
    agent_id = uuid.uuid4()
    page = SimpleNamespace(
        id=uuid.uuid4(),
        short_id="abc123",
        source_path="workspace/report.html",
        title="Report",
        view_count=2,
        created_at=None,
    )
    db = _RecordingDB([_ScalarResult(items=[page])])

    async def allow_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.core.permissions.check_agent_access", allow_access)
    monkeypatch.setattr(
        platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://astra.example/"),
    )

    result = await pages_api.list_pages(
        agent_id=agent_id,
        request=SimpleNamespace(),
        current_user=SimpleNamespace(),
        db=db,
    )

    assert result[0]["url"] == "https://astra.example/p/abc123"


@pytest.mark.asyncio
async def test_agent_page_listing_never_falls_back_to_relative_urls(monkeypatch):
    agent_id = uuid.uuid4()
    page = SimpleNamespace(
        short_id="abc123",
        title="Report",
        source_path="workspace/report.html",
        view_count=2,
    )
    db = _RecordingDB([_ScalarResult(items=[page])])

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(agent_tools, "async_session", session)
    monkeypatch.setattr(
        platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://try.astra.ai"),
    )

    result = await agent_tools._list_published_pages(agent_id)

    assert "URL: https://try.astra.ai/p/abc123" in result
    assert "URL: /p/" not in result


@pytest.mark.asyncio
async def test_publish_page_returns_absolute_public_url(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    storage = SimpleNamespace(
        exists=AsyncMock(return_value=True),
        is_file=AsyncMock(return_value=True),
        read_text=AsyncMock(return_value="<html><title>Report</title></html>"),
    )
    db = _RecordingDB([_ScalarResult(scalar=tenant_id)])

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "async_session", session)
    monkeypatch.setattr(
        platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://astra.example/"),
    )

    async def public_url(url):
        return url, None

    monkeypatch.setattr(agent_tools, "_validate_public_http_url", public_url)

    result = await agent_tools._publish_page(
        agent_id,
        uuid.uuid4(),
        tmp_path,
        {"path": "workspace/report.html"},
    )

    assert "Public URL: https://astra.example/p/" in result
    assert db.committed is True
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_publish_page_rejects_cross_agent_source_before_storage_read(
    monkeypatch,
    tmp_path,
):
    storage = SimpleNamespace(
        exists=AsyncMock(return_value=True),
        is_file=AsyncMock(return_value=True),
        read_text=AsyncMock(return_value="<html>private</html>"),
    )
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr(agent_tools, "get_storage_backend", storage_factory)

    result = await agent_tools._publish_page(
        uuid.uuid4(),
        uuid.uuid4(),
        tmp_path,
        {"path": f"../{uuid.uuid4()}/workspace/private.html"},
    )

    assert "File path must stay within the Agent workspace" in result
    storage_factory.assert_not_called()
    storage.exists.assert_not_awaited()
    storage.is_file.assert_not_awaited()
    storage.read_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_page_rejects_unsafe_legacy_source_path(monkeypatch):
    page = SimpleNamespace(
        agent_id=uuid.uuid4(),
        source_path=f"../{uuid.uuid4()}/workspace/private.html",
    )
    db = _RecordingDB([_ScalarResult(scalar=page)])
    storage = SimpleNamespace(
        exists=AsyncMock(return_value=True),
        is_file=AsyncMock(return_value=True),
        read_text=AsyncMock(return_value="<html>private</html>"),
    )
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr(pages_api, "get_storage_backend", storage_factory)

    with pytest.raises(HTTPException) as exc:
        await pages_api.render_page("unsafe", db=db)

    assert exc.value.status_code == 404
    storage_factory.assert_not_called()
    storage.exists.assert_not_awaited()
    storage.is_file.assert_not_awaited()
    storage.read_text.assert_not_awaited()
