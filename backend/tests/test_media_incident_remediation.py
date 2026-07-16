import uuid
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

from app.api import saas as saas_api
from app.schemas.saas import MediaProviderDebtResolutionIn
from app.services.media_incident_remediation import (
    _normalize_task_ids,
    _validate_provider_debt_state,
    _validate_refundable_state,
    _validate_task_scope,
)


def test_media_remediation_requires_exact_nonempty_ids_and_deduplicates():
    task_id = uuid.uuid4()
    assert _normalize_task_ids((task_id, task_id)) == (task_id,)
    with pytest.raises(ValueError, match="at least one"):
        _normalize_task_ids(())


def test_media_remediation_fails_closed_on_tenant_mismatch():
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, tenant_id=uuid.uuid4(), status="retrying")
    with pytest.raises(ValueError, match="outside the expected tenant"):
        _validate_task_scope([task], (task_id,), uuid.uuid4())


def test_media_remediation_never_terminalizes_successful_assets():
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id, status="succeeded")
    with pytest.raises(ValueError, match="must never"):
        _validate_task_scope([task], (task_id,), tenant_id)


@pytest.mark.parametrize(
    "task_status",
    [
        "submission_ambiguous",
        "asset_repairing",
        "asset_delivery_failed",
        "settlement_ready",
    ],
)
def test_media_remediation_refuses_provider_debt_task_states(task_status):
    task = SimpleNamespace(status=task_status)
    with pytest.raises(ValueError, match="provider-accepted media debt"):
        _validate_refundable_state(task, None)


@pytest.mark.parametrize(
    "reservation_status",
    ["provider_inflight", "settlement_ready", "finalized"],
)
def test_media_remediation_refuses_provider_debt_reservation_states(
    reservation_status,
):
    task = SimpleNamespace(status="retrying")
    reservation = SimpleNamespace(status=reservation_status)
    with pytest.raises(ValueError, match="provider-accepted media debt"):
        _validate_refundable_state(task, reservation)


@pytest.mark.parametrize(
    ("resolution", "task_status", "reservation_status"),
    [
        ("provider_rejected", "submission_ambiguous", "provider_inflight"),
        ("provider_accepted", "submission_ambiguous", "provider_inflight"),
        ("provider_accepted", "submission_ambiguous", "settlement_ready"),
        ("close_asset_loss", "asset_delivery_failed", "finalized"),
        ("close_asset_loss", "asset_delivery_failed", "settlement_ready"),
    ],
)
def test_provider_debt_resolution_accepts_only_explicit_matching_state(
    resolution,
    task_status,
    reservation_status,
):
    task = SimpleNamespace(status=task_status)
    reservation = SimpleNamespace(status=reservation_status)

    assert _validate_provider_debt_state(task, reservation, resolution) is False


def test_provider_debt_resolution_fails_closed_on_mismatched_evidence_action():
    task = SimpleNamespace(status="asset_delivery_failed")
    reservation = SimpleNamespace(status="provider_inflight")

    with pytest.raises(ValueError, match="provider_rejected requires"):
        _validate_provider_debt_state(task, reservation, "provider_rejected")
    with pytest.raises(ValueError, match="already settled"):
        _validate_provider_debt_state(task, reservation, "close_asset_loss")


@pytest.mark.asyncio
async def test_saas_provider_debt_endpoint_forwards_actor_tenant_and_evidence():
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4())
    result = SimpleNamespace(to_dict=lambda: {"applied": False})
    request = MediaProviderDebtResolutionIn(
        task_ids=[task_id],
        expected_tenant_id=tenant_id,
        incident_key="INC-2026-0716",
        evidence_ref="provider-ticket:MM-123",
        resolution="provider_accepted",
        apply=False,
    )

    with patch.object(
        saas_api,
        "resolve_media_provider_debt",
        AsyncMock(return_value=result),
    ) as resolve:
        response = await saas_api.resolve_provider_media_debt(
            request,
            current_user=admin,
        )

    assert response == {"applied": False}
    resolve.assert_awaited_once_with(
        task_ids=(task_id,),
        expected_tenant_id=tenant_id,
        incident_key="INC-2026-0716",
        evidence_ref="provider-ticket:MM-123",
        resolution="provider_accepted",
        actor_user_id=admin.id,
        apply=False,
    )
