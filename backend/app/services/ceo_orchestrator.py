"""CEO orchestrator (P1 observer) — per-tenant enablement, triggers, meetings, budget gates.

The CEO is a regular ``Agent`` row with ``is_system=True`` and the builtin
``ceo`` role template. It is created only through explicit tenant opt-in here
(never by a global seeder), never consumes an employee seat (``is_system`` is
excluded by ``quota_guard._count_active_tenant_agents``), and its automation is
triple-gated: global rollout allowlist AND tenant ``enabled`` AND the specific
cadence switch — all default off.

094 compatibility: this module never reads or writes the OKR Agent row, the
OKR system triggers, or ``OKRSettings``; briefings reuse only OKR *read* models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent, AgentPermission, AgentTemplate
from app.models.ceo import CeoOrchestratorSettings
from app.models.group import Group
from app.models.audit import AuditLog, ChatMessage
from app.models.org import AgentAgentRelationship
from app.models.subscription import CreditTransaction
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User
from app.services.agent_manager import _render_soul_template, agent_manager
from app.services.agent_tool_assignments import upsert_agent_tool
from app.services.ceo_briefing import (
    ceo_coordination_rollout_allowed,
    ceo_orchestrator_allowed,
)
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.focus_service import ensure_focus_item
from app.services import group_chat_service
from app.services.participant_identity import (
    get_or_create_agent_participant,
    get_or_create_user_participant,
)
from app.services.storage import agent_storage_key, get_storage_backend, store_agent_bytes
from app.services.trigger_runtime.intake import (
    TriggerRuntimeIntakeError,
    enqueue_trigger_runtime,
)


CEO_TEMPLATE_ROLE_KEY = "ceo"
CEO_SYSTEM_FOCUS_REF = "system:ceo_briefings"

CEO_DAILY_BRIEF_TRIGGER = "ceo_daily_brief"
CEO_DAILY_COLLECTION_TRIGGER = "ceo_daily_collection"
CEO_WEEKLY_BRIEF_TRIGGER = "ceo_weekly_brief"
CEO_MORNING_MEETING_TRIGGER = "ceo_morning_meeting"
CEO_WEEKLY_MEETING_TRIGGER = "ceo_weekly_meeting"

CEO_BRIEFING_TRIGGER_NAMES = frozenset(
    {
        CEO_DAILY_BRIEF_TRIGGER,
        CEO_DAILY_COLLECTION_TRIGGER,
        CEO_WEEKLY_BRIEF_TRIGGER,
    }
)
CEO_SYSTEM_TRIGGER_NAMES = frozenset(
    {
        *CEO_BRIEFING_TRIGGER_NAMES,
        CEO_MORNING_MEETING_TRIGGER,
        CEO_WEEKLY_MEETING_TRIGGER,
    }
)

CEO_MEETING_KINDS = frozenset({"morning", "weekly"})
CEO_MEETING_MEMBER_LIMIT = 12

DEFAULT_DAILY_CREDIT_CAP = 20
DEFAULT_MONTHLY_CREDIT_CAP = 300

_CEO_TRIGGER_SPECS = (
    {
        "name": CEO_DAILY_BRIEF_TRIGGER,
        "expr": "0 9 * * *",
        "cadence": "briefing",
        "reason": (
            "系统触发器：每日 09:00 生成 CEO 简报。执行要求：1. 调用 company_brief_snapshot "
            "获取业务全景，必要时用 get_okr/get_okr_settings 只读补充；2. 简报结构固定为"
            "昨日进展/今日优先级/阻塞/风险，缺失数据明确写“无数据”，长度遵守系统上限；"
            "3. 用 write_file 把简报写入 workspace 的 briefs/YYYY-MM-DD.md；"
            "4. 最终回复是给启用者的简报摘要。禁止向成员催报，禁止创建任务或修改 OKR。"
        ),
    },
    {
        "name": CEO_DAILY_COLLECTION_TRIGGER,
        "expr": "0 18 * * *",
        "cadence": "briefing",
        "reason": (
            "系统触发器：每日 18:00 聚合当日成员日报可见性摘要（纯观察，不催报）。"
            "执行要求：1. 调用 company_brief_snapshot 与 OKR 只读工具汇总谁已报/谁未报；"
            "2. 用 write_file 把摘要写入 workspace 的 briefs/YYYY-MM-DD-collection.md，"
            "供次日简报引用；3. 不向任何成员发送任何消息。"
        ),
    },
    {
        "name": CEO_WEEKLY_BRIEF_TRIGGER,
        "expr": "0 9 * * 1",
        "cadence": "briefing",
        "reason": (
            "系统触发器：每周一 09:00 生成 CEO 周报。执行要求与每日简报一致，"
            "观察窗口为最近 7 天（company_brief_snapshot window_hours=168），"
            "纪要写入 workspace 的 briefs/YYYY-Www.md。"
        ),
    },
    {
        "name": CEO_MORNING_MEETING_TRIGGER,
        "expr": "0 9 * * 1-5",
        "cadence": "meeting",
        "reason": (
            "系统触发器：主持晨会/周会（单轮定向询问 + 汇总）。执行要求："
            "1. 调用 company_brief_snapshot 获取全景；2. 对会议成员中的每位员工用 "
            "send_message_to_agent（msg_type=consult）定向询问一次进展（等待回复，不重复发送）；"
            "3. 汇总后用 write_file 把纪要写入 workspace 的 meeting-minutes/YYYY-MM-DD.md，"
            "纪要中的行动项仅以文本“建议行动项”呈现；4. 最终回复是会议汇总。"
            "禁止创建任务、禁止修改任何人的 Focus 或 OKR、禁止外发消息。"
        ),
    },
    {
        "name": CEO_WEEKLY_MEETING_TRIGGER,
        "expr": "0 9 * * 1",
        "cadence": "meeting",
        # Weekly meetings are a distinct manual run today.  Keeping a disabled
        # system trigger gives them their own durable identity without adding a
        # second Monday automation beside the weekday morning meeting.
        "scheduled": False,
        "reason": (
            "系统触发器：主持手动周会（单轮定向询问 + 汇总）。执行要求："
            "1. 调用 company_brief_snapshot 获取最近 7 天业务全景；2. 对会议成员中的每位员工用 "
            "send_message_to_agent（msg_type=consult）定向询问一次本周进展（等待回复，不重复发送）；"
            "3. 汇总后用 write_file 把纪要写入 workspace 的 meeting-minutes/YYYY-Www.md，"
            "纪要中的行动项仅以文本“建议行动项”呈现；4. 最终回复是周会汇总。"
            "禁止创建任务、禁止修改任何人的 Focus 或 OKR、禁止外发消息。"
        ),
    },
)


class CeoOrchestratorError(ValueError):
    """Structured failure for CEO orchestration entry points."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─── Settings access ─────────────────────────────────────────────────


