import uuid
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

from app.api import saas as saas_api
from app.schemas.saas import MediaProviderDebtResolutionIn
from app.services import media_incident_remediation as remediation
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


class _DebtResult:
    def __init__(self, task):
        self.task = task

    def scalars(self):
        return self

    def all(self):
        return [self.task]


class _DebtSession:
    def __init__(self, task, reservation):
        self.task = task
        self.reservation = reservation
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return _DebtResult(self.task)

    async def get(self, _model, record_id, **_kwargs):
        if record_id == self.reservation.id:
            return self.reservation
        return None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_provider_rejected_resolution_releases_hold_and_is_audited(monkeypatch):
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        ref_type="media_task",
        ref_id=task_id,
        status="provider_inflight",
        amount=490,
    )
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        reservation_id=reservation.id,
        status="submission_ambiguous",
        last_error=None,
        completed_at=None,
        next_poll_at=None,
        request_metadata={},
    )
    session = _DebtSession(task, reservation)

    async def release(_db, reservation_id, **_kwargs):
        assert reservation_id == reservation.id
        assert _kwargs == {"status": "released", "release_provider_inflight": True}
        reservation.status = "released"

    delete_brand = AsyncMock()
    monkeypatch.setattr(remediation, "async_session", lambda: session)
    monkeypatch.setattr(remediation, "release_reserved_credits_in_session", release)
    monkeypatch.setattr(remediation, "_delete_private_media_recovery_assets", delete_brand)

    result = await remediation.resolve_media_provider_debt(
        task_ids=(task.id,),
        expected_tenant_id=tenant_id,
        incident_key="INC-REJECTED",
        evidence_ref="provider-ticket:rejected",
        resolution="provider_rejected",
        actor_user_id=uuid.uuid4(),
        apply=True,
    )

    assert result.applied is True
    assert task.status == "failed"
    assert reservation.status == "released"
    assert session.committed is True
    assert len(session.added) == 1
    delete_brand.assert_awaited_once_with(task, strict=True)


@pytest.mark.asyncio
async def test_provider_accepted_resolution_consumes_and_compensates_once(monkeypatch):
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        ref_type="media_task",
        ref_id=task_id,
        status="provider_inflight",
        amount=446,
    )
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        reservation_id=reservation.id,
        status="submission_ambiguous",
        last_error=None,
        completed_at=None,
        next_poll_at=None,
        request_metadata={},
    )
    sessions = []

    def session_factory():
        session = _DebtSession(task, reservation)
        sessions.append(session)
        return session

    settle = AsyncMock()
    finalize = AsyncMock()
    grant = AsyncMock()

    async def mark_ready(_db, reservation_id, *, amount):
        assert reservation_id == reservation.id
        assert amount == 446
        reservation.status = "settlement_ready"
        await settle()

    async def finalize_once(_db, reservation_id):
        assert reservation_id == reservation.id
        reservation.status = "finalized"
        await finalize()

    delete_brand = AsyncMock(return_value=True)
    monkeypatch.setattr(remediation, "async_session", session_factory)
    monkeypatch.setattr(
        remediation,
        "mark_credit_reservation_settlement_ready_in_session",
        mark_ready,
    )
    monkeypatch.setattr(
        remediation,
        "finalize_reserved_credits_in_session",
        finalize_once,
    )
    monkeypatch.setattr(remediation, "grant_credits_in_session", grant)
    monkeypatch.setattr(remediation, "_delete_private_media_recovery_assets", delete_brand)

    first = await remediation.resolve_media_provider_debt(
        task_ids=(task.id,),
        expected_tenant_id=tenant_id,
        incident_key="INC-ACCEPTED",
        evidence_ref="provider-ticket:accepted",
        resolution="provider_accepted",
        apply=True,
    )
    second = await remediation.resolve_media_provider_debt(
        task_ids=(task.id,),
        expected_tenant_id=tenant_id,
        incident_key="INC-ACCEPTED",
        evidence_ref="provider-ticket:accepted",
        resolution="provider_accepted",
        apply=True,
    )

    assert first.items[0].status_after == "compensated"
    assert first.items[0].compensation_credits == 446
    assert second.items[0].status_before == "compensated"
    assert task.status == "compensated"
    assert reservation.status == "finalized"
    settle.assert_awaited_once()
    finalize.assert_awaited_once()
    grant.assert_awaited_once_with(
        sessions[0],
        tenant_id=tenant_id,
        amount=446,
        reason="refund",
        granted_by=user_id,
        ref_type="media_task",
        ref_id=task.id,
    )
    assert delete_brand.await_count == 2
    assert all(session.committed for session in sessions)


def test_legacy_nonrefundable_asset_loss_is_repairable_with_compensation():
    task = SimpleNamespace(status="closed_nonrefundable")
    reservation = SimpleNamespace(status="finalized")

    assert _validate_provider_debt_state(
        task,
        reservation,
        "close_asset_loss",
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value_factory"),
    [
        ("tenant_id", uuid.uuid4),
        ("agent_id", uuid.uuid4),
        ("user_id", uuid.uuid4),
        ("ref_type", lambda: "other_operation"),
        ("ref_id", uuid.uuid4),
    ],
)
async def test_provider_debt_resolution_rejects_non_owned_reservation(
    field,
    value_factory,
):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=task.tenant_id,
        agent_id=task.agent_id,
        user_id=task.user_id,
        ref_type="media_task",
        ref_id=task.id,
    )
    task.reservation_id = reservation.id
    setattr(reservation, field, value_factory())

    with pytest.raises(ValueError, match="reservation ownership is invalid"):
        await remediation._lock_owned_media_reservation(
            _DebtSession(task, reservation),
            task,
        )
