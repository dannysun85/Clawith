import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import enterprise, organization, users
from app.schemas.schemas import LLMModelCreate, UserUpdate


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value


class FakeDB:
    def __init__(self, value):
        self.value = value
        self.flushed = False
        self.committed = False
        self.statements = []
        self.added = []

    async def execute(self, _statement):
        self.statements.append(_statement)
        return ScalarResult(self.value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    def add(self, value):
        self.added.append(value)


class SequenceDB(FakeDB):
    def __init__(self, values):
        super().__init__(None)
        self.values = list(values)

    async def execute(self, _statement):
        self.statements.append(_statement)
        value = self.values.pop(0) if self.values else None
        return ScalarResult(value)


@pytest.fixture(autouse=True)
def _stub_identity_login_namespace(monkeypatch):
    """These API policy tests isolate the shared namespace service."""
    monkeypatch.setattr(
        organization,
        "validate_identity_login_namespace",
        AsyncMock(),
    )


@pytest.mark.asyncio
async def test_org_admin_cannot_appoint_another_org_admin():
    tenant_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)
    target = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    db = FakeDB(target)

    with pytest.raises(HTTPException) as exc:
        await users.update_user_role(
            target.id,
            users.RoleUpdate(role="org_admin"),
            actor,
            db,
        )

    assert exc.value.status_code == 400
    assert target.role == "member"
    assert db.committed is False


@pytest.mark.asyncio
async def test_org_owner_can_appoint_org_admin_inside_company():
    tenant_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_owner", tenant_id=tenant_id)
    target = SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        tenant_id=tenant_id,
        is_active=True,
    )
    tenant = SimpleNamespace(id=tenant_id, owner_user_id=actor.id)
    db = SequenceDB([target, tenant])

    result = await users.update_user_role(
        target.id,
        users.RoleUpdate(role="org_admin"),
        actor,
        db,
    )

    assert result["role"] == "org_admin"
    assert target.role == "org_admin"
    assert db.committed is True


@pytest.mark.asyncio
async def test_org_admin_cannot_assign_legacy_agent_admin_role():
    actor = SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=uuid.uuid4(),
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        tenant_id=actor.tenant_id,
        is_active=True,
    )
    db = FakeDB(target)

    with pytest.raises(HTTPException) as exc:
        await users.update_user_role(
            target.id,
            users.RoleUpdate(role="agent_admin"),
            actor,
            db,
        )

    assert exc.value.status_code == 400
    assert target.role == "member"
    assert db.committed is False


@pytest.mark.asyncio
async def test_platform_operator_cannot_mutate_tenant_role_without_membership():
    tenant_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None)
    target = SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=tenant_id,
        is_active=False,
    )
    db = FakeDB(target)

    with pytest.raises(HTTPException) as exc:
        await users.update_user_role(
            target.id,
            users.RoleUpdate(role="member"),
            actor,
            db,
        )

    assert exc.value.status_code == 403
    assert target.role == "org_admin"
    assert len(db.statements) == 0


@pytest.mark.asyncio
async def test_owner_role_is_changed_only_by_ownership_transfer():
    tenant_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_owner", tenant_id=tenant_id)
    target = SimpleNamespace(
        id=actor.id,
        role="org_owner",
        tenant_id=tenant_id,
        is_active=True,
    )
    tenant = SimpleNamespace(id=tenant_id, owner_user_id=target.id)
    db = SequenceDB([target, tenant])

    with pytest.raises(HTTPException) as exc:
        await users.update_user_role(
            target.id,
            users.RoleUpdate(role="member"),
            actor,
            db,
        )

    assert exc.value.status_code == 409
    assert target.role == "org_owner"


@pytest.mark.asyncio
async def test_org_admin_cannot_edit_user_from_another_tenant():
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=uuid.uuid4())
    target = SimpleNamespace(
        id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(),
        email="target@example.com", primary_mobile=None,
    )
    db = FakeDB(target)

    with pytest.raises(HTTPException) as exc:
        await organization.admin_update_user(
            target.id,
            UserUpdate(display_name="Compromised"),
            actor,
            db,
        )

    assert exc.value.status_code == 403
    assert not hasattr(target, "display_name")
    assert db.flushed is False