async def get_ceo_settings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> CeoOrchestratorSettings | None:
    result = await db.execute(
        select(CeoOrchestratorSettings).where(
            CeoOrchestratorSettings.tenant_id == tenant_id
        )
    )
    return result.scalar_one_or_none()


async def get_enabled_ceo_settings_for_agent(
    db: AsyncSession,
    agent_id: uuid.UUID,
) -> CeoOrchestratorSettings | None:
    """Return the settings row when ``agent_id`` is an enabled tenant CEO."""
    result = await db.execute(
        select(CeoOrchestratorSettings).where(
            CeoOrchestratorSettings.ceo_agent_id == agent_id
        )
    )
    row = result.scalar_one_or_none()
    if row is None or not row.enabled:
        return None
    return row


async def _get_ceo_template(db: AsyncSession) -> AgentTemplate | None:
    result = await db.execute(
        select(AgentTemplate).where(
            AgentTemplate.is_builtin.is_(True),
            AgentTemplate.role_key == CEO_TEMPLATE_ROLE_KEY,
        )
    )
    return result.scalar_one_or_none()


async def is_enabled_ceo_agent(db: AsyncSession, agent: Agent) -> bool:
    """True when the Agent is this tenant's currently enabled CEO."""
    if not agent.is_system or agent.tenant_id is None:
        return False
    if agent.template_id is None:
        return False
    template_result = await db.execute(
        select(AgentTemplate.role_key).where(AgentTemplate.id == agent.template_id)
    )
    if template_result.scalar_one_or_none() != CEO_TEMPLATE_ROLE_KEY:
        return False
    row = await get_enabled_ceo_settings_for_agent(db, agent.id)
    return row is not None and row.tenant_id == agent.tenant_id


# ─── Enable / disable / settings ─────────────────────────────────────


