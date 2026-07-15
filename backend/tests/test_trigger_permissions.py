import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import triggers as triggers_api


class FakeSession:
    def __init__(self):
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        raise AssertionError("trigger data must not be queried before access is granted")


class FakeSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MutableTriggerSession:
    def __init__(self, trigger):
        self.trigger = trigger
        self.committed = False

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.trigger)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "update", "delete"])
async def test_trigger_endpoints_reject_cross_tenant_access_before_query(monkeypatch, operation):
    session = FakeSession()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    agent_id = uuid.uuid4()

    async def reject_access(_db, _user, _agent_id):
        raise HTTPException(status_code=403, detail="No access to this agent")

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", reject_access)

    with pytest.raises(HTTPException) as exc:
        if operation == "list":
            await triggers_api.list_agent_triggers(agent_id, user)
        elif operation == "update":
            await triggers_api.update_trigger(
                agent_id,
                uuid.uuid4(),
                triggers_api.TriggerUpdate(is_enabled=False),
                user,
            )
        else:
            await triggers_api.delete_trigger(agent_id, uuid.uuid4(), user)

    assert exc.value.status_code == 403
    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_trigger_mutations_require_manage_access(monkeypatch, operation):
    session = FakeSession()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    agent_id = uuid.uuid4()

    async def grant_use_only(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_use_only)

    with pytest.raises(HTTPException) as exc:
        if operation == "update":
            await triggers_api.update_trigger(
                agent_id,
                uuid.uuid4(),
                triggers_api.TriggerUpdate(is_enabled=False),
                user,
            )
        else:
            await triggers_api.delete_trigger(agent_id, uuid.uuid4(), user)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Manage access required"
    assert session.execute_calls == 0


def test_public_trigger_config_redacts_hmac_secret():
    config = {
        "token": "public-token",
        "secret": "private-secret",
        "_origin_user_id": "private-routing-context",
    }

    assert triggers_api._public_trigger_config(config) == {
        "token": "public-token",
        "secret": triggers_api.REDACTED_TRIGGER_SECRET,
    }
    assert config["secret"] == "private-secret"


@pytest.mark.asyncio
async def test_trigger_update_rejects_internal_routing_fields(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="user-trigger",
        type="interval",
        config={"minutes": 30, "_origin_user_id": str(uuid.uuid4())},
        reason="old",
        is_enabled=True,
        is_system=False,
        max_fires=None,
        cooldown_seconds=0,
        expires_at=None,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    with pytest.raises(HTTPException) as exc:
        await triggers_api.update_trigger(
            agent_id,
            trigger.id,
            triggers_api.TriggerUpdate(
                config={"_origin_user_id": str(uuid.uuid4())},
            ),
            SimpleNamespace(),
        )

    assert exc.value.status_code == 400
    assert "_origin_user_id" in exc.value.detail
    assert session.committed is False


@pytest.mark.asyncio
async def test_webhook_update_preserves_redacted_secret(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        type="webhook",
        config={"token": "stable-token", "secret": "private-secret"},
        reason="old",
        is_enabled=True,
        max_fires=None,
        cooldown_seconds=0,
        expires_at=None,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    await triggers_api.update_trigger(
        agent_id,
        trigger.id,
        triggers_api.TriggerUpdate(
            config={"token": "", "secret": triggers_api.REDACTED_TRIGGER_SECRET, "custom": "value"},
        ),
        SimpleNamespace(),
    )

    assert trigger.config == {
        "token": "stable-token",
        "secret": "private-secret",
        "custom": "value",
    }
    assert session.committed is True


@pytest.mark.asyncio
async def test_legacy_unsigned_webhook_cannot_be_enabled(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        type="webhook",
        config={"token": "legacy-token"},
        reason="old",
        is_enabled=False,
        max_fires=None,
        cooldown_seconds=0,
        expires_at=None,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    with pytest.raises(HTTPException) as exc:
        await triggers_api.update_trigger(
            agent_id,
            trigger.id,
            triggers_api.TriggerUpdate(is_enabled=True),
            SimpleNamespace(),
        )

    assert exc.value.status_code == 400
    assert session.committed is False


@pytest.mark.asyncio
async def test_internal_a2a_trigger_cannot_be_modified(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name=triggers_api.INTERNAL_A2A_TRIGGER_NAME,
        type="a2a",
        config={},
        reason="internal",
        is_enabled=True,
        is_system=True,
        max_fires=None,
        cooldown_seconds=0,
        expires_at=None,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    with pytest.raises(HTTPException) as exc:
        await triggers_api.update_trigger(
            agent_id,
            trigger.id,
            triggers_api.TriggerUpdate(is_enabled=False),
            SimpleNamespace(),
        )

    assert exc.value.status_code == 403
    assert trigger.is_enabled is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_system_trigger_cannot_be_deleted(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="daily_okr_report",
        is_system=True,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    with pytest.raises(HTTPException) as exc:
        await triggers_api.delete_trigger(
            agent_id,
            trigger.id,
            SimpleNamespace(),
        )

    assert exc.value.status_code == 403
    assert session.committed is False


@pytest.mark.asyncio
async def test_system_trigger_only_allows_enable_disable(monkeypatch):
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="daily_okr_report",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="system",
        is_enabled=True,
        is_system=True,
        max_fires=None,
        cooldown_seconds=0,
        expires_at=None,
    )
    session = MutableTriggerSession(trigger)

    async def grant_manage(*_args):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(triggers_api, "async_session", FakeSessionFactory(session))
    monkeypatch.setattr(triggers_api, "check_agent_access", grant_manage)

    await triggers_api.update_trigger(
        agent_id,
        trigger.id,
        triggers_api.TriggerUpdate(is_enabled=False),
        SimpleNamespace(),
    )

    assert trigger.is_enabled is False
    assert session.committed is True
