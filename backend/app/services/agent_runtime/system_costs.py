"""Durable, tenant-fenced system-cost accounting for Group Planning calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Protocol
import uuid

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models.agent_run import AgentRun, LLMSystemCostReceipt
from app.models.chat_session import ChatSession
from app.models.group import Group
from app.models.llm import LLMModel
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.state import RuntimeContext
from app.services.llm.caller import get_llm_request_options
from app.services.provider_pricing import provider_text_credits
from app.services.token_tracker import TokenUsage


class PlanningCostAccountingError(RuntimeError):
    """A system-cost call cannot safely start, settle, or replay."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlanningCostReplay:
    """Normalized provider result persisted before lifecycle advancement."""

    content: str | None
    tool_calls: tuple[dict, ...]
    usage: TokenUsage
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class PlanningCostAttempt:
    """One new provider call lease or an idempotent finalized replay."""

    receipt_id: uuid.UUID
    replay: PlanningCostReplay | None = None


@dataclass(frozen=True, slots=True)
class PlanningCostBudgetPolicy:
    """Server-owned pre-call caps for platform-funded Planning usage."""

    max_credits_per_run: int
    max_credits_per_tenant_day: int
    max_calls_per_run: int
    max_calls_per_tenant_day: int
    unpriced_reservation_credits: int

    @classmethod
    def from_settings(cls) -> "PlanningCostBudgetPolicy":
        settings = get_settings()
        return cls(
            max_credits_per_run=settings.PLANNING_SYSTEM_COST_MAX_CREDITS_PER_RUN,
            max_credits_per_tenant_day=(
                settings.PLANNING_SYSTEM_COST_MAX_CREDITS_PER_TENANT_DAY
            ),
            max_calls_per_run=settings.PLANNING_SYSTEM_COST_MAX_CALLS_PER_RUN,
            max_calls_per_tenant_day=(
                settings.PLANNING_SYSTEM_COST_MAX_CALLS_PER_TENANT_DAY
            ),
            unpriced_reservation_credits=(
                settings.PLANNING_SYSTEM_COST_UNPRICED_RESERVATION_CREDITS
            ),
        )


class PlanningCostAccountingPort(Protocol):
    async def find_replay_or_raise(
        self,
        *,
        context: RuntimeContext,
        model: LLMModel,
        request_fingerprint: str,
        call_index: int,
    ) -> PlanningCostReplay | None: ...

    async def begin(
        self,
        *,
        context: RuntimeContext,
        model: LLMModel,
        credential_id: uuid.UUID | None,
        request_fingerprint: str,
        call_index: int,
        request_input_token_upper_bound: int,
        request_max_output_tokens: int,
    ) -> PlanningCostAttempt: ...

    async def finalize(
        self,
        receipt_id: uuid.UUID,
        *,
        content: str | None,
        tool_calls: tuple[dict, ...],
        usage: TokenUsage,
        finish_reason: str | None,
    ) -> None: ...

    async def discard_unaccepted(self, receipt_id: uuid.UUID) -> None: ...

    async def mark_reconciling(
        self,
        receipt_id: uuid.UUID,
        *,
        provider_outcome: str,
        error_code: str,
        content: str | None = None,
        tool_calls: tuple[dict, ...] = (),
        usage: TokenUsage | None = None,
        finish_reason: str | None = None,
    ) -> None: ...


def planning_request_fingerprint(messages: list[object]) -> str:
    """Hash the exact normalized prompt without storing tenant business text."""

    payload = []
    for message in messages:
        formatter = getattr(message, "to_openai_format", None)
        payload.append(formatter() if callable(formatter) else str(message))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def planning_request_token_upper_bound(messages: list[object]) -> int:
    """Return a conservative, content-free input-token upper bound.

    UTF-8 byte length is deliberately used instead of a provider tokenizer:
    one token cannot encode more bytes than this bound, so non-ASCII prompts
    do not inherit the optimistic ``chars / 3`` heuristic used for reporting.
    Only the resulting integer is persisted.
    """

    payload = []
    for message in messages:
        formatter = getattr(message, "to_openai_format", None)
        payload.append(formatter() if callable(formatter) else str(message))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return max(len(canonical.encode("utf-8")), 1)