async def _validate_member_agents(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_agent_ids: list[uuid.UUID],
) -> list[Agent]:
    if len(member_agent_ids) > CEO_MEETING_MEMBER_LIMIT:
        raise CeoOrchestratorError(
            "ceo_meeting_member_limit",
            f"Meeting membership is capped at {CEO_MEETING_MEMBER_LIMIT} employees",
        )
    members: list[Agent] = []
    for member_id in dict.fromkeys(member_agent_ids):
        result = await db.execute(
            select(Agent).where(
                Agent.id == member_id,
                Agent.tenant_id == tenant_id,
                Agent.is_system.is_(False),
                Agent.deleted_at.is_(None),
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise CeoOrchestratorError(
                "ceo_meeting_member_invalid",
                f"Meeting member {member_id} is not an active employee of this company",
            )
        members.append(member)
    return members


def _parse_ceo_meeting_member_ids(raw_ids: list[object] | None) -> list[uuid.UUID]:
    """Normalize persisted/API meeting members or fail closed on bad data."""
    member_ids: list[uuid.UUID] = []
    for raw_id in raw_ids or []:
        try:
            member_ids.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError) as exc:
            raise CeoOrchestratorError(
                "ceo_meeting_member_invalid",
                "CEO meeting membership contains an invalid employee identifier",
            ) from exc
    return list(dict.fromkeys(member_ids))


async def get_validated_ceo_meeting_members(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
) -> list[Agent]:
    """Resolve the persisted meeting roster to active tenant employees.

    Settings are validated when written, but an employee can later be deleted
    or legacy data can contain a malformed identifier.  Manual meetings must
    fail closed against that current state instead of creating a CEO-only room.
    """

    member_ids = _parse_ceo_meeting_member_ids(settings.meeting_member_agent_ids)
    return await _validate_member_agents(
        db,
        tenant_id=settings.tenant_id,
        member_agent_ids=member_ids,
    )


async def _ensure_company_use_permission(db: AsyncSession, agent_id: uuid.UUID) -> None:
    existing = await db.execute(
        select(AgentPermission).where(
            AgentPermission.agent_id == agent_id,
            AgentPermission.scope_type == "company",
            AgentPermission.access_level == "use",
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(AgentPermission(agent_id=agent_id, scope_type="company", access_level="use"))


async def _write_ceo_soul_once(
    db: AsyncSession,
    *,
    agent: Agent,
    template: AgentTemplate,
    admin: User,
) -> None:
    """Render the template soul on first enable; never overwrite on re-enable."""
    storage = get_storage_backend()
    if await storage.exists(agent_storage_key(agent.id, "soul.md")):
        return
    rendered = _render_soul_template(
        template.soul_template,
        agent_name=agent.name,
        creator_name=admin.display_name,
        created_at=datetime.now(timezone.utc).date().isoformat(),
    )
    await store_agent_bytes(
        agent.id,
        "soul.md",
        (rendered.strip() + "\n").encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
    )


async def _grant_template_tools(
    db: AsyncSession,
    *,
    agent: Agent,
    template: AgentTemplate,
) -> None:
    for tool_name in template.default_tools or []:
        result = await db.execute(select(Tool).where(Tool.name == tool_name))
        tool = result.scalar_one_or_none()
        if tool is None:
            raise CeoOrchestratorError(
                "ceo_tool_not_seeded",
                f"CEO template tool '{tool_name}' is not seeded; run the builtin tool seeder first",
            )
        await upsert_agent_tool(
            db,
            agent_id=agent.id,
            tool_id=tool.id,
            enabled=True,
            on_conflict="preserve",
        )


async def _sync_ceo_relationships(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    member_ids: list[uuid.UUID],
    actor_id: uuid.UUID,
) -> None:
    """Keep CEO↔member A2A relationships aligned with the meeting selection."""
    wanted = set(member_ids)
    existing_result = await db.execute(
        select(AgentAgentRelationship).where(
            (AgentAgentRelationship.agent_id == settings.ceo_agent_id)
            | (AgentAgentRelationship.target_agent_id == settings.ceo_agent_id)
        )
    )
    existing = existing_result.scalars().all()
    existing_pairs = {(row.agent_id, row.target_agent_id) for row in existing}
    for member_id in sorted(wanted, key=str):
        for pair in (
            (settings.ceo_agent_id, member_id),
            (member_id, settings.ceo_agent_id),
        ):
            if pair in existing_pairs:
                continue
            db.add(
                AgentAgentRelationship(
                    agent_id=pair[0],
                    target_agent_id=pair[1],
                    relation="collaborator",
                    description="Company CEO collaboration member",
                    created_by_user_id=actor_id,
                )
            )
    for row in existing:
        if row.agent_id == settings.ceo_agent_id and row.target_agent_id not in wanted:
            await db.delete(row)
        elif row.target_agent_id == settings.ceo_agent_id and row.agent_id not in wanted:
            await db.delete(row)


def _trigger_gate_enabled(settings: CeoOrchestratorSettings, *, cadence: str) -> bool:
    """Triple gate: rollout allowlist AND tenant opt-in AND cadence switch."""
    if not ceo_orchestrator_allowed(
        tenant_id=settings.tenant_id,
        agent_id=settings.ceo_agent_id,
    ):
        return False
    if not settings.enabled:
        return False
    if cadence == "briefing":
        return bool(settings.briefing_enabled)
    return bool(settings.morning_meeting_enabled)


async def _sync_ceo_triggers(db: AsyncSession, settings: CeoOrchestratorSettings) -> None:
    """Create/reconcile the CEO's is_system cron triggers (FR-CEO-3/4).

    Fires flow through the existing durable chain (_tick → enqueue_due_trigger →
    claim → enqueue_trigger_runtime); no new scheduler is introduced. The OKR
    evaluator special-cases match OKR trigger names only, so CEO triggers are
    never intercepted by the 094 path.
    """
    system_focus_ref = await ensure_focus_item(
        settings.ceo_agent_id,
        focus_ref=CEO_SYSTEM_FOCUS_REF,
        description="CEO 简报、晨会与业务全景节奏",
        system=True,
        db=db,
    )

    delivery_config: dict = {}
    if settings.enabled_by_user_id is not None:
        # Server-owned delivery context: admitted into the execution payload by
        # runtime_execution_payload only because _server_context_version matches.
        delivery_config = {
            "_server_context_version": 1,
            "_origin_user_id": str(settings.enabled_by_user_id),
            "_origin_source_channel": "platform",
        }

    meeting_members_ready = False
    if _trigger_gate_enabled(settings, cadence="meeting"):
        try:
            meeting_members_ready = bool(
                await get_validated_ceo_meeting_members(db, settings=settings)
            )
        except CeoOrchestratorError as exc:
            logger.warning(
                "[CEO] meeting trigger kept disabled tenant={} agent={} reason={}",
                settings.tenant_id,
                settings.ceo_agent_id,
                exc.code,
            )

    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == settings.ceo_agent_id,
            AgentTrigger.name.in_(sorted(CEO_SYSTEM_TRIGGER_NAMES)),
        )
    )
    existing = {trigger.name: trigger for trigger in result.scalars().all()}

    for spec in _CEO_TRIGGER_SPECS:
        config = {
            "expr": spec["expr"],
            "attach_brief_snapshot": True,
            **delivery_config,
        }
        is_enabled = bool(spec.get("scheduled", True)) and _trigger_gate_enabled(
            settings,
            cadence=spec["cadence"],
        )
        if spec["cadence"] == "meeting":
            is_enabled = is_enabled and meeting_members_ready
        trigger = existing.get(spec["name"])
        if trigger is None:
            db.add(
                AgentTrigger(
                    agent_id=settings.ceo_agent_id,
                    name=spec["name"],
                    type="cron",
                    config=config,
                    reason=spec["reason"],
                    cooldown_seconds=3600,
                    is_system=True,
                    focus_ref=system_focus_ref,
                    is_enabled=is_enabled,
                )
            )
            continue
        trigger.config = config
        trigger.reason = spec["reason"]
        trigger.is_enabled = is_enabled
        trigger.focus_ref = trigger.focus_ref or system_focus_ref


async def enable_ceo_orchestrator(
    db: AsyncSession,
    *,
    tenant: Tenant,
    admin: User,
    member_agent_ids: list[uuid.UUID],
    briefing_enabled: bool = False,
    morning_meeting_enabled: bool = False,
    observer_only_confirmed: bool = False,
    daily_credit_cap: int = DEFAULT_DAILY_CREDIT_CAP,
    monthly_credit_cap: int = DEFAULT_MONTHLY_CREDIT_CAP,
) -> CeoOrchestratorSettings:
    """Idempotently enable the tenant CEO (creates the Agent on first enable).

    Uniqueness is enforced twice: check-then-create in this transaction plus
    the ``ceo_agent_id`` UNIQUE constraint as the final guard.
    """
    if not ceo_orchestrator_allowed(tenant_id=tenant.id):
        raise CeoOrchestratorError(
            "ceo_orchestrator_not_available",
            "CEO orchestrator is not enabled for this company (rollout gate closed)",
        )
    template = await _get_ceo_template(db)
    if template is None:
        raise CeoOrchestratorError(
            "ceo_template_missing",
            "The builtin 'ceo' Agent template is not seeded yet; restart the backend first",
        )
    if not briefing_enabled and not morning_meeting_enabled and not observer_only_confirmed:
        raise CeoOrchestratorError(
            "ceo_enable_intent_required",
            "Select at least one CEO cadence or explicitly confirm observer-only enablement",
        )
    if morning_meeting_enabled and not member_agent_ids:
        raise CeoOrchestratorError(
            "ceo_meeting_members_required",
            "Select at least one active digital employee before enabling the meeting cadence",
        )
    members = await _validate_member_agents(
        db,
        tenant_id=tenant.id,
        member_agent_ids=member_agent_ids,
    )

    settings_row = await get_ceo_settings(db, tenant.id)
    if settings_row is not None:
        agent = await db.get(Agent, settings_row.ceo_agent_id)
        if agent is None or agent.deleted_at is not None:
            raise CeoOrchestratorError(
                "ceo_agent_inconsistent",
                "CEO settings reference a missing Agent; contact platform support",
            )
    else:
        agent = Agent(
            name=template.name,
            role_description=template.description[:500],
            bio=template.description,
            avatar_url="",
            creator_id=admin.id,
            tenant_id=tenant.id,
            status="idle",
            is_system=True,
            heartbeat_enabled=False,
            template_id=template.id,
            # Same default-model rule as ordinary agent creation; runtime model
            # resolution (and its failure semantics) stay identical to any Agent.
            primary_model_id=tenant.default_model_id,
        )
        db.add(agent)
        await db.flush()
        settings_row = CeoOrchestratorSettings(
            tenant_id=tenant.id,
            ceo_agent_id=agent.id,
        )
        db.add(settings_row)
        await db.flush()

    await get_or_create_agent_participant(db, agent.id, agent.name, agent.avatar_url)
    await _ensure_company_use_permission(db, agent.id)
    await agent_manager.initialize_agent_files(db, agent)
    await _write_ceo_soul_once(db, agent=agent, template=template, admin=admin)
    await _grant_template_tools(db, agent=agent, template=template)

    settings_row.enabled = True
    settings_row.enabled_by_user_id = admin.id
    if settings_row.enabled_at is None:
        settings_row.enabled_at = datetime.now(timezone.utc)
    settings_row.briefing_enabled = bool(briefing_enabled)
    settings_row.morning_meeting_enabled = bool(morning_meeting_enabled)
    settings_row.daily_credit_cap = int(daily_credit_cap)
    settings_row.monthly_credit_cap = int(monthly_credit_cap)
    settings_row.meeting_member_agent_ids = [str(member.id) for member in members]
    await db.flush()

    await _sync_ceo_triggers(db, settings_row)
    await _sync_ceo_relationships(
        db,
        settings=settings_row,
        member_ids=[member.id for member in members],
        actor_id=admin.id,
    )
    return settings_row


async def disable_ceo_orchestrator(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
) -> None:
    """Switch off cadence triggers and the master flag; never deletes history."""
    settings.enabled = False
    # Re-enabling P1 must never silently restore P2 dispatch authority.
    settings.coordination_enabled = False
    settings.auto_dispatch_enabled = False
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == settings.ceo_agent_id,
            AgentTrigger.name.in_(sorted(CEO_SYSTEM_TRIGGER_NAMES)),
        )
    )
    for trigger in result.scalars().all():
        trigger.is_enabled = False
    await db.flush()


