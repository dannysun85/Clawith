"""Astra model-selection invariants on the unified v1.11 Direct Chat API."""

from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import chat_sessions


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, *values):
        self.values = list(values)
        self.committed = False

    async def execute(self, _statement):
        if not self.values:
            raise AssertionError("unexpected database query")
        return _Result(self.values.pop(0))

    async def commit(self):
        self.committed = True

    async def refresh(self, _value):
        return None


def _user(tenant_id, *, tier="ultra", revision=3):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        display_name="Owner",
        avatar_url=None,
        preferred_chat_tier=tier,
        preferred_chat_tier_revision=revision,
    )


def _agent(tenant_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        preferred_tier="lite",
        preferred_modality="text",
    )


@pytest.mark.asyncio
async def test_create_snapshots_cross_agent_user_tier_into_runtime_session(monkeypatch):
    tenant_id = uuid.uuid4()
    user = _user(tenant_id)
    agent = _agent(tenant_id)
    db = _DB(user)
    created = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent.id,
        user_id=user.id,
        source_channel="web",
        title="Topic",
        created_at=datetime.now(UTC),
        last_message_at=None,
        is_primary=False,
        peer_agent_id=None,
        model_tier="ultra",
        model_modality="text",
    )
    captured = {}

    async def access(_db, _user, _agent_id):
        return agent, tenant_id, "owner"

    async def participant(*_args):
        return SimpleNamespace(id=uuid.uuid4())

    async def resolve(_agent, tier, modality, *, strict):
        assert (tier, modality, strict) == ("ultra", "text", False)
        return "ultra", "text"

    async def create(_db, **kwargs):
        captured.update(kwargs)
        return created

    monkeypatch.setattr(chat_sessions, "_check_direct_agent_access", access)
    monkeypatch.setattr(chat_sessions, "get_or_create_user_participant", participant)
    monkeypatch.setattr(chat_sessions, "_resolve_session_model_selection", resolve)
    monkeypatch.setattr(chat_sessions, "create_direct_session", create)

    result = await chat_sessions.create_session(
        agent.id,
        chat_sessions.CreateSessionIn(title="Topic"),
        user,
        db,
    )

    assert captured["model_tier"] == "ultra"
    assert captured["model_modality"] == "text"
    assert result.model_tier == "ultra"
    assert user.preferred_chat_tier == "ultra"


@pytest.mark.asyncio
async def test_patch_model_selection_updates_session_and_user_with_revision_cas(monkeypatch):
    tenant_id = uuid.uuid4()
    user = _user(tenant_id, tier="lite", revision=4)
    agent = _agent(tenant_id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_type="direct",
        agent_id=agent.id,
        user_id=user.id,
        source_channel="web",
        title="Chat",
        model_tier="lite",
        model_modality="text",
        updated_at=None,
    )
    db = _DB(session, user)

    async def access(_db, _user, _agent_id):
        return agent, tenant_id, "owner"

    async def resolve(_agent, tier, modality, *, strict):
        return (tier or "lite", modality or "text")

    monkeypatch.setattr(chat_sessions, "_check_direct_agent_access", access)
    monkeypatch.setattr(chat_sessions, "_resolve_session_model_selection", resolve)

    result = await chat_sessions.rename_session(
        agent.id,
        session.id,
        chat_sessions.PatchSessionIn(
            model_tier="ultra",
            model_modality="image",
            preference_revision=4,
        ),
        user,
        db,
    )

    assert session.model_tier == "ultra"
    assert session.model_modality == "image"
    assert user.preferred_chat_tier == "ultra"
    assert user.preferred_chat_tier_revision == 5
    assert result["preferred_chat_tier_revision"] == 5
    assert db.committed is True


@pytest.mark.asyncio
async def test_stale_model_selection_revision_fails_without_mutation(monkeypatch):
    tenant_id = uuid.uuid4()
    user = _user(tenant_id, tier="pro", revision=8)
    agent = _agent(tenant_id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_type="direct",
        agent_id=agent.id,
        user_id=user.id,
        source_channel="web",
        title="Chat",
        model_tier="pro",
        model_modality="text",
        updated_at=None,
    )
    db = _DB(session, user)

    async def access(_db, _user, _agent_id):
        return agent, tenant_id, "owner"

    async def resolve(_agent, tier, modality, *, strict):
        return (tier or "pro", modality or "text")

    monkeypatch.setattr(chat_sessions, "_check_direct_agent_access", access)
    monkeypatch.setattr(chat_sessions, "_resolve_session_model_selection", resolve)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions.rename_session(
            agent.id,
            session.id,
            chat_sessions.PatchSessionIn(
                model_tier="ultra",
                preference_revision=7,
            ),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert session.model_tier == "pro"
    assert user.preferred_chat_tier_revision == 8
    assert db.committed is False
