"""Authority and aggregation contracts for company resource analytics."""

from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api import tenants as tenants_api
from app.core.security import get_company_analytics_viewer, get_current_user
from app.database import get_db


def _user(role: str, *, platform_operator: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        is_active=True,
        identity=SimpleNamespace(is_platform_admin=platform_operator),
    )


def _app_for(user, *, db_dependency):
    app = FastAPI()
    app.include_router(tenants_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = db_dependency
    return app


@pytest.mark.parametrize("platform_operator", [False, True])
def test_member_company_token_usage_fails_before_database(platform_operator):
    calls = 0

    async def forbidden_db():
        nonlocal calls
        calls += 1
        raise AssertionError("company analytics database dependency must not run")

    user = _user("member", platform_operator=platform_operator)
    with TestClient(_app_for(user, db_dependency=forbidden_db)) as client:
        response = client.get("/api/tenants/me/token-usage")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "company_analytics_view_required"
    assert calls == 0


@pytest.mark.asyncio
async def test_only_company_governors_receive_analytics_authority():
    admin = _user("org_admin")
    owner = _user("org_owner")

    assert await get_company_analytics_viewer(admin) is admin
    assert await get_company_analytics_viewer(owner) is owner

    platform_member = _user("member", platform_operator=True)
    with pytest.raises(HTTPException) as error:
        await get_company_analytics_viewer(platform_member)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "company_analytics_view_required"


class _AggregateResult:
    def one(self):
        return SimpleNamespace(
            tokens_today=100,
            tokens_month=200,
            tokens_total=300,
            cache_today=25,
            cache_month=50,
            cache_total=75,
            cache_creation_today=5,
            cache_creation_month=10,
            cache_creation_total=15,
        )


class _RecordingDb:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _AggregateResult()


@pytest.mark.asyncio
async def test_company_token_usage_is_current_tenant_and_excludes_deleted_agents():
    user = _user("org_admin")
    db = _RecordingDb()

    result = await tenants_api.get_my_tenant_token_usage(
        current_user=user,
        db=db,
    )

    assert result == {
        "today": {
            "total_tokens": 100,
            "cache_read_tokens": 25,
            "cache_creation_tokens": 5,
            "cache_hit_rate": 0.25,
        },
        "month": {
            "total_tokens": 200,
            "cache_read_tokens": 50,
            "cache_creation_tokens": 10,
            "cache_hit_rate": 0.25,
        },
        "total": {
            "total_tokens": 300,
            "cache_read_tokens": 75,
            "cache_creation_tokens": 15,
            "cache_hit_rate": 0.25,
        },
    }
    query = str(db.statements[0])
    assert "agents.tenant_id" in query
    assert "agents.deleted_at IS NULL" in query