async def update_ceo_settings(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    actor: User,
    briefing_enabled: bool | None = None,
    morning_meeting_enabled: bool | None = None,
    daily_credit_cap: int | None = None,
    monthly_credit_cap: int | None = None,
    member_agent_ids: list[uuid.UUID] | None = None,
    coordination_enabled: bool | None = None,
    auto_dispatch_enabled: bool | None = None,
    max_parallel_delegations: int | None = None,
) -> CeoOrchestratorSettings:
    """Patch CEO settings while preserving the independent P2 authority gate."""
    projected_meeting_enabled = (
        bool(settings.morning_meeting_enabled)
        if morning_meeting_enabled is None
        else bool(morning_meeting_enabled)
    )
    meeting_configuration_changed = (
        morning_meeting_enabled is not None or member_agent_ids is not None
    )
    projected_member_ids: list[uuid.UUID] | None = None
    if member_agent_ids is not None:
        projected_member_ids = _parse_ceo_meeting_member_ids(list(member_agent_ids))
    elif projected_meeting_enabled and meeting_configuration_changed:
        projected_member_ids = _parse_ceo_meeting_member_ids(
            list(settings.meeting_member_agent_ids or [])
        )
    validated_members: list[Agent] | None = None
    if projected_meeting_enabled and meeting_configuration_changed:
        if not projected_member_ids:
            raise CeoOrchestratorError(
                "ceo_meeting_members_required",
                "Select at least one active digital employee before enabling the meeting cadence",
            )
        validated_members = await _validate_member_agents(
            db,
            tenant_id=settings.tenant_id,
            member_agent_ids=projected_member_ids,
        )
    if briefing_enabled is not None:
        settings.briefing_enabled = bool(briefing_enabled)
    if morning_meeting_enabled is not None:
        settings.morning_meeting_enabled = bool(morning_meeting_enabled)
    if daily_credit_cap is not None:
        settings.daily_credit_cap = max(0, int(daily_credit_cap))
    if monthly_credit_cap is not None:
        settings.monthly_credit_cap = max(0, int(monthly_credit_cap))
    if max_parallel_delegations is not None:
        normalized_parallelism = int(max_parallel_delegations)
        if not 1 <= normalized_parallelism <= 12:
            raise CeoOrchestratorError(
                "ceo_parallel_delegation_limit_invalid",
                "CEO max_parallel_delegations must be between 1 and 12",
            )
        settings.max_parallel_delegations = normalized_parallelism
    if coordination_enabled is not None:
        if coordination_enabled and not ceo_coordination_rollout_allowed(
            tenant_id=settings.tenant_id,
            agent_id=settings.ceo_agent_id,
        ):
            raise CeoOrchestratorError(
                "ceo_coordination_not_available",
                "CEO coordination rollout gate is closed for this company",
            )
        settings.coordination_enabled = bool(coordination_enabled)
        if coordination_enabled:
            settings.coordination_enabled_by_user_id = actor.id
            if settings.coordination_enabled_at is None:
                settings.coordination_enabled_at = datetime.now(timezone.utc)
        else:
            settings.auto_dispatch_enabled = False
    if auto_dispatch_enabled is not None:
        if auto_dispatch_enabled and not bool(settings.coordination_enabled):
            raise CeoOrchestratorError(
                "ceo_coordination_required",
                "Enable CEO coordination before autonomous dispatch",
            )
        if auto_dispatch_enabled and not ceo_coordination_rollout_allowed(
            tenant_id=settings.tenant_id,
            agent_id=settings.ceo_agent_id,
        ):
            raise CeoOrchestratorError(
                "ceo_coordination_not_available",
                "CEO coordination rollout gate is closed for this company",
            )
        settings.auto_dispatch_enabled = bool(auto_dispatch_enabled)
    if member_agent_ids is not None:
        member_ids_to_validate = projected_member_ids or []
        members = validated_members
        if members is None:
            members = await _validate_member_agents(
                db,
                tenant_id=settings.tenant_id,
                member_agent_ids=member_ids_to_validate,
            )
        settings.meeting_member_agent_ids = [str(member.id) for member in members]
        await _sync_ceo_relationships(
            db,
            settings=settings,
            member_ids=[member.id for member in members],
            actor_id=actor.id,
        )
    await db.flush()
    await _sync_ceo_triggers(db, settings)
    return settings


