import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_tools


class _Result:
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
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_feishu_user_search_filters_active_org_members_without_agent_cartesian_join(monkeypatch):
    tenant_id = uuid.uuid4()
    member = SimpleNamespace(
        name="Alice",
        external_id="user-1",
        open_id="open-1",
        email="alice@example.com",
        department_path="Engineering",
    )
    db = _RecordingDB([
        _Result(scalar=tenant_id),
        _Result(items=[member]),
    ])

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr("app.database.async_session", session)
    monkeypatch.setattr(
        agent_tools,
        "_get_feishu_credentials",
        AsyncMock(return_value=("app-id", "app-secret")),
    )

    result = await agent_tools._feishu_user_search(uuid.uuid4(), {"name": "Ali"})

    query_sql = str(db.statements[1])
    assert "org_members.status" in query_sql
    assert "agents.status" not in query_sql
    assert "Alice" in result
