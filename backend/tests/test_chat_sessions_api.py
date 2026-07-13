import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat_sessions as chat_sessions_api


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.committed = False
        self.refreshed = []
        self.deleted = []

    async def execute(self, _statement, _params=None):
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)

    async def delete(self, value):
        self.deleted.append(value)


@pytest.mark.asyncio
async def test_org_admin_can_list_all_sessions(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([agent]),
            DummyResult([session]),
            DummyResult([(str(session.id), 3)]),
            DummyResult([(owner_id, "Alice")]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    assert len(sessions) == 1
    assert sessions[0].id == str(session.id)
    assert sessions[0].user_id == str(owner_id)
    assert sessions[0].username == "Alice"


@pytest.mark.asyncio
async def test_creator_cannot_list_other_users_sessions(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    current_user = SimpleNamespace(id=creator_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    db = RecordingDB(responses=[DummyResult([agent])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.list_sessions(
            agent_id=agent_id,
            scope="all",
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_can_view_other_users_session_messages(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert messages == [
        {
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_creator_cannot_view_other_users_session_messages(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    current_user = SimpleNamespace(id=creator_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=other_user_id,
        source_channel="web",
    )
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.get_session_messages(
            agent_id=agent_id,
            session_id=session_id,
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_a2a_messages_include_stable_sender_identity(monkeypatch):
    user_id = uuid.uuid4()
    current_agent_id = uuid.uuid4()
    peer_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    current_participant_id = uuid.uuid4()
    peer_participant_id = uuid.uuid4()
    now = datetime.now(UTC)
    session = SimpleNamespace(
        id=session_id,
        agent_id=current_agent_id,
        peer_agent_id=peer_agent_id,
        user_id=user_id,
        source_channel="agent",
        is_group=False,
    )
    current_message = SimpleNamespace(
        role="user",
        content="from current",
        created_at=now,
        participant_id=current_participant_id,
    )
    peer_message = SimpleNamespace(
        role="user",
        content="from peer",
        created_at=now,
        participant_id=peer_participant_id,
    )
    db = RecordingDB(responses=[
        DummyResult([session]),
        DummyResult([peer_message, current_message]),
        DummyResult([
            (current_participant_id, "Same Name", current_agent_id),
            (peer_participant_id, "Same Name", peer_agent_id),
        ]),
    ])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=current_agent_id), "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=current_agent_id,
        session_id=session_id,
        current_user=SimpleNamespace(id=user_id, role="member"),
        db=db,
    )

    assert messages[0]["sender_agent_id"] == str(current_agent_id)
    assert messages[0]["is_current_agent"] is True
    assert messages[1]["sender_agent_id"] == str(peer_agent_id)
    assert messages[1]["is_current_agent"] is False


@pytest.mark.asyncio
async def test_create_session_returns_web_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    current_user = SimpleNamespace(id=user_id, role="member")
    db = RecordingDB()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    session = await chat_sessions_api.create_session(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert session.agent_id == str(agent_id)
    assert session.user_id == str(user_id)
    assert session.source_channel == "web"
    assert session.participant_type == "user"
    assert session.is_group is False
    assert session.model_tier is None
    assert session.model_modality is None
    assert db.committed is True
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_create_session_snapshots_agent_model_default(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=None,
        preferred_tier="ultra",
        preferred_modality="image",
    )
    db = RecordingDB()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    session = await chat_sessions_api.create_session(
        agent_id=agent_id,
        current_user=SimpleNamespace(id=user_id, role="member"),
        db=db,
    )

    assert session.model_tier == "ultra"
    assert session.model_modality == "image"
    assert db.added[0].model_tier == "ultra"
    assert db.added[0].model_modality == "image"


@pytest.mark.asyncio
async def test_session_owner_can_persist_model_selection(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=None,
        preferred_tier="lite",
        preferred_modality="text",
    )
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=user_id,
        title="Current chat",
        source_channel="web",
        is_group=False,
        model_tier="lite",
        model_modality="text",
    )
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    result = await chat_sessions_api.rename_session(
        agent_id=agent_id,
        session_id=session_id,
        body=chat_sessions_api.PatchSessionIn(model_tier="ultra", model_modality="image"),
        current_user=SimpleNamespace(id=user_id, role="member"),
        db=db,
    )

    assert result == {
        "id": str(session_id),
        "title": "Current chat",
        "model_tier": "ultra",
        "model_modality": "image",
    }
    assert session.model_tier == "ultra"
    assert session.model_modality == "image"
    assert db.committed is True


@pytest.mark.asyncio
async def test_admin_cannot_change_another_users_session_model_selection(monkeypatch):
    admin_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=None)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        title="Private chat",
        source_channel="web",
        is_group=False,
        model_tier="lite",
        model_modality="text",
    )
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.rename_session(
            agent_id=agent_id,
            session_id=session_id,
            body=chat_sessions_api.PatchSessionIn(model_tier="ultra"),
            current_user=SimpleNamespace(id=admin_id, role="org_admin"),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert session.model_tier == "lite"
    assert db.committed is False


@pytest.mark.asyncio
async def test_session_model_selection_rejects_tier_outside_plan(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=uuid.uuid4(),
        preferred_tier="lite",
        preferred_modality="text",
    )
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=user_id,
        title="Plan protected chat",
        source_channel="web",
        is_group=False,
        model_tier="lite",
        model_modality="text",
    )
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_resolve(_agent, _tier, _modality, *, strict):
        if strict:
            raise chat_sessions_api.InvalidAgentPlanSelection(
                "Tier 'ultra' is not included in your plan.",
                quota_type="model_tier",
            )
        return "lite", "text"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api, "_resolve_session_model_selection", fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.rename_session(
            agent_id=agent_id,
            session_id=session_id,
            body=chat_sessions_api.PatchSessionIn(model_tier="ultra"),
            current_user=SimpleNamespace(id=user_id, role="member"),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert "not included" in exc_info.value.detail
    assert session.model_tier == "lite"
    assert db.committed is False


@pytest.mark.asyncio
async def test_peer_agent_can_rename_a2a_session(monkeypatch):
    user_id = uuid.uuid4()
    owner_agent_id = uuid.uuid4()
    peer_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        agent_id=owner_agent_id,
        peer_agent_id=peer_agent_id,
        user_id=user_id,
        title="Old title",
    )
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=peer_agent_id, creator_id=user_id), "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    result = await chat_sessions_api.rename_session(
        agent_id=peer_agent_id,
        session_id=session_id,
        body=chat_sessions_api.PatchSessionIn(title="New title"),
        current_user=SimpleNamespace(id=user_id, role="member"),
        db=db,
    )

    assert result == {"id": str(session_id), "title": "New title"}
    assert session.title == "New title"
    assert db.committed is True


@pytest.mark.asyncio
async def test_peer_agent_can_delete_a2a_session_and_messages(monkeypatch):
    user_id = uuid.uuid4()
    owner_agent_id = uuid.uuid4()
    peer_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        agent_id=owner_agent_id,
        peer_agent_id=peer_agent_id,
        user_id=user_id,
    )
    db = RecordingDB(responses=[DummyResult([session]), DummyResult()])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=peer_agent_id, creator_id=user_id), "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    result = await chat_sessions_api.delete_session(
        agent_id=peer_agent_id,
        session_id=session_id,
        current_user=SimpleNamespace(id=user_id, role="member"),
        db=db,
    )

    assert result is None
    assert db.deleted == [session]
    assert db.committed is True
