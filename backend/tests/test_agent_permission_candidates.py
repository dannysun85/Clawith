import uuid
import pytest
from types import SimpleNamespace

from app.api import agents as agents_api
from app.models.org import OrgMember
from app.models.user import User, Identity


class DummyResult:
    def __init__(self, values=None):
        self._values = list(values or [])

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed_sql = []
        self.added = []
        self.committed = False

    async def execute(self, statement, params=None):
        self.executed_sql.append(str(statement))
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_get_agent_permission_candidates_is_read_only_and_skips_unlinked_members(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    current_user = User(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    # Mock access check
    async def fake_check_access(_db, _user, _agent_id, **kwargs):
        assert kwargs["required_level"] == "manage"
        return agent, "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_access)

    # Only a membership that is already linked to an active platform User can
    # receive an Agent object grant. A GET must never create that User.
    member_1 = OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Member One",
        status="active",
        user_id=uuid.uuid4(),
    )
    member_2 = OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Member Two",
        status="active",
        user_id=None,
        email="member2@example.com",
    )

    # Target users
    identity_1 = Identity(username="member_one", email="member1@example.com")
    user_1 = User(id=member_1.user_id, identity=identity_1, tenant_id=tenant_id)

    # Database responses:
    # 1. members query: returns member_1 and member_2
    # 2. batch load of linked users (only member_1.user_id): returns user_1
    db = RecordingDB(responses=[
        DummyResult([member_1, member_2]),
        DummyResult([user_1]),
    ])

    result = await agents_api.get_agent_permission_candidates(
        agent_id=agent_id,
        search=None,
        current_user=current_user,
        db=db,
    )

    assert len(result["users"]) == 1
    assert result["users"][0]["name"] == "Member One"
    assert result["users"][0]["username"] == "member_one"
    assert db.added == []
    assert db.committed is False
