"""Fast contracts for the guarded tenant-purge operator lane."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.api import admin
from app.services import tenant_purge


BACKEND_ROOT = Path(__file__).parents[1]


def _settings(**overrides):
    values = {
        "DATABASE_URL": "postgresql+asyncpg://local@127.0.0.1:5432/clawith_g11_purge_test",
        "ENVIRONMENT": "test",
        "ALLOW_LOCAL_TENANT_PURGE": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_physical_execution_requires_every_independent_local_guard(monkeypatch) -> None:
    tenant = SimpleNamespace(slug="g11-purge-fixture")
    monkeypatch.setattr(tenant_purge, "get_settings", lambda: _settings())
    tenant_purge.validate_local_purge_execution_target(tenant)

    cases = [
        ({"ALLOW_LOCAL_TENANT_PURGE": False}, "local_purge_disabled"),
        ({"ENVIRONMENT": "production"}, "local_purge_environment_required"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://local@database.internal/clawith_g11_purge_test"},
            "loopback_database_required",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://local@127.0.0.1/clawith"},
            "fixture_database_required",
        ),
    ]
    for overrides, expected_code in cases:
        monkeypatch.setattr(tenant_purge, "get_settings", lambda values=overrides: _settings(**values))
        with pytest.raises(tenant_purge.TenantPurgeError) as exc:
            tenant_purge.validate_local_purge_execution_target(tenant)
        assert exc.value.code == expected_code

    monkeypatch.setattr(tenant_purge, "get_settings", lambda: _settings())
    with pytest.raises(tenant_purge.TenantPurgeError) as exc:
        tenant_purge.validate_local_purge_execution_target(SimpleNamespace(slug="real-company"))
    assert exc.value.code == "fixture_tenant_required"


def test_delete_order_is_dependency_safe_and_forces_tenant_last() -> None:
    planner = tenant_purge.TenantRowPlanner(AsyncMock(), uuid.uuid4())
    planner.counts = {"tenants": 1, "users": 1, "audit_logs": 1}
    planner.foreign_keys = (
        tenant_purge.ForeignKeySpec(
            name="users_tenant",
            child_table="users",
            parent_table="tenants",
            child_columns=("tenant_id",),
            parent_columns=("id",),
            on_delete="NO ACTION",
        ),
        tenant_purge.ForeignKeySpec(
            name="audit_user",
            child_table="audit_logs",
            parent_table="users",
            child_columns=("user_id",),
            parent_columns=("id",),
            on_delete="NO ACTION",
        ),
    )

    assert planner._delete_order(set()) == ["audit_logs", "users", "tenants"]
    assert planner._delete_order({"users_tenant", "audit_user"})[-1] == "tenants"


def test_reason_codes_and_storage_prefixes_are_safe_and_stable() -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assert tenant_purge._safe_reason_code(" Case.REVIEW-123 ") == "case.review-123"
    with pytest.raises(tenant_purge.TenantPurgeError) as exc:
        tenant_purge._safe_reason_code("contains private email@example.com")
    assert exc.value.code == "invalid_reason_code"
    assert tenant_purge._storage_prefixes(tenant_id, (agent_id,)) == tuple(
        sorted(
            {
                str(agent_id),
                f"enterprise_info_{tenant_id}",
                f"_tenant_logos/{tenant_id}.png",
            }
        )
    )


@pytest.mark.asyncio
async def test_platform_endpoints_expose_plan_and_holds_but_not_execution(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    hold_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), identity_id=uuid.uuid4())
    db = AsyncMock()
    dry_run = AsyncMock(return_value={"status": "dry_run_passed"})
    create_hold = AsyncMock(return_value={"status": "active"})
    release_hold = AsyncMock(return_value={"status": "released"})
    monkeypatch.setattr(admin, "dry_run_tenant_purge", dry_run)
    monkeypatch.setattr(admin, "create_tenant_purge_hold", create_hold)
    monkeypatch.setattr(admin, "release_tenant_purge_hold", release_hold)

    assert await admin.run_tenant_deletion_dry_run(tenant_id, user, db) == {
        "status": "dry_run_passed"
    }
    assert await admin.add_tenant_deletion_hold(
        tenant_id,
        admin.TenantPurgeHoldRequest(
            hold_type="legal",
            reason_code="case.legal.123",
        ),
        user,
        db,
    ) == {"status": "active"}
    assert await admin.remove_tenant_deletion_hold(
        tenant_id,
        hold_id,
        admin.TenantPurgeHoldReleaseRequest(reason_code="case.review.complete"),
        user,
        db,
    ) == {"status": "released"}

    route_paths = {route.path for route in admin.router.routes}
    assert "/admin/tenant-deletions/{tenant_id}/dry-run" in route_paths
    assert "/admin/tenant-deletions/{tenant_id}/holds" in route_paths
    assert all("execute" not in path and "purge" not in path for path in route_paths)


@pytest.mark.asyncio
async def test_platform_endpoint_preserves_safe_error_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        admin,
        "dry_run_tenant_purge",
        AsyncMock(
            side_effect=tenant_purge.TenantPurgeError(
                "tenant_not_due",
                "The recoverable deletion window has not elapsed",
            )
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await admin.run_tenant_deletion_dry_run(
            uuid.uuid4(),
            SimpleNamespace(id=uuid.uuid4()),
            AsyncMock(),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "tenant_not_due",
        "message": "The recoverable deletion window has not elapsed",
    }


def test_controlled_runner_requires_explicit_confirmation_and_prints_no_names() -> None:
    source = (BACKEND_ROOT / "scripts/purge_expired_tenants.py").read_text(encoding="utf-8")
    assert "--confirm-local-fixture-purge" in source
    assert "dry_run_tenant_purge" in source
    assert "execute_tenant_purge" in source
    assert '"tenant_id"' in source
    assert '"error_code"' in source
    assert "tenant_name" not in source
