"""Organization user tenant-boundary regression tests.

Ported from upstream f04fe661 and adapted to the local authority model, which
is stricter than upstream: company governors can never write global Identity
login fields (email/username/phone) through this endpoint at all.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import organization
from app.schemas.schemas import UserUpdate


class DummyResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return []


class RecordingDB:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []

    async def execute(self, statement, _params=None):
        self.statements.append(statement)
        return self.responses.pop(0)


def _org_admin(*, tenant_id: uuid.UUID, identity_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        role="org_admin",
        tenant_id=tenant_id,
        identity_id=identity_id,
        identity=SimpleNamespace(is_platform_admin=False),
    )


@pytest.mark.asyncio
async def test_org_admin_cannot_list_users_from_another_tenant() -> None:
    tenant_id = uuid.uuid4()
    db = RecordingDB([])

    with pytest.raises(HTTPException) as raised:
        await organization.list_users(
            tenant_id=uuid.uuid4(),
            current_user=_org_admin(tenant_id=tenant_id, identity_id=uuid.uuid4()),
            db=db,
        )

    assert raised.value.status_code == 403
    assert db.statements == []  # rejected before any query executes


@pytest.mark.asyncio
async def test_org_admin_cannot_update_user_from_another_tenant() -> None:
    tenant_id = uuid.uuid4()
    target = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), identity_id=uuid.uuid4())
    db = RecordingDB([DummyResult(target)])

    with pytest.raises(HTTPException) as raised:
        await organization.admin_update_user(
            user_id=target.id,
            data=UserUpdate(display_name="Changed"),
            current_user=_org_admin(tenant_id=tenant_id, identity_id=uuid.uuid4()),
            db=db,
        )

    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_change_another_members_global_login_email() -> None:
    tenant_id = uuid.uuid4()
    target_identity_id = uuid.uuid4()
    target = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, identity_id=target_identity_id)
    identity = SimpleNamespace(
        id=target_identity_id,
        email="old-address@example.com",
        username="member1",
        phone=None,
    )
    db = RecordingDB([DummyResult(target), DummyResult(identity)])

    with pytest.raises(HTTPException) as raised:
        await organization.admin_update_user(
            user_id=target.id,
            data=UserUpdate(email="new-address@example.com"),
            current_user=_org_admin(tenant_id=tenant_id, identity_id=uuid.uuid4()),
            db=db,
        )

    assert raised.value.status_code == 403
    assert "global login identity fields" in raised.value.detail
