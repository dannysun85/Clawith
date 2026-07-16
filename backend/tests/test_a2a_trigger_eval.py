import uuid
import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, AsyncMock, patch

from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.evaluator import check_new_agent_messages

class DummyResult:
    def __init__(self, values=None, scalar_value=None, scalars_list=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value
        self._scalars_list = scalars_list

    def scalar_one_or_none(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def first(self):
        if self._scalars_list is not None:
            return self._scalars_list[0] if self._scalars_list else None
        return self._values[0] if self._values else None

    def all(self):
        return list(self._scalars_list or self._values)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.statements = []
        self.committed = False
        self.flushed = False

    async def execute(self, _statement, _params=None):
        self.statements.append(_statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_check_new_agent_messages_matches_user_role():
    """Verify check_new_agent_messages matches messages from agent with role='user'."""
    agent_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    
    # Mock source agent
    source_agent = MagicMock()
    source_agent.id = source_agent_id
    source_agent.name = "Ray"
    source_agent.tenant_id = tenant_id
    target_agent = MagicMock()
    target_agent.id = agent_id
    target_agent.tenant_id = tenant_id

    # Mock chat message
    chat_message = MagicMock()
    chat_message.id = uuid.uuid4()
    chat_message.content = "Designed the logo"
    chat_message.role = "user"  # Role is user
    chat_message.conversation_id = conversation_id

    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test_trigger",
        type="on_message",
        config={
            "from_agent_name": "Ray",
            "from_agent_id": str(source_agent_id),
            "expected_conversation_id": conversation_id,
            "_origin_user_id": str(owner_id),
            "_server_context_version": 1,
        },
        is_enabled=True,
        created_at=datetime.now(UTC),
        fire_count=0,
    )

    db = RecordingDB(responses=[
        DummyResult(scalar_value=target_agent),  # Target Agent lookup
        DummyResult(scalar_value=source_agent),  # AgentModel lookup by stable id
        DummyResult(scalars_list=[MagicMock()]),  # Exact active relationship
        DummyResult(scalar_value=participant_id),  # Participant lookup
        DummyResult(scalar_value=chat_message),    # ChatMessage lookup
    ])

    with (
        patch("app.services.trigger_runtime.evaluator.async_session") as mock_session_ctx,
        patch(
            "app.core.permissions.get_agent_access_level_for_user_id",
            new_callable=AsyncMock,
            return_value="manage",
        ),
        patch(
            "app.core.permissions.evaluate_agent_relationship_status",
            new_callable=AsyncMock,
            return_value={"access_status": "active"},
        ),
    ):
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await check_new_agent_messages(trigger)

    assert result is True
    assert trigger.config["_matched_message"] == "Designed the logo"
    assert trigger.config["_matched_from"] == "Ray"
    assert trigger.config["_matched_from_agent_id"] == str(source_agent_id)
    assert trigger.config["_matched_message_id"] == str(chat_message.id)
    assert tenant_id in db.statements[1].compile().params.values()
    assert conversation_id in db.statements[-1].compile().params.values()
    assert agent_id in db.statements[-1].compile().params.values()
    assert source_agent_id in db.statements[-1].compile().params.values()
    assert owner_id in db.statements[-1].compile().params.values()
    assert "agent" in db.statements[-1].compile().params.values()
    assert trigger.config["_matched_conversation_id"] == conversation_id
    assert trigger.config["_match_context_version"] == 1


@pytest.mark.asyncio
async def test_check_new_agent_messages_rejects_unbound_agent_watch():
    agent_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    target_agent = MagicMock()
    target_agent.id = agent_id
    target_agent.tenant_id = tenant_id
    source_agent = MagicMock()
    source_agent.id = source_agent_id
    source_agent.name = "Ray"
    source_agent.tenant_id = tenant_id
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="legacy_unbound",
        type="on_message",
        config={"from_agent_id": str(source_agent_id)},
        is_enabled=True,
        created_at=datetime.now(UTC),
        fire_count=0,
    )
    db = RecordingDB(
        responses=[
            DummyResult(scalar_value=target_agent),
            DummyResult(scalar_value=source_agent),
        ]
    )

    with patch("app.services.trigger_runtime.evaluator.async_session") as session:
        session.return_value.__aenter__ = AsyncMock(return_value=db)
        session.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await check_new_agent_messages(trigger) is False

    assert len(db.statements) == 0
    assert "_matched_message" not in trigger.config
