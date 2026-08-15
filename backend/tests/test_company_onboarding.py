"""Company onboarding identity and idempotency contracts."""

from datetime import UTC, datetime
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
        self.added = []
        self.commit = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.results.pop(0))

    def add(self, value):
        self.added.append(value)


def _user():
    identity_id = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        tenant_id=uuid.uuid4(),
        role="org_owner",
        display_name="Owner",
        title=None,
        timezone=None,
        work_hours_start=None,
        work_hours_end=None,
    )


def _tenant(user, *, initialized=True):
    return SimpleNamespace(
        id=user.tenant_id,
        name="Acme",
        timezone="UTC",
        country_region="001",
        company_size="unspecified",
        allow_member_private_agents=False,
        default_approval_policy="high_risk",
        initialization_completed_at=datetime.now(UTC) if initialized else None,
        initialized_by_user_id=user.id if initialized else None,
        created_by_identity_id=user.identity_id,
    )


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

    actual = await onboarding_api._ensure_row(db, user, "join", lock=True)

    assert actual is row
    assert row.entry_mode == "create"
    assert db.statements[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_start_ignores_client_entry_mode_and_uses_company_provenance():
    user = _user()
    company = _tenant(user, initialized=False)
    row = _row(user, entry_mode="create", current_step="company")
    db = _RecordingDB(company, row)

    payload = await onboarding_api.start_onboarding(
        onboarding_api.OnboardingStartRequest(entry_mode="join"),
        current_user=user,
        db=db,
    )

    assert row.entry_mode == "create"
    assert row.current_step == "company"
    assert payload["entry_mode"] == "create"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_personal_assistant_reuses_the_locked_existing_companion():
    user = _user()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Astra")
    row = _row(user, personal_assistant_agent_id=agent.id)
    db = _RecordingDB(row, _tenant(user), agent)
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


@pytest.mark.asyncio
async def test_company_initialization_persists_only_product_policy_and_advances_profile():
    user = _user()
    company = _tenant(user, initialized=False)
    row = _row(user, current_step="company")
    db = _RecordingDB(row, company)

    payload = await onboarding_api.complete_company_initialization(
        onboarding_api.CompanyInitializationRequest(
            name="Acme China",
            timezone="Asia/Shanghai",
            country_region="cn",
            company_size="11-50",
            allow_member_private_agents=True,
            default_approval_policy="external_actions",
        ),
        current_user=user,
        db=db,
    )

    assert company.name == "Acme China"
    assert company.timezone == "Asia/Shanghai"
    assert company.country_region == "CN"
    assert company.company_size == "11-50"
    assert company.allow_member_private_agents is True
    assert company.default_approval_policy == "external_actions"
    assert company.initialization_completed_at is not None
    assert company.initialized_by_user_id == user.id
    assert row.current_step == "profile"
    assert payload["company_initialization_required"] is False
    assert [entry.action for entry in db.added] == ["company_initialization_completed"]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_member_profile_is_membership_scoped_and_advances_assistant():
    user = _user()
    company = _tenant(user)
    row = _row(user, current_step="profile")
    db = _RecordingDB(row, company, None)

    payload = await onboarding_api.complete_member_profile(
        onboarding_api.MemberProfileRequest(
            display_name="  Alice  ",
            title="  Product Lead  ",
            timezone="Asia/Shanghai",
            work_hours_start="09:30",
            work_hours_end="18:30",
        ),
        current_user=user,
        db=db,
    )

    assert user.display_name == "Alice"
    assert user.title == "Product Lead"
    assert user.timezone == "Asia/Shanghai"
    assert user.work_hours_start == "09:30"
    assert user.work_hours_end == "18:30"
    assert row.current_step == "assistant"
    assert payload["member_profile"]["display_name"] == "Alice"
    assert "participants" in str(db.statements[2])
    db.commit.assert_awaited_once()
