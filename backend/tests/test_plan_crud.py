"""Tests for plan CRUD API (admin) + LLMModel modality/tier exposure.

Covers:
- LLMModelOut/Create/Update expose modality/tier (frontend filter prerequisite)
- PlanCreateIn/PlanUpdateIn schemas
- create_plan / update_plan / delete_plan logic (direct call, admin user, mocked DB)
- non-admin rejected with 403 (TestClient, end-to-end dep chain)
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.subscription import create_plan, delete_plan, router, update_plan
from app.core.security import get_current_user
from app.database import get_db
from app.models.subscription import Plan
from app.schemas.schemas import LLMModelCreate, LLMModelOut, LLMModelUpdate
from pydantic import ValidationError

from app.schemas.saas import BillingRuleCreateIn, CreditPackCreateIn
from app.schemas.subscription import PlanCreateIn, PlanUpdateIn


# ── Mock DB ──────────────────────────────────────────────────────────


class DummyResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return [self._scalar] if self._scalar else []


class MockDB:
    def __init__(self, get_value=None, execute_scalar=None):
        self._get_value = get_value
        self._execute_scalar = execute_scalar
        self.added = []
        self.committed = False

    async def execute(self, _stmt=None, _params=None):
        return DummyResult(scalar=self._execute_scalar)

    def add(self, v):
        self.added.append(v)

    async def get(self, _model, _id, **_kwargs):
        return self._get_value

    async def commit(self):
        self.committed = True

    async def refresh(self, _v):
        pass

    async def flush(self):
        pass


def _admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(), role="platform_admin", tenant_id=uuid.uuid4(), identity=None
    )


# ── Schema tests (pure, no DB) ───────────────────────────────────────


def test_llm_model_out_exposes_modality_tier():
    """Frontend plan-filter needs modality/tier in the API response."""
    fields = LLMModelOut.model_fields
    assert "modality" in fields
    assert "tier" in fields


def test_llm_model_create_defaults():
    c = LLMModelCreate(provider="openai", model="gpt-4o", api_key="sk-x", label="GPT")
    assert c.modality == "text"
    assert c.tier == "standard"


def test_llm_model_update_modality_tier_optional():
    u = LLMModelUpdate()
    assert u.modality is None
    assert u.tier is None


def test_plan_create_in_defaults():
    p = PlanCreateIn(code="pro", name="Pro")
    assert p.max_agents == 2
    assert p.allowed_modalities is None
    assert p.tier == 0  # plan tier (rank), not model tier


def test_plan_update_requires_concurrency_precondition():
    with pytest.raises(ValidationError):
        PlanUpdateIn()

    expected_updated_at = datetime.now(timezone.utc)
    p = PlanUpdateIn(expected_updated_at=expected_updated_at)
    assert p.name is None
    assert p.allowed_modalities is None
    assert p.is_active is None
    assert p.expected_updated_at == expected_updated_at


def test_billing_rule_schema_rejects_unknown_tier_and_negative_cost():
    with pytest.raises(ValidationError):
        BillingRuleCreateIn(action="chat", modality="text", tier="standard", credit_cost=1)

    with pytest.raises(ValidationError):
        BillingRuleCreateIn(action="chat", modality="text", tier="lite", credit_cost=-1)


def test_credit_pack_schema_rejects_invalid_money_values():
    with pytest.raises(ValidationError):
        CreditPackCreateIn(code="bad", name="Bad", credits=0, price_cents=100, currency="USD")

    with pytest.raises(ValidationError):
        CreditPackCreateIn(code="bad", name="Bad", credits=1000, price_cents=-1, currency="USD")

    with pytest.raises(ValidationError):
        CreditPackCreateIn(code="bad", name="Bad", credits=1000, price_cents=100, currency="US")


# ── create_plan logic ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_plan_success():
    db = MockDB(execute_scalar=None)  # no existing plan with that code
    data = PlanCreateIn(
        code="pro",
        name="Pro",
        allowed_modalities=["text", "vision"],
        allowed_tiers=["standard", "premium"],
    )
    plan = await create_plan(data, current_user=_admin_user(), db=db)
    assert plan.code == "pro"
    assert plan.allowed_modalities == ["text", "vision"]
    assert plan.allowed_tiers == ["standard", "premium"]
    assert db.committed
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_create_plan_duplicate_code_409():
    existing = Plan(code="pro", name="Old Pro")
    db = MockDB(execute_scalar=existing)
    data = PlanCreateIn(code="pro", name="Pro")
    with pytest.raises(HTTPException) as exc:
        await create_plan(data, current_user=_admin_user(), db=db)
    assert exc.value.status_code == 409


# ── update_plan logic ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_plan_not_found_404():
    db = MockDB(get_value=None)
    with pytest.raises(HTTPException) as exc:
        await update_plan(
            uuid.uuid4(),
            PlanUpdateIn(
                allowed_modalities=["text"],
                expected_updated_at=datetime.now(timezone.utc),
            ),
            current_user=_admin_user(),
            db=db,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_plan_applies_fields():
    current_updated_at = datetime.now(timezone.utc)
    existing = Plan(
        code="pro",
        name="Pro",
        allowed_modalities=["text"],
        allowed_tiers=["standard"],
        is_active=True,
        updated_at=current_updated_at,
    )
    db = MockDB(get_value=existing)
    await update_plan(
        uuid.uuid4(),
        PlanUpdateIn(
            allowed_modalities=["text", "vision"],
            is_active=False,
            expected_updated_at=current_updated_at,
        ),
        current_user=_admin_user(),
        db=db,
    )
    assert existing.allowed_modalities == ["text", "vision"]
    assert existing.is_active is False
    assert db.committed


@pytest.mark.asyncio
async def test_update_plan_rejects_stale_admin_snapshot():
    current_updated_at = datetime.now(timezone.utc)
    existing = Plan(
        code="pro",
        name="Pro",
        price_cents=100,
        updated_at=current_updated_at,
    )
    db = MockDB(get_value=existing)

    with pytest.raises(HTTPException) as exc:
        await update_plan(
            uuid.uuid4(),
            PlanUpdateIn(
                price_cents=200,
                expected_updated_at=current_updated_at - timedelta(seconds=1),
            ),
            current_user=_admin_user(),
            db=db,
        )

    assert exc.value.status_code == 409
    assert existing.price_cents == 100
    assert not db.committed


@pytest.mark.asyncio
async def test_update_plan_accepts_matching_admin_snapshot_without_persisting_cas_field():
    current_updated_at = datetime.now(timezone.utc)
    existing = Plan(
        code="pro",
        name="Pro",
        price_cents=100,
        updated_at=current_updated_at,
    )
    db = MockDB(get_value=existing)

    await update_plan(
        uuid.uuid4(),
        PlanUpdateIn(price_cents=200, expected_updated_at=current_updated_at),
        current_user=_admin_user(),
        db=db,
    )

    assert existing.price_cents == 200
    assert "expected_updated_at" not in existing.__dict__
    assert db.committed


# ── delete_plan logic ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_plan_not_found_404():
    db = MockDB(get_value=None)
    with pytest.raises(HTTPException) as exc:
        await delete_plan(uuid.uuid4(), current_user=_admin_user(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_plan_free_protected_400():
    free_plan = Plan(code="free", name="Free", is_active=True)
    db = MockDB(get_value=free_plan)
    with pytest.raises(HTTPException) as exc:
        await delete_plan(uuid.uuid4(), current_user=_admin_user(), db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_plan_soft_deletes():
    plan = Plan(code="pro", name="Pro", is_active=True)
    db = MockDB(get_value=plan)
    await delete_plan(uuid.uuid4(), current_user=_admin_user(), db=db)
    assert plan.is_active is False
    assert db.committed


# ── Non-admin 403 (TestClient, end-to-end dependency chain) ──────────


def _app_with_user(*, role="member", email="member@example.com"):
    """Mount the subscription router with a chosen identity + mock DB."""
    app = FastAPI()
    app.include_router(router)
    user = SimpleNamespace(
        id=uuid.uuid4(), role=role, email=email, tenant_id=uuid.uuid4(), identity=None
    )

    async def _fake_user():
        return user

    async def _fake_db():
        return MockDB()

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = _fake_db
    return app


def _app_with_non_admin():
    return _app_with_user()


def test_non_admin_cannot_create_plan():
    client = TestClient(_app_with_non_admin())
    r = client.post("/subscription/plans", json={"code": "x", "name": "X"})
    assert r.status_code == 403


def test_non_admin_cannot_update_plan():
    client = TestClient(_app_with_non_admin())
    r = client.patch(
        f"/subscription/plans/{uuid.uuid4()}",
        json={"expected_updated_at": datetime.now(timezone.utc).isoformat()},
    )
    assert r.status_code == 403


def test_non_admin_cannot_delete_plan():
    client = TestClient(_app_with_non_admin())
    r = client.delete(f"/subscription/plans/{uuid.uuid4()}")
    assert r.status_code == 403


def test_tenant_org_admin_cannot_mutate_global_plan_catalog():
    client = TestClient(_app_with_user(role="org_admin", email="org-admin@example.com"))

    assert client.post("/subscription/plans", json={"code": "x", "name": "X"}).status_code == 403
    assert client.patch(
        f"/subscription/plans/{uuid.uuid4()}",
        json={"expected_updated_at": datetime.now(timezone.utc).isoformat()},
    ).status_code == 403
    assert client.delete(f"/subscription/plans/{uuid.uuid4()}").status_code == 403


def test_tenant_org_admin_cannot_assign_cross_tenant_subscription():
    client = TestClient(_app_with_user(role="org_admin", email="org-admin@example.com"))
    response = client.post(
        "/subscription/subscriptions/assign",
        json={"tenant_id": str(uuid.uuid4()), "plan_id": str(uuid.uuid4())},
    )

    assert response.status_code == 403