async def sync_ceo_orchestrators_on_startup() -> None:
    """Idempotent startup reconciliation for existing CEO settings rows.

    CEO has no global seeder — this only realigns trigger rows for tenants that
    already opted in, so restarts converge gates after config changes.
    """
    async with async_session() as db:
        result = await db.execute(select(CeoOrchestratorSettings))
        rows = result.scalars().all()
        for row in rows:
            await _sync_ceo_triggers(db, row)
        await db.commit()
    if rows:
        logger.info("[CEO] Startup trigger sync applied to {} tenant(s)", len(rows))


# ─── Budget cap (FR-CEO-5) ────────────────────────────────────────────


async def _credits_consumed_since(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    since: datetime,
) -> int:
    """Read-only aggregation over the existing credit ledger (consume rows only)."""
    result = await db.execute(
        select(CreditTransaction).where(
            CreditTransaction.tenant_id == settings.tenant_id,
            CreditTransaction.agent_id == settings.ceo_agent_id,
            CreditTransaction.delta < 0,
            CreditTransaction.created_at >= since,
        )
    )
    return sum(-row.delta for row in result.scalars().all())


async def automation_budget_denial(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    now: datetime | None = None,
) -> str | None:
    """Return a denial reason when the CEO automation budget cap is exceeded.

    Caps constrain autonomous automation only; interactive chat with the CEO is
    never blocked here (it remains subject to the tenant's credit balance).
    A cap of 0 means unlimited (not recommended). UTC day/month boundaries.
    """
    moment = now or datetime.now(timezone.utc)
    if settings.daily_credit_cap > 0:
        day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        spent_today = await _credits_consumed_since(db, settings=settings, since=day_start)
        if spent_today >= settings.daily_credit_cap:
            return f"daily_credit_cap_exceeded:{spent_today}/{settings.daily_credit_cap}"
    if settings.monthly_credit_cap > 0:
        month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent_month = await _credits_consumed_since(db, settings=settings, since=month_start)
        if spent_month >= settings.monthly_credit_cap:
            return f"monthly_credit_cap_exceeded:{spent_month}/{settings.monthly_credit_cap}"
    return None


