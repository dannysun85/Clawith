import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import enterprise, organization, users
from app.schemas.schemas import LLMModelCreate, UserUpdate


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, value):
        self.value = value
        self.flushed = False
        self.committed = False

    async def execute(self, _statement):
        return ScalarResult(self.value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_org_admin_cannot_demote_platform_admin():
    tenant_id = uuid.uuid4()
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)
    target = SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=tenant_id)
    db = FakeDB(target)

    with pytest.raises(HTTPException) as exc:
        await users.update_user_role(
            target.id,
            users.RoleUpdate(role="member"),
            actor,
            db,
        )

    assert exc.value.status_code == 403
    assert target.role == "platform_admin"
    assert db.committed is False


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
