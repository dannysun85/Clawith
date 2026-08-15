"""Independent Identity, membership, surface, and Agent-object contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.core import permissions, security
from app.services import access_control


def _identity(*, platform_operator: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="person@example.com",
        is_active=True,
        is_platform_admin=platform_operator,
        auth_version=3,
    )


def _user(
    *,
    role: str = "member",
    tenant_id: uuid.UUID | None = None,
    platform_operator: bool = False,
):
    identity = _identity(platform_operator=platform_operator)
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        role=role,
        tenant_id=tenant_id,
        is_active=True,
    )


def _agent(user, *, access_mode: str = "custom", creator_id=None, tenant_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else user.tenant_id,
        creator_id=creator_id or uuid.uuid4(),
        access_mode=access_mode,
        deleted_at=None,
        is_expired=False,
        expires_at=None,
        status="running",
    )


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _PermissionDB:
    def __init__(self, *values):
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.values.pop(0) if self.values else None)


@pytest.mark.parametrize(
    ("role", "tenant", "expected"),
    [
        ("member", True, "member"),
        ("org_admin", True, "org_admin"),
        ("org_owner", True, "org_owner"),
        ("agent_admin", True, "member"),
        ("platform_admin", True, "member"),
        ("org_owner", False, None),
        ("platform_admin", False, None),
    ],
)
def test_membership_role_is_independent_from_legacy_or_global_role(role, tenant, expected):
    user = _user(role=role, tenant_id=uuid.uuid4() if tenant else None)

    assert access_control.normalized_membership_role(user) == expected


@pytest.mark.asyncio
async def test_company_and_platform_dependencies_are_independent():
    company_owner = _user(role="org_owner", tenant_id=uuid.uuid4())
    platform_only = _user(role="platform_admin", platform_operator=True)
    dual_scope = _user(
        role="org_admin",
        tenant_id=uuid.uuid4(),
        platform_operator=True,
    )

    assert await security.get_company_governor(company_owner) is company_owner
    assert await security.get_platform_operator(platform_only) is platform_only
    assert await security.get_company_governor(dual_scope) is dual_scope
    assert await security.get_platform_operator(dual_scope) is dual_scope

    with pytest.raises(HTTPException) as company_error:
        await security.get_company_governor(platform_only)
    assert company_error.value.status_code == 403
    assert company_error.value.detail["code"] == "company_governance_required"

    with pytest.raises(HTTPException) as platform_error:
        await security.get_platform_operator(company_owner)
    assert platform_error.value.status_code == 403
    assert platform_error.value.detail["code"] == "platform_operator_required"


@pytest.mark.asyncio
async def test_effective_access_projects_surfaces_without_role_inheritance(monkeypatch):
    user = _user(
        role="platform_admin",
        tenant_id=uuid.uuid4(),
        platform_operator=True,
    )
    monkeypatch.setattr(
        access_control,
        "_identity_capabilities",
        AsyncMock(return_value={"company.create"}),
    )
    monkeypatch.setattr(access_control, "_has_managed_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(
        access_control,
        "_member_private_agent_creation_allowed",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        access_control,
        "_pending_invitation_count",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        access_control,
        "_active_support_session",
        AsyncMock(return_value={"id": "support-session"}),
    )

    resolved = await access_control.resolve_effective_access(SimpleNamespace(), user)

    assert resolved.membership_id == user.id
    assert resolved.membership_role == "member"
    assert resolved.global_roles == ("platform_operator",)
    assert resolved.available_surfaces == ("work", "platform_admin")
    assert "work.use" in resolved.effective_capabilities
    assert "agent.manage.assigned" in resolved.effective_capabilities
    assert "company.create" in resolved.effective_capabilities
    assert "platform.tenants.manage" in resolved.effective_capabilities
    assert "company.settings.manage" not in resolved.effective_capabilities
    assert resolved.pending_invitation_count == 2
    assert resolved.current_support_session == {"id": "support-session"}


@pytest.mark.asyncio
async def test_owner_gets_company_surface_but_not_platform_surface(monkeypatch):
    owner = _user(role="org_owner", tenant_id=uuid.uuid4())
    monkeypatch.setattr(access_control, "_identity_capabilities", AsyncMock(return_value=set()))
    monkeypatch.setattr(access_control, "_has_managed_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(access_control, "_pending_invitation_count", AsyncMock(return_value=0))

    resolved = await access_control.resolve_effective_access(SimpleNamespace(), owner)

    assert resolved.available_surfaces == ("work", "company_admin")
    assert "company.settings.manage" in resolved.effective_capabilities
    assert "company.ownership.transfer" in resolved.effective_capabilities
    assert not any(value.startswith("platform.") for value in resolved.effective_capabilities)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_enabled", [False, True])
async def test_member_private_agent_creation_capability_follows_company_policy(
    monkeypatch,
    policy_enabled,
):
    member = _user(role="member", tenant_id=uuid.uuid4())
    monkeypatch.setattr(access_control, "_identity_capabilities", AsyncMock(return_value=set()))
    monkeypatch.setattr(access_control, "_has_managed_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(
        access_control,
        "_member_private_agent_creation_allowed",
        AsyncMock(return_value=policy_enabled),
    )
    monkeypatch.setattr(access_control, "_pending_invitation_count", AsyncMock(return_value=0))

    resolved = await access_control.resolve_effective_access(SimpleNamespace(), member)

    assert ("agent.create.private" in resolved.effective_capabilities) is policy_enabled


@pytest.mark.asyncio
async def test_private_agent_stays_creator_only_even_with_admin_or_explicit_grant():
    tenant_id = uuid.uuid4()
    creator = _user(tenant_id=tenant_id)
    company_admin = _user(role="org_admin", tenant_id=tenant_id)
    delegated = _user(role="agent_admin", tenant_id=tenant_id)
    private_agent = _agent(creator, access_mode="private", creator_id=creator.id)

    assert await permissions.can_manage_agent(_PermissionDB(), creator, private_agent)
    assert not await permissions.can_manage_agent(_PermissionDB(object()), company_admin, private_agent)
    assert not await permissions.can_manage_agent(_PermissionDB(object()), delegated, private_agent)
    assert not await permissions.can_use_agent(_PermissionDB(object()), delegated, private_agent)


@pytest.mark.asyncio
async def test_custom_agent_requires_exact_use_or_manage_object_grant():
    tenant_id = uuid.uuid4()
    delegated = _user(role="agent_admin", tenant_id=tenant_id)
    agent = _agent(delegated, access_mode="custom")

    assert not await permissions.can_manage_agent(_PermissionDB(None), delegated, agent)
    assert await permissions.can_manage_agent(_PermissionDB(object()), delegated, agent)
    assert await permissions.can_use_agent(_PermissionDB(object()), delegated, agent)


@pytest.mark.asyncio
async def test_use_grant_cannot_cross_manage_boundary(monkeypatch):
    tenant_id = uuid.uuid4()
    user = _user(tenant_id=tenant_id)
    agent = _agent(user, access_mode="custom")
    db = _PermissionDB(agent)
    monkeypatch.setattr(permissions, "can_manage_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(permissions, "can_use_agent", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await permissions.check_agent_access(
            db,
            user,
            agent.id,
            required_level="manage",
            lock_authority=True,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Manage access is required for this Agent"
    assert "FOR UPDATE" in str(db.statements[0])


@pytest.mark.asyncio
async def test_cross_tenant_agent_id_is_rejected_before_object_grants(monkeypatch):
    user = _user(tenant_id=uuid.uuid4(), platform_operator=True)
    foreign_agent = _agent(user, tenant_id=uuid.uuid4())
    db = _PermissionDB(foreign_agent)
    manage = AsyncMock(return_value=True)
    use = AsyncMock(return_value=True)
    monkeypatch.setattr(permissions, "can_manage_agent", manage)
    monkeypatch.setattr(permissions, "can_use_agent", use)

    with pytest.raises(HTTPException) as exc:
        await permissions.check_agent_access(db, user, foreign_agent.id)

    assert exc.value.status_code == 403
    manage.assert_not_awaited()
    use.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_revocation_is_observed_on_the_next_access_check(monkeypatch):
    user = _user(tenant_id=uuid.uuid4())
    agent = _agent(user, access_mode="custom")
    db = _PermissionDB(agent, agent)
    manage = AsyncMock(side_effect=[True, False])
    use = AsyncMock(return_value=False)
    monkeypatch.setattr(permissions, "can_manage_agent", manage)
    monkeypatch.setattr(permissions, "can_use_agent", use)

    _, first_level = await permissions.check_agent_access(db, user, agent.id)
    assert first_level == "manage"

    with pytest.raises(HTTPException) as revoked:
        await permissions.check_agent_access(db, user, agent.id)
    assert revoked.value.status_code == 403
    assert manage.await_count == 2