async def _notify_enabler_automation_blocked(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    reason: str,
) -> None:
    """Post one system message to the enabler's CEO session (best effort)."""
    if settings.enabled_by_user_id is None:
        return
    try:
        session = await ensure_primary_platform_session(
            db,
            settings.ceo_agent_id,
            settings.enabled_by_user_id,
        )
        db.add(
            ChatMessage(
                id=uuid.uuid4(),
                agent_id=settings.ceo_agent_id,
                user_id=None,
                role="system",
                content=(
                    "CEO 自动化已暂停：本次触发因预算帽被拦截（"
                    f"{reason}）。可在 CEO 设置中调整日/月 Credits 预算帽后恢复。"
                ),
                conversation_id=str(session.id),
                participant_id=None,
                mentions=[],
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()
    except Exception as exc:
        logger.warning(
            "[CEO] budget-block notification skipped tenant_id={} error_type={}",
            settings.tenant_id,
            type(exc).__name__,
        )


def _audit_ceo_automation_blocked(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    reason: str,
    source: str,
) -> None:
    db.add(
        AuditLog(
            tenant_id=settings.tenant_id,
            user_id=settings.enabled_by_user_id,
            action="ceo_automation_budget_blocked",
            details={
                "tenant_id": str(settings.tenant_id),
                "ceo_agent_id": str(settings.ceo_agent_id),
                "reason": reason,
                "source": source,
            },
        )
    )


def _audit_ceo_meeting_membership_blocked(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    reason: str,
    trigger: AgentTrigger,
) -> None:
    db.add(
        AuditLog(
            tenant_id=settings.tenant_id,
            user_id=settings.enabled_by_user_id,
            action="ceo_automation_membership_blocked",
            details={
                "tenant_id": str(settings.tenant_id),
                "ceo_agent_id": str(settings.ceo_agent_id),
                "trigger_id": str(trigger.id),
                "trigger_name": trigger.name,
                "reason": reason,
            },
        )
    )


async def gate_ceo_trigger_automation(trigger: AgentTrigger, now: datetime) -> bool:
    """Budget/opt-in gate for CEO system triggers inside the daemon tick.

    Returns True when the fire was handled here (skipped); False when the
    trigger is not a CEO system trigger or is allowed to proceed onto the
    durable enqueue chain. Fail-closed: any inconsistency skips the fire.
    """
    if not trigger.is_system or trigger.name not in CEO_SYSTEM_TRIGGER_NAMES:
        return False

    async with async_session() as db:
        settings_result = await db.execute(
            select(CeoOrchestratorSettings).where(
                CeoOrchestratorSettings.ceo_agent_id == trigger.agent_id
            )
        )
        settings = settings_result.scalar_one_or_none()
        rollout_open = settings is not None and ceo_orchestrator_allowed(
            tenant_id=settings.tenant_id,
            agent_id=settings.ceo_agent_id,
        )
        if settings is None or not settings.enabled or not rollout_open:
            trigger_result = await db.execute(
                select(AgentTrigger).where(AgentTrigger.id == trigger.id)
            )
            stored = trigger_result.scalar_one_or_none()
            if stored is not None and stored.is_enabled:
                stored.is_enabled = False
                await db.commit()
            return True
        if trigger.name in {
            CEO_MORNING_MEETING_TRIGGER,
            CEO_WEEKLY_MEETING_TRIGGER,
        }:
            meeting_denial: str | None = None
            if not settings.morning_meeting_enabled:
                meeting_denial = "ceo_meeting_not_enabled"
            else:
                try:
                    members = await get_validated_ceo_meeting_members(
                        db,
                        settings=settings,
                    )
                    if not members:
                        meeting_denial = "ceo_meeting_members_required"
                except CeoOrchestratorError as exc:
                    meeting_denial = exc.code
            if meeting_denial is not None:
                trigger_result = await db.execute(
                    select(AgentTrigger).where(AgentTrigger.id == trigger.id)
                )
                stored = trigger_result.scalar_one_or_none()
                if stored is not None:
                    stored.is_enabled = False
                    stored.last_fired_at = now
                _audit_ceo_meeting_membership_blocked(
                    db,
                    settings=settings,
                    reason=meeting_denial,
                    trigger=trigger,
                )
                await db.commit()
                logger.warning(
                    "[CEO] meeting automation fire blocked trigger={} reason={}",
                    trigger.id,
                    meeting_denial,
                )
                return True
        denial = await automation_budget_denial(db, settings=settings, now=now)
        if denial is None:
            return False
        trigger_result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.id == trigger.id)
        )
        stored = trigger_result.scalar_one_or_none()
        if stored is not None:
            stored.last_fired_at = now
        await _notify_enabler_automation_blocked(db, settings=settings, reason=denial)
        _audit_ceo_automation_blocked(db, settings=settings, reason=denial, source="trigger")
        await db.commit()
        logger.info(
            "[CEO] automation fire blocked trigger={} reason={}",
            trigger.id,
            denial,
        )
        return True


