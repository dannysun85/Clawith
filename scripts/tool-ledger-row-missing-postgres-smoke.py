#!/usr/bin/env python3
"""Incident W0 reproduction against real PostgreSQL.

Production v1.11.41 signature: a durable Run's tool ledger row was lost by the
environment between reserve-commit and settle, and the Runtime answered with
``run_failed error_code=tool_execution_not_found``. This smoke proves the fix:

- Phase 1: an ``is_system`` Agent really executes ``list_focus_items`` and
  ``list_triggers`` through ``RuntimeToolStepService.execute_pending`` with the
  production session factory, provider, executor, and autonomy enforcer. Both
  ledger rows must settle ``succeeded``.
- Phase 2 (fault injection): the just-committed ledger row is deleted from the
  tool-executor boundary, exactly where the environment swallowed it.
  - read + safe (``list_focus_items``): the step defers with
    ``tool_ledger_row_missing`` (no run death); the next attempt re-reserves,
    re-executes, and settles ``succeeded``.
  - write (``upsert_focus_item``): never blindly re-executed — the lost row is
    rebuilt as ``unknown`` and the step enters the existing
    ``requires_confirmation`` waiting path with the call still pending.

Run with: cd backend && .venv/bin/python ../scripts/tool-ledger-row-missing-postgres-smoke.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import uuid

from sqlalchemy import delete, select

from app.database import async_session
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_tool_execution import AgentToolExecution
from app.models.chat_session import ChatSession
from app.models.group import Group  # noqa: F401 - registers ChatSession FK metadata
from app.models.llm import LLMModel
from app.models.participant import Participant  # noqa: F401 - registers FK metadata
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_runtime.cancel_source import DatabaseRuntimeCancelSource
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.agent_runtime.tool_execution import (
    ToolExecutionReconciliationPending,
    can_user_reconcile_unknown_execution,
    reconcile_unknown_tool_execution,
)
from app.services.agent_runtime.tool_step_service import RuntimeToolStepService
from app.services.agent_tools import (
    WORKSPACE_ROOT,
    execute_builtin_tool_outcome,
    get_runtime_agent_tools_for_llm,
)


def _call(call_id: str, name: str, arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}, ensure_ascii=False),
        },
    }


def _state(
    *,
    tenant: Tenant,
    agent: Agent,
    session: ChatSession,
    run: AgentRun,
    model_id: uuid.UUID,
    calls: tuple[dict, ...],
    assistant_message_id: str,
) -> RuntimeGraphState:
    return {
        "registry": RunRegistrySnapshot(
            tenant_id=str(tenant.id),
            run_id=str(run.id),
            goal="W0 incident smoke",
            run_kind="foreground",
            source_type="chat",
            model_id=str(model_id),
            graph_name="runtime_graph",
            graph_version="v1",
            agent_id=str(agent.id),
            session_id=str(session.id),
        ),
        "snapshots": RunInputSnapshots(
            session_context={"version": 0},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={},
        ),
        "lifecycle": {
            "status": "running",
            "next_route": "tool",
            "run_messages": [
                {
                    "id": assistant_message_id,
                    "role": "assistant",
                    "content": "",
                    "tool_calls": list(calls),
                }
            ],
            "pending_tool_calls": list(calls),
        },
    }


def _context(
    state: RuntimeGraphState,
    *,
    command_id: str,
    actor_user_id: uuid.UUID,
) -> RuntimeContext:
    registry = state["registry"]
    return RuntimeContext(
        tenant_id=registry.tenant_id,
        run_id=registry.run_id,
        command_id=command_id,
        executor=object(),  # type: ignore[arg-type]
        goal=registry.goal,
        run_kind=registry.run_kind,
        source_type=registry.source_type,
        model_id=registry.model_id,
        graph_name=registry.graph_name,
        graph_version=registry.graph_version,
        agent_id=registry.agent_id,
        session_id=registry.session_id,
        system_role=registry.system_role,
        parent_run_id=registry.parent_run_id,
        root_run_id=registry.root_run_id,
        actor_user_id=str(actor_user_id),
    )


def _service(tool_executor=execute_builtin_tool_outcome) -> RuntimeToolStepService:
    return RuntimeToolStepService(
        session_factory=async_session,
        cancel_source=DatabaseRuntimeCancelSource(session_factory=async_session),
        tool_executor=tool_executor,
    )


async def _forbidden_executor(*args, **kwargs):
    raise AssertionError(f"a guarded tool must never re-execute: {args}, {kwargs}")


async def _ledger_rows(tenant_id: uuid.UUID, run_id: uuid.UUID) -> list[AgentToolExecution]:
    async with async_session() as db:
        result = await db.execute(
            select(AgentToolExecution)
            .where(
                AgentToolExecution.tenant_id == tenant_id,
                AgentToolExecution.run_id == run_id,
            )
            .order_by(AgentToolExecution.started_at, AgentToolExecution.id)
        )
        return list(result.scalars().all())


def _deleting_executor(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_call_id: str,
    deleted_ids: list[uuid.UUID],
):
    """Delete the just-committed ledger row exactly where the environment did."""

    async def executor(tool_name, arguments, agent_id, user_id, session_id="", on_output=None):
        async with async_session() as db:
            async with db.begin():
                doomed = (
                    await db.execute(
                        select(AgentToolExecution.id).where(
                            AgentToolExecution.tenant_id == tenant_id,
                            AgentToolExecution.run_id == run_id,
                            AgentToolExecution.tool_call_id == tool_call_id,
                        )
                    )
                ).scalar_one()
                deleted_ids.append(doomed)
                await db.execute(
                    delete(AgentToolExecution).where(
                        AgentToolExecution.tenant_id == tenant_id,
                        AgentToolExecution.run_id == run_id,
                        AgentToolExecution.tool_call_id == tool_call_id,
                    )
                )
        return await execute_builtin_tool_outcome(
            tool_name,
            arguments,
            agent_id,
            user_id,
            session_id,
        )

    return executor


async def main() -> None:
    async with async_session() as db:
        tenant = Tenant(
            name="Tool Ledger Missing Smoke Company",
            slug=f"tool-ledger-missing-smoke-{uuid.uuid4().hex[:12]}",
            im_provider="web_only",
            is_active=True,
        )
        db.add(tenant)
        await db.flush()
        owner = User(
            display_name="Tool Ledger Missing Smoke",
            role="org_owner",
            tenant_id=tenant.id,
        )
        db.add(owner)
        await db.flush()
        model_id = (
            await db.execute(
                select(LLMModel.id)
                .where(
                    LLMModel.enabled.is_(True),
                    LLMModel.modality == "text",
                )
                .order_by(LLMModel.created_at, LLMModel.id)
                .limit(1)
            )
        ).scalar_one()
        agent = Agent(
            name="Smoke System CEO",
            creator_id=owner.id,
            tenant_id=tenant.id,
            primary_model_id=model_id,
            status="idle",
            is_system=True,
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            tenant_id=tenant.id,
            session_type="direct",
            agent_id=agent.id,
            user_id=owner.id,
            title="W0 smoke",
            source_channel="web",
            is_group=False,
            is_primary=True,
        )
        db.add(session)
        await db.flush()

        def new_run() -> AgentRun:
            now = datetime.now(UTC)
            run_id = uuid.uuid4()
            return AgentRun(
                id=run_id,
                tenant_id=tenant.id,
                agent_id=agent.id,
                session_id=session.id,
                source_type="chat",
                goal="W0 incident smoke",
                run_kind="foreground",
                model_id=model_id,
                model_turn_limit=50,
                runtime_type="langgraph",
                runtime_thread_id=str(session.id),
                graph_name="runtime_graph",
                graph_version="v1",
                scheduling_lane_key=f"direct_chat_thread:{tenant.id}:{session.id}:{run_id}",
                scheduling_position_created_at=now,
                scheduling_position_id=uuid.uuid4(),
                lane_held=True,
                delivery_status="not_required",
                origin_user_id=owner.id,
            )

        run_happy = new_run()
        run_read_lost = new_run()
        run_write_lost = new_run()
        run_write_pending = new_run()
        db.add_all([run_happy, run_read_lost, run_write_lost, run_write_pending])
        await db.commit()

    visible_tool_names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in await get_runtime_agent_tools_for_llm(agent.id)
    }
    for required in ("list_focus_items", "list_triggers", "write_file"):
        assert required in visible_tool_names, f"{required} is not visible to the smoke Agent"

    # Append mode proves single execution; the tool requires the file to exist.
    workspace_dir = WORKSPACE_ROOT / str(agent.id)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: both incident tools settle succeeded through the real path.
    happy_calls = (
        _call("call-focus", "list_focus_items", {"include_completed": True}),
        _call("call-triggers", "list_triggers"),
    )
    happy_state = _state(
        tenant=tenant,
        agent=agent,
        session=session,
        run=run_happy,
        model_id=model_id,
        calls=happy_calls,
        assistant_message_id="assistant-happy",
    )
    happy_result = await _service().execute_pending(
        happy_state,
        _context(happy_state, command_id="command-happy", actor_user_id=owner.id),
        happy_calls,
    )
    assert happy_result.error is None, happy_result.error
    assert happy_result.waiting_request is None
    assert [message["execution_status"] for message in happy_result.messages] == [
        "succeeded",
        "succeeded",
    ]
    happy_rows = await _ledger_rows(tenant.id, run_happy.id)
    assert {row.tool_name: row.status for row in happy_rows} == {
        "list_focus_items": "succeeded",
        "list_triggers": "succeeded",
    }, [(row.tool_name, row.status) for row in happy_rows]
    print("phase 1 passed: system Agent executed list_focus_items/list_triggers to succeeded")

    # Phase 2a: the read + safe row vanishes between reserve and settle.
    read_call = _call("call-lost-read", "list_focus_items", {"include_completed": False})
    read_state = _state(
        tenant=tenant,
        agent=agent,
        session=session,
        run=run_read_lost,
        model_id=model_id,
        calls=(read_call,),
        assistant_message_id="assistant-read-lost",
    )
    deleted_read_ids: list[uuid.UUID] = []
    try:
        await _service(
            _deleting_executor(tenant.id, run_read_lost.id, "call-lost-read", deleted_read_ids)
        ).execute_pending(
            read_state,
            _context(read_state, command_id="command-read-lost", actor_user_id=owner.id),
            (read_call,),
        )
    except ToolExecutionReconciliationPending as exc:
        assert exc.code == "tool_ledger_row_missing", exc.code
        assert exc.defer_without_attempt is True
    else:
        raise AssertionError("a lost read ledger row must defer, never kill the Run")
    assert await _ledger_rows(tenant.id, run_read_lost.id) == []

    # The deferred next attempt re-reserves (recreates the row) and re-executes.
    retry_result = await _service().execute_pending(
        read_state,
        _context(read_state, command_id="command-read-retry", actor_user_id=owner.id),
        (read_call,),
    )
    assert retry_result.error is None, retry_result.error
    assert [message["execution_status"] for message in retry_result.messages] == ["succeeded"]
    read_rows = await _ledger_rows(tenant.id, run_read_lost.id)
    assert [row.status for row in read_rows] == ["succeeded"], [
        (row.tool_name, row.status) for row in read_rows
    ]
    assert read_rows[0].id != deleted_read_ids[0]
    print("phase 2a passed: lost read row deferred then self-healed to succeeded")

    # Phase 2b: the write row vanishes between reserve and settle.
    write_path = "w0-smoke-ledger-missing.md"
    (workspace_dir / write_path).write_text("", encoding="utf-8")
    write_call = _call(
        "call-lost-write",
        "write_file",
        {"path": write_path, "content": "A", "mode": "append"},
    )
    write_state = _state(
        tenant=tenant,
        agent=agent,
        session=session,
        run=run_write_lost,
        model_id=model_id,
        calls=(write_call,),
        assistant_message_id="assistant-write-lost",
    )
    deleted_write_ids: list[uuid.UUID] = []
    write_result = await _service(
        _deleting_executor(tenant.id, run_write_lost.id, "call-lost-write", deleted_write_ids)
    ).execute_pending(
        write_state,
        _context(write_state, command_id="command-write-lost", actor_user_id=owner.id),
        (write_call,),
    )
    assert write_result.error is None, write_result.error
    assert write_result.waiting_request == {
        "waiting_type": "user",
        "correlation_id": str(uuid.uuid5(run_write_lost.id, "tool-reconcile:call-lost-write")),
        "reason": "tool_ledger_row_missing",
        "tool_call_id": "call-lost-write",
    }, write_result.waiting_request
    assert write_result.pending_tool_calls == (write_call,)
    assert [message["execution_status"] for message in write_result.messages] == ["unknown"]

    write_rows = await _ledger_rows(tenant.id, run_write_lost.id)
    assert len(write_rows) == 1, [(row.id, row.status) for row in write_rows]
    rebuilt = write_rows[0]
    assert rebuilt.id != deleted_write_ids[0]
    assert rebuilt.status == "unknown"
    assert rebuilt.tool_name == "write_file"
    metadata = rebuilt.result_metadata or {}
    assert metadata.get("error_code") == "tool_ledger_row_missing", metadata
    assert can_user_reconcile_unknown_execution(rebuilt) is True

    # The write ran exactly once (append mode would duplicate on a blind retry)
    # and the rebuilt row settles through the existing reconcile path.
    workspace_file = WORKSPACE_ROOT / str(agent.id) / write_path
    assert workspace_file.read_text(encoding="utf-8") == "A"

    async with async_session() as db:
        async with db.begin():
            settled = await reconcile_unknown_tool_execution(
                db,
                tenant_id=tenant.id,
                run_id=run_write_lost.id,
                execution_id=rebuilt.id,
                confirmed_status="succeeded",
                confirmed_by_user_id=owner.id,
                note="smoke: the write applied exactly once",
            )
    assert settled.status == "succeeded"

    # A resume after reconciliation reuses the confirmed receipt; the tool does
    # not execute again.
    resumed_result = await _service(_forbidden_executor).execute_pending(
        write_state,
        _context(write_state, command_id="command-write-resume", actor_user_id=owner.id),
        (write_call,),
    )
    assert resumed_result.error is None, resumed_result.error
    assert resumed_result.waiting_request is None
    assert [message["execution_status"] for message in resumed_result.messages] == ["succeeded"]
    assert workspace_file.read_text(encoding="utf-8") == "A"

    # While unreconciled, the rebuilt unknown row must hold the Run in the
    # confirmation waiting path instead of re-executing the write.
    pending_path = "w0-smoke-ledger-pending.md"
    (workspace_dir / pending_path).write_text("", encoding="utf-8")
    pending_call = _call(
        "call-pending-write",
        "write_file",
        {"path": pending_path, "content": "B", "mode": "append"},
    )
    pending_state = _state(
        tenant=tenant,
        agent=agent,
        session=session,
        run=run_write_pending,
        model_id=model_id,
        calls=(pending_call,),
        assistant_message_id="assistant-write-pending",
    )
    deleted_pending_ids: list[uuid.UUID] = []
    pending_result = await _service(
        _deleting_executor(
            tenant.id,
            run_write_pending.id,
            "call-pending-write",
            deleted_pending_ids,
        )
    ).execute_pending(
        pending_state,
        _context(pending_state, command_id="command-write-pending", actor_user_id=owner.id),
        (pending_call,),
    )
    assert pending_result.waiting_request is not None
    resume_result = await _service(_forbidden_executor).execute_pending(
        pending_state,
        _context(pending_state, command_id="command-write-pending-resume", actor_user_id=owner.id),
        (pending_call,),
    )
    assert resume_result.error is None, resume_result.error
    assert resume_result.waiting_request == {
        "waiting_type": "user",
        "correlation_id": str(uuid.uuid5(run_write_pending.id, "tool-reconcile:call-pending-write")),
        "reason": "tool_outcome_unknown",
        "tool_call_id": "call-pending-write",
    }, resume_result.waiting_request
    assert resume_result.pending_tool_calls == (pending_call,)
    pending_rows = await _ledger_rows(tenant.id, run_write_pending.id)
    assert [row.status for row in pending_rows] == ["unknown"]
    pending_file = WORKSPACE_ROOT / str(agent.id) / pending_path
    assert pending_file.read_text(encoding="utf-8") == "B"

    print("phase 2b passed: lost write row rebuilt as unknown, human confirmation gate holds")
    print("tool ledger row missing PostgreSQL smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
