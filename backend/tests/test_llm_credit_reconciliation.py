import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import llm_credit_reconciliation as reconciliation


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, reservations):
        self.reservations = reservations
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return _Result(self.reservations)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


def _reservation(*, tenant_id, status="provider_inflight", amount=2):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        ref_type="llm_round",
        status=status,
        amount=amount,
    )


@pytest.mark.asyncio
async def test_provider_completed_resolution_settles_exact_hold_with_audit(monkeypatch):
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    reservation = _reservation(tenant_id=tenant_id)
    session = _Session([reservation])
    mark_ready = AsyncMock()
    finalize = AsyncMock()

    async def mark(_db, reservation_id, *, amount):
        assert reservation_id == reservation.id
        assert amount == 2
        reservation.status = "settlement_ready"
        await mark_ready()

    async def finish(_db, reservation_id):
        assert reservation_id == reservation.id
        reservation.status = "finalized"
        await finalize()

    monkeypatch.setattr(reconciliation, "async_session", lambda: session)
    monkeypatch.setattr(
        reconciliation,
        "mark_credit_reservation_settlement_ready_in_session",
        mark,
    )
    monkeypatch.setattr(
        reconciliation,
        "finalize_reserved_credits_in_session",
        finish,
    )

    result = await reconciliation.resolve_llm_credit_holds(
        reservation_ids=(reservation.id,),
        expected_tenant_id=tenant_id,
        incident_key="LOCAL-QA-LLM-HOLD",
        evidence_ref="chat-message:tool-call-returned",
        resolution="provider_completed",
        settlement_amount=2,
        actor_user_id=actor_id,
        apply=True,
    )

    assert result.items[0].status_after == "finalized"
    mark_ready.assert_awaited_once()
    finalize.assert_awaited_once()
    assert session.committed is True
    assert len(session.added) == 1
    assert session.added[0].action == "llm_credit_hold_resolution"


@pytest.mark.asyncio
async def test_provider_not_accepted_resolution_releases_only_inflight_hold(monkeypatch):
    tenant_id = uuid.uuid4()
    reservation = _reservation(tenant_id=tenant_id)
    session = _Session([reservation])
    release = AsyncMock()

    async def release_hold(_db, reservation_id, **kwargs):
        assert reservation_id == reservation.id
        assert kwargs == {
            "status": "released",
            "release_provider_inflight": True,
        }
        reservation.status = "released"
        await release()

    monkeypatch.setattr(reconciliation, "async_session", lambda: session)
    monkeypatch.setattr(
        reconciliation,
        "release_reserved_credits_in_session",
        release_hold,
    )

    result = await reconciliation.resolve_llm_credit_holds(
        reservation_ids=(reservation.id,),
        expected_tenant_id=tenant_id,
        incident_key="PROVIDER-REJECTED",
        evidence_ref="provider-ticket:rejected",
        resolution="provider_not_accepted",
        apply=True,
    )

    assert result.items[0].status_after == "released"
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolution_rejects_cross_tenant_or_non_llm_reservation(monkeypatch):
    tenant_id = uuid.uuid4()
    reservation = _reservation(tenant_id=uuid.uuid4())
    session = _Session([reservation])
    monkeypatch.setattr(reconciliation, "async_session", lambda: session)

    with pytest.raises(ValueError, match="outside the expected tenant"):
        await reconciliation.resolve_llm_credit_holds(
            reservation_ids=(reservation.id,),
            expected_tenant_id=tenant_id,
            incident_key="INC",
            evidence_ref="provider-ticket:1",
            resolution="provider_completed",
            settlement_amount=2,
        )

    reservation.tenant_id = tenant_id
    reservation.ref_type = "media_task"
    with pytest.raises(ValueError, match="only llm_round"):
        await reconciliation.resolve_llm_credit_holds(
            reservation_ids=(reservation.id,),
            expected_tenant_id=tenant_id,
            incident_key="INC",
            evidence_ref="provider-ticket:1",
            resolution="provider_completed",
            settlement_amount=2,
        )


@pytest.mark.asyncio
async def test_completed_resolution_rejects_a_different_finalized_amount(monkeypatch):
    tenant_id = uuid.uuid4()
    reservation = _reservation(tenant_id=tenant_id, status="finalized", amount=2)
    session = _Session([reservation])
    monkeypatch.setattr(reconciliation, "async_session", lambda: session)

    with pytest.raises(ValueError, match="different exact amount"):
        await reconciliation.resolve_llm_credit_holds(
            reservation_ids=(reservation.id,),
            expected_tenant_id=tenant_id,
            incident_key="INC",
            evidence_ref="provider-ticket:1",
            resolution="provider_completed",
            settlement_amount=3,
        )
