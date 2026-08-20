from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import files as files_api
from app.models.agent import Agent
from app.models.user import User
from app.services import workspace_collaboration
from app.services.storage_runtime.local import LocalStorageBackend


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return User(**values)


def make_agent(creator_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": creator_id,
        "status": "idle",
        "agent_type": "native",
    }
    values.update(overrides)
    return Agent(**values)


@pytest.mark.parametrize(
    ("path", "requires_manage"),
    [
        ("soul.md", True),
        ("SOUL.md", True),
        ("memory.md", True),
        ("memory/memory.md", True),
        ("memory/daily/2026-08-20.md", True),
        ("skills/review/SKILL.md", True),
        ("workspace/../memory.md", True),
        ("workspace/../soul.md", True),
        ("workspace/../skills/review/SKILL.md", True),
        ("workspace/report.md", False),
    ],
)
def test_agent_control_plane_paths_require_manage_access(path, requires_manage):
    assert files_api._requires_agent_file_manage_access(path) is requires_manage


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["soul.md", "memory.md", "memory/memory.md"])
async def test_use_access_cannot_write_agent_identity_or_memory(monkeypatch, path):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)

    async def fake_check_agent_access(_db, _current_user, _agent_id, **kwargs):
        if kwargs.get("required_level") == "manage":
            raise HTTPException(status_code=403, detail="Manage access required")
        return agent, "use"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.write_file(
            agent_id=agent.id,
            path=path,
            data=files_api.FileWrite(content="unapproved behavior change"),
            current_user=user,
            db=object(),
        )

    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "workspace/../memory.md",
        "workspace/../soul.md",
        "workspace/../skills/review/SKILL.md",
    ],
)
@pytest.mark.asyncio
async def test_use_access_cannot_write_control_plane_through_path_alias(monkeypatch, path):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)

    async def fake_check_agent_access(_db, _current_user, _agent_id, **kwargs):
        if kwargs.get("required_level") == "manage":
            raise HTTPException(status_code=403, detail="Manage access required")
        return agent, "use"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.write_file(
            agent_id=agent.id,
            path=path,
            data=files_api.FileWrite(content="unapproved behavior change"),
            current_user=user,
            db=object(),
        )

    assert exc.value.status_code == 403


def test_internal_skill_metadata_rejects_path_alias():
    assert files_api._is_internal_skill_metadata_path(
        "foo/../skills/review/.astra-managed.json"
    )


@pytest.mark.asyncio
async def test_use_access_cannot_restore_agent_memory_revision(monkeypatch):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)
    revision_id = uuid.uuid4()
    access_levels: list[str] = []

    async def fake_check_agent_access(_db, _current_user, _agent_id, **kwargs):
        required_level = kwargs.get("required_level", "use")
        access_levels.append(required_level)
        if required_level == "manage":
            raise HTTPException(status_code=403, detail="Manage access required")
        return agent, "use"

    class QueryResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                id=revision_id,
                agent_id=agent.id,
                path="memory/memory.md",
                after_content="old memory",
            )

    class DB:
        async def execute(self, _query):
            return QueryResult()

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.restore_file_revision(
            agent_id=agent.id,
            data=files_api.RestoreRevisionBody(revision_id=revision_id),
            current_user=user,
            db=DB(),
        )

    assert exc.value.status_code == 403
    assert access_levels == ["use", "manage"]


@pytest.mark.asyncio
async def test_use_access_cannot_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "important.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("do not delete", encoding="utf-8")

    async def fake_check_agent_access(_db, _current_user, _agent_id, **kwargs):
        if kwargs.get("required_level") == "manage":
            raise HTTPException(status_code=403, detail="Manage access required")
        return agent, "use"

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.delete_file(
            agent_id=agent.id,
            path="workspace/important.md",
            current_user=user,
            db=object(),
        )

    assert exc.value.status_code == 403
    assert workspace_file.exists()


@pytest.mark.asyncio
async def test_manage_access_can_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "obsolete.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("delete me", encoding="utf-8")

    async def fake_check_agent_access(_db, _current_user, _agent_id, **_kwargs):
        return agent, "manage"

    @asynccontextmanager
    async def no_workspace_lock(*_args, **_kwargs):
        yield

    async def no_revision(*_args, **_kwargs):
        return None

    class DB:
        async def commit(self):
            return None

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        workspace_collaboration,
        "get_storage_backend",
        lambda: LocalStorageBackend(str(tmp_path)),
    )
    monkeypatch.setattr(
        workspace_collaboration,
        "workspace_locks",
        no_workspace_lock,
    )
    monkeypatch.setattr(
        workspace_collaboration,
        "record_revision",
        no_revision,
    )

    result = await files_api.delete_file(
        agent_id=agent.id,
        path="workspace/obsolete.md",
        current_user=user,
        db=DB(),
    )

    assert result == {"status": "ok", "path": "workspace/obsolete.md"}
    assert not workspace_file.exists()
