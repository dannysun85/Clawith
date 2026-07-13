import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api import agents as agents_api
from app.api import files as files_api
from app.api import skills as skills_api
from app.models.skill import Skill, SkillFile
from app.schemas.schemas import AgentCreate
from app.services.skill_scope import (
    prefer_tenant_skill_overrides,
    resolve_agent_skills,
    scope_skill_query,
)


class FakeResult:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None

    def scalar_one_or_none(self):
        if len(self.values) > 1:
            raise AssertionError("expected at most one value")
        return self.values[0] if self.values else None


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.deleted = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _query):
        if not self.results:
            raise AssertionError("unexpected database query")
        return FakeResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def delete(self, value):
        self.deleted.append(value)


class FakeSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _skill(
    *,
    tenant_id,
    folder_name="shared",
    name="Shared",
    is_default=False,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        folder_name=folder_name,
        name=name,
        is_default=is_default,
        files=[],
    )


def test_skill_model_uses_global_and_per_tenant_unique_indexes():
    indexes = {index.name: index for index in Skill.__table__.indexes}
    expected = {
        "ux_skills_global_name",
        "ux_skills_global_folder_name",
        "ux_skills_tenant_name",
        "ux_skills_tenant_folder_name",
    }

    assert expected <= indexes.keys()
    assert all(indexes[name].unique for name in expected)
    assert Skill.__table__.c.name.unique is not True
    assert Skill.__table__.c.folder_name.unique is not True


def test_skill_visibility_sql_is_global_plus_exact_tenant():
    tenant_id = uuid.uuid4()
    statement = scope_skill_query(select(Skill), tenant_id)
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "skills.tenant_id IS NULL" in sql
    assert str(tenant_id) in sql


def test_tenant_override_wins_without_leaking_other_tenants():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    global_skill = _skill(tenant_id=None, name="Global")
    override_a = _skill(tenant_id=tenant_a, name="Tenant A")
    override_b = _skill(tenant_id=tenant_b, name="Tenant B")

    assert prefer_tenant_skill_overrides(
        [global_skill, override_a, override_b], tenant_a
    ) == [override_a]
    assert prefer_tenant_skill_overrides(
        [global_skill, override_a, override_b], tenant_b
    ) == [override_b]
    assert prefer_tenant_skill_overrides(
        [global_skill, override_a, override_b], None
    ) == [global_skill]


def test_agent_skill_resolution_uses_override_for_global_default():
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    global_default = _skill(tenant_id=None, is_default=True)
    tenant_override = _skill(tenant_id=tenant_id, name="Customized")
    foreign_override = _skill(tenant_id=other_tenant_id, name="Foreign")

    resolved = resolve_agent_skills(
        [global_default, tenant_override, foreign_override],
        tenant_id,
    )

    assert resolved == [tenant_override]


@pytest.mark.asyncio
async def test_tenant_admin_browse_write_clones_global_skill(monkeypatch):
    tenant_id = uuid.uuid4()
    global_file = SkillFile(path="SKILL.md", content="# global")
    global_skill = Skill(
        tenant_id=None,
        name="Shared",
        description="Global preset",
        category="builtin",
        icon="S",
        folder_name="shared",
        is_builtin=True,
        is_default=True,
        files=[global_file],
    )
    session = FakeSession([global_skill], [])
    monkeypatch.setattr(
        skills_api,
        "async_session",
        FakeSessionFactory(session),
    )
    user = SimpleNamespace(
        role="org_admin",
        tenant_id=tenant_id,
        identity=None,
    )

    response = await skills_api.browse_write(
        skills_api.BrowseWriteIn(
            path="shared/SKILL.md",
            content="# tenant",
        ),
        current_user=user,
    )

    clone = next(value for value in session.added if isinstance(value, Skill))
    assert response == {"ok": True}
    assert clone.tenant_id == tenant_id
    assert clone.is_builtin is False
    assert clone.files[0].content == "# tenant"
    assert global_file.content == "# global"
    assert session.committed is True


@pytest.mark.asyncio
async def test_tenant_admin_cannot_delete_global_skill(monkeypatch):
    global_skill = _skill(tenant_id=None)
    global_skill.is_builtin = True
    session = FakeSession([global_skill])
    monkeypatch.setattr(
        skills_api,
        "async_session",
        FakeSessionFactory(session),
    )
    user = SimpleNamespace(
        role="org_admin",
        tenant_id=uuid.uuid4(),
        identity=None,
    )

    with pytest.raises(HTTPException) as exc:
        await skills_api.browse_delete("shared", current_user=user)

    assert exc.value.status_code == 403
    assert session.deleted == []


@pytest.mark.asyncio
async def test_agent_selection_rejects_foreign_skill_id():
    visible_id = uuid.uuid4()
    foreign_id = uuid.uuid4()
    db = FakeSession([visible_id])

    with pytest.raises(HTTPException) as exc:
        await agents_api._validate_agent_skill_selection(
            db,
            [visible_id, foreign_id],
            uuid.uuid4(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_create_agent_in_another_tenant():
    current_tenant = uuid.uuid4()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=current_tenant,
        identity=None,
        quota_agent_ttl_hours=0,
    )

    with pytest.raises(HTTPException) as exc:
        await agents_api.create_agent(
            AgentCreate(name="Scoped Agent", tenant_id=uuid.uuid4()),
            background_tasks=BackgroundTasks(),
            current_user=user,
            db=FakeSession(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_import_skill_returns_not_found_outside_agent_tenant():
    agent = SimpleNamespace(tenant_id=uuid.uuid4())
    db = FakeSession([])
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=agent.tenant_id)

    with patch.object(
        files_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    ):
        with pytest.raises(HTTPException) as exc:
            await files_api.import_skill_to_agent(
                agent_id=uuid.uuid4(),
                body=files_api.ImportSkillBody(skill_id=uuid.uuid4()),
                current_user=user,
                db=db,
            )

    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["../secret", "scripts/../../secret", "/absolute", "scripts\\evil.py"],
)
def test_skill_file_paths_reject_traversal(path):
    with pytest.raises(HTTPException) as exc:
        skills_api._validate_skill_file_path(path)

    assert exc.value.status_code == 400
