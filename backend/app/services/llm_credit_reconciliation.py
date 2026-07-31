"""Auditable operator reconciliation for ambiguous LLM Credits holds."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.database import async_session
from app.models.audit import AuditLog
from app.models.subscription import CreditReservation
from app.services.credit_service import (
    finalize_reserved_credits_in_session,
    mark_credit_reservation_settlement_ready_in_session,
    release_reserved_credits_in_session,
)


LLM_DEBT_RESOLUTIONS = {"provider_completed", "provider_not_accepted"}


@dataclass(frozen=True)
class LLMCreditReconciliationItem:
    reservation_id: str
    tenant_id: str
    status_before: str
    status_after: str
    held_amount: int
    settlement_amount: int | None


@dataclass(frozen=True)
class LLMCreditReconciliationResult:
    incident_key: str
    evidence_ref: str
    resolution: str
    applied: bool
    items: tuple[LLMCreditReconciliationItem, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_reservation_ids(
    reservation_ids: tuple[uuid.UUID, ...],
) -> tuple[uuid.UUID, ...]:
    unique = tuple(dict.fromkeys(reservation_ids))
    if not unique:
        raise ValueError("at least one exact reservation_id is required")
    if len(unique) > 100:
        raise ValueError("at most 100 exact reservation IDs may be reconciled at once")
    return unique


def _validate_resolution_state(
    reservation: CreditReservation,
    *,
    expected_tenant_id: uuid.UUID,
    resolution: str,
    settlement_amount: int | None,
) -> bool:
    if reservation.tenant_id != expected_tenant_id:
        raise ValueError("one or more credit reservations are outside the expected tenant")
    if reservation.ref_type != "llm_round":
        raise ValueError("only llm_round credit reservations may use this reconciliation")

    if resolution == "provider_completed":
        if settlement_amount is None or settlement_amount < 0:
            raise ValueError("provider_completed requires an exact settlement_amount")
        if reservation.status == "finalized":
            if reservation.amount != settlement_amount:
                raise ValueError("finalized reservation has a different exact amount")
            return True
        if reservation.status not in {"provider_inflight", "settlement_ready"}:
            raise ValueError(
                "provider_completed requires provider_inflight or settlement_ready status"
            )
        if reservation.status == "settlement_ready" and reservation.amount != settlement_amount:
            raise ValueError("settlement_ready reservation has a different exact amount")
        return False

    if resolution == "provider_not_accepted":
        if settlement_amount is not None:
            raise ValueError("provider_not_accepted must not include settlement_amount")
        if reservation.status in {"released", "expired"}:
            return True
        if reservation.status != "provider_inflight":
            raise ValueError("provider_not_accepted requires provider_inflight status")
        return False

    raise ValueError("unsupported LLM credit resolution")


async def resolve_llm_credit_holds(
    *,
    reservation_ids: tuple[uuid.UUID, ...],
    expected_tenant_id: uuid.UUID,
    incident_key: str,
    evidence_ref: str,
    resolution: str,
    settlement_amount: int | None = None,
    actor_user_id: uuid.UUID | None = None,
    apply: bool = False,
) -> LLMCreditReconciliationResult:
    """Resolve exact ambiguous LLM holds with evidence and tenant fencing."""

    normalized_ids = _normalize_reservation_ids(reservation_ids)
    normalized_incident_key = incident_key.strip()
    normalized_evidence = evidence_ref.strip()
    normalized_resolution = resolution.strip().lower()
    if not normalized_incident_key:
        raise ValueError("incident_key is required")
    if not normalized_evidence:
        raise ValueError("provider evidence_ref is required")
    if len(normalized_incident_key) > 200 or len(normalized_evidence) > 500:
        raise ValueError("incident_key or evidence_ref is too long")
    if normalized_resolution not in LLM_DEBT_RESOLUTIONS:
        raise ValueError("unsupported LLM credit resolution")

    async with async_session() as db:
        query = select(CreditReservation).where(
            CreditReservation.id.in_(normalized_ids)
        )
        if apply:
            query = query.with_for_update()
        result = await db.execute(query)
        reservations = list(result.scalars().all())
        reservation_by_id = {reservation.id: reservation for reservation in reservations}
        missing = [
            str(reservation_id)
            for reservation_id in normalized_ids
            if reservation_id not in reservation_by_id
        ]
        if missing:
            raise ValueError(f"credit reservation not found: {', '.join(missing)}")

        items: list[LLMCreditReconciliationItem] = []
        for reservation_id in normalized_ids:
            reservation = reservation_by_id[reservation_id]
            already_resolved = _validate_resolution_state(
                reservation,
                expected_tenant_id=expected_tenant_id,
                resolution=normalized_resolution,
                settlement_amount=settlement_amount,
            )
            status_before = reservation.status
            held_amount = int(reservation.amount or 0)
            if apply and not already_resolved:
                if normalized_resolution == "provider_completed":
                    assert settlement_amount is not None
                    if reservation.status == "provider_inflight":
                        await mark_credit_reservation_settlement_ready_in_session(
                            db,
                            reservation.id,
                            amount=settlement_amount,
                        )
                    await finalize_reserved_credits_in_session(db, reservation.id)
                else:
                    await release_reserved_credits_in_session(
                        db,
                        reservation.id,
                        status="released",
                        release_provider_inflight=True,
                    )

            items.append(
                LLMCreditReconciliationItem(
                    reservation_id=str(reservation.id),
                    tenant_id=str(reservation.tenant_id),
                    status_before=status_before,
                    status_after=reservation.status if apply else status_before,
                    held_amount=held_amount,
                    settlement_amount=(
                        settlement_amount
                        if normalized_resolution == "provider_completed"
                        else None
                    ),
                )
            )

        if apply:
            db.add(
                AuditLog(
                    user_id=actor_user_id,
                    action="llm_credit_hold_resolution",
                    details={
                        "incident_key": normalized_incident_key,
                        "evidence_ref": normalized_evidence,
                        "resolution": normalized_resolution,
                        "reservation_ids": [
                            str(reservation_id) for reservation_id in normalized_ids
                        ],
                        "expected_tenant_id": str(expected_tenant_id),
                        "settlement_amount": settlement_amount,
                    },
                )
            )
            await db.commit()

    return LLMCreditReconciliationResult(
        incident_key=normalized_incident_key,
        evidence_ref=normalized_evidence,
        resolution=normalized_resolution,
        applied=apply,
        items=tuple(items),
    )