@pytest.mark.asyncio
async def test_org_admin_cannot_change_global_identity_email(monkeypatch):
    tenant_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)
    identity = SimpleNamespace(
        id=identity_id,
        email="old@example.com",
        username="member",
        phone=None,
        email_verified=True,
        auth_version=0,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        identity=identity,
        role="member",
        tenant_id=tenant_id,
        email="old@example.com",
        primary_mobile=None,
    )
    db = SequenceDB([target, identity])

    from app.services.email_verification_service import email_verification_service
    from app.services.registration_service import registration_service

    invalidate = AsyncMock()
    monkeypatch.setattr(
        email_verification_service,
        "invalidate_email_verification_tokens",
        invalidate,
    )
    monkeypatch.setattr(
        registration_service,
        "sync_org_member_contact_from_user",
        AsyncMock(),
    )
    monkeypatch.setattr(
        organization.UserOut,
        "model_validate",
        lambda value: value,
    )

    with pytest.raises(HTTPException) as exc:
        await organization.admin_update_user(
            target.id,
            UserUpdate(email="new@example.com"),
            actor,
            db,
        )

    assert exc.value.status_code == 403
    assert identity.email == "old@example.com"
    assert identity.email_verified is True
    invalidate.assert_not_awaited()
    assert db.flushed is False


@pytest.mark.asyncio
async def test_platform_operator_cannot_change_company_member_identity_without_membership():
    tenant_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    actor = SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        tenant_id=None,
        identity=SimpleNamespace(is_platform_admin=True),
    )
    identity = SimpleNamespace(
        id=identity_id,
        email="old@example.com",
        username="member",
        phone=None,
        email_verified=True,
        auth_version=0,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        identity=identity,
        role="member",
        tenant_id=tenant_id,
        email="old@example.com",
        primary_mobile=None,
    )
    db = SequenceDB([target, identity, None])

    with pytest.raises(HTTPException) as exc:
        await organization.admin_update_user(
            target.id,
            UserUpdate(email="new@example.com"),
            actor,
            db,
        )

    assert exc.value.status_code == 403
    assert identity.email == "old@example.com"
    assert identity.email_verified is True
    assert identity.auth_version == 0
    assert db.statements == []
    assert db.flushed is False


@pytest.mark.asyncio
async def test_org_admin_full_form_with_unchanged_global_fields_updates_profile(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)
    identity = SimpleNamespace(
        id=identity_id,
        email="member@example.com",
        username="member",
        phone="13800138000",
        email_verified=True,
        auth_version=0,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        identity=identity,
        role="member",
        tenant_id=tenant_id,
        email=identity.email,
        username=identity.username,
        primary_mobile=identity.phone,
        display_name="Old Name",
    )
    db = SequenceDB([target, identity])

    from app.services import password_reset_service
    from app.services.email_verification_service import email_verification_service

    invalidate_email = AsyncMock()
    invalidate_reset = AsyncMock()
    monkeypatch.setattr(
        email_verification_service,
        "invalidate_email_verification_tokens",
        invalidate_email,
    )
    monkeypatch.setattr(
        password_reset_service,
        "invalidate_password_reset_tokens",
        invalidate_reset,
    )
    monkeypatch.setattr(organization.UserOut, "model_validate", lambda value: value)

    result = await organization.admin_update_user(
        target.id,
        UserUpdate(
            email=identity.email,
            username=identity.username,
            primary_mobile=identity.phone,
            display_name="New Name",
        ),
        actor,
        db,
    )

    assert result is target
    assert target.display_name == "New Name"
    assert identity.email_verified is True
    assert identity.auth_version == 0
    invalidate_email.assert_not_awaited()
    invalidate_reset.assert_not_awaited()
    assert db.flushed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_status"),
    [("member", 403), ("platform_admin", 403)],
)
async def test_tenantless_user_cannot_list_every_organization(role, expected_status):
    actor = SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        tenant_id=None,
        identity=SimpleNamespace(is_platform_admin=role == "platform_admin"),
    )
    db = FakeDB(None)

    with pytest.raises(HTTPException) as exc:
        await organization.list_users(current_user=actor, db=db)

    assert exc.value.status_code == expected_status


def test_org_admin_cannot_manage_any_real_model():
    actor = SimpleNamespace(
        id=uuid.uuid4(), role="org_admin", tenant_id=uuid.uuid4(), identity=None,
    )

    with pytest.raises(HTTPException) as exc:
        enterprise._require_platform_model_admin(actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_list_real_model_catalog():
    actor = SimpleNamespace(
        id=uuid.uuid4(), role="org_admin", tenant_id=uuid.uuid4(), identity=None,
    )

    with pytest.raises(HTTPException) as exc:
        await enterprise.list_llm_models(current_user=actor, db=FakeDB(None))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_create_platform_model():
    actor = SimpleNamespace(
        id=uuid.uuid4(), role="org_admin", tenant_id=uuid.uuid4(), identity=None,
    )
    data = LLMModelCreate(
        provider="deepseek",
        model="deepseek-chat",
        label="Foreign platform model",
        max_output_tokens=8192,
    )

    with pytest.raises(HTTPException) as exc:
        await enterprise.add_llm_model(
            data,
            platform=True,
            current_user=actor,
            db=FakeDB(None),
        )

    assert exc.value.status_code == 403
