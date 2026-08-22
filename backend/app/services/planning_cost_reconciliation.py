"""Evidence-backed resolution and stale-state scanning for Planning costs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models.agent_run import LLMSystemCostReceipt, LLMSystemCostResolution
from app.models.audit import AuditLog


PLANNING_COST_DISPOSITIONS = frozenset(
    {"confirm_not_accepted", "settle_accepted"}
)


class PlanningCostResolutionError(ValueError):
    """A fail-closed reconciliation error safe for the platform API."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PlanningCostResolutionResult:
    receipt: LLMSystemCostReceipt
    resolution: LLMSystemCostResolution
    replayed: bool


@dataclass(frozen=True, slots=True)
class PlanningCostStaleScanResult:
    cutoff: datetime
    candidate_receipt_ids: tuple[uuid.UUID, ...]
    applied_count: int


def hash_planning_cost_idempotency_key(raw_key: str | None) -> str:
    normalized = str(raw_key or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise PlanningCostResolutionError(
            "Idempotency-Key must contain between 8 and 128 non-whitespace characters",
            status_code=400,
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _resolution_fingerprint(
    *,
    receipt_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    expected_status: str,
    expected_provider_outcome: str,
    disposition: str,
    evidence_ref: str,
    reason: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
    system_cost_credits: int | None,
) -> str:
    payload = {
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "disposition": disposition,
        "evidence_ref": evidence_ref,
        "expected_provider_outcome": expected_provider_outcome,
        "expected_status": expected_status,
        "expected_tenant_id": str(expected_tenant_id),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reason": reason,
        "receipt_id": str(receipt_id),
        "system_cost_credits": system_cost_credits,
        "total_tokens": total_tokens,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_disposition_shape(
    *,
    disposition: str,
    expected_status: str,
    expected_provider_outcome: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
    system_cost_credits: int | None,
) -> None:
    if disposition not in PLANNING_COST_DISPOSITIONS:
        raise PlanningCostResolutionError(
            "Unsupported Planning cost disposition",
            status_code=422,
        )
    if expected_status != "reconciling" or expected_provider_outcome != "acceptance_unknown":
        raise PlanningCostResolutionError(
            "Planning cost resolution requires reconciling/acceptance_unknown",
            status_code=422,
        )
    values = (
        input_tokens,
        output_tokens,
        total_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        system_cost_credits,
    )
    if disposition == "confirm_not_accepted":
        if any(value is not None for value in values):
            raise PlanningCostResolutionError(
                "confirm_not_accepted must not include usage or cost",
                status_code=422,
            )
        return
    if any(value is None for value in values):
        raise PlanningCostResolutionError(
            "settle_accepted requires complete usage and system cost",
            status_code=422,
        )
    normalized = tuple(int(value) for value in values if value is not None)
    if any(value < 0 for value in normalized):
        raise PlanningCostResolutionError(
            "settle_accepted usage and cost must be non-negative",
            status_code=422,
        )
    assert input_tokens is not None
    assert output_tokens is not None
    assert total_tokens is not None
    if total_tokens <= 0 or total_tokens < input_tokens + output_tokens:
        raise PlanningCostResolutionError(
            "settle_accepted total_tokens must cover positive input and output usage",
            status_code=422,
        )


async def apply_planning_cost_resolution_in_session(
    db: AsyncSession,
    *,
    receipt_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    expected_status: str,
    expected_provider_outcome: str,
    disposition: str,
    evidence_ref: str,
    reason: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
    system_cost_credits: int | None,
    actor_user_id: uuid.UUID,
    idempotency_key: str | None,
) -> PlanningCostResolutionResult:
    """Resolve exactly one ambiguous receipt without inferring Provider state."""

    _validate_disposition_shape(
        disposition=disposition,
        expected_status=expected_status,
        expected_provider_outcome=expected_provider_outcome,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        system_cost_credits=system_cost_credits,
    )
    idempotency_key_hash = hash_planning_cost_idempotency_key(idempotency_key)
    request_fingerprint = _resolution_fingerprint(
        receipt_id=receipt_id,
        expected_tenant_id=expected_tenant_id,
        expected_status=expected_status,
        expected_provider_outcome=expected_provider_outcome,
        disposition=disposition,
        evidence_ref=evidence_ref,
        reason=reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        system_cost_credits=system_cost_credits,
    )
    receipt_result = await db.execute(
        select(LLMSystemCostReceipt)
        .where(LLMSystemCostReceipt.id == receipt_id)
        .with_for_update()
    )
    receipt = receipt_result.scalar_one_or_none()
    if receipt is None:
        raise PlanningCostResolutionError("Planning cost receipt not found", status_code=404)
    existing_result = await db.execute(
        select(LLMSystemCostResolution).where(
            LLMSystemCostResolution.receipt_id == receipt_id,
            LLMSystemCostResolution.idempotency_key_hash == idempotency_key_hash,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise PlanningCostResolutionError(
                "Idempotency-Key was already used with a different Planning cost resolution"
            )
        return PlanningCostResolutionResult(
            receipt=receipt,
            resolution=existing,
            replayed=True,
        )
    if receipt.tenant_id != expected_tenant_id:
        raise PlanningCostResolutionError(
            "Planning cost receipt tenant does not match expected_tenant_id"
        )
    if receipt.status != expected_status or receipt.provider_outcome != expected_provider_outcome:
        raise PlanningCostResolutionError(
            "Planning cost receipt state changed before resolution"
        )

    now = datetime.now(UTC)
    previous_status = receipt.status
    previous_outcome = receipt.provider_outcome
    if disposition == "confirm_not_accepted":
        receipt.status = resulting_status = "voided"
        receipt.provider_outcome = resulting_outcome = "not_accepted"
        receipt.usage_source = "unknown"
        receipt.cost_status = "not_applicable"
        receipt.system_cost_credits = 0
    else:
        assert disposition == "settle_accepted"
        assert input_tokens is not None
        assert output_tokens is not None
        assert total_tokens is not None
        assert cache_read_tokens is not None
        assert cache_creation_tokens is not None
        assert system_cost_credits is not None
        receipt.status = resulting_status = "reconciled"
        receipt.provider_outcome = resulting_outcome = "accepted"
        receipt.usage_source = "operator_reported"
        receipt.input_tokens = input_tokens
        receipt.output_tokens = output_tokens
        receipt.total_tokens = total_tokens
        receipt.cache_read_tokens = cache_read_tokens
        receipt.cache_creation_tokens = cache_creation_tokens
        receipt.estimated_tokens = 0
        receipt.system_cost_credits = system_cost_credits
        receipt.cost_status = "priced"
        receipt.provider_accepted_at = receipt.provider_accepted_at or now
    receipt.finalized_at = now
    receipt.reconciliation_error_code = None
    resolution = LLMSystemCostResolution(
        id=uuid.uuid4(),
        receipt_id=receipt.id,
        tenant_id=receipt.tenant_id,
        actor_user_id=actor_user_id,
        idempotency_key_hash=idempotency_key_hash,
        request_fingerprint=request_fingerprint,
        action=disposition,
        source="operator",
        evidence_ref=evidence_ref,
        reason=reason,
        previous_status=previous_status,
        resulting_status=resulting_status,
        previous_provider_outcome=previous_outcome,
        resulting_provider_outcome=resulting_outcome,
        reported_system_cost_credits=system_cost_credits,
        created_at=now,
    )
    db.add(resolution)
    db.add(
        AuditLog(
            tenant_id=receipt.tenant_id,
            user_id=actor_user_id,
            agent_id=None,
            action="saas_planning_cost_resolution",
            details={
                "receipt_id": str(receipt.id),
                "resolution_id": str(resolution.id),
                "disposition": disposition,
                "evidence_ref": evidence_ref,
                "reason": reason,
                "before": {
                    "status": previous_status,
                    "provider_outcome": previous_outcome,
                },
                "after": {
                    "status": resulting_status,
                    "provider_outcome": resulting_outcome,
                    "system_cost_credits": system_cost_credits,
                },
                "idempotency_key_hash": idempotency_key_hash,
                "non_targets": [
                    "customer Credits",
                    "other tenants",
                    "other Planning receipts",
                    "Provider retry",
                ],
            },
        )
    )
    await db.flush()
    return PlanningCostResolutionResult(
        receipt=receipt,
        resolution=resolution,
        replayed=False,
    )


async def scan_stale_planning_costs_in_session(
    db: AsyncSession,
    *,
    stale_after_seconds: int,
    limit: int,
    apply: bool,
    source: str,
    actor_user_id: uuid.UUID | None,
    evidence_ref: str,
    reason: str,
    now: datetime | None = None,
) -> PlanningCostStaleScanResult:
    """Move stale inflight rows to ambiguity review; never infer acceptance."""

    if source not in {"operator", "daemon"}:
        raise PlanningCostResolutionError("Unsupported stale-scan source", status_code=422)
    if source == "operator" and actor_user_id is None:
        raise PlanningCostResolutionError("Operator stale scan requires an actor", status_code=422)
    if stale_after_seconds < 60:
        raise PlanningCostResolutionError(
            "stale_after_seconds must be at least 60",
            status_code=422,
        )
    bounded_limit = min(max(int(limit), 1), 500)
    effective_now = now or datetime.now(UTC)
    cutoff = effective_now - timedelta(seconds=stale_after_seconds)
    stmt = (
        select(LLMSystemCostReceipt)
        .where(
            LLMSystemCostReceipt.status == "provider_inflight",
            LLMSystemCostReceipt.provider_outcome == "pending",
            LLMSystemCostReceipt.updated_at < cutoff,
        )
        .order_by(
            LLMSystemCostReceipt.updated_at.asc(),
            LLMSystemCostReceipt.id.asc(),
        )
        .limit(bounded_limit)
    )
    if apply:
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    receipts = list(result.scalars().all())
    if not apply:
        return PlanningCostStaleScanResult(
            cutoff=cutoff,
            candidate_receipt_ids=tuple(receipt.id for receipt in receipts),
            applied_count=0,
        )

    for receipt in receipts:
        previous_status = receipt.status
        previous_outcome = receipt.provider_outcome
        raw_key = f"stale:{receipt.id}:{receipt.updated_at.isoformat()}"
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        resolution = LLMSystemCostResolution(
            id=uuid.uuid4(),
            receipt_id=receipt.id,
            tenant_id=receipt.tenant_id,
            actor_user_id=actor_user_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=hashlib.sha256(
                f"mark_stale_unknown:{receipt.id}:{cutoff.isoformat()}".encode()
            ).hexdigest(),
            action="mark_stale_unknown",
            source=source,
            evidence_ref=evidence_ref,
            reason=reason,
            previous_status=previous_status,
            resulting_status="reconciling",
            previous_provider_outcome=previous_outcome,
            resulting_provider_outcome="acceptance_unknown",
            reported_system_cost_credits=None,
            created_at=effective_now,
        )
        receipt.status = "reconciling"
        receipt.provider_outcome = "acceptance_unknown"
        receipt.usage_source = "unknown"
        receipt.cost_status = "unpriced"
        receipt.system_cost_credits = None
        receipt.reconciliation_error_code = "stale_provider_inflight"
        db.add(resolution)
        db.add(
            AuditLog(
                tenant_id=receipt.tenant_id,
                user_id=actor_user_id,
                agent_id=None,
                action="planning_cost_stale_to_reconciling",
                details={
                    "receipt_id": str(receipt.id),
                    "resolution_id": str(resolution.id),
                    "source": source,
                    "before": {
                        "status": previous_status,
                        "provider_outcome": previous_outcome,
                    },
                    "after": {
                        "status": "reconciling",
                        "provider_outcome": "acceptance_unknown",
                    },
                    "non_targets": [
                        "customer Credits",
                        "Provider retry",
                        "receipt deletion",
                    ],
                },
            )
        )
    await db.flush()
    return PlanningCostStaleScanResult(
        cutoff=cutoff,
        candidate_receipt_ids=tuple(receipt.id for receipt in receipts),
        applied_count=len(receipts),
    )


async def run_planning_cost_stale_scan_once() -> PlanningCostStaleScanResult:
    settings = get_settings()
    async with async_session() as db:
        result = await scan_stale_planning_costs_in_session(
            db,
            stale_after_seconds=settings.PLANNING_SYSTEM_COST_INFLIGHT_STALE_SECONDS,
            limit=settings.PLANNING_SYSTEM_COST_RECONCILIATION_BATCH_SIZE,
            apply=True,
            source="daemon",
            actor_user_id=None,
            evidence_ref="runtime:planning-cost-stale-scan",
            reason="Provider acceptance could not be proven after the inflight lease became stale",
        )
        await db.commit()
        return result


async def start_planning_cost_reconciliation_daemon() -> None:
    settings = get_settings()
    interval = settings.PLANNING_SYSTEM_COST_RECONCILIATION_SCAN_SECONDS
    logger.info(
        "[planning-cost] reconciliation daemon started interval={}s stale_after={}s",
        interval,
        settings.PLANNING_SYSTEM_COST_INFLIGHT_STALE_SECONDS,
    )
    while True:
        try:
            result = await run_planning_cost_stale_scan_once()
            if result.applied_count:
                logger.warning(
                    "[planning-cost] stale receipts moved to reconciliation count={}",
                    result.applied_count,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[planning-cost] reconciliation iteration failed error_type={}",
                type(exc).__name__,
            )
        await asyncio.sleep(interval)


__all__ = [
    "PLANNING_COST_DISPOSITIONS",
    "PlanningCostResolutionError",
    "PlanningCostResolutionResult",
    "PlanningCostStaleScanResult",
    "apply_planning_cost_resolution_in_session",
    "hash_planning_cost_idempotency_key",
    "run_planning_cost_stale_scan_once",
    "scan_stale_planning_costs_in_session",
    "start_planning_cost_reconciliation_daemon",
]
