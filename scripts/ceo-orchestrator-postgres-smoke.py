#!/usr/bin/env python3
"""CEO orchestrator P1 integration smoke against real PostgreSQL.

Proves the FR-CEO-1/2/3/4/5 contracts end to end with zero provider calls:

- FR-CEO-1: per-tenant CEO entity (is_system Agent + Participant + company/use
  permission), idempotent enable, uniqueness, and no employee-seat consumption.
- FR-CEO-2: company_brief_snapshot typed adapter succeeds for the enabled CEO
  and stays fail-closed (ceo_only) for any other Agent; the projection output
  respects the hard length bound.
- FR-CEO-3: five is_system trigger identities exist under the CEO (including a
  distinct manual weekly-meeting identity), default to disabled, and follow
  the triple gate when cadence switches flip.
- FR-CEO-4: a manual meeting start registers a durable trigger-runtime run with
  stable identity, lazily creates the meeting Group, and writes zero Task rows.
- FR-CEO-5: budget caps fail closed (skip + enabler notification + audit) for
  both the trigger gate and the manual meeting path.

Run with: cd backend && .venv/bin/python ../scripts/ceo-orchestrator-postgres-smoke.py
The script creates one synthetic tenant and removes every row it created.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import shutil
import uuid

os.environ.setdefault("CEO_ORCHESTRATOR_ENABLED", "true")

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import AuditLog, ChatMessage
from app.models.ceo import CeoOrchestratorSettings
from app.models.chat_session import ChatSession
from app.models.focus import AgentFocusItem
from app.models.group import Group, GroupMember
from app.models.llm import LLMModel
from app.models.org import AgentAgentRelationship
from app.models.participant import Participant
from app.models.subscription import CreditTransaction
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User
from app.services.agent_tools import _company_brief_snapshot_outcome
from app.services.ceo_briefing import build_company_brief_snapshot
from app.services.ceo_orchestrator import (
    CEO_SYSTEM_TRIGGER_NAMES,
    CeoOrchestratorError,
    automation_budget_denial,
    disable_ceo_orchestrator,
    enable_ceo_orchestrator,
    gate_ceo_trigger_automation,
    get_ceo_settings,
    start_ceo_meeting,
)
from app.services.quota_guard import _count_active_tenant_agents
from app.services.storage import agent_storage_key, get_storage_backend
from app.services.template_seeder import seed_agent_templates
from app.services.tool_seeder import seed_builtin_tools


PASS = "PASS"


def _ok(label: str, detail: str = "") -> None:
    print(f"[{PASS}] {label}" + (f" — {detail}" if detail else ""))


async def _seed_prerequisites() -> None:
    """Seed builtin tools + templates if this dev DB predates them."""
    async with async_session() as db:
        tool_row = (
            await db.execute(select(Tool.id).where(Tool.name == "company_brief_snapshot"))
        ).scalar_one_or_none()
        template_row = (
            await db.execute(
                select(AgentTemplate.id).where(
                    AgentTemplate.is_builtin.is_(True),
                    AgentTemplate.role_key == "ceo",
                )
            )
        ).scalar_one_or_none()
    if tool_row is None:
        await seed_builtin_tools()
    if template_row is None:
        await seed_agent_templates()
    async with async_session() as db:
        assert (
            await db.execute(select(Tool.id).where(Tool.name == "company_brief_snapshot"))
        ).scalar_one_or_none() is not None, "company_brief_snapshot tool row missing after seed"
        assert (
            await db.execute(
                select(AgentTemplate.id).where(
                    AgentTemplate.is_builtin.is_(True),
                    AgentTemplate.role_key == "ceo",
                )
            )
        ).scalar_one_or_none() is not None, "ceo template row missing after seed"
    _ok("prerequisites seeded", "company_brief_snapshot tool + ceo template present")


async def main() -> None:
    await _seed_prerequisites()

    tenant = None
    owner = None
    employees: list[Agent] = []
    ceo_agent_id: uuid.UUID | None = None
    created_group_ids: list[uuid.UUID] = []
    marker = uuid.uuid4().hex[:10]

    try:
        async with async_session() as db:
            tenant = Tenant(
                name=f"CEO Smoke {marker}",
                slug=f"ceo-smoke-{marker}",
                im_provider="web_only",
                is_active=True,
            )
            db.add(tenant)
            await db.flush()
            model_id = (
                await db.execute(
                    select(LLMModel.id)
                    .where(
                        LLMModel.enabled.is_(True),
                        LLMModel.modality == "text",
                        LLMModel.tenant_id.is_(None),
                        LLMModel.deleted_at.is_(None),
                    )
                    .order_by(LLMModel.created_at, LLMModel.id)
                    .limit(1)
                )
            ).scalar_one()
            tenant.default_model_id = model_id
            owner = User(
                display_name="CEO Smoke Owner",
                role="org_owner",
                tenant_id=tenant.id,
            )
            db.add(owner)
            await db.flush()
            for idx in range(2):
                employee = Agent(
                    name=f"Smoke Employee {idx}",
                    creator_id=owner.id,
                    tenant_id=tenant.id,
                    primary_model_id=model_id,
                    status="idle",
                )
                db.add(employee)
                employees.append(employee)
            await db.flush()
            seats_before = await _count_active_tenant_agents(tenant.id, db)
            await db.commit()

        # Open the rollout gate for exactly this tenant (in-process settings).
        runtime_settings = get_settings()
        runtime_settings.CEO_ORCHESTRATOR_TENANT_IDS = str(tenant.id)

        # ── FR-CEO-1: enable creates the per-tenant CEO entity ──
        async with async_session() as db:
            tenant_row = await db.get(Tenant, tenant.id)
            owner_row = await db.get(User, owner.id)
            settings_row = await enable_ceo_orchestrator(
                db,
                tenant=tenant_row,
                admin=owner_row,
                member_agent_ids=[employee.id for employee in employees],
                observer_only_confirmed=True,
            )
            await db.commit()
            ceo_agent_id = settings_row.ceo_agent_id

        async with async_session() as db:
            agent = await db.get(Agent, ceo_agent_id)
            assert agent is not None and agent.is_system and agent.tenant_id == tenant.id
            assert agent.status == "idle" and agent.heartbeat_enabled is False
            template = await db.get(AgentTemplate, agent.template_id)
            assert template is not None and template.role_key == "ceo"
            participant = (
                await db.execute(
                    select(Participant).where(
                        Participant.type == "agent", Participant.ref_id == agent.id
                    )
                )
            ).scalar_one_or_none()
            assert participant is not None, "CEO Participant missing"
            permission = (
                await db.execute(
                    select(AgentPermission).where(
                        AgentPermission.agent_id == agent.id,
                        AgentPermission.scope_type == "company",
                        AgentPermission.access_level == "use",
                    )
                )
            ).scalar_one_or_none()
            assert permission is not None, "company/use permission missing"
            relationships = (
                await db.execute(
                    select(AgentAgentRelationship).where(
                        (AgentAgentRelationship.agent_id == agent.id)
                        | (AgentAgentRelationship.target_agent_id == agent.id)
                    )
                )
            ).scalars().all()
            pairs = {(row.agent_id, row.target_agent_id) for row in relationships}
            for employee in employees:
                assert (agent.id, employee.id) in pairs
                assert (employee.id, agent.id) in pairs
            seats_after = await _count_active_tenant_agents(tenant.id, db)
            assert seats_after == seats_before == 2, (
                f"CEO must not consume a seat: before={seats_before} after={seats_after}"
            )
            storage = get_storage_backend()
            assert await storage.exists(agent_storage_key(agent.id, "soul.md")), "soul.md missing"
            triggers = (
                await db.execute(
                    select(AgentTrigger).where(AgentTrigger.agent_id == agent.id)
                )
            ).scalars().all()
            by_name = {trigger.name: trigger for trigger in triggers}
            assert set(by_name) == set(CEO_SYSTEM_TRIGGER_NAMES), sorted(by_name)
            assert all(trigger.is_system for trigger in triggers)
            assert all(trigger.type == "cron" for trigger in triggers)
            assert all(not trigger.is_enabled for trigger in triggers), (
                "all CEO triggers must default to disabled (triple gate closed)"
            )
            focus = (
                await db.execute(
                    select(AgentFocusItem).where(AgentFocusItem.agent_id == agent.id)
                )
            ).scalars().all()
            assert any(item.kind == "system" for item in focus), "system focus item missing"
        _ok("FR-CEO-1 entity/permissions/triggers/seat", "seats before=after=2, 5 disabled system triggers")

        # ── Idempotent re-enable + uniqueness ──
        async with async_session() as db:
            tenant_row = await db.get(Tenant, tenant.id)
            owner_row = await db.get(User, owner.id)
            again = await enable_ceo_orchestrator(
                db,
                tenant=tenant_row,
                admin=owner_row,
                member_agent_ids=[employees[0].id],
                briefing_enabled=True,
            )
            await db.commit()
            assert again.ceo_agent_id == ceo_agent_id, "re-enable must reuse the same CEO Agent"
            count = (
                await db.execute(
                    select(func.count()).select_from(CeoOrchestratorSettings)
                )
            ).scalar_one()
            assert count == 1, "re-enable must not insert a second settings row"
        async with async_session() as db:
            tenant_rows = (
                await db.execute(
                    select(func.count())
                    .select_from(CeoOrchestratorSettings)
                    .where(CeoOrchestratorSettings.tenant_id == tenant.id)
                )
            ).scalar_one()
            assert tenant_rows == 1, "exactly one settings row per tenant"
            triggers = (
                await db.execute(
                    select(AgentTrigger).where(AgentTrigger.agent_id == ceo_agent_id)
                )
            ).scalars().all()
            by_name = {trigger.name: trigger for trigger in triggers}
            assert by_name["ceo_daily_brief"].is_enabled is True
            assert by_name["ceo_daily_collection"].is_enabled is True
            assert by_name["ceo_weekly_brief"].is_enabled is True
            assert by_name["ceo_morning_meeting"].is_enabled is False, (
                "meeting cadence switch was not enabled"
            )
        _ok("FR-CEO-1 idempotency + FR-CEO-3 triple gate", "briefing triggers on, meeting trigger off")

        # ── FR-CEO-2: snapshot adapter fail-closed + success ──
        denied = await _company_brief_snapshot_outcome(employees[0].id, {})
        assert denied.status == "failed" and denied.error_code == "ceo_only", (
            f"non-CEO agent must be rejected, got {denied.status}/{denied.error_code}"
        )
        granted = await _company_brief_snapshot_outcome(ceo_agent_id, {"window_hours": 48})
        assert granted.status == "succeeded", (
            f"CEO snapshot failed: {granted.error_code} {granted.result_summary}"
        )
        max_chars = get_settings().CEO_BRIEF_SNAPSHOT_MAX_CHARS
        assert granted.result_summary is not None and len(granted.result_summary) <= max_chars + 200
        assert "Company brief snapshot" in granted.result_summary
        async with async_session() as db:
            snapshot = await build_company_brief_snapshot(
                db,
                tenant_id=tenant.id,
                viewer_user_id=owner.id,
                window_hours=48,
            )
            assert snapshot.employee_total >= 3  # 2 employees + CEO
        _ok("FR-CEO-2 snapshot tool", "ceo_only for employee; bounded markdown for CEO")

        # ── FR-CEO-4: manual meeting registers a durable run, zero Task writes ──
        async with async_session() as db:
            task_count_before = (
                await db.execute(select(func.count()).select_from(Task))
            ).scalar_one()
            settings_row = await get_ceo_settings(db, tenant.id)
            owner_row = await db.get(User, owner.id)
            execution = await start_ceo_meeting(
                db,
                settings=settings_row,
                actor=owner_row,
                kind="morning",
            )
            await db.commit()
            execution_id = execution.id

        async with async_session() as db:
            execution_row = await db.get(TriggerExecution, execution_id)
            assert execution_row is not None
            run = (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.tenant_id == tenant.id,
                        AgentRun.source_execution_id == str(execution_id),
                    )
                )
            ).scalar_one_or_none()
            assert run is not None, "durable run must be registered for the meeting"
            command = (
                await db.execute(
                    select(AgentRunCommand).where(
                        AgentRunCommand.run_id == run.id,
                        AgentRunCommand.idempotency_key == f"start:trigger:{execution_id}",
                    )
                )
            ).scalar_one_or_none()
            assert command is not None, "stable start command identity missing"
            assert run.source_type == "trigger" and run.run_kind == "background"
            settings_row = await get_ceo_settings(db, tenant.id)
            assert settings_row.meeting_group_id is not None, "meeting group must be lazily created"
            created_group_ids.append(settings_row.meeting_group_id)
            group = await db.get(Group, settings_row.meeting_group_id)
            assert group is not None and group.tenant_id == tenant.id
            member_count = (
                await db.execute(
                    select(func.count())
                    .select_from(GroupMember)
                    .where(GroupMember.group_id == group.id)
                )
            ).scalar_one()
            # owner + CEO + 1 selected employee (re-enable narrowed the list)
            assert member_count == 3, f"unexpected meeting group size: {member_count}"
            task_count_after = (
                await db.execute(select(func.count()).select_from(Task))
            ).scalar_one()
            assert task_count_after == task_count_before, "meeting start must not write Task rows"

            # Second meeting reuses the same group
            owner_row = await db.get(User, owner.id)
            execution2 = await start_ceo_meeting(
                db,
                settings=settings_row,
                actor=owner_row,
                kind="weekly",
            )
            await db.commit()
            assert execution2.id != execution_id
        async with async_session() as db:
            settings_row = await get_ceo_settings(db, tenant.id)
            assert settings_row.meeting_group_id == created_group_ids[0]
        _ok("FR-CEO-4 meeting", "durable run registered, group lazy-created/reused, zero Task rows")

        # ── FR-CEO-5: budget cap fail-closed ──
        async with async_session() as db:
            balance = (
                await db.execute(
                    select(func.coalesce(func.sum(CreditTransaction.delta), 0)).where(
                        CreditTransaction.tenant_id == tenant.id
                    )
                )
            ).scalar_one()
            db.add(
                CreditTransaction(
                    tenant_id=tenant.id,
                    delta=-100,
                    balance_after=int(balance) - 100,
                    reason="consume",
                    agent_id=ceo_agent_id,
                )
            )
            settings_row = await get_ceo_settings(db, tenant.id)
            assert settings_row.daily_credit_cap == 20
            denial = await automation_budget_denial(db, settings=settings_row)
            assert denial is not None and denial.startswith("daily_credit_cap_exceeded"), denial

            owner_row = await db.get(User, owner.id)
            try:
                await start_ceo_meeting(db, settings=settings_row, actor=owner_row, kind="morning")
            except CeoOrchestratorError as exc:
                assert exc.code == "ceo_budget_cap_exceeded"
            else:
                raise AssertionError("meeting start must be blocked by the budget cap")

            meeting_trigger = (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == ceo_agent_id,
                        AgentTrigger.name == "ceo_morning_meeting",
                    )
                )
            ).scalar_one()
            await db.commit()

        gated = await gate_ceo_trigger_automation(meeting_trigger, datetime.now(UTC))
        assert gated is True, "over-budget CEO trigger fire must be consumed (skipped)"

        async with async_session() as db:
            refreshed = await db.get(AgentTrigger, meeting_trigger.id)
            assert refreshed.last_fired_at is not None, "skipped fire must mark last_fired_at"
            audit_rows = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant.id,
                        AuditLog.action == "ceo_automation_budget_blocked",
                    )
                )
            ).scalars().all()
            assert len(audit_rows) >= 2, "trigger gate + meeting start must both audit"
            session_ids = (
                (
                    await db.execute(
                        select(ChatSession.id).where(ChatSession.tenant_id == tenant.id)
                    )
                )
                .scalars()
                .all()
            )
            notice = (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id.in_([str(sid) for sid in session_ids]),
                        ChatMessage.role == "system",
                    )
                )
            ).scalars().all()
            assert any("预算帽" in row.content for row in notice), "enabler notification missing"
        _ok("FR-CEO-5 budget cap", "meeting blocked, trigger skipped, audit + notification written")

        # ── disable keeps history ──
        async with async_session() as db:
            settings_row = await get_ceo_settings(db, tenant.id)
            await disable_ceo_orchestrator(db, settings=settings_row)
            await db.commit()
        async with async_session() as db:
            settings_row = await get_ceo_settings(db, tenant.id)
            assert settings_row.enabled is False
            agent = await db.get(Agent, ceo_agent_id)
            assert agent is not None and agent.deleted_at is None, "disable never deletes the Agent"
            triggers = (
                await db.execute(
                    select(AgentTrigger).where(AgentTrigger.agent_id == ceo_agent_id)
                )
            ).scalars().all()
            assert all(not trigger.is_enabled for trigger in triggers)
        _ok("disable", "triggers off, Agent and history retained")

        print("\nALL CEO ORCHESTRATOR CHECKS PASSED")
    finally:
        try:
            await _cleanup(
                tenant_id=tenant.id if tenant else None,
                owner_id=owner.id if owner else None,
                employee_ids=[employee.id for employee in employees],
                ceo_agent_id=ceo_agent_id,
                group_ids=created_group_ids,
            )
        except Exception:
            logger_msg = "cleanup failed"
            print(f"[cleanup] {logger_msg}")


async def _cleanup(
    *,
    tenant_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    employee_ids: list[uuid.UUID],
    ceo_agent_id: uuid.UUID | None,
    group_ids: list[uuid.UUID],
) -> None:
    if tenant_id is None:
        return
    agent_ids = [a for a in [ceo_agent_id, *employee_ids] if a is not None]
    async with async_session() as db:
        run_ids = (
            (
                await db.execute(
                    select(AgentRun.id).where(AgentRun.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        if run_ids:
            await db.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(run_ids)))
            await db.execute(delete(AgentRunCommand).where(AgentRunCommand.run_id.in_(run_ids)))
            await db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
        session_ids = (
            (
                await db.execute(
                    select(ChatSession.id).where(ChatSession.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        if session_ids:
            await db.execute(
                delete(ChatMessage).where(ChatMessage.conversation_id.in_([str(s) for s in session_ids]))
            )
            await db.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
        if agent_ids:
            await db.execute(
                delete(TriggerExecution).where(TriggerExecution.agent_id.in_(agent_ids))
            )
            await db.execute(delete(AgentTrigger).where(AgentTrigger.agent_id.in_(agent_ids)))
            await db.execute(delete(AgentFocusItem).where(AgentFocusItem.agent_id.in_(agent_ids)))
            await db.execute(
                delete(AgentAgentRelationship).where(
                    (AgentAgentRelationship.agent_id.in_(agent_ids))
                    | (AgentAgentRelationship.target_agent_id.in_(agent_ids))
                )
            )
            await db.execute(
                delete(AgentPermission).where(AgentPermission.agent_id.in_(agent_ids))
            )
        tenant_group_ids = list(
            (
                await db.execute(select(Group.id).where(Group.tenant_id == tenant_id))
            )
            .scalars()
            .all()
        )
        for gid in group_ids:
            if gid not in tenant_group_ids:
                tenant_group_ids.append(gid)
        if tenant_group_ids:
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(tenant_group_ids)))
            await db.execute(delete(Group).where(Group.id.in_(tenant_group_ids)))
        await db.execute(
            delete(CeoOrchestratorSettings).where(CeoOrchestratorSettings.tenant_id == tenant_id)
        )
        await db.execute(
            delete(CreditTransaction).where(CreditTransaction.tenant_id == tenant_id)
        )
        await db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
        subject_ids = [a for a in [*agent_ids, owner_id] if a is not None]
        if subject_ids:
            await db.execute(
                delete(Participant).where(Participant.ref_id.in_(subject_ids))
            )
        if agent_ids:
            await db.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
        if owner_id is not None:
            await db.execute(delete(User).where(User.id == owner_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()

    storage_root = get_settings().STORAGE_LOCAL_ROOT or get_settings().AGENT_DATA_DIR
    for agent_id in agent_ids:
        shutil.rmtree(os.path.join(storage_root, str(agent_id)), ignore_errors=True)
    print("[cleanup] synthetic tenant removed")


if __name__ == "__main__":
    asyncio.run(main())
