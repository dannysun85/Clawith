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
async def test_first_joiner_stays_member_even_when_company_has_no_admin(monkeypatch):
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
        owner_user_id=uuid.uuid4(),
        owner_resolution_required=False,
        logo_url=None,
        created_at=None,
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
    )
    from app.models.identity_governance import OrganizationJoinLink
    from app.services.identity_governance import ResolvedOrganizationCredential

    invitation = OrganizationJoinLink(
        tenant_id=tenant_id,
        token_hash="a" * 64,
        token_prefix="JOIN-TEST",
        used_count=0,
        max_uses=10,
        status="active",
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=uuid.uuid4(),
        tenant_id=None,
        role="member",
        display_name="New Member",
        is_active=True,
    )
    identity = SimpleNamespace(id=user.identity_id, email="member@example.com", is_active=True, auth_version=0)
    db = _DB([tenant, None])
    monkeypatch.setattr(
        tenants,
        "_lock_current_membership",
        AsyncMock(return_value=(user, identity)),
    )
    bind_member = AsyncMock()
    monkeypatch.setattr(registration_service, "bind_org_member", bind_member)
    monkeypatch.setattr(
        "app.services.identity_governance.resolve_organization_credential",
        AsyncMock(
            return_value=ResolvedOrganizationCredential(
                kind="organization_join_link",
                tenant_id=tenant_id,
                role="member",
                record=invitation,
            )
        ),
    )

    result = await tenants.join_company(
        tenants.JoinRequest(invitation_code="INVITE-CODE"),
        current_user=user,
        db=db,
    )

    assert result.role == "member"
    assert user.role == "member"
    assert invitation.used_count == 1
    assert db.committed is True
    bind_member.assert_awaited_once_with(user)
