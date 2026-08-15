"""Identity, invitation, membership, and ownership product contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
import uuid

from fastapi import BackgroundTasks, HTTPException
import pytest
from starlette.requests import Request

from app.api import auth as auth_api
from app.api import identity_governance as governance_api
from app.api import tenants
from app.api import users as users_api
from app.models.identity_governance import (
    OrganizationInvitation,
    OrganizationJoinLink,
    PlatformSupportSession,
    RegistrationGrant,
    TenantOwnershipTransfer,
)
from app.models.audit import AuditLog
from app.services.identity_governance import (
    GovernanceCredentialError,
    ResolvedOrganizationCredential,
    consume_organization_credential,
    governance_token_hash,
    issue_registration_grant,
    resolve_organization_invitation_by_id,
    resolve_registration_grant,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def first(self):
        return self.value

    def one(self):
        return self.value


class _SequenceDB:
    def __init__(self, *values, get_values=()):
        self.values = list(values)
        self.get_values = list(get_values)
        self.statements = []
        self.added = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _Result(self.values.pop(0))

    async def get(self, _model, _identifier):
        return self.get_values.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        for value in self.added:
            if isinstance(value, TenantOwnershipTransfer):
                if value.id is None:
                    value.id = uuid.uuid4()
                if value.created_at is None:
                    value.created_at = datetime.now(UTC)

    async def commit(self):
        self.commit_count += 1


def _tenant(*, owner_user_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme-123",
        im_provider="web_only",
        timezone="UTC",
        country_region="001",
        is_active=True,
        sso_enabled=False,
        sso_domain=None,
        a2a_async_enabled=True,
        default_model_id=None,
        logo_url=None,
        created_at=datetime.now(UTC),
        owner_user_id=owner_user_id,
        owner_resolution_required=False,
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
        deletion_requested_at=None,
        deletion_scheduled_for=None,
        deletion_requested_by_user_id=None,
    )


def _identity(*, verified: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="member@example.com",
        email_verified=verified,
        is_active=True,
        is_platform_admin=False,
        auth_version=0,
        password_login_enabled=True,
        password_hash="hashed",
    )


def _user(
    *,
    tenant_id: uuid.UUID | None,
    role: str = "member",
    verified: bool = True,
    registration_source: str = "web",
):
    identity = _identity(verified=verified)
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        tenant_id=tenant_id,
        role=role,
        display_name="Member",
        avatar_url=None,
        is_active=True,
        registration_source=registration_source,
    )


def _transfer(tenant_id: uuid.UUID, owner_id: uuid.UUID, target_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "current_owner_user_id": owner_id,
        "proposed_owner_user_id": target_id,
        "status": "pending",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "accepted_at": None,
        "cancelled_at": None,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


@pytest.mark.asyncio
async def test_my_tenants_preserves_each_membership_role(monkeypatch):
    identity_id = uuid.uuid4()
    owner_tenant = _tenant()
    admin_tenant = _tenant()
    owner = _user(tenant_id=owner_tenant.id, role="org_owner")
    admin = _user(tenant_id=admin_tenant.id, role="org_admin")
    owner.identity_id = identity_id
    admin.identity_id = identity_id

    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_id",
        AsyncMock(return_value=[owner, admin]),
    )
    monkeypatch.setattr(
        auth_api.tenant_dao,
        "get_by_ids",
        AsyncMock(return_value=[owner_tenant, admin_tenant]),
    )

    choices = await auth_api.get_my_tenants(
        current_user=SimpleNamespace(identity_id=identity_id),
    )

    assert [(choice.tenant_id, choice.membership_role) for choice in choices] == [
        (owner_tenant.id, "org_owner"),
        (admin_tenant.id, "org_admin"),
    ]


@pytest.mark.asyncio
async def test_company_owner_can_issue_org_admin_invitation(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    invitation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=company.id,
        target_email="admin@example.com",
        invited_role="org_admin",
        token_prefix="ORG-ADMIN",
        delivery_mode="email",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        accepted_at=None,
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        governance_api,
        "issue_organization_invitation",
        AsyncMock(return_value=SimpleNamespace(record=invitation, raw_token="ORG-ADMIN-TOKEN")),
    )
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        purpose="company_invitation",
        status="queued",
        recipient_mask="a***@example.com",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=datetime.now(UTC),
        last_error_code=None,
        smtp_accepted_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(governance_api, "enqueue_template_email", AsyncMock(return_value=delivery))
    monkeypatch.setattr(
        governance_api.platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://app.example.com"),
    )
    db = _SequenceDB(company)

    result = await governance_api.create_organization_invitation(
        company.id,
        governance_api.OrganizationInvitationCreate(
            email="admin@example.com",
            role="org_admin",
            expires_in_days=1,
        ),
        current_user=owner,
        db=db,
    )

    assert result["role"] == "org_admin"
    assert result["status"] == "pending"
    assert "token" not in result
    assert result["delivery_status"] == "queued"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_organization_invitation_idempotency_replays_without_new_secret(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    invitation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=company.id,
        target_email="member@example.com",
        invited_role="member",
        token_prefix="ORG-REPLAY",
        delivery_mode="email",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        accepted_at=None,
        created_at=datetime.now(UTC),
    )
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        purpose="company_invitation",
        status="smtp_accepted",
        recipient_mask="m***@example.com",
        attempt_count=1,
        max_attempts=5,
        next_attempt_at=None,
        last_error_code=None,
        smtp_accepted_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    issue = AsyncMock()
    monkeypatch.setattr(governance_api, "issue_organization_invitation", issue)
    db = _SequenceDB(company, None, (invitation, delivery))

    result = await governance_api.create_organization_invitation(
        company.id,
        governance_api.OrganizationInvitationCreate(email="member@example.com"),
        current_user=owner,
        db=db,
        request=_request(),
        idempotency_key="raw-client-key-must-not-be-stored",
    )

    assert result["id"] == str(invitation.id)
    assert result["delivery_status"] == "smtp_accepted"
    assert "token" not in result
    assert "raw-client-key-must-not-be-stored" not in governance_api._invitation_idempotency_key(
        company.id,
        "raw-client-key-must-not-be-stored",
        "create",
    )
    issue.assert_not_awaited()
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_resend_rotates_invitation_and_cancels_previous_delivery(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    old_invitation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=company.id,
        target_email="member@example.com",
        invited_role="member",
        token_prefix="ORG-OLD",
        delivery_mode="email",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        accepted_at=None,
        created_at=datetime.now(UTC),
    )
    replacement = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=company.id,
        target_email=old_invitation.target_email,
        invited_role="member",
        token_prefix="ORG-NEW",
        delivery_mode="email",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        accepted_at=None,
        created_at=datetime.now(UTC),
    )
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        purpose="company_invitation",
        status="queued",
        recipient_mask="m***@example.com",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=datetime.now(UTC),
        last_error_code=None,
        smtp_accepted_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(governance_api, "cancel_invitation_deliveries", cancel)
    monkeypatch.setattr(
        governance_api,
        "issue_organization_invitation",
        AsyncMock(
            return_value=SimpleNamespace(record=replacement, raw_token="ORG-NEW-SECRET")
        ),
    )
    monkeypatch.setattr(
        governance_api,
        "enqueue_template_email",
        AsyncMock(return_value=delivery),
    )
    monkeypatch.setattr(
        governance_api.platform_service,
        "get_public_base_url",
        AsyncMock(return_value="https://app.example.com"),
    )
    db = _SequenceDB(old_invitation, get_values=(company,))
    background_tasks = BackgroundTasks()

    result = await governance_api.resend_organization_invitation(
        request=_request(),
        background_tasks=background_tasks,
        tenant_id=company.id,
        invitation_id=old_invitation.id,
        current_user=owner,
        db=db,
    )

    cancel.assert_awaited_once_with(db, old_invitation.id)
    assert result["id"] == str(replacement.id)
    assert result["delivery_status"] == "queued"
    assert "token" not in result
    assert len(background_tasks.tasks) == 1
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_manual_invitation_link_requires_successful_password_reauthentication(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    monkeypatch.setattr(governance_api, "enforce_auth_rate_limit", AsyncMock())
    monkeypatch.setattr(governance_api, "verify_password_async", AsyncMock(return_value=False))
    db = _SequenceDB()

    with pytest.raises(HTTPException) as error:
        await governance_api.issue_organization_invitation_manual_link(
            request=_request(),
            tenant_id=owner.tenant_id,
            invitation_id=uuid.uuid4(),
            body=governance_api.OrganizationInvitationManualLink(current_password="wrong"),
            current_user=owner,
            db=db,
        )

    assert error.value.status_code == 401
    assert db.statements == []
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_invitation_rejects_cross_tenant_and_admin_role_escalation(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    foreign_tenant_id = uuid.uuid4()
    cross_tenant_db = _SequenceDB()
    with pytest.raises(HTTPException) as cross_tenant:
        await governance_api.create_organization_invitation(
            foreign_tenant_id,
            governance_api.OrganizationInvitationCreate(email="member@example.com"),
            current_user=owner,
            db=cross_tenant_db,
        )
    assert cross_tenant.value.status_code == 403
    assert cross_tenant_db.statements == []

    admin = _user(tenant_id=uuid.uuid4(), role="org_admin")
    company = _tenant(owner_user_id=uuid.uuid4())
    admin.tenant_id = company.id
    issue = AsyncMock()
    monkeypatch.setattr(governance_api, "issue_organization_invitation", issue)
    role_db = _SequenceDB(company)
    with pytest.raises(HTTPException) as escalation:
        await governance_api.create_organization_invitation(
            company.id,
            governance_api.OrganizationInvitationCreate(
                email="admin@example.com",
                role="org_admin",
            ),
            current_user=admin,
            db=role_db,
        )
    assert escalation.value.status_code == 403
    issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_grant_returns_raw_token_once_and_stores_only_hash():
    db = _SequenceDB()

    issued = await issue_registration_grant(
        db,
        max_uses=1,
        created_by_identity_id=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )

    assert issued.raw_token.startswith("REG-")
    assert issued.record.token_hash == governance_token_hash(issued.raw_token)
    assert issued.record.token_hash != issued.raw_token
    assert not hasattr(issued.record, "raw_token")
    assert issued.record.token_prefix == issued.raw_token[:12]


@pytest.mark.asyncio
async def test_registration_grant_expiry_and_replay_are_rejected():
    expired = RegistrationGrant(
        token_hash=governance_token_hash("REG-EXPIRED"),
        token_prefix="REG-EXPIRED",
        max_uses=1,
        used_count=0,
        status="active",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(GovernanceCredentialError) as expired_error:
        await resolve_registration_grant(_SequenceDB(expired), "REG-EXPIRED")
    assert expired_error.value.code == "registration_grant_expired"

    exhausted = RegistrationGrant(
        token_hash=governance_token_hash("REG-USED"),
        token_prefix="REG-USED",
        max_uses=1,
        used_count=1,
        status="exhausted",
    )
    with pytest.raises(GovernanceCredentialError) as replay_error:
        await resolve_registration_grant(_SequenceDB(exhausted), "REG-USED")
    assert replay_error.value.code in {"registration_grant_inactive", "registration_grant_exhausted"}


@pytest.mark.asyncio
async def test_organization_invitation_is_email_bound_and_single_use():
    invitation = OrganizationInvitation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        target_email="owner@example.com",
        invited_role="org_admin",
        token_hash="a" * 64,
        token_prefix="ORG-TEST",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    with pytest.raises(GovernanceCredentialError) as mismatch:
        await resolve_organization_invitation_by_id(
            _SequenceDB(invitation),
            invitation.id,
            identity_email="other@example.com",
        )
    assert mismatch.value.status_code == 403
    assert mismatch.value.code == "organization_invitation_email_mismatch"

    credential = await resolve_organization_invitation_by_id(
        _SequenceDB(invitation),
        invitation.id,
        identity_email=" OWNER@example.com ",
    )
    assert credential is not None
    consume_organization_credential(credential, accepted_by_user_id=uuid.uuid4())
    assert invitation.status == "accepted"

    with pytest.raises(GovernanceCredentialError) as replay:
        await resolve_organization_invitation_by_id(
            _SequenceDB(invitation),
            invitation.id,
            identity_email="owner@example.com",
        )
    assert replay.value.code == "organization_invitation_inactive"


@pytest.mark.asyncio
async def test_existing_member_does_not_consume_reusable_join_link():
    member = _user(tenant_id=uuid.uuid4())
    company = _tenant(owner_user_id=uuid.uuid4())
    member.tenant_id = company.id
    link = OrganizationJoinLink(
        tenant_id=company.id,
        token_hash="b" * 64,
        token_prefix="JOIN-TEST",
        max_uses=10,
        used_count=3,
        status="active",
    )
    credential = ResolvedOrganizationCredential(
        kind="organization_join_link",
        tenant_id=company.id,
        role="member",
        record=link,
    )

    result = await tenants._accept_organization_credential(
        credential,
        current_user=member,
        locked_identity=member.identity,
        mfa_verified=False,
        db=_SequenceDB(company, member),
    )

    assert result.role == "member"
    assert result.access_token
    assert link.used_count == 3


@pytest.mark.asyncio
async def test_self_create_idempotency_replays_original_company_without_writes(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    db = _SequenceDB(company, owner)
    monkeypatch.setattr(tenants, "_lock_current_membership", AsyncMock(return_value=(owner, owner.identity)))
    monkeypatch.setattr(
        "app.services.identity_governance.identity_has_capability",
        AsyncMock(return_value=True),
    )

    result = await tenants.self_create_company(
        tenants.TenantCreate(name=company.name),
        idempotency_key="stable-company-request",
        current_user=owner,
        db=db,
    )

    assert result.tenant.id == company.id
    assert result.access_token
    assert db.added == []
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_self_create_idempotency_rejects_changed_company_name(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    db = _SequenceDB(company)
    monkeypatch.setattr(tenants, "_lock_current_membership", AsyncMock(return_value=(owner, owner.identity)))
    monkeypatch.setattr(
        "app.services.identity_governance.identity_has_capability",
        AsyncMock(return_value=True),
    )

    with pytest.raises(HTTPException) as exc:
        await tenants.self_create_company(
            tenants.TenantCreate(name="Different Company"),
            idempotency_key="stable-company-request",
            current_user=owner,
            db=db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "idempotency_key_reused"


@pytest.mark.asyncio
async def test_ownership_transfer_requires_target_confirmation(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    target = _user(tenant_id=company.id, role="member")
    db = _SequenceDB(company, owner, None, target)
    monkeypatch.setattr(tenants, "_require_password_proof", AsyncMock())

    result = await tenants.request_tenant_ownership_transfer(
        company.id,
        tenants.OwnershipTransferRequest(
            new_owner_user_id=target.id,
            current_password="correct-password",
        ),
        current_user=owner,
        db=db,
    )

    assert result["status"] == "pending"
    assert company.owner_user_id == owner.id
    assert owner.role == "org_owner"
    assert target.role == "member"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_only_proposed_owner_can_accept_ownership_transfer():
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    target = _user(tenant_id=company.id)
    transfer = _transfer(company.id, owner.id, target.id)
    stranger = _user(tenant_id=company.id)

    with pytest.raises(HTTPException) as exc:
        await tenants.accept_tenant_ownership_transfer(
            company.id,
            transfer.id,
            current_user=stranger,
            db=_SequenceDB(company, transfer),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "proposed_owner_required"
    assert company.owner_user_id == owner.id


@pytest.mark.asyncio
async def test_proposed_owner_acceptance_atomically_swaps_unique_owner():
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    target = _user(tenant_id=company.id)
    transfer = _transfer(company.id, owner.id, target.id)
    db = _SequenceDB(company, transfer, owner, target)

    result = await tenants.accept_tenant_ownership_transfer(
        company.id,
        transfer.id,
        current_user=target,
        db=db,
    )

    assert result["status"] == "transferred"
    assert transfer.status == "accepted"
    assert owner.role == "org_admin"
    assert target.role == "org_owner"
    assert company.owner_user_id == target.id
    assert db.flush_count == 1
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_expired_ownership_transfer_cannot_change_roles():
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    target = _user(tenant_id=company.id)
    transfer = _transfer(
        company.id,
        owner.id,
        target.id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db = _SequenceDB(company, transfer)

    with pytest.raises(HTTPException) as exc:
        await tenants.accept_tenant_ownership_transfer(
            company.id,
            transfer.id,
            current_user=target,
            db=db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ownership_transfer_expired"
    assert transfer.status == "expired"
    assert owner.role == "org_owner"
    assert target.role == "member"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_owner_cannot_leave_before_transferring_company():
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id

    with pytest.raises(HTTPException) as exc:
        await tenants.leave_tenant(
            company.id,
            tenants.TenantLeaveRequest(confirmation="LEAVE"),
            current_user=owner,
            db=_SequenceDB(owner, company),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "owner_must_transfer_before_leaving"


@pytest.mark.asyncio
async def test_member_leave_returns_valid_fallback_membership_context(monkeypatch):
    member = _user(tenant_id=uuid.uuid4())
    company = _tenant(owner_user_id=uuid.uuid4())
    company.id = member.tenant_id
    fallback = _user(tenant_id=uuid.uuid4(), role="org_admin")
    fallback.identity = member.identity
    fallback.identity_id = member.identity_id
    monkeypatch.setattr(tenants, "create_access_token", lambda *_args, **_kwargs: "fallback-token")
    monkeypatch.setattr(
        tenants,
        "_tenant_leave_preflight",
        AsyncMock(
            return_value={
                "blockers": [],
                "requires_acknowledgement": False,
                "summary": {},
            }
        ),
    )
    monkeypatch.setattr(
        tenants,
        "_revoke_departed_membership_scope",
        AsyncMock(
            return_value={
                "revoked_agent_grants": 0,
                "expired_personal_credentials": 0,
                "deactivated_directory_members": 0,
            }
        ),
    )
    db = _SequenceDB(member, company, fallback)

    result = await tenants.leave_tenant(
        company.id,
        tenants.TenantLeaveRequest(confirmation="LEAVE"),
        current_user=member,
        db=db,
    )

    assert member.is_active is False
    assert member.identity.is_active is True
    assert result == {
        "status": "left",
        "fallback_tenant_id": str(fallback.tenant_id),
        "access_token": "fallback-token",
    }
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_leave_requires_server_acknowledgement_for_remaining_work(monkeypatch):
    member = _user(tenant_id=uuid.uuid4())
    company = _tenant(owner_user_id=uuid.uuid4())
    company.id = member.tenant_id
    preflight = {
        "blockers": [],
        "requires_acknowledgement": True,
        "summary": {"open_tasks": 2},
    }
    monkeypatch.setattr(
        tenants,
        "_tenant_leave_preflight",
        AsyncMock(return_value=preflight),
    )

    with pytest.raises(HTTPException) as exc:
        await tenants.leave_tenant(
            company.id,
            tenants.TenantLeaveRequest(confirmation="LEAVE"),
            current_user=member,
            db=_SequenceDB(member, company),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "leave_responsibilities_acknowledgement_required"
    assert exc.value.detail["preflight"] == preflight
    assert member.is_active is True


@pytest.mark.asyncio
async def test_leave_blocks_owned_agents_until_handover_or_deletion(monkeypatch):
    member = _user(tenant_id=uuid.uuid4())
    company = _tenant(owner_user_id=uuid.uuid4())
    company.id = member.tenant_id
    preflight = {
        "blockers": [
            {
                "code": "agent_ownership_handoff_required",
                "message": "Handover or delete every owned Agent before leaving",
                "count": 1,
            }
        ],
        "requires_acknowledgement": False,
        "summary": {"owned_agents": 1},
    }
    monkeypatch.setattr(
        tenants,
        "_tenant_leave_preflight",
        AsyncMock(return_value=preflight),
    )

    with pytest.raises(HTTPException) as exc:
        await tenants.leave_tenant(
            company.id,
            tenants.TenantLeaveRequest(
                confirmation="LEAVE",
                acknowledge_responsibilities=True,
            ),
            current_user=member,
            db=_SequenceDB(member, company),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "agent_ownership_handoff_required"
    assert member.is_active is True


@pytest.mark.asyncio
async def test_leave_preflight_is_scoped_to_current_membership(monkeypatch):
    member = _user(tenant_id=uuid.uuid4())
    company = _tenant(owner_user_id=uuid.uuid4())
    company.id = member.tenant_id
    preflight = {
        "tenant_id": str(company.id),
        "membership_id": str(member.id),
        "can_leave": True,
    }
    builder = AsyncMock(return_value=preflight)
    monkeypatch.setattr(tenants, "_tenant_leave_preflight", builder)

    result = await tenants.get_tenant_leave_preflight(
        company.id,
        current_user=member,
        db=_SequenceDB(get_values=(company,)),
    )

    assert result == preflight
    builder.assert_awaited_once_with(
        ANY,
        membership=member,
        tenant=company,
    )


@pytest.mark.asyncio
async def test_member_deactivation_does_not_disable_global_identity(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    member = _user(tenant_id=company.id)
    member.activation_pending_email_verification = False
    monkeypatch.setattr(
        tenants,
        "_tenant_leave_preflight",
        AsyncMock(
            return_value={
                "blockers": [],
                "requires_acknowledgement": False,
                "summary": {},
            }
        ),
    )

    result = await users_api.deactivate_membership(
        member.id,
        current_user=owner,
        db=_SequenceDB(member, get_values=(company,)),
    )

    assert result == {"status": "deactivated"}
    assert member.is_active is False
    assert member.identity.is_active is True

    reactivated = await users_api.reactivate_membership(
        member.id,
        current_user=owner,
        db=_SequenceDB(member),
    )
    assert reactivated == {"status": "active"}
    assert member.is_active is True


@pytest.mark.asyncio
async def test_governor_deactivation_requires_responsibility_acknowledgement(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    member = _user(tenant_id=company.id)
    monkeypatch.setattr(
        tenants,
        "_tenant_leave_preflight",
        AsyncMock(
            return_value={
                "blockers": [
                    {
                        "code": "agent_ownership_handoff_required",
                        "count": 1,
                        "message": "handoff required",
                    }
                ],
                "requires_acknowledgement": True,
                "summary": {"owned_agents": 1},
            }
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await users_api.deactivate_membership(
            member.id,
            current_user=owner,
            db=_SequenceDB(member, get_values=(company,)),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "deactivation_responsibilities_acknowledgement_required"
    assert member.is_active is True


@pytest.mark.asyncio
async def test_company_deletion_is_recoverable_and_does_not_delete_data(monkeypatch):
    owner = _user(tenant_id=uuid.uuid4(), role="org_owner")
    company = _tenant(owner_user_id=owner.id)
    owner.tenant_id = company.id
    password_proof = AsyncMock()
    monkeypatch.setattr(tenants, "_require_password_proof", password_proof)
    monkeypatch.setattr(tenants, "create_access_token", lambda *_args, **_kwargs: "fallback-token")
    fallback = _user(tenant_id=uuid.uuid4(), role="org_admin")
    fallback.identity = owner.identity
    fallback.identity_id = owner.identity_id
    db = _SequenceDB(company, None, fallback)

    result = await tenants.delete_tenant(
        company.id,
        tenants.TenantDeletionRequest(
            company_name=company.name,
            current_password="correct-password",
        ),
        current_user=owner,
        db=db,
    )

    assert result["status"] == "scheduled"
    assert company.is_active is False
    assert company.deletion_requested_at is not None
    assert company.deletion_scheduled_for > company.deletion_requested_at
    assert company.deletion_requested_by_user_id == owner.id
    assert owner.is_active is True
    assert result["fallback_tenant_id"] == str(fallback.tenant_id)
    assert result["access_token"] == "fallback-token"
    password_proof.assert_awaited_once_with(owner, "correct-password")
    assert any(isinstance(item, tenants.TenantDeletionJob) for item in db.added)

    restore_db = _SequenceDB(company, None, None)
    restored = await tenants.restore_tenant(
        company.id,
        tenants.TenantRestoreRequest(current_password="correct-password"),
        current_user=owner,
        db=restore_db,
    )
    assert restored == {"status": "restored"}
    assert company.is_active is True
    assert company.deletion_requested_at is None


@pytest.mark.asyncio
async def test_support_session_summary_applies_scope_and_returns_no_private_data():
    operator = _user(tenant_id=None, role="platform_admin")
    company = _tenant(owner_user_id=uuid.uuid4())
    support_session = PlatformSupportSession(
        id=uuid.uuid4(),
        platform_identity_id=operator.identity_id,
        tenant_id=company.id,
        reason="Investigate aggregate tenant health",
        scopes=["tenant.metadata.read", "tenant.diagnostics.read"],
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    db = _SequenceDB(
        support_session,
        (4, 3),
        (6, 5),
        get_values=(company,),
    )

    result = await governance_api.get_platform_support_tenant_summary(
        support_session.id,
        company.id,
        current_user=operator,
        db=db,
    )

    assert result["support_session_id"] == str(support_session.id)
    assert result["tenant_id"] == str(company.id)
    assert result["scopes_applied"] == [
        "tenant.diagnostics.read",
        "tenant.metadata.read",
    ]
    assert result["metadata"]["name"] == company.name
    assert result["diagnostics"] == {
        "memberships_total": 4,
        "memberships_active": 3,
        "agents_total": 6,
        "agents_active": 5,
    }
    assert "owner_user_id" not in result["metadata"]
    assert "messages" not in result
    assert "files" not in result
    assert support_session.last_used_at is not None
    audits = [item for item in db.added if isinstance(item, AuditLog)]
    assert len(audits) == 1
    assert audits[0].action == "platform_support_tenant_summary_read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_overrides", "target_tenant_id", "expected_code"),
    [
        ({"ended_at": datetime.now(UTC)}, None, "support_session_inactive"),
        (
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            None,
            "support_session_expired",
        ),
        ({}, uuid.uuid4(), "support_session_tenant_mismatch"),
        ({"scopes": ["tenant.lifecycle.manage"]}, None, "support_scope_required"),
    ],
)
async def test_support_session_summary_fails_closed(
    session_overrides,
    target_tenant_id,
    expected_code,
):
    operator = _user(tenant_id=None, role="platform_admin")
    company = _tenant(owner_user_id=uuid.uuid4())
    values = {
        "id": uuid.uuid4(),
        "platform_identity_id": operator.identity_id,
        "tenant_id": company.id,
        "reason": "Investigate aggregate tenant health",
        "scopes": ["tenant.metadata.read"],
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
        "ended_at": None,
        "last_used_at": None,
    }
    values.update(session_overrides)
    support_session = SimpleNamespace(**values)

    with pytest.raises(HTTPException) as exc:
        await governance_api.get_platform_support_tenant_summary(
            support_session.id,
            target_tenant_id or company.id,
            current_user=operator,
            db=_SequenceDB(support_session),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == expected_code


@pytest.mark.asyncio
async def test_company_switch_requires_active_membership_and_returns_target_token(monkeypatch):
    current = _user(tenant_id=uuid.uuid4())
    target = _user(tenant_id=uuid.uuid4())
    target.identity = current.identity
    target.identity_id = current.identity_id
    company = _tenant(owner_user_id=uuid.uuid4())
    company.id = target.tenant_id
    monkeypatch.setattr(
        auth_api.user_dao,
        "get_by_identity_and_tenant",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(auth_api.tenant_dao, "get", AsyncMock(return_value=company))
    monkeypatch.setattr(
        auth_api.system_setting_dao,
        "is_sso_custom_domain_redirect_enabled",
        AsyncMock(return_value=False),
    )
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/switch-tenant", "headers": []})

    result = await auth_api.switch_tenant(
        auth_api.TenantSwitchRequest(tenant_id=company.id),
        request=request,
        current_user=current,
    )

    assert result.target_tenant_id == company.id
    assert result.access_token
    assert result.redirect_url is None


@pytest.mark.asyncio
async def test_direct_membership_reassignment_is_retired():
    with pytest.raises(HTTPException) as exc:
        await tenants.assign_user_to_tenant(
            uuid.uuid4(),
            uuid.uuid4(),
            current_user=_user(tenant_id=None),
            db=_SequenceDB(),
        )

    assert exc.value.status_code == 410
    assert exc.value.detail["code"] == "direct_membership_assignment_retired"
