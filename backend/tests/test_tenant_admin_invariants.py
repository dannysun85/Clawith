import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import tenants
from app.services.registration_service import registration_service


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value


class _DB:
    def __init__(self, values):
        self.values = list(values)
        self.statements = []
        self.flushed = False
        self.committed = False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.values.pop(0))

    def add(self, _value):
        return None

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_first_joiner_ignores_disabled_admin_memberships(monkeypatch):
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        name="Available Company",
        slug="available-company",
        im_provider="web_only",
        timezone="UTC",
        country_region="001",
        is_active=True,
        sso_enabled=False,
        sso_domain=None,
        a2a_async_enabled=True,
        default_model_id=None,
        logo_url=None,
        created_at=None,
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
    )
    invitation = SimpleNamespace(
        tenant_id=tenant_id,
        used_count=0,
        max_uses=10,
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=uuid.uuid4(),
        tenant_id=None,
        role="member",
        display_name="New Member",
        is_active=True,
    )
    identity = SimpleNamespace(id=user.identity_id, is_active=True, auth_version=0)
    db = _DB([invitation, tenant, None, 0])
    monkeypatch.setattr(
        tenants,
        "_lock_current_membership",
        AsyncMock(return_value=(user, identity)),
    )
    bind_member = AsyncMock()
    monkeypatch.setattr(registration_service, "bind_org_member", bind_member)

    result = await tenants.join_company(
        tenants.JoinRequest(invitation_code="INVITE-CODE"),
        current_user=user,
        db=db,
    )

    assert result.role == "org_admin"
    assert user.role == "org_admin"
    assert "users.is_active IS true" in str(db.statements[3])
    assert invitation.used_count == 1
    assert db.committed is True
    bind_member.assert_awaited_once_with(user)
