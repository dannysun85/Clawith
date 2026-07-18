import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_directory


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
    source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="company",
    )
    db = _RecordingDB([
        _Result(scalar=source),
        _Result(items=[]),
    ])

    result = await agent_directory.query_agent_directory(
        db,
        source_agent_id=source.id,
        query="Ali",
        member_type="human",
        include_uncontactable=False,
    )

    query_sql = str(db.statements[1])
    assert "org_members.status" in query_sql
    assert "agents.status" not in query_sql
    assert result["ok"] is True
