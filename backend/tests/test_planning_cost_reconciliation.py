"""Fail-closed contracts for Planning cost operations."""

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from pydantic import ValidationError

from app.models.agent_run import LLMSystemCostReceipt, LLMSystemCostResolution
from app.models.audit import AuditLog
from app.schemas.saas import PlanningCostResolutionIn
from app.services.planning_cost_reconciliation import (
    PlanningCostResolutionError,
    apply_planning_cost_resolution_in_session,
    scan_stale_planning_costs_in_session,
)


class _Result:
    def __init__(self, *, one=None, values=None) -> None:
        self._one = one
        self._values = values or []

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.added = []
        self.flushes = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


def _ambiguous_receipt() -> LLMSystemCostReceipt:
    now = datetime.now(UTC)
    return LLMSystemCostReceipt(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        call_index=1,
        operation="group_planning",
        model_id=uuid.uuid4(),
        provider="minimax",
        model="MiniMax-M3",
        provider_service_tier="standard",
        request_fingerprint="a" * 64,
        status="reconciling",
        provider_outcome="acceptance_unknown",
        usage_source="unknown",
        budget_reservation_credits=4,
        request_input_token_upper_bound=2_000,
        request_max_output_tokens=2_048,
        cost_status="unpriced",
        reconciliation_error_code="ProviderTimeout",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_confirm_not_accepted_is_tenant_fenced_audited_and_replay_safe() -> None:
    receipt = _ambiguous_receipt()
    db = _Session([_Result(one=receipt), _Result(one=None)])
    actor_id = uuid.uuid4()

    result = await apply_planning_cost_resolution_in_session(
        db,  # type: ignore[arg-type]
        receipt_id=receipt.id,
        expected_tenant_id=receipt.tenant_id,
        expected_status="reconciling",
        expected_provider_outcome="acceptance_unknown",
        disposition="confirm_not_accepted",
        evidence_ref="provider-query:ORDER_NOT_FOUND",
        reason="Provider support confirmed the request was never accepted",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        system_cost_credits=None,
        actor_user_id=actor_id,
        idempotency_key="cost-resolution-0001",
    )

    assert result.replayed is False
    assert receipt.status == "voided"
    assert receipt.provider_outcome == "not_accepted"
    assert receipt.cost_status == "not_applicable"
    assert receipt.system_cost_credits == 0
    resolution = next(
        item for item in db.added if isinstance(item, LLMSystemCostResolution)
    )
    audit = next(item for item in db.added if isinstance(item, AuditLog))
    assert resolution.actor_user_id == actor_id
    assert audit.details["non_targets"] == [
        "customer Credits",
        "other tenants",
        "other Planning receipts",
        "Provider retry",
    ]
    assert db.flushes == 1

    replay_db = _Session([_Result(one=receipt), _Result(one=resolution)])
    replay = await apply_planning_cost_resolution_in_session(
        replay_db,  # type: ignore[arg-type]
        receipt_id=receipt.id,
        expected_tenant_id=receipt.tenant_id,
        expected_status="reconciling",
        expected_provider_outcome="acceptance_unknown",
        disposition="confirm_not_accepted",
        evidence_ref="provider-query:ORDER_NOT_FOUND",
        reason="Provider support confirmed the request was never accepted",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        system_cost_credits=None,
        actor_user_id=actor_id,
        idempotency_key="cost-resolution-0001",
    )
    assert replay.replayed is True
    assert replay_db.added == []


@pytest.mark.asyncio
async def test_settle_accepted_requires_exact_tenant_state_usage_and_cost() -> None:
    receipt = _ambiguous_receipt()
    db = _Session([_Result(one=receipt), _Result(one=None)])

    result = await apply_planning_cost_resolution_in_session(
        db,  # type: ignore[arg-type]
        receipt_id=receipt.id,
        expected_tenant_id=receipt.tenant_id,
        expected_status="reconciling",
        expected_provider_outcome="acceptance_unknown",
        disposition="settle_accepted",
        evidence_ref="provider-bill:statement-20260822",
        reason="Provider query confirms acceptance and exact metered usage",
        input_tokens=1_000,
        output_tokens=200,
        total_tokens=1_200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        system_cost_credits=2,
        actor_user_id=uuid.uuid4(),
        idempotency_key="cost-resolution-0002",
    )

    assert result.receipt.status == "reconciled"
    assert result.receipt.usage_source == "operator_reported"
    assert result.receipt.system_cost_credits == 2
    assert result.receipt.response_snapshot is None

    cross_tenant = _ambiguous_receipt()
    cross_db = _Session([_Result(one=cross_tenant), _Result(one=None)])
    with pytest.raises(PlanningCostResolutionError, match="tenant"):
        await apply_planning_cost_resolution_in_session(
            cross_db,  # type: ignore[arg-type]
            receipt_id=cross_tenant.id,
            expected_tenant_id=uuid.uuid4(),
            expected_status="reconciling",
            expected_provider_outcome="acceptance_unknown",
            disposition="confirm_not_accepted",
            evidence_ref="provider-query:not-found",
            reason="Provider confirms the request was not accepted",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            system_cost_credits=None,
            actor_user_id=uuid.uuid4(),
            idempotency_key="cost-resolution-cross-tenant",
        )


@pytest.mark.asyncio
async def test_resolution_rejects_short_key_and_payload_drift_on_replay() -> None:
    receipt = _ambiguous_receipt()
    short_key_db = _Session([])
    with pytest.raises(PlanningCostResolutionError, match="Idempotency-Key") as error:
        await apply_planning_cost_resolution_in_session(
            short_key_db,  # type: ignore[arg-type]
            receipt_id=receipt.id,
            expected_tenant_id=receipt.tenant_id,
            expected_status="reconciling",
            expected_provider_outcome="acceptance_unknown",
            disposition="confirm_not_accepted",
            evidence_ref="provider-query:not-found",
            reason="Provider confirms the request was not accepted",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            system_cost_credits=None,
            actor_user_id=uuid.uuid4(),
            idempotency_key="short",
        )
    assert error.value.status_code == 400
    assert short_key_db.results == []

    existing = LLMSystemCostResolution(
        id=uuid.uuid4(),
        receipt_id=receipt.id,
        tenant_id=receipt.tenant_id,
        actor_user_id=uuid.uuid4(),
        idempotency_key_hash="unused-by-stub",
        request_fingerprint="different-payload-fingerprint",
        action="confirm_not_accepted",
        source="operator",
        evidence_ref="provider-query:not-found",
        reason="Provider confirms the request was not accepted",
        previous_status="reconciling",
        resulting_status="voided",
        previous_provider_outcome="acceptance_unknown",
        resulting_provider_outcome="not_accepted",
        created_at=datetime.now(UTC),
    )
    drift_db = _Session([_Result(one=receipt), _Result(one=existing)])
    with pytest.raises(PlanningCostResolutionError, match="different"):
        await apply_planning_cost_resolution_in_session(
            drift_db,  # type: ignore[arg-type]
            receipt_id=receipt.id,
            expected_tenant_id=receipt.tenant_id,
            expected_status="reconciling",
            expected_provider_outcome="acceptance_unknown",
            disposition="confirm_not_accepted",
            evidence_ref="provider-query:not-found",
            reason="Provider confirms the request was not accepted",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            system_cost_credits=None,
            actor_user_id=uuid.uuid4(),
            idempotency_key="cost-resolution-drift",
        )
    assert drift_db.added == []


def test_resolution_schema_rejects_partial_usage_extra_fields_and_fake_zero_usage() -> None:
    base = {
        "expected_tenant_id": uuid.uuid4(),
        "expected_status": "reconciling",
        "expected_provider_outcome": "acceptance_unknown",
        "disposition": "settle_accepted",
        "evidence_ref": "provider-bill:statement",
        "reason": "Provider statement proves exact accepted usage",
    }
    with pytest.raises(ValidationError):
        PlanningCostResolutionIn.model_validate(
            {**base, "input_tokens": 1, "unexpected_override": "skip checks"}
        )
    with pytest.raises(ValidationError):
        PlanningCostResolutionIn.model_validate(
            {
                **base,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "system_cost_credits": 0,
            }
        )


@pytest.mark.asyncio
async def test_stale_scan_never_retries_or_voids_and_preview_does_not_write() -> None:
    now = datetime.now(UTC)
    stale = _ambiguous_receipt()
    stale.status = "provider_inflight"
    stale.provider_outcome = "pending"
    stale.usage_source = "pending"
    stale.cost_status = "pending"
    stale.updated_at = now - timedelta(hours=1)

    preview_db = _Session([_Result(values=[stale])])
    preview = await scan_stale_planning_costs_in_session(
        preview_db,  # type: ignore[arg-type]
        stale_after_seconds=600,
        limit=10,
        apply=False,
        source="operator",
        actor_user_id=uuid.uuid4(),
        evidence_ref="ops-preview:planning-cost",
        reason="Preview stale provider inflight receipts before applying",
        now=now,
    )
    assert preview.candidate_receipt_ids == (stale.id,)
    assert preview.applied_count == 0
    assert preview_db.added == []
    assert stale.status == "provider_inflight"

    apply_db = _Session([_Result(values=[stale])])
    applied = await scan_stale_planning_costs_in_session(
        apply_db,  # type: ignore[arg-type]
        stale_after_seconds=600,
        limit=10,
        apply=True,
        source="daemon",
        actor_user_id=None,
        evidence_ref="runtime:planning-cost-stale-scan",
        reason="Provider acceptance is unknown after the inflight lease became stale",
        now=now,
    )
    assert applied.applied_count == 1
    assert stale.status == "reconciling"
    assert stale.provider_outcome == "acceptance_unknown"
    assert stale.reconciliation_error_code == "stale_provider_inflight"
    assert sum(isinstance(item, LLMSystemCostResolution) for item in apply_db.added) == 1
    assert sum(isinstance(item, AuditLog) for item in apply_db.added) == 1