# ─── Meetings (FR-CEO-4) ──────────────────────────────────────────────


async def _ensure_meeting_group(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    actor: User,
    member_agents: list[Agent] | None = None,
) -> uuid.UUID:
    """Lazily create the meeting Group on first use (never at enable time)."""
    if settings.meeting_group_id is not None:
        existing = await db.get(Group, settings.meeting_group_id)
        if (
            existing is not None
            and existing.tenant_id == settings.tenant_id
            and existing.deleted_at is None
        ):
            # Repair the legacy morning-only label because this one room is
            # shared by both morning and weekly meetings.
            existing.name = "CEO 会议室"
            existing.description = "CEO orchestrator meeting room: governed summaries and minutes."
            return settings.meeting_group_id
        settings.meeting_group_id = None

    creator = await get_or_create_user_participant(
        db,
        actor.id,
        actor.display_name,
        actor.avatar_url,
    )
    ceo_agent = await db.get(Agent, settings.ceo_agent_id)
    member_participant_ids: list[uuid.UUID] = []
    if ceo_agent is not None:
        ceo_participant = await get_or_create_agent_participant(
            db,
            ceo_agent.id,
            ceo_agent.name,
            ceo_agent.avatar_url,
        )
        member_participant_ids.append(ceo_participant.id)
    if member_agents is None:
        member_agents = await get_validated_ceo_meeting_members(
            db,
            settings=settings,
        )
    for member in member_agents:
        participant = await get_or_create_agent_participant(
            db,
            member.id,
            member.name,
            member.avatar_url,
        )
        member_participant_ids.append(participant.id)

    group = await group_chat_service.create_group(
        db,
        tenant_id=settings.tenant_id,
        creator_participant_id=creator.id,
        name="CEO 会议室",
        description="CEO orchestrator meeting room: governed summaries and minutes.",
        member_participant_ids=member_participant_ids,
    )
    settings.meeting_group_id = group.id
    await db.flush()
    return group.id


