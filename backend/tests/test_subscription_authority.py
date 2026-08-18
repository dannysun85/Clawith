"""HTTP and projection contracts for membership-scoped Billing access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api import subscription as subscription_api
from app.core.security import (
    get_company_billing_manager,
    get_company_billing_viewer,
    get_current_user,
)
from app.database import get_db


def _user(role: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        is_active=True,
        identity=SimpleNamespace(is_platform_admin=False),
    )


def _app_for(user, *, db_dependency):
    app = FastAPI()
    app.include_router(subscription_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = db_dependency
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/api/subscription/subscriptions",
        "/api/subscription/usage",
        "/api/subscription/credits",
        "/api/subscription/seats",
        "/api/subscription/summary",
        "/api/subscription/credit-transactions",
        "/api/subscription/orders",
        "/api/subscription/billing/profile",
        f"/api/subscription/checkout/{uuid.uuid4()}/status",
    ],
)
def test_member_sensitive_billing_reads_fail_before_database(path):
    calls = 0

    async def forbidden_db():
        nonlocal calls
        calls += 1
        raise AssertionError("sensitive database dependency must not run")

    with TestClient(_app_for(_user("member"), db_dependency=forbidden_db)) as client:
        response = client.get(path)

    assert response.status_code == 403
    assert calls == 0


@pytest.mark.asyncio
async def test_admin_views_billing_but_only_owner_manages_it():
    admin = _user("org_admin")
    owner = _user("org_owner")

    assert await get_company_billing_viewer(admin) is admin
    assert await get_company_billing_viewer(owner) is owner
    with pytest.raises(HTTPException) as error:
        await get_company_billing_manager(admin)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "company_billing_manage_required"
    assert await get_company_billing_manager(owner) is owner


class _PersonalUsageResult:
    def one(self):
        return (17, 3)


class _PersonalUsageDb:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _PersonalUsageResult()


@pytest.mark.asyncio
async def test_personal_usage_is_attributed_and_contains_no_company_balance(monkeypatch):
    user = _user("member")
    db = _PersonalUsageDb()
    monkeypatch.setattr(
        subscription_api,
        "get_tenant_entitlements",
        AsyncMock(
            return_value=SimpleNamespace(
                max_llm_calls_per_day=100,
                message_limit=50,
                max_triggers=10,
            )
        ),
    )

    result = await subscription_api.get_personal_usage(current_user=user, db=db)
    payload = result.model_dump()

    assert payload == {
        "attribution_status": "partial",
        "attribution_note": (
            "Only ledger consumption with an exact membership attribution is shown; "
            "company calls, messages, tokens, balances, and other members are excluded."
        ),
        "consumed_credits": 17,
        "attributed_transactions": 3,
        "llm_calls_limit": 100,
        "message_limit": 50,
        "max_triggers": 10,
    }
    query = str(db.statements[0])
    assert "credit_transactions.tenant_id" in query
    assert "credit_transactions.user_id" in query
    assert "credit_transactions.reason" in query
