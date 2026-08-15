"""Tenant isolation contracts for enterprise context and governance audit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.api import enterprise as enterprise_api
from app.models.audit import EnterpriseInfo
from app.services import enterprise_sync as enterprise_sync_module
from app.services.enterprise_sync import EnterpriseSyncService


class _ScalarCollection:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _Result:
    def __init__(self, values=(), scalar=None):
        self.values = list(values)
        self.scalar_value = scalar

    def scalars(self):
        return _ScalarCollection(self.values)

    def scalar_one_or_none(self):
        return self.scalar_value


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.flush = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value):
        self.added.append(value)


def _user(*, role="member"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_enterprise_info_list_is_always_filtered_to_current_membership():
    user = _user()
    db = _DB(_Result(values=[]))

    assert await enterprise_api.list_enterprise_info(user, db) == []

    sql = str(db.statements[0])
    assert "enterprise_info.tenant_id" in sql
    assert user.tenant_id in db.statements[0].compile().params.values()


@pytest.mark.asyncio
async def test_enterprise_info_update_uses_tenant_composite_key(monkeypatch):
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    db = _DB(_Result(scalar=None))
    publish = AsyncMock()
    monkeypatch.setattr(enterprise_sync_module, "publish_event", publish)

    info = await EnterpriseSyncService().update_enterprise_info(
        db,
        tenant_id=tenant_id,
        info_type="company_profile",
        content={"name": "Acme"},
        visible_roles=[],
        updated_by=actor_id,
    )

    assert isinstance(info, EnterpriseInfo)
    assert info.tenant_id == tenant_id
    assert info.info_type == "company_profile"
    sql = str(db.statements[0])
    assert "enterprise_info.tenant_id" in sql
    assert tenant_id in db.statements[0].compile().params.values()
    publish.assert_awaited_once()
    assert publish.await_args.args[1]["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_enterprise_sync_targets_only_agents_in_the_same_tenant(monkeypatch):
    tenant_id = uuid.uuid4()
    agents = [
        SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role_description="finance"),
        SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role_description="sales"),
    ]
    db = _DB(_Result(values=agents))
    service = EnterpriseSyncService()
    sync = AsyncMock()
    monkeypatch.setattr(service, "sync_to_agent", sync)

    count = await service.sync_to_all_agents(db, tenant_id=tenant_id)

    assert count == 2
    statement = db.statements[0]
    assert "agents.tenant_id" in str(statement)
    assert tenant_id in statement.compile().params.values()
    assert sync.await_count == 2
    for call in sync.await_args_list:
        assert call.kwargs["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_company_audit_query_includes_direct_user_and_agent_tenant_scope():
    user = _user(role="org_owner")
    db = _DB(_Result(values=[]))

    result = await enterprise_api.list_audit_logs(
        agent_id=None,
        tenant_id=None,
        limit=50,
        current_user=user,
        db=db,
    )

    assert result == []
    sql = str(db.statements[0])
    assert "audit_logs.tenant_id" in sql
    assert "audit_logs.agent_id IN" in sql
    assert "audit_logs.user_id IN" in sql
    assert sql.count("tenant_id") >= 3


def test_access_control_migration_declares_durable_constraints():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "202608151100_harden_tenant_access_control.py"
    ).read_text(encoding="utf-8")

    assert "uq_agent_permissions_company" in source
    assert "uq_agent_permissions_scoped" in source
    assert "ck_agent_permissions_access_level" in source
    assert "uq_enterprise_info_tenant_type" in source
    assert "UPDATE audit_logs AS audit" in source
