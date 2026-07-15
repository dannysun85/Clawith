import json
import uuid

import pytest

from app.services.chat_session_service import (
    build_persisted_trigger_notification,
    save_tool_call_log,
)


class RecordingDB:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class RecordingDBContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_trigger_notification_uses_the_persisted_message_id():
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())

    message, payload = build_persisted_trigger_notification(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        content="scheduled result",
        triggers=["schedule"],
    )

    assert payload == {
        "type": "trigger_notification",
        "content": "scheduled result",
        "triggers": ["schedule"],
        "session_id": conversation_id,
        "message_id": str(message.id),
    }
    assert message.agent_id == agent_id
    assert message.user_id == user_id
    assert message.conversation_id == conversation_id
    assert message.role == "assistant"


@pytest.mark.asyncio
async def test_tool_call_log_returns_the_persisted_message_id(monkeypatch):
    db = RecordingDB()
    monkeypatch.setattr(
        "app.database.async_session",
        lambda: RecordingDBContext(db),
    )

    message_id = await save_tool_call_log(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=str(uuid.uuid4()),
        tool_name="example_tool",
        arguments={"value": 1},
        result="ready",
        tool_call_id="call-1",
    )

    assert db.committed is True
    assert len(db.added) == 1
    assert message_id == str(db.added[0].id)
    assert json.loads(db.added[0].content) == {
        "name": "example_tool",
        "args": {"value": 1},
        "status": "done",
        "result": "ready",
        "tool_call_id": "call-1",
        "reasoning_content": None,
    }
