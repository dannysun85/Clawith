"""Regression tests for subscription trigger quota enforcement."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class RecordingDB:
    def __init__(self, current):
        self.current = current
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return ScalarResult(self.current)


@pytest.mark.asyncio
async def test_trigger_quota_counts_only_active_non_system_triggers():
    from app.services.quota_guard import check_trigger_quota

    tenant_id = uuid.uuid4()
    db = RecordingDB(current=2)
    with (
        patch(
            "app.services.quota_guard.get_tenant_entitlements",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(max_triggers=3),
        ),
        patch("app.services.quota_guard.async_session") as session_factory,
    ):
        session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await check_trigger_quota(tenant_id)

    sql = str(db.statement)
    assert "agent_triggers.is_enabled IS true" in sql
    assert "agent_triggers.is_system IS false" in sql
    assert tenant_id in db.statement.compile().params.values()