def planning_budget_reservation_credits(
    *,
    model: LLMModel,
    request_input_token_upper_bound: int,
    request_max_output_tokens: int,
    fallback_credits: int,
) -> int:
    """Quote the maximum configured call shape before Provider submission."""

    request_options = get_llm_request_options(model)
    quoted = provider_text_credits(
        model.provider,
        model.model,
        TokenUsage(
            input_tokens=request_input_token_upper_bound,
            output_tokens=request_max_output_tokens,
            total_tokens=request_input_token_upper_bound + request_max_output_tokens,
        ),
        service_tier=str(request_options.get("service_tier") or "standard"),
    )
    return max(int(quoted if quoted is not None else fallback_credits), 1)


def _response_snapshot(
    *,
    content: str | None,
    tool_calls: tuple[dict, ...],
    finish_reason: str | None,
) -> dict:
    snapshot = {
        "content": content,
        "tool_calls": [dict(item) for item in tool_calls],
        "finish_reason": finish_reason,
    }
    # JSONB is the replay authority.  Normalize provider-adapter values before
    # assigning the snapshot so a non-native scalar cannot make the final
    # ledger commit fail after the Provider has already accepted the request.
    return json.loads(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    )


def _normalized_usage(usage: TokenUsage) -> dict[str, int]:
    return {
        "input_tokens": max(int(usage.input_tokens or 0), 0),
        "output_tokens": max(int(usage.output_tokens or 0), 0),
        "total_tokens": max(int(usage.total_tokens or 0), 0),
        "cache_read_tokens": max(int(usage.cache_read_tokens or 0), 0),
        "cache_creation_tokens": max(int(usage.cache_creation_tokens or 0), 0),
        "estimated_tokens": max(int(usage.estimated_tokens or 0), 0),
    }


