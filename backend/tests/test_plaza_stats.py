from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import plaza as plaza_api


class _Result:
    def __init__(self, *, scalar_value=None, rows=None):
        self.scalar_value = scalar_value
        self.rows = rows or []

    def scalar(self):
        return self.scalar_value

    def fetchall(self):
        return self.rows


class _Session:
    def __init__(self):
        self.results = iter(
            [
                _Result(scalar_value=4),
                _Result(scalar_value=7),
                _Result(scalar_value=2),
                _Result(rows=[("Astra", "agent", 3)]),
            ]
        )
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return next(self.results)


class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_platform_admin_without_tenant_can_read_plaza_stats(monkeypatch):
    """A tenantless platform admin must not combine Python bools with SQL filters."""
    session = _Session()
    monkeypatch.setattr(plaza_api, "async_session", lambda: session)

    result = await plaza_api.plaza_stats(
        current_user=SimpleNamespace(tenant_id=None, role="platform_admin")
    )

    assert result == {
        "total_posts": 4,
        "total_comments": 7,
        "today_posts": 2,
        "top_contributors": [{"name": "Astra", "type": "agent", "posts": 3}],
    }
    assert len(session.statements) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["list", "stats", "create", "get", "delete", "comment", "like"],
)
async def test_tenantless_non_admin_plaza_requests_fail_closed(operation):
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        role="member",
        display_name="Tenantless",
    )

    with pytest.raises(HTTPException) as captured:
        if operation == "list":
            await plaza_api.list_posts(current_user=user)
        elif operation == "stats":
            await plaza_api.plaza_stats(current_user=user)
        elif operation == "create":
            await plaza_api.create_post(
                plaza_api.PostCreate(
                    content="blocked",
                    author_id=user.id,
                    author_name="ignored",
                ),
                current_user=user,
            )
        elif operation == "get":
            await plaza_api.get_post(uuid.uuid4(), current_user=user)
        elif operation == "delete":
            await plaza_api.delete_post(uuid.uuid4(), current_user=user)
        elif operation == "comment":
            await plaza_api.create_comment(
                uuid.uuid4(),
                plaza_api.CommentCreate(
                    content="blocked",
                    author_id=user.id,
                    author_name="ignored",
                ),
                current_user=user,
            )
        else:
            await plaza_api.like_post(
                uuid.uuid4(),
                author_id=user.id,
                current_user=user,
            )

    assert captured.value.status_code == 403


def test_tenantless_platform_admin_write_requires_explicit_tenant():
    user = SimpleNamespace(tenant_id=None, role="platform_admin")

    with pytest.raises(HTTPException) as captured:
        plaza_api._effective_plaza_tenant_id(user)

    assert captured.value.status_code == 400
    tenant_id = uuid.uuid4()
    assert plaza_api._effective_plaza_tenant_id(user, tenant_id) == str(tenant_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["post", "comment", "like"])
async def test_plaza_write_endpoints_reject_cross_user_impersonation(
    monkeypatch,
    operation,
):
    current_user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="member",
        display_name="Authenticated User",
    )
    forged_user_id = uuid.uuid4()
    monkeypatch.setattr(plaza_api, "async_session", lambda: _NoopSession())

    with pytest.raises(HTTPException) as captured:
        if operation == "post":
            await plaza_api.create_post(
                plaza_api.PostCreate(
                    content="forged post",
                    author_id=forged_user_id,
                    author_type="human",
                    author_name="Victim",
                ),
                current_user=current_user,
            )
        elif operation == "comment":
            await plaza_api.create_comment(
                uuid.uuid4(),
                plaza_api.CommentCreate(
                    content="forged comment",
                    author_id=forged_user_id,
                    author_type="human",
                    author_name="Victim",
                ),
                current_user=current_user,
            )
        else:
            await plaza_api.like_post(
                uuid.uuid4(),
                author_id=forged_user_id,
                author_type="human",
                current_user=current_user,
            )

    assert captured.value.status_code == 403


@pytest.mark.asyncio
async def test_plaza_human_identity_name_is_server_derived():
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="member",
        display_name="Canonical Name",
    )

    resolved = await plaza_api._resolve_authenticated_author(
        _NoopSession(),
        current_user=user,
        requested_author_id=user.id,
        requested_author_type="human",
    )

    assert resolved == (user.id, "human", "Canonical Name")


@pytest.mark.asyncio
async def test_plaza_agent_identity_requires_manage_access(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Canonical Agent",
        is_system=False,
        access_mode="company",
    )
    access = AsyncMock(return_value=(agent, "use"))
    monkeypatch.setattr(plaza_api, "check_agent_access", access)

    with pytest.raises(HTTPException) as captured:
        await plaza_api._resolve_authenticated_author(
            _NoopSession(),
            current_user=user,
            requested_author_id=agent.id,
            requested_author_type="agent",
        )

    assert captured.value.status_code == 403

    access.return_value = (agent, "manage")
    assert await plaza_api._resolve_authenticated_author(
        _NoopSession(),
        current_user=user,
        requested_author_id=agent.id,
        requested_author_type="agent",
    ) == (agent.id, "agent", "Canonical Agent")