async def start_ceo_meeting(
    db: AsyncSession,
    *,
    settings: CeoOrchestratorSettings,
    actor: User,
    kind: str,
) -> TriggerExecution:
    """Manually fire one CEO meeting as a durable trigger-runtime run.

    Registration goes through the same ``enqueue_trigger_runtime`` intake the
    daemon queue uses, so identity (``source_execution_id`` +
    ``start:trigger:<execution>`` idempotency) and audit shape are identical to
    a scheduled fire. The queue's ``is_enabled`` guard protects the *scheduled*
    cadence only; the manual path carries its own gates (opt-in + rollout +
    budget cap). The action item policy is text-only: no Task rows are created
    anywhere in this flow.
    """
    if kind not in CEO_MEETING_KINDS:
        raise CeoOrchestratorError(
            "ceo_meeting_kind_invalid",
            f"Meeting kind must be one of {sorted(CEO_MEETING_KINDS)}",
        )
    if not settings.enabled:
        raise CeoOrchestratorError(
            "ceo_orchestrator_disabled",
            "CEO orchestrator is disabled for this company",
        )
    if not ceo_orchestrator_allowed(
        tenant_id=settings.tenant_id,
        agent_id=settings.ceo_agent_id,
    ):
        raise CeoOrchestratorError(
            "ceo_orchestrator_not_available",
            "CEO orchestrator rollout gate is closed for this company",
        )
    if not settings.morning_meeting_enabled:
        raise CeoOrchestratorError(
            "ceo_meeting_not_enabled",
            "Enable the CEO meeting cadence before starting a meeting",
        )
    meeting_members = await get_validated_ceo_meeting_members(
        db,
        settings=settings,
    )
    if not meeting_members:
        raise CeoOrchestratorError(
            "ceo_meeting_members_required",
            "Select at least one active digital employee before starting a meeting",
        )
    denial = await automation_budget_denial(db, settings=settings)
    if denial is not None:
        # The API layer converts the error below into HTTPException and the
        # request transaction is rolled back, which would silently drop the
        # notification and audit rows. Persist them in an independent session
        # (same pattern as the daemon gate in gate_ceo_trigger_automation).
        tenant_id = settings.tenant_id
        async with async_session() as audit_db:
            persisted_result = await audit_db.execute(
                select(CeoOrchestratorSettings).where(
                    CeoOrchestratorSettings.tenant_id == tenant_id
                )
            )
            persisted_settings = persisted_result.scalar_one_or_none()
            if persisted_settings is not None:
                await _notify_enabler_automation_blocked(audit_db, settings=persisted_settings, reason=denial)
                _audit_ceo_automation_blocked(audit_db, settings=persisted_settings, reason=denial, source="meeting_start")
                await audit_db.commit()
        raise CeoOrchestratorError(
            "ceo_budget_cap_exceeded",
            f"CEO automation budget cap reached ({denial}); the meeting was not started",
        )

    group_id = await _ensure_meeting_group(
        db,
        settings=settings,
        actor=actor,
        member_agents=meeting_members,
    )

    trigger_name = (
        CEO_MORNING_MEETING_TRIGGER
        if kind == "morning"
        else CEO_WEEKLY_MEETING_TRIGGER
    )
    trigger_result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == settings.ceo_agent_id,
            AgentTrigger.name == trigger_name,
        )
    )
    trigger = trigger_result.scalar_one_or_none()
    if trigger is None:
        raise CeoOrchestratorError(
            "ceo_meeting_trigger_missing",
            "CEO meeting trigger is not synced; re-save CEO settings first",
        )
    agent = await db.get(Agent, settings.ceo_agent_id)
    if agent is None or agent.deleted_at is not None:
        raise CeoOrchestratorError(
            "ceo_agent_inconsistent",
            "CEO settings reference a missing Agent; contact platform support",
        )

    now = datetime.now(timezone.utc)
    execution = TriggerExecution(
        id=uuid.uuid4(),
        trigger_id=trigger.id,
        agent_id=trigger.agent_id,
        source="manual",
        status="pending",
        idempotency_key=f"ceo-meeting:{kind}:{uuid.uuid4()}",
        payload={
            "meeting_kind": kind,
            "meeting_group_id": str(group_id),
            "requested_by_user_id": str(actor.id),
        },
        payload_text="",
        scheduled_at=now,
    )
    db.add(execution)
    await db.flush()
    try:
        handle = await enqueue_trigger_runtime(
            db,
            execution=execution,
            trigger=trigger,
            agent=agent,
        )
    except TriggerRuntimeIntakeError as exc:
        raise CeoOrchestratorError(
            f"ceo_meeting_registration_failed:{exc.code}",
            f"Meeting run registration failed: {exc}",
        ) from exc
    if handle is None:
        raise CeoOrchestratorError(
            "ceo_runtime_v2_disabled",
            "Unified Runtime is required for CEO meeting execution",
        )
    trigger.last_fired_at = now
    trigger.fire_count = (trigger.fire_count or 0) + 1
    await db.flush()
    return execution


__all__ = [
    "CEO_BRIEFING_TRIGGER_NAMES",
    "CEO_MEETING_KINDS",
    "CEO_MEETING_MEMBER_LIMIT",
    "CEO_MORNING_MEETING_TRIGGER",
    "CEO_WEEKLY_MEETING_TRIGGER",
    "CEO_SYSTEM_TRIGGER_NAMES",
    "CEO_TEMPLATE_ROLE_KEY",
    "CeoOrchestratorError",
    "DEFAULT_DAILY_CREDIT_CAP",
    "DEFAULT_MONTHLY_CREDIT_CAP",
    "automation_budget_denial",
    "disable_ceo_orchestrator",
    "enable_ceo_orchestrator",
    "gate_ceo_trigger_automation",
    "get_ceo_settings",
    "get_validated_ceo_meeting_members",
    "get_enabled_ceo_settings_for_agent",
    "is_enabled_ceo_agent",
    "start_ceo_meeting",
    "sync_ceo_orchestrators_on_startup",
    "update_ceo_settings",
]
