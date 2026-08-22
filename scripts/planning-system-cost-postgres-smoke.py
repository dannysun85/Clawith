#!/usr/bin/env python3
"""PostgreSQL smoke for Group Planning system-cost receipts.

This exercises the real PostgreSQL constraints and accounting service without
calling an external Provider.  It proves ledger behavior only; it is not
``provider_verified`` evidence.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast
import uuid

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.saas import (
    list_llm_system_cost_resolutions,
    list_llm_system_costs,
    summarize_llm_system_costs,
)
from app.database import async_session
from app.models.agent_run import (
    AgentRun,
    LLMSystemCostReceipt,
    LLMSystemCostResolution,
)
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.group import Group
from app.models.llm import LLMModel
from app.models.participant import Participant
from app.models.subscription import CreditReservation, CreditTransaction
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.saas import LLMSystemCostReceiptOut
from app.services.agent_runtime.state import RuntimeContext, RuntimeNodeExecutor
from app.services.agent_runtime.system_costs import (
    PlanningCostAccountingError,
    PlanningCostBudgetPolicy,
    PlanningSystemCostService,
    planning_request_fingerprint,
)
from app.services.planning_cost_reconciliation import (
    PlanningCostResolutionError,
    apply_planning_cost_resolution_in_session,
    scan_stale_planning_costs_in_session,
)
from app.services.token_tracker import TokenUsage


def _context(
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    model_id: uuid.UUID,
) -> RuntimeContext:
    return RuntimeContext(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        command_id=str(uuid.uuid4()),
        executor=cast(RuntimeNodeExecutor, object()),
        goal="Create a two-Agent commercial launch plan",
        run_kind="orchestration",
        source_type="chat",
        model_id=str(model_id),
        graph_name="runtime_group_planning",
        graph_version="v2",
        agent_id=None,
        session_id=str(session_id),
        system_role="group_planning",
    )


async def _count(model, *filters) -> int:
    async with async_session() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(*filters)
                )
            ).scalar_one()
        )


async def _assert_schema_ready() -> None:
    """Fail with an actionable preflight error before touching fixture data."""

    required_tables = (
        "llm_system_cost_receipts",
        "llm_system_cost_resolutions",
    )
    async with async_session() as db:
        table_names = (
            await db.execute(
                text(
                    "SELECT to_regclass(:receipts)::text, "
                    "to_regclass(:resolutions)::text"
                ),
                {
                    "receipts": required_tables[0],
                    "resolutions": required_tables[1],
                },
            )
        ).one()

    missing = [
        table_name
        for table_name, resolved_name in zip(required_tables, table_names, strict=True)
        if resolved_name is None
    ]
    if missing:
        raise RuntimeError(
            "Planning system-cost PostgreSQL smoke preflight failed: DATABASE_URL "
            "must point to PostgreSQL upgraded through Alembic head "
            "planning_cost_controls; missing table(s): "
            + ", ".join(missing)
        )


def _planning_run(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    model_id: uuid.UUID,
    suffix: str,
    label: str,
    goal: str,
) -> AgentRun:
    return AgentRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=None,
        session_id=session_id,
        source_type="chat",
        source_id=f"postgres-smoke-{label}:{suffix}",
        source_execution_id=f"planning-cost-{label}:{suffix}",
        goal=goal,
        run_kind="orchestration",
        system_role="group_planning",
        model_id=model_id,
        model_turn_limit=None,
        runtime_type="langgraph",
        runtime_thread_id=f"planning-cost-{label}:{suffix}",
        graph_name="runtime_group_planning",
        graph_version="v2",
        delivery_status="pending",
    )


async def _main() -> None:
    await _assert_schema_ready()

    suffix = uuid.uuid4().hex
    tenant_a = Tenant(
        id=uuid.uuid4(),
        name="Planning Cost PostgreSQL A",
        slug=f"planning-cost-a-{suffix}",
        im_provider="web_only",
        is_active=True,
    )
    tenant_b = Tenant(
        id=uuid.uuid4(),
        name="Planning Cost PostgreSQL B",
        slug=f"planning-cost-b-{suffix}",
        im_provider="web_only",
        is_active=True,
    )
    identity = Identity(
        id=uuid.uuid4(),
        email=f"planning-cost-{suffix}@local.clawith.test",
        password_login_enabled=False,
        email_verified=True,
        is_active=True,
        is_platform_admin=True,
    )
    operator = User(
        id=uuid.uuid4(),
        identity_id=identity.id,
        tenant_id=tenant_a.id,
        display_name="Planning Cost Operator",
        role="platform_admin",
        is_active=True,
    )
    participant = Participant(
        id=uuid.uuid4(),
        type="user",
        ref_id=operator.id,
        display_name="Planning Cost QA",
    )
    group = Group(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Planning Cost Group",
        created_by_participant_id=participant.id,
    )
    session = ChatSession(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        session_type="group",
        group_id=group.id,
        title="Planning Cost Group",
        source_channel="web",
        is_group=True,
        group_name="Planning Cost Group",
        created_by_participant_id=participant.id,
        is_primary=True,
    )
    model = LLMModel(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="minimax",
        model="MiniMax-M3",
        api_key_encrypted="migration-smoke-placeholder",
        label="Planning Cost PostgreSQL",
        enabled=True,
        supports_vision=False,
        modality="text",
        tier="basic",
        verification_status="verified",
        max_input_tokens=64_000,
        max_output_tokens=2_048,
    )
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        agent_id=None,
        session_id=session.id,
        source_type="chat",
        source_id=f"postgres-smoke:{suffix}",
        source_execution_id=f"planning-cost:{suffix}",
        goal="Create a two-Agent commercial launch plan",
        run_kind="orchestration",
        system_role="group_planning",
        model_id=model.id,
        model_turn_limit=None,
        runtime_type="langgraph",
        runtime_thread_id=f"planning-cost:{suffix}",
        graph_name="runtime_group_planning",
        graph_version="v2",
        delivery_status="pending",
    )
    recovery_run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        agent_id=None,
        session_id=session.id,
        source_type="chat",
        source_id=f"postgres-smoke-recovery:{suffix}",
        source_execution_id=f"planning-cost-recovery:{suffix}",
        goal="Recover an accepted Planning call",
        run_kind="orchestration",
        system_role="group_planning",
        model_id=model.id,
        model_turn_limit=None,
        runtime_type="langgraph",
        runtime_thread_id=f"planning-cost-recovery:{suffix}",
        graph_name="runtime_group_planning",
        graph_version="v2",
        delivery_status="pending",
    )
    budget_run = _planning_run(
        tenant_id=tenant_a.id,
        session_id=session.id,
        model_id=model.id,
        suffix=suffix,
        label="budget",
        goal="Prove concurrent Planning cost caps",
    )
    void_run = _planning_run(
        tenant_id=tenant_a.id,
        session_id=session.id,
        model_id=model.id,
        suffix=suffix,
        label="void",
        goal="Reconcile a Provider request that was not accepted",
    )
    settle_run = _planning_run(
        tenant_id=tenant_a.id,
        session_id=session.id,
        model_id=model.id,
        suffix=suffix,
        label="settle",
        goal="Reconcile a Provider request with exact accepted usage",
    )
    fixture_ids = {
        "tenant": (tenant_a.id, tenant_b.id),
        "identity": identity.id,
        "user": operator.id,
        "participant": participant.id,
        "group": group.id,
        "session": session.id,
        "model": model.id,
        "run": (
            run.id,
            recovery_run.id,
            budget_run.id,
            void_run.id,
            settle_run.id,
        ),
    }

    try:
        async with async_session() as db:
            # Keep fixture ordering explicit.  These domain models deliberately
            # avoid broad ORM relationships, while PostgreSQL owns the actual
            # tenant-composite foreign-key enforcement.
            db.add_all([tenant_a, tenant_b, identity, model])
            await db.flush()
            db.add_all([operator, participant])
            await db.flush()
            db.add(group)
            await db.flush()
            db.add(session)
            await db.flush()
            db.add_all([run, recovery_run, budget_run, void_run, settle_run])
            await db.commit()

        service = PlanningSystemCostService(async_session)
        context = _context(
            tenant_id=tenant_a.id,
            run_id=run.id,
            session_id=session.id,
            model_id=model.id,
        )
        fingerprint = planning_request_fingerprint(
            ["system:planning-v2", "user:commercial-launch"]
        )
        before_credit_transactions = await _count(
            CreditTransaction,
            CreditTransaction.tenant_id == tenant_a.id,
        )
        before_credit_reservations = await _count(
            CreditReservation,
            CreditReservation.tenant_id == tenant_a.id,
        )

        # Credential/readiness failure occurs after this replay lookup and
        # before begin().  A lookup alone must never synthesize provider debt.
        assert (
            await service.find_replay_or_raise(
                context=context,
                model=model,
                request_fingerprint=fingerprint,
                call_index=1,
            )
            is None
        )
        assert (
            await _count(
                LLMSystemCostReceipt,
                LLMSystemCostReceipt.tenant_id == tenant_a.id,
            )
            == 0
        )

        attempt = await service.begin(
            context=context,
            model=model,
            credential_id=uuid.uuid4(),
            request_fingerprint=fingerprint,
            call_index=1,
            request_input_token_upper_bound=4_000,
            request_max_output_tokens=2_048,
        )
        usage = TokenUsage(input_tokens=1_000, output_tokens=200, total_tokens=1_200)
        await service.finalize(
            attempt.receipt_id,
            content='{"version":2,"mode":"advisory"}',
            tool_calls=(),
            usage=usage,
            finish_reason="stop",
        )
        # Exact settlement replay is a no-op; different usage must conflict.
        await service.finalize(
            attempt.receipt_id,
            content='{"version":2,"mode":"advisory"}',
            tool_calls=(),
            usage=usage,
            finish_reason="stop",
        )
        try:
            await service.finalize(
                attempt.receipt_id,
                content='{"version":2,"mode":"advisory"}',
                tool_calls=(),
                usage=TokenUsage(
                    input_tokens=1_001,
                    output_tokens=200,
                    total_tokens=1_201,
                ),
                finish_reason="stop",
            )
        except PlanningCostAccountingError as exc:
            assert exc.code == "planning_cost_idempotency_conflict"
        else:
            raise AssertionError("different usage replay did not fail closed")

        replay = await service.find_replay_or_raise(
            context=context,
            model=model,
            request_fingerprint=fingerprint,
            call_index=1,
        )
        assert replay is not None and replay.usage.total_tokens == 1_200
        assert (
            await _count(
                LLMSystemCostReceipt,
                LLMSystemCostReceipt.tenant_id == tenant_a.id,
            )
            == 1
        )

        recovery_context = _context(
            tenant_id=tenant_a.id,
            run_id=recovery_run.id,
            session_id=session.id,
            model_id=model.id,
        )
        recovery_attempt = await service.begin(
            context=recovery_context,
            model=model,
            credential_id=uuid.uuid4(),
            request_fingerprint=fingerprint,
            call_index=1,
            request_input_token_upper_bound=4_000,
            request_max_output_tokens=2_048,
        )
        await service.mark_reconciling(
            recovery_attempt.receipt_id,
            provider_outcome="accepted",
            error_code="InjectedFinalizationFailure",
            content="recovered plan",
            usage=TokenUsage(estimated_tokens=120, total_tokens=120),
            finish_reason="stop",
        )
        recovered = await service.find_replay_or_raise(
            context=recovery_context,
            model=model,
            request_fingerprint=fingerprint,
            call_index=1,
        )
        assert recovered is not None and recovered.content == "recovered plan"

        # Service boundary and PostgreSQL composite foreign keys both reject a
        # tenant B context that points at tenant A's Run/session/Group.
        cross_context = _context(
            tenant_id=tenant_b.id,
            run_id=run.id,
            session_id=session.id,
            model_id=model.id,
        )
        try:
            await service.begin(
                context=cross_context,
                model=model,
                credential_id=uuid.uuid4(),
                request_fingerprint=fingerprint,
                call_index=2,
                request_input_token_upper_bound=4_000,
                request_max_output_tokens=2_048,
            )
        except PlanningCostAccountingError as exc:
            assert exc.code == "planning_cost_context_mismatch"
        else:
            raise AssertionError("cross-tenant Planning cost context was accepted")

        async with async_session() as db:
            db.add(
                LLMSystemCostReceipt(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b.id,
                    group_id=group.id,
                    session_id=session.id,
                    run_id=run.id,
                    call_index=99,
                    operation="group_planning",
                    model_id=model.id,
                    provider="minimax",
                    model="MiniMax-M3",
                    provider_service_tier="standard",
                    request_fingerprint="b" * 64,
                    budget_reservation_credits=1,
                    request_input_token_upper_bound=4_000,
                    request_max_output_tokens=2_048,
                    status="provider_inflight",
                    provider_outcome="pending",
                    usage_source="pending",
                    cost_status="pending",
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
            else:
                raise AssertionError("PostgreSQL accepted a cross-tenant cost receipt")

        # Serialize simultaneous tenant/day reservations in PostgreSQL. With a
        # one-call Run cap, exactly one contender can create a receipt and the
        # other must fail before any Provider submission.
        capped_service = PlanningSystemCostService(
            async_session,
            budget_policy=PlanningCostBudgetPolicy(
                max_credits_per_run=100_000,
                max_credits_per_tenant_day=1_000_000,
                max_calls_per_run=1,
                max_calls_per_tenant_day=100,
                unpriced_reservation_credits=1_000,
            ),
        )
        budget_context = _context(
            tenant_id=tenant_a.id,
            run_id=budget_run.id,
            session_id=session.id,
            model_id=model.id,
        )

        async def _concurrent_begin(call_index: int) -> tuple[str, object]:
            try:
                started = await capped_service.begin(
                    context=budget_context,
                    model=model,
                    credential_id=uuid.uuid4(),
                    request_fingerprint=planning_request_fingerprint(
                        ["system:planning-v2", f"user:cap-{call_index}"]
                    ),
                    call_index=call_index,
                    request_input_token_upper_bound=4_000,
                    request_max_output_tokens=2_048,
                )
                return "started", started.receipt_id
            except PlanningCostAccountingError as exc:
                return "blocked", exc.code

        concurrency_results = await asyncio.gather(
            _concurrent_begin(1),
            _concurrent_begin(2),
        )
        assert sum(status == "started" for status, _ in concurrency_results) == 1
        assert (
            concurrency_results.count(
                ("blocked", "planning_cost_run_call_cap_exceeded")
            )
            == 1
        )

        # Stale inflight receipts are quarantined into ambiguity review. The
        # scanner is intentionally unable to retry, settle, or void them.
        void_context = _context(
            tenant_id=tenant_a.id,
            run_id=void_run.id,
            session_id=session.id,
            model_id=model.id,
        )
        settle_context = _context(
            tenant_id=tenant_a.id,
            run_id=settle_run.id,
            session_id=session.id,
            model_id=model.id,
        )
        void_attempt = await service.begin(
            context=void_context,
            model=model,
            credential_id=uuid.uuid4(),
            request_fingerprint=planning_request_fingerprint(
                ["system:planning-v2", "user:void-after-provider-query"]
            ),
            call_index=1,
            request_input_token_upper_bound=4_000,
            request_max_output_tokens=2_048,
        )
        settle_attempt = await service.begin(
            context=settle_context,
            model=model,
            credential_id=uuid.uuid4(),
            request_fingerprint=planning_request_fingerprint(
                ["system:planning-v2", "user:settle-from-provider-bill"]
            ),
            call_index=1,
            request_input_token_upper_bound=4_000,
            request_max_output_tokens=2_048,
        )
        stale_now = datetime.now(UTC)
        stale_at = stale_now - timedelta(hours=1)
        async with async_session() as db:
            await db.execute(
                update(LLMSystemCostReceipt)
                .where(
                    LLMSystemCostReceipt.id.in_(
                        (void_attempt.receipt_id, settle_attempt.receipt_id)
                    )
                )
                .values(updated_at=stale_at)
            )
            await db.commit()

        async with async_session() as db:
            preview = await scan_stale_planning_costs_in_session(
                db,
                stale_after_seconds=600,
                limit=10,
                apply=False,
                source="operator",
                actor_user_id=operator.id,
                evidence_ref="ops-preview:postgres-smoke",
                reason="Preview stale Planning receipts without changing state",
                now=stale_now,
            )
        assert set(preview.candidate_receipt_ids) == {
            void_attempt.receipt_id,
            settle_attempt.receipt_id,
        }
        assert preview.applied_count == 0

        async with async_session() as db:
            stale_result = await scan_stale_planning_costs_in_session(
                db,
                stale_after_seconds=600,
                limit=10,
                apply=True,
                source="daemon",
                actor_user_id=None,
                evidence_ref="runtime:postgres-smoke-stale-scan",
                reason="Provider acceptance remains unknown after the lease expired",
                now=stale_now,
            )
            await db.commit()
        assert stale_result.applied_count == 2

        # A platform operator is still tenant-fenced before changing state.
        async with async_session() as db:
            try:
                await apply_planning_cost_resolution_in_session(
                    db,
                    receipt_id=void_attempt.receipt_id,
                    expected_tenant_id=tenant_b.id,
                    expected_status="reconciling",
                    expected_provider_outcome="acceptance_unknown",
                    disposition="confirm_not_accepted",
                    evidence_ref="provider-query:wrong-tenant",
                    reason="A cross-tenant operator must never resolve this receipt",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cache_read_tokens=None,
                    cache_creation_tokens=None,
                    system_cost_credits=None,
                    actor_user_id=operator.id,
                    idempotency_key="postgres-smoke-cross-tenant",
                )
            except PlanningCostResolutionError as exc:
                assert "tenant" in str(exc).lower()
            else:
                raise AssertionError("cross-tenant cost resolution was accepted")

        resolution_kwargs = {
            "receipt_id": void_attempt.receipt_id,
            "expected_tenant_id": tenant_a.id,
            "expected_status": "reconciling",
            "expected_provider_outcome": "acceptance_unknown",
            "disposition": "confirm_not_accepted",
            "evidence_ref": "provider-query:request-not-found",
            "reason": "Provider query confirms that the request was never accepted",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_creation_tokens": None,
            "system_cost_credits": None,
            "actor_user_id": operator.id,
            "idempotency_key": "postgres-smoke-confirm-not-accepted",
        }
        async with async_session() as db:
            voided = await apply_planning_cost_resolution_in_session(
                db,
                **resolution_kwargs,
            )
            await db.commit()
        assert voided.receipt.status == "voided"
        assert voided.receipt.system_cost_credits == 0

        async with async_session() as db:
            replayed = await apply_planning_cost_resolution_in_session(
                db,
                **resolution_kwargs,
            )
            await db.commit()
        assert replayed.replayed is True
        assert replayed.resolution.id == voided.resolution.id

        async with async_session() as db:
            settled = await apply_planning_cost_resolution_in_session(
                db,
                receipt_id=settle_attempt.receipt_id,
                expected_tenant_id=tenant_a.id,
                expected_status="reconciling",
                expected_provider_outcome="acceptance_unknown",
                disposition="settle_accepted",
                evidence_ref="provider-bill:postgres-smoke-statement",
                reason="Provider bill proves exact accepted usage and cost",
                input_tokens=1_000,
                output_tokens=200,
                total_tokens=1_200,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                system_cost_credits=2,
                actor_user_id=operator.id,
                idempotency_key="postgres-smoke-settle-accepted",
            )
            await db.commit()
        assert settled.receipt.status == "reconciled"
        assert settled.receipt.response_snapshot is None

        # The append-only ledger has its own tenant/receipt composite FK, so a
        # direct database write cannot bypass the service tenant fence.
        async with async_session() as db:
            db.add(
                LLMSystemCostResolution(
                    id=uuid.uuid4(),
                    receipt_id=void_attempt.receipt_id,
                    tenant_id=tenant_b.id,
                    actor_user_id=operator.id,
                    idempotency_key_hash="c" * 64,
                    request_fingerprint="d" * 64,
                    action="confirm_not_accepted",
                    source="operator",
                    evidence_ref="provider-query:invalid-cross-tenant",
                    reason="PostgreSQL must reject this synthetic cross-tenant row",
                    previous_status="reconciling",
                    resulting_status="voided",
                    previous_provider_outcome="acceptance_unknown",
                    resulting_provider_outcome="not_accepted",
                    reported_system_cost_credits=None,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
            else:
                raise AssertionError(
                    "PostgreSQL accepted a cross-tenant cost resolution"
                )

        async with async_session() as db:
            listed = await list_llm_system_costs(
                tenant_id=tenant_a.id,
                group_id=group.id,
                run_id=None,
                status_filter=None,
                page=1,
                limit=100,
                current_user=cast(object, object()),
                db=db,
            )
            summary = await summarize_llm_system_costs(
                tenant_id=tenant_a.id,
                group_id=group.id,
                run_id=None,
                current_user=cast(object, object()),
                db=db,
            )
            resolutions = await list_llm_system_cost_resolutions(
                tenant_id=tenant_a.id,
                receipt_id=None,
                page=1,
                limit=100,
                current_user=cast(object, object()),
                db=db,
            )
        assert len(listed) == 5
        assert len(resolutions) == 4
        assert summary.receipt_count == 5
        assert summary.finalized_count == 2
        assert summary.reconciling_count == 0
        assert summary.provider_inflight_count == 1
        assert summary.reconciled_count == 1
        assert summary.voided_count == 1
        payload = LLMSystemCostReceiptOut.model_validate(listed[0]).model_dump()
        assert "request_fingerprint" not in payload
        assert "response_snapshot" not in payload

        after_credit_transactions = await _count(
            CreditTransaction,
            CreditTransaction.tenant_id == tenant_a.id,
        )
        after_credit_reservations = await _count(
            CreditReservation,
            CreditReservation.tenant_id == tenant_a.id,
        )
        assert after_credit_transactions == before_credit_transactions
        assert after_credit_reservations == before_credit_reservations
        assert (
            await _count(
                AuditLog,
                AuditLog.tenant_id == tenant_a.id,
            )
            == 4
        )

        print(
            "planning_system_cost_postgres_smoke_ok "
            "receipts=5 finalized=2 inflight=1 reconciled=1 voided=1 "
            "resolutions=4 concurrent_cap=1_of_2 stale_quarantine=2 "
            "idempotent_resolution=1 customer_transactions_delta=0 "
            "customer_reservations_delta=0 tenant_fence=service+database"
        )
    finally:
        # The release migration smoke drops the whole disposable database, but
        # exact cleanup keeps this script safe for bounded local diagnostics.
        with suppress(Exception):
            async with async_session() as db:
                await db.execute(
                    delete(LLMSystemCostResolution).where(
                        LLMSystemCostResolution.tenant_id.in_(fixture_ids["tenant"])
                    )
                )
                await db.execute(
                    delete(AuditLog).where(
                        AuditLog.tenant_id.in_(fixture_ids["tenant"])
                    )
                )
                await db.execute(
                    delete(LLMSystemCostReceipt).where(
                        LLMSystemCostReceipt.tenant_id.in_(fixture_ids["tenant"])
                    )
                )
                await db.execute(
                    delete(AgentRun).where(AgentRun.id.in_(fixture_ids["run"]))
                )
                await db.execute(
                    delete(ChatSession).where(ChatSession.id == fixture_ids["session"])
                )
                await db.execute(delete(Group).where(Group.id == fixture_ids["group"]))
                await db.execute(
                    delete(LLMModel).where(LLMModel.id == fixture_ids["model"])
                )
                await db.execute(
                    delete(Participant).where(
                        Participant.id == fixture_ids["participant"]
                    )
                )
                await db.execute(delete(User).where(User.id == fixture_ids["user"]))
                await db.execute(
                    delete(Identity).where(Identity.id == fixture_ids["identity"])
                )
                await db.execute(
                    delete(Tenant).where(Tenant.id.in_(fixture_ids["tenant"]))
                )
                await db.commit()


if __name__ == "__main__":
    asyncio.run(_main())
