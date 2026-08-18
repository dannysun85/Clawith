"""Red-line product contracts for the four 2026-08-18 P0 closures.

These tests intentionally describe the target contract before the implementation
stories land.  They should fail only on the missing boundary they name, never on
test collection or infrastructure setup.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.schemas.work import WorkTaskPreflight, WorkTaskPreflightOut
from app.schemas.workforce_topology import WorkforceTopologyNodeOut
from app.services import access_control


def _member(*, role: str = "member") -> SimpleNamespace:
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="member@example.com",
        is_active=True,
        is_platform_admin=False,
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        role=role,
        tenant_id=uuid.uuid4(),
        is_active=True,
    )


def _stub_access_queries(monkeypatch) -> None:
    monkeypatch.setattr(access_control, "_has_managed_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(
        access_control,
        "_member_private_agent_creation_allowed",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        access_control,
        "_pending_invitation_count",
        AsyncMock(return_value=0),
    )


@pytest.mark.asyncio
async def test_tenant_sensitive_capabilities_come_from_membership_not_global_identity_grants(
    monkeypatch,
):
    member = _member()
    monkeypatch.setattr(
        access_control,
        "_identity_capabilities",
        AsyncMock(
            return_value={
                "company.create",
                "company.billing.manage",
                "company.analytics.view",
                "company.okr.manage",
            }
        ),
    )
    _stub_access_queries(monkeypatch)

    resolved = await access_control.resolve_effective_access(SimpleNamespace(), member)

    # ``company.create`` is intentionally account-global.  Tenant finance,
    # analytics, and OKR authority must never cross memberships with it.
    assert "company.create" in resolved.effective_capabilities
    assert "company.billing.manage" not in resolved.effective_capabilities
    assert "company.analytics.view" not in resolved.effective_capabilities
    assert "company.okr.manage" not in resolved.effective_capabilities


@pytest.mark.asyncio
async def test_billing_and_analytics_role_matrix_is_separated(monkeypatch):
    monkeypatch.setattr(access_control, "_identity_capabilities", AsyncMock(return_value=set()))
    _stub_access_queries(monkeypatch)

    admin = await access_control.resolve_effective_access(
        SimpleNamespace(),
        _member(role="org_admin"),
    )
    owner = await access_control.resolve_effective_access(
        SimpleNamespace(),
        _member(role="org_owner"),
    )

    assert "company.billing.view" in admin.effective_capabilities
    assert "company.analytics.view" in admin.effective_capabilities
    assert "company.billing.manage" not in admin.effective_capabilities
    assert "company.billing.manage" in owner.effective_capabilities


def test_topology_redacts_company_resource_metrics_with_null_not_zero():
    node = WorkforceTopologyNodeOut(
        id=uuid.uuid4(),
        name="Research Agent",
        status="running",
    )

    assert node.tokens_used_today is None
    assert node.cache_read_tokens_today is None
    assert node.max_tokens_per_day is None


def test_work_preflight_defaults_to_server_routing_and_preserves_manual_compatibility():
    automatic = WorkTaskPreflight(title="Review launch", intent="Review the launch plan")
    manual = WorkTaskPreflight(
        title="Review launch",
        intent="Review the launch plan",
        executor_kind="personal_assistant",
    )

    assert automatic.routing_mode == "auto"
    assert automatic.executor_kind is None
    assert manual.routing_mode == "manual"
    assert manual.executor_kind == "personal_assistant"


def test_work_preflight_response_contains_an_explainable_executor_proposal():
    fields = WorkTaskPreflightOut.model_fields

    assert "executor_proposal" in fields
    proposal_fields = fields["executor_proposal"].annotation.model_fields
    assert {
        "policy_version",
        "chosen_executor_kind",
        "agent_id",
        "reason_codes",
        "confidence",
        "candidates_considered",
        "capability_snapshot",
        "fallback",
    } <= set(proposal_fields)
