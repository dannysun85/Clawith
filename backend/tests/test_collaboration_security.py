import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import advanced
from app.services import access_relationships, collaboration


class _Result:
    def __init__(self, values):
        self.values = values

    def scalar_one_or_none(self):
        if isinstance(self.values, list):
            return self.values[0] if self.values else None
        return self.values

    def scalars(self):
        return self

    def all(self):
        if isinstance(self.values, list):
            return self.values
        return [] if self.values is None else [self.values]


class _DB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.added = []
        self.deleted = []
        self.flush_count = 0

    async def execute(self, _statement):
        value = self.responses.pop(0) if self.responses else None
        return _Result(value)

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        self.flush_count += 1


def _agent(agent_id, tenant_id, creator_id, **overrides):
    values = {
        "id": agent_id,
        "tenant_id": tenant_id,
        "creator_id": creator_id,
        "name": f"Agent-{str(agent_id)[:8]}",
        "status": "running",
        "is_expired": False,
        "expires_at": None,
        "access_mode": "company",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_list_collaborators_passes_the_authorized_agent(monkeypatch):
    tenant_id = uuid.uuid4()
    requester = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    agent = _agent(uuid.uuid4(), tenant_id, requester.id)
    db = _DB([])
    monkeypatch.setattr(
        advanced,
        "check_agent_access",
        AsyncMock(return_value=(agent, "use")),
    )
    list_collaborators = AsyncMock(return_value=[])
    monkeypatch.setattr(
        advanced.collaboration_service,
        "list_collaborators",
        list_collaborators,
    )

    result = await advanced.list_collaborators(
        agent.id,
        current_user=requester,
        db=db,
    )

    assert result == []
    list_collaborators.assert_awaited_once_with(
        db,
        agent,
        requester=requester,
    )


@pytest.mark.asyncio
async def test_cross_tenant_collaboration_is_rejected_before_storage(monkeypatch):
    requester_tenant = uuid.uuid4()
    requester = SimpleNamespace(id=uuid.uuid4(), tenant_id=requester_tenant)
    source = _agent(uuid.uuid4(), requester_tenant, requester.id)
    target = _agent(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    db = _DB([[source, target]])
    store = AsyncMock()
    monkeypatch.setattr(collaboration, "store_agent_bytes", store)

    with pytest.raises(PermissionError, match="not available"):
        await collaboration.collaboration_service.send_message_between_agents(
            db,
            source.id,
            target.id,
            "private message",
            requester=requester,
        )

    store.assert_not_awaited()
    assert db.added == []


@pytest.mark.asyncio
async def test_collaboration_requires_direct_access_or_valid_relationship(monkeypatch):
    tenant_id = uuid.uuid4()
    requester = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    source = _agent(uuid.uuid4(), tenant_id, uuid.uuid4())
    target = _agent(uuid.uuid4(), tenant_id, uuid.uuid4())
    db = _DB([[source, target], None])
    monkeypatch.setattr(
        collaboration,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value=None),
    )

    with pytest.raises(PermissionError, match="not available"):
        await collaboration.collaboration_service.delegate_task(
            db,
            source.id,
            target.id,
            "restricted task",
            "",
            requester=requester,
        )

    assert db.added == []


@pytest.mark.asyncio
async def test_concurrent_collaboration_messages_use_distinct_inbox_paths(monkeypatch):
    tenant_id = uuid.uuid4()
    requester = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    source = _agent(uuid.uuid4(), tenant_id, requester.id)
    target = _agent(uuid.uuid4(), tenant_id, requester.id)
    db = _DB([[source, target], [source, target]])
    monkeypatch.setattr(
        collaboration,
        "get_agent_access_level_for_user_id",
        AsyncMock(return_value="manage"),
    )
    store = AsyncMock()
    monkeypatch.setattr(collaboration, "store_agent_bytes", store)

    await asyncio.gather(
        collaboration.collaboration_service.send_message_between_agents(
            db, source.id, target.id, "one", requester=requester
        ),
        collaboration.collaboration_service.send_message_between_agents(
            db, source.id, target.id, "two", requester=requester
        ),
    )

    paths = [call.args[1] for call in store.await_args_list]
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert all(path.startswith("workspace/inbox/") for path in paths)


@pytest.mark.asyncio
async def test_handover_rejects_cross_tenant_or_inactive_target(monkeypatch):
    tenant_id = uuid.uuid4()
    current_user = SimpleNamespace(id=uuid.uuid4())
    agent_id = uuid.uuid4()
    agent = _agent(agent_id, tenant_id, current_user.id)
    monkeypatch.setattr(
        advanced,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    )

    for target in (
        None,
        SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            is_active=True,
            identity=SimpleNamespace(is_active=False),
        ),
    ):
        db = _DB([agent, target])
        with pytest.raises(HTTPException) as exc:
            await advanced.handover_agent(
                agent_id,
                advanced.HandoverRequest(new_creator_id=uuid.uuid4()),
                current_user=current_user,
                db=db,
            )
        assert exc.value.status_code == 403
        assert agent.creator_id == current_user.id
        assert db.added == []


@pytest.mark.asyncio
async def test_handover_rebuilds_manage_permission_and_relationship(monkeypatch):
    tenant_id = uuid.uuid4()
    current_user = SimpleNamespace(id=uuid.uuid4())
    new_creator_id = uuid.uuid4()
    agent = _agent(
        uuid.uuid4(),
        tenant_id,
        current_user.id,
        access_mode="private",
    )
    new_creator = SimpleNamespace(
        id=new_creator_id,
        tenant_id=tenant_id,
        is_active=True,
        identity=SimpleNamespace(is_active=True),
        display_name="New owner",
    )
    db = _DB([agent, new_creator, [], None])
    monkeypatch.setattr(
        advanced,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    )
    ensure_relationships = AsyncMock(return_value=True)
    monkeypatch.setattr(
        access_relationships,
        "ensure_access_granted_platform_relationships",
        ensure_relationships,
    )

    response = await advanced.handover_agent(
        agent.id,
        advanced.HandoverRequest(new_creator_id=new_creator_id),
        current_user=current_user,
        db=db,
    )

    assert response["status"] == "transferred"
    assert agent.creator_id == new_creator_id
    permission = next(
        item for item in db.added if item.__class__.__name__ == "AgentPermission"
    )
    assert permission.scope_id == new_creator_id
    assert permission.access_level == "manage"
    ensure_relationships.assert_awaited_once_with(
        db,
        agent,
        created_by_user_id=current_user.id,
    )
