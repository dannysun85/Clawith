"""Company onboarding identity and idempotency contracts."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api import onboarding as onboarding_api
from app.models.onboarding import UserTenantOnboarding


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RecordingDB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.commit = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.results.pop(0))


def _user():
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())


def _row(user, **overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "entry_mode": "create",
        "current_step": "assistant",
        "status": "in_progress",
        "personal_assistant_agent_id": None,
    }
    values.update(overrides)
    return UserTenantOnboarding(**values)


@pytest.mark.asyncio
async def test_locked_ensure_row_preserves_the_recorded_entry_mode():
    user = _user()
    row = _row(user, entry_mode="create")
    db = _RecordingDB(row)

    actual = await onboarding_api._ensure_row(db, user, None, lock=True)

    assert actual is row
    assert row.entry_mode == "create"
    assert db.statements[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_create_personal_assistant_reuses_the_locked_existing_companion():
    user = _user()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Astra")
    row = _row(user, personal_assistant_agent_id=agent.id)
    db = _RecordingDB(row, agent)
    request = onboarding_api.PersonalAssistantRequest(name="Replacement")

    with patch.object(
        onboarding_api,
        "_create_personal_assistant",
        AsyncMock(),
    ) as create_agent:
        payload = await onboarding_api.create_personal_assistant(
            request,
            current_user=user,
            db=db,
        )

    create_agent.assert_not_awaited()
    db.commit.assert_awaited_once()
    assert db.statements[0]._for_update_arg is not None
    assert row.entry_mode == "create"
    assert row.current_step == "opening"
    assert payload["agent"] == {"id": str(agent.id), "name": "Astra"}