def _finalization_fingerprint(snapshot: dict, usage: TokenUsage) -> str:
    canonical = json.dumps(
        {"response": snapshot, "usage": _normalized_usage(usage)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _usage_source(usage: TokenUsage) -> str:
    normalized = _normalized_usage(usage)
    if normalized["estimated_tokens"] > 0:
        return "estimated"
    if any(
        normalized[field] > 0
        for field in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    ):
        return "provider_reported"
    return "unknown"


def _replay_from_receipt(receipt: LLMSystemCostReceipt) -> PlanningCostReplay:
    snapshot = receipt.response_snapshot
    if not isinstance(snapshot, dict):
        raise PlanningCostAccountingError(
            "planning_cost_receipt_invalid",
            "Finalized Planning cost receipt has no response snapshot",
        )
    raw_calls = snapshot.get("tool_calls", [])
    if not isinstance(raw_calls, list) or any(not isinstance(item, dict) for item in raw_calls):
        raise PlanningCostAccountingError(
            "planning_cost_receipt_invalid",
            "Finalized Planning cost receipt has invalid tool calls",
        )
    return PlanningCostReplay(
        content=(snapshot.get("content") if isinstance(snapshot.get("content"), str) else None),
        tool_calls=tuple(dict(item) for item in raw_calls),
        usage=TokenUsage(
            input_tokens=int(receipt.input_tokens or 0),
            output_tokens=int(receipt.output_tokens or 0),
            total_tokens=int(receipt.total_tokens or 0),
            cache_read_tokens=int(receipt.cache_read_tokens or 0),
            cache_creation_tokens=int(receipt.cache_creation_tokens or 0),
            estimated_tokens=int(receipt.estimated_tokens or 0),
        ),
        finish_reason=(
            snapshot.get("finish_reason")
            if isinstance(snapshot.get("finish_reason"), str)
            else None
        ),
    )


class PlanningSystemCostService:
    """Exactly-once system-cost outbox for Planning's provider boundary."""

    def __init__(
        self,
        session_factory: RuntimeSessionFactory,
        *,
        budget_policy: PlanningCostBudgetPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._budget_policy = budget_policy or PlanningCostBudgetPolicy.from_settings()

    @staticmethod
    def _ids(context: RuntimeContext) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        try:
            tenant_id = uuid.UUID(context.tenant_id)
            run_id = uuid.UUID(context.run_id)
            session_id = uuid.UUID(str(context.session_id or ""))
        except (AttributeError, TypeError, ValueError) as exc:
            raise PlanningCostAccountingError(
                "planning_cost_context_invalid",
                "Planning cost context has invalid identifiers",
            ) from exc
        return tenant_id, run_id, session_id

    @staticmethod
    def _validate_call(
        *,
        model: LLMModel,
        request_fingerprint: str,
        call_index: int,
        request_input_token_upper_bound: int | None = None,
        request_max_output_tokens: int | None = None,
    ) -> None:
        if call_index <= 0:
            raise PlanningCostAccountingError(
                "planning_cost_context_invalid",
                "Planning cost call index must be positive",
            )
        if len(request_fingerprint) != 64:
            raise PlanningCostAccountingError(
                "planning_cost_context_invalid",
                "Planning request fingerprint is invalid",
            )
        if not isinstance(getattr(model, "id", None), uuid.UUID):
            raise PlanningCostAccountingError(
                "planning_cost_context_invalid",
                "Planning model identity is invalid",
            )
        for value, label in (
            (request_input_token_upper_bound, "input token upper bound"),
            (request_max_output_tokens, "maximum output tokens"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise PlanningCostAccountingError(
                    "planning_cost_context_invalid",
                    f"Planning {label} must be a positive integer",
                )

    @staticmethod
    async def _acquire_tenant_day_budget_lock(
        db,
        *,
        tenant_id: uuid.UUID,
        period_start: datetime,
    ) -> None:
        """Serialize every tenant/day reservation in PostgreSQL.

        The production contract is PostgreSQL-only for durable Runtime. Failing
        closed on another dialect avoids presenting process-local locking as a
        cross-worker cost cap.
        """

        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            raise PlanningCostAccountingError(
                "planning_cost_database_unsupported",
                "Planning cost caps require PostgreSQL transaction locks",
            )
        digest = hashlib.sha256(
            f"planning-cost:{tenant_id}:{period_start.date().isoformat()}".encode()
        ).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    @staticmethod
    async def _budget_usage(
        db,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> tuple[int, int]:
        filters = [
            LLMSystemCostReceipt.tenant_id == tenant_id,
            LLMSystemCostReceipt.status != "voided",
        ]
        if run_id is not None:
            filters.append(LLMSystemCostReceipt.run_id == run_id)
        if period_start is not None:
            filters.append(LLMSystemCostReceipt.created_at >= period_start)
        if period_end is not None:
            filters.append(LLMSystemCostReceipt.created_at < period_end)
        effective_cost = case(
            (
                LLMSystemCostReceipt.system_cost_credits.is_not(None),
                LLMSystemCostReceipt.system_cost_credits,
            ),
            else_=LLMSystemCostReceipt.budget_reservation_credits,
        )
        result = await db.execute(
            select(
                func.count(LLMSystemCostReceipt.id),
                func.coalesce(func.sum(effective_cost), 0),
            ).where(*filters)
        )
        count, credits = result.one()
        return int(count or 0), int(credits or 0)

    async def _enforce_budget(
        self,
        db,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        reservation_credits: int,
        now: datetime,
    ) -> None:
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = period_start + timedelta(days=1)
        run_calls, run_credits = await self._budget_usage(
            db,
            tenant_id=tenant_id,
            run_id=run_id,
        )
        tenant_calls, tenant_credits = await self._budget_usage(
            db,
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
        )
        policy = self._budget_policy
        if run_calls + 1 > policy.max_calls_per_run:
            raise PlanningCostAccountingError(
                "planning_cost_run_call_cap_exceeded",
                "Planning run call cap was reached before Provider submission",
            )
        if tenant_calls + 1 > policy.max_calls_per_tenant_day:
            raise PlanningCostAccountingError(
                "planning_cost_tenant_daily_call_cap_exceeded",
                "Planning tenant daily call cap was reached before Provider submission",
            )
        if run_credits + reservation_credits > policy.max_credits_per_run:
            raise PlanningCostAccountingError(
                "planning_cost_run_cap_exceeded",
                "Planning run cost cap was reached before Provider submission",
            )
        if (
            tenant_credits + reservation_credits
            > policy.max_credits_per_tenant_day
        ):
            raise PlanningCostAccountingError(
                "planning_cost_tenant_daily_cap_exceeded",
                "Planning tenant daily cost cap was reached before Provider submission",
            )

    async def _validate_database_context(
        self,
        db,
        *,
        context: RuntimeContext,
        model: LLMModel,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> uuid.UUID:
        run = await db.get(AgentRun, run_id)
        if (
            run is None
            or run.tenant_id != tenant_id
            or run.run_kind != "orchestration"
            or run.system_role != "group_planning"
            or run.agent_id is not None
            or run.session_id != session_id
            or run.model_id != model.id
        ):
            raise PlanningCostAccountingError(
                "planning_cost_context_mismatch",
                "Planning cost context does not match the persisted orchestration Run",
            )
        session = await db.get(ChatSession, session_id)
        if (
            session is None
            or session.tenant_id != tenant_id
            or session.session_type != "group"
            or session.group_id is None
        ):
            raise PlanningCostAccountingError(
                "planning_cost_context_mismatch",
                "Planning cost context does not match a tenant Group session",
            )
        group = await db.get(Group, session.group_id)
        if group is None or group.tenant_id != tenant_id or group.deleted_at is not None:
            raise PlanningCostAccountingError(
                "planning_cost_context_mismatch",
                "Planning cost context does not match an active tenant Group",
            )
        return group.id

    @staticmethod
    async def _find_existing(db, *, tenant_id: uuid.UUID, run_id: uuid.UUID, call_index: int):
        result = await db.execute(
            select(LLMSystemCostReceipt)
            .where(
                LLMSystemCostReceipt.tenant_id == tenant_id,
                LLMSystemCostReceipt.run_id == run_id,
                LLMSystemCostReceipt.call_index == call_index,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _validate_existing(
        receipt: LLMSystemCostReceipt,
        *,
        model: LLMModel,
        request_fingerprint: str,
    ) -> None:
        if (
            receipt.model_id != model.id
            or receipt.provider != model.provider
            or receipt.model != model.model
            or receipt.request_fingerprint != request_fingerprint
        ):
            raise PlanningCostAccountingError(
                "planning_cost_idempotency_conflict",
                "Planning call identity was reused with different request or model data",
            )

    @staticmethod
    def _existing_result(receipt: LLMSystemCostReceipt) -> PlanningCostReplay | None:
        if receipt.status == "finalized":
            return _replay_from_receipt(receipt)
        if (
            receipt.status == "reconciling"
            and receipt.provider_outcome == "accepted"
            and receipt.response_snapshot is not None
            and receipt.usage_source != "pending"
            and receipt.cost_status != "pending"
        ):
            # A prior final-ledger commit failed after the response snapshot was
            # durably captured.  The caller may promote and replay it without a
            # second Provider request.
            return _replay_from_receipt(receipt)
        raise PlanningCostAccountingError(
            "planning_cost_reconciliation_required",
            "A prior Planning provider attempt requires reconciliation",
        )

    async def find_replay_or_raise(
        self,
        *,
        context: RuntimeContext,
        model: LLMModel,
        request_fingerprint: str,
        call_index: int,
    ) -> PlanningCostReplay | None:
        self._validate_call(
            model=model,
            request_fingerprint=request_fingerprint,
            call_index=call_index,
        )
        tenant_id, run_id, _session_id = self._ids(context)
        async with self._session_factory() as db:
            receipt = await self._find_existing(
                db,
                tenant_id=tenant_id,
                run_id=run_id,
                call_index=call_index,
            )
            if receipt is None:
                return None
            self._validate_existing(
                receipt,
                model=model,
                request_fingerprint=request_fingerprint,
            )
            replay = self._existing_result(receipt)
            if receipt.status == "reconciling":
                receipt.status = "finalized"
                receipt.reconciliation_error_code = None
                receipt.finalized_at = receipt.finalized_at or datetime.now(UTC)
                await db.commit()
            return replay

    async def begin(
        self,
        *,
        context: RuntimeContext,
        model: LLMModel,
        credential_id: uuid.UUID | None,
        request_fingerprint: str,
        call_index: int,
        request_input_token_upper_bound: int,
        request_max_output_tokens: int,
    ) -> PlanningCostAttempt:
        self._validate_call(
            model=model,
            request_fingerprint=request_fingerprint,
            call_index=call_index,
            request_input_token_upper_bound=request_input_token_upper_bound,
            request_max_output_tokens=request_max_output_tokens,
        )
        tenant_id, run_id, session_id = self._ids(context)
        request_options = get_llm_request_options(model)
        provider_service_tier = str(
            request_options.get("service_tier") or "standard"
        )
        reservation_credits = planning_budget_reservation_credits(
            model=model,
            request_input_token_upper_bound=request_input_token_upper_bound,
            request_max_output_tokens=request_max_output_tokens,
            fallback_credits=self._budget_policy.unpriced_reservation_credits,
        )
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            group_id = await self._validate_database_context(
                db,
                context=context,
                model=model,
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=session_id,
            )
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            await self._acquire_tenant_day_budget_lock(
                db,
                tenant_id=tenant_id,
                period_start=period_start,
            )
            existing = await self._find_existing(
                db,
                tenant_id=tenant_id,
                run_id=run_id,
                call_index=call_index,
            )
            if existing is not None:
                self._validate_existing(
                    existing,
                    model=model,
                    request_fingerprint=request_fingerprint,
                )
                return PlanningCostAttempt(
                    receipt_id=existing.id,
                    replay=self._existing_result(existing),
                )
            await self._enforce_budget(
                db,
                tenant_id=tenant_id,
                run_id=run_id,
                reservation_credits=reservation_credits,
                now=now,
            )
            receipt = LLMSystemCostReceipt(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                group_id=group_id,
                session_id=session_id,
                run_id=run_id,
                call_index=call_index,
                operation="group_planning",
                model_id=model.id,
                credential_id=credential_id,
                provider=model.provider,
                model=model.model,
                provider_service_tier=provider_service_tier,
                request_fingerprint=request_fingerprint,
                budget_reservation_credits=reservation_credits,
                request_input_token_upper_bound=request_input_token_upper_bound,
                request_max_output_tokens=request_max_output_tokens,
                status="provider_inflight",
                provider_outcome="pending",
                usage_source="pending",
                cost_status="pending",
            )
            db.add(receipt)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await self._find_existing(
                    db,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    call_index=call_index,
                )
                if existing is None:
                    raise
                self._validate_existing(
                    existing,
                    model=model,
                    request_fingerprint=request_fingerprint,
                )
                return PlanningCostAttempt(
                    receipt_id=existing.id,
                    replay=self._existing_result(existing),
                )
            return PlanningCostAttempt(receipt_id=receipt.id)

    @staticmethod
    def _apply_response(
        receipt: LLMSystemCostReceipt,
        *,
        content: str | None,
        tool_calls: tuple[dict, ...],
        usage: TokenUsage,
        finish_reason: str | None,
    ) -> None:
        snapshot = _response_snapshot(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )
        receipt.response_snapshot = snapshot
        receipt.response_fingerprint = _finalization_fingerprint(snapshot, usage)
        normalized_usage = _normalized_usage(usage)
        for field, value in normalized_usage.items():
            setattr(receipt, field, value)
        receipt.usage_source = usage_source = _usage_source(usage)
        cost = (
            provider_text_credits(
                receipt.provider,
                receipt.model,
                usage,
                service_tier=receipt.provider_service_tier,
            )
            if usage_source != "unknown"
            else None
        )
        receipt.system_cost_credits = cost
        receipt.cost_status = "priced" if cost is not None else "unpriced"
        receipt.provider_outcome = "accepted"
        receipt.provider_accepted_at = receipt.provider_accepted_at or datetime.now(UTC)

    async def finalize(
        self,
        receipt_id: uuid.UUID,
        *,
        content: str | None,
        tool_calls: tuple[dict, ...],
        usage: TokenUsage,
        finish_reason: str | None,
    ) -> None:
        async with self._session_factory() as db:
            receipt = await db.get(
                LLMSystemCostReceipt,
                receipt_id,
                with_for_update=True,
                populate_existing=True,
            )
            if receipt is None:
                raise PlanningCostAccountingError(
                    "planning_cost_receipt_missing",
                    "Planning cost receipt is missing",
                )
            snapshot = _response_snapshot(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
            fingerprint = _finalization_fingerprint(snapshot, usage)
            if receipt.status == "finalized":
                if receipt.response_fingerprint != fingerprint:
                    raise PlanningCostAccountingError(
                        "planning_cost_idempotency_conflict",
                        "Planning cost receipt was finalized with a different response",
                    )
                return
            if receipt.status != "provider_inflight":
                raise PlanningCostAccountingError(
                    "planning_cost_reconciliation_required",
                    "Planning cost receipt is not finalizable without reconciliation",
                )
            self._apply_response(
                receipt,
                content=content,
                tool_calls=tool_calls,
                usage=usage,
                finish_reason=finish_reason,
            )
            receipt.status = "finalized"
            receipt.finalized_at = datetime.now(UTC)
            receipt.reconciliation_error_code = None
            await db.commit()

    async def discard_unaccepted(self, receipt_id: uuid.UUID) -> None:
        async with self._session_factory() as db:
            receipt = await db.get(
                LLMSystemCostReceipt,
                receipt_id,
                with_for_update=True,
                populate_existing=True,
            )
            if receipt is None:
                return
            if receipt.status != "provider_inflight" or receipt.provider_outcome != "pending":
                raise PlanningCostAccountingError(
                    "planning_cost_reconciliation_required",
                    "Accepted or ambiguous Planning cost cannot be discarded",
                )
            await db.delete(receipt)
            await db.commit()

    async def mark_reconciling(
        self,
        receipt_id: uuid.UUID,
        *,
        provider_outcome: str,
        error_code: str,
        content: str | None = None,
        tool_calls: tuple[dict, ...] = (),
        usage: TokenUsage | None = None,
        finish_reason: str | None = None,
    ) -> None:
        if provider_outcome not in {"accepted", "acceptance_unknown"}:
            raise ValueError("Unsupported Planning provider outcome")
        if usage is not None and provider_outcome != "accepted":
            raise ValueError(
                "Planning usage can only be captured for an accepted Provider outcome"
            )
        async with self._session_factory() as db:
            receipt = await db.get(
                LLMSystemCostReceipt,
                receipt_id,
                with_for_update=True,
                populate_existing=True,
            )
            if receipt is None or receipt.status == "finalized":
                return
            if usage is not None:
                self._apply_response(
                    receipt,
                    content=content,
                    tool_calls=tool_calls,
                    usage=usage,
                    finish_reason=finish_reason,
                )
            else:
                receipt.provider_outcome = provider_outcome
                receipt.usage_source = "unknown"
                receipt.cost_status = "unpriced"
                receipt.system_cost_credits = None
            receipt.status = "reconciling"
            receipt.reconciliation_error_code = str(error_code or "unknown")[:100]
            await db.commit()


__all__ = [
    "PlanningCostAccountingError",
    "PlanningCostAccountingPort",
    "PlanningCostAttempt",
    "PlanningCostBudgetPolicy",
    "PlanningCostReplay",
    "PlanningSystemCostService",
    "planning_budget_reservation_credits",
    "planning_request_fingerprint",
    "planning_request_token_upper_bound",
]
