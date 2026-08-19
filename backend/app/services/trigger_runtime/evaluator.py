"""Trigger evaluation and deterministic special-case handlers."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger

MIN_POLL_INTERVAL_MINUTES = 5
OKR_AUTOMATION_TRIGGER_NAMES = frozenset({
    "daily_okr_collection",
    "daily_okr_report",
    "weekly_okr_report",
    "biweekly_okr_checkin",
    "monthly_okr_report",
})
runtime_settings = get_settings()


async def should_skip_non_workday(trigger: AgentTrigger, local_now: datetime) -> bool:
    if trigger.name != "daily_okr_collection":
        return False

    from app.models.okr import OKRSettings
    from app.models.tenant import Tenant
    from app.services.business_calendar import is_non_workday

    async with async_session() as db:
        result = await db.execute(
            select(Agent.tenant_id).where(Agent.id == trigger.agent_id)
        )
        tenant_id = result.scalar_one_or_none()
        if not tenant_id:
            return False

        settings_result = await db.execute(
            select(OKRSettings.daily_report_skip_non_workdays).where(OKRSettings.tenant_id == tenant_id)
        )
        skip_enabled = settings_result.scalar_one_or_none()
        if skip_enabled is False:
            return False

        tenant_result = await db.execute(
            select(Tenant.country_region).where(Tenant.id == tenant_id)
        )
        country_region = tenant_result.scalar_one_or_none()

    return is_non_workday(local_now.date(), country_region)


async def mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with async_session() as db:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if trigger:
                trigger.last_fired_at = now
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to mark skipped trigger {trigger_id}: {e}")


async def mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with async_session() as db:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if trigger:
                trigger.last_fired_at = now
                trigger.fire_count += 1
                if trigger.type == "once":
                    trigger.is_enabled = False
                if trigger.max_fires and trigger.fire_count >= trigger.max_fires:
                    trigger.is_enabled = False
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to mark fired trigger {trigger_id}: {e}")


async def handle_okr_report_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    if not trigger.is_system or trigger.name not in {
        "daily_okr_report",
        "weekly_okr_report",
        "monthly_okr_report",
    }:
        return False
    if not runtime_settings.OKR_AUTOMATION_ENABLED:
        return True

    from zoneinfo import ZoneInfo
    from app.models.okr import OKRSettings
    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )
    from app.services.timezone_utils import get_agent_timezone

    async with async_session() as db:
        agent_result = await db.execute(select(Agent.tenant_id).where(Agent.id == trigger.agent_id))
        tenant_id = agent_result.scalar_one_or_none()
        if not tenant_id:
            return True

        settings_result = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.enabled:
            return True

    tz_name = await get_agent_timezone(trigger.agent_id)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local_today = now.astimezone(tz).date()

    if trigger.name == "daily_okr_report":
        await generate_company_daily_report(tenant_id, local_today - timedelta(days=1))
    elif trigger.name == "weekly_okr_report":
        previous_week_anchor = local_today - timedelta(days=7)
        week_start = previous_week_anchor - timedelta(days=previous_week_anchor.weekday())
        await generate_company_weekly_report(tenant_id, week_start)
    elif trigger.name == "monthly_okr_report":
        previous_month_end = local_today.replace(day=1) - timedelta(days=1)
        await generate_company_monthly_report(tenant_id, previous_month_end)

    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Auto-generated OKR report for trigger {trigger.id}")
    return True


async def handle_okr_collection_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    if not trigger.is_system or trigger.name != "daily_okr_collection":
        return False
    if not runtime_settings.OKR_AUTOMATION_ENABLED:
        return True

    from app.models.okr import OKRSettings
    from app.services.okr_daily_collection import trigger_daily_collection_for_tenant

    async with async_session() as db:
        agent_result = await db.execute(select(Agent.tenant_id).where(Agent.id == trigger.agent_id))
        tenant_id = agent_result.scalar_one_or_none()
        if not tenant_id:
            return True

        settings_result = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.enabled or not settings.daily_report_enabled:
            return True

    await trigger_daily_collection_for_tenant(tenant_id)
    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Deterministic OKR collection sent for trigger {trigger.id}")
    return True


async def handle_ceo_automation_gate(trigger: AgentTrigger, now: datetime) -> bool:
    """Budget/opt-in gate for CEO system triggers (FR-CEO-5, fail-closed).

    CEO trigger names never collide with the OKR special cases above, so the
    094 semantics are untouched. Returns True only when the fire was consumed
    here (skipped); an allowed CEO fire returns False and proceeds onto the
    ordinary durable enqueue chain.
    """
    from app.services.ceo_orchestrator import (
        CEO_SYSTEM_TRIGGER_NAMES,
        gate_ceo_trigger_automation,
    )

    if not trigger.is_system or trigger.name not in CEO_SYSTEM_TRIGGER_NAMES:
        return False
    return await gate_ceo_trigger_automation(trigger, now)


def is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        import socket
        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except (socket.gaierror, ValueError):
            return True
        return False
    except Exception:
        return True


async def evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    if not trigger.is_enabled:
        return False
    if (
        trigger.is_system
        and trigger.name in OKR_AUTOMATION_TRIGGER_NAMES
        and not runtime_settings.OKR_AUTOMATION_ENABLED
    ):
        return False
    if trigger.expires_at and now >= trigger.expires_at:
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    if trigger.last_fired_at:
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        base = trigger.last_fired_at or trigger.created_at
        try:
            tz_name = cfg.get("timezone")
            if not tz_name:
                from app.services.timezone_utils import get_agent_timezone
                tz_name = await get_agent_timezone(trigger.agent_id)
            from zoneinfo import ZoneInfo
            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, Exception):
                tz = ZoneInfo("UTC")
            local_now = now.astimezone(tz)
            local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
            cron = croniter(expr, local_base)
            next_run = cron.get_next(datetime)
            if local_now >= next_run:
                if await should_skip_non_workday(trigger, local_now):
                    await mark_trigger_skipped(trigger.id, now)
                    logger.info(f"[Trigger] Skipped {trigger.id} on non-workday {local_now.date()}")
                    return False
                return True
            return False
        except Exception as e:
            logger.warning(f"Invalid cron expr for trigger {trigger.id}: {e}")
            return False

    if t == "once":
        at_str = cfg.get("at")
        if not at_str:
            return False
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return now >= at and trigger.fire_count == 0
        except Exception:
            return False

    if t == "interval":
        minutes = cfg.get("minutes", 30)
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    if t == "poll":
        interval_min = max(cfg.get("interval_min", 5), MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        return await poll_check(trigger)

    if t == "on_message":
        return await check_new_agent_messages(trigger)

    if t == "webhook":
        return False

    return False


async def poll_check(trigger: AgentTrigger) -> bool:
    import httpx

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    url = cfg.get("url")
    if not url:
        return False
    if is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.id}: private/internal URL")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(cfg.get("method", "GET"), url, headers=cfg.get("headers", {}))
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        current_value = extract_json_path(data, json_path)
        current_str = str(current_value)
        fire_on = cfg.get("fire_on", "change")
        should_fire = False
        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
        else:
            last_value = cfg.get("_last_value")
            should_fire = last_value is not None and current_str != last_value

        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(AgentTrigger).where(AgentTrigger.id == trigger.id).values(config=cfg)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.id}: {e}")

        return should_fire
    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.id}: {e}")
        return False


def extract_json_path(data, path: str):
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def check_new_agent_messages(trigger: AgentTrigger) -> bool:
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    from_agent_name = cfg.get("from_agent_name")
    from_agent_id = cfg.get("from_agent_id")
    from_user_name = cfg.get("from_user_name")
    if not from_agent_name and not from_agent_id and not from_user_name:
        return False

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        async with async_session() as db:
            if from_agent_name or from_agent_id:
                from app.models.participant import Participant
                from app.models.agent import Agent as AgentModel
                if not from_agent_name or not from_agent_id:
                    logger.warning(
                        "Refusing incompletely bound agent on_message trigger {}",
                        trigger.id,
                    )
                    return False
                target_r = await db.execute(
                    select(AgentModel).where(AgentModel.id == trigger.agent_id)
                )
                target_agent = target_r.scalar_one_or_none()
                if not target_agent:
                    return False
                target_tenant_id = target_agent.tenant_id
                try:
                    source_id = uuid.UUID(str(from_agent_id))
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid from_agent_id on trigger {}",
                        trigger.id,
                    )
                    return False
                source_query = select(AgentModel).where(
                    AgentModel.id == source_id
                )
                if target_tenant_id:
                    source_query = source_query.where(
                        AgentModel.tenant_id == target_tenant_id
                    )
                else:
                    source_query = source_query.where(
                        AgentModel.tenant_id.is_(None)
                    )
                agent_r = await db.execute(source_query)
                source_agent = agent_r.scalar_one_or_none()
                if not source_agent:
                    return False
                if source_agent.id == target_agent.id:
                    return False

                from app.services.trigger_runtime.config import (
                    SERVER_CONTEXT_VERSION,
                    SERVER_CONTEXT_VERSION_KEY,
                )

                expected_conversation_id = cfg.get("expected_conversation_id")
                origin_user_id = (
                    cfg.get("_origin_user_id")
                    if cfg.get(SERVER_CONTEXT_VERSION_KEY) == SERVER_CONTEXT_VERSION
                    else None
                )
                try:
                    expected_session_id = (
                        uuid.UUID(str(expected_conversation_id))
                        if expected_conversation_id
                        else None
                    )
                    expected_owner_id = (
                        uuid.UUID(str(origin_user_id))
                        if origin_user_id
                        else None
                    )
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid conversation/owner binding on trigger {}",
                        trigger.id,
                    )
                    return False
                if expected_owner_id is None or expected_session_id is None:
                    logger.warning(
                        "Refusing unbound agent on_message trigger {}",
                        trigger.id,
                    )
                    return False

                from app.core.permissions import (
                    evaluate_agent_relationship_status,
                    get_agent_access_level_for_user_id,
                )
                from app.models.org import AgentAgentRelationship

                if not await get_agent_access_level_for_user_id(
                    db, expected_owner_id, target_agent
                ) or not await get_agent_access_level_for_user_id(
                    db, expected_owner_id, source_agent
                ):
                    logger.warning(
                        "Refusing revoked agent on_message trigger {}",
                        trigger.id,
                    )
                    return False
                relationship_result = await db.execute(
                    select(AgentAgentRelationship).where(
                        AgentAgentRelationship.agent_id == target_agent.id,
                        AgentAgentRelationship.target_agent_id == source_agent.id,
                    )
                )
                relationships = list(relationship_result.scalars().all())
                if len(relationships) != 1:
                    logger.warning(
                        "Refusing ambiguous agent on_message relationship for {}",
                        trigger.id,
                    )
                    return False
                relationship_status = await evaluate_agent_relationship_status(
                    db,
                    relationships[0],
                    current_user_id=expected_owner_id,
                )
                if relationship_status.get("access_status") != "active":
                    return False

                result = await db.execute(
                    select(Participant.id).where(Participant.type == "agent", Participant.ref_id == source_agent.id)
                )
                from_participant = result.scalar_one_or_none()
                if not from_participant:
                    return False
                from sqlalchemy import String as SaString, cast as sa_cast
                session_agent_id = min(target_agent.id, source_agent.id, key=str)
                session_peer_id = max(target_agent.id, source_agent.id, key=str)
                message_query = (
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        # Fix 1: Only match real conversational messages,
                        # not internal tool_call / system records.
                        ChatMessage.role.in_(["assistant", "user"]),
                        # A participant id alone is not an authorization
                        # boundary. Match the exact private A2A lane.
                        ChatSession.source_channel == "agent",
                        ChatSession.agent_id == session_agent_id,
                        ChatSession.peer_agent_id == session_peer_id,
                    )
                )
                if expected_session_id is not None:
                    message_query = message_query.where(
                        ChatSession.id == expected_session_id,
                        ChatMessage.conversation_id == str(expected_session_id),
                    )
                if expected_owner_id is not None:
                    message_query = message_query.where(
                        ChatSession.user_id == expected_owner_id
                    )
                message_query = message_query.order_by(
                    ChatMessage.created_at.desc()
                ).limit(1)
                result = await db.execute(message_query)
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = from_agent_name or source_agent.name
                cfg["_matched_from_agent_id"] = str(source_agent.id)
                cfg["_matched_conversation_id"] = str(msg.conversation_id)
                cfg["_matched_message_id"] = str(msg.id)
                from app.services.trigger_runtime.config import mark_verified_message_context
                mark_verified_message_context(cfg)
                return True

            if from_user_name:
                from sqlalchemy import String as SaString, cast as sa_cast
                if isinstance(from_user_name, list):
                    from_user_name = from_user_name[0] if from_user_name else ""
                if not isinstance(from_user_name, str) or not from_user_name.strip():
                    return False
                from app.services.trigger_runtime.config import (
                    SERVER_CONTEXT_VERSION,
                    SERVER_CONTEXT_VERSION_KEY,
                )

                # A human display name is not an authorization boundary.  The
                # watched source is resolved to one exact P2P session when the
                # trigger is created.  Origin metadata remains the separate
                # destination that receives the trigger result.
                if cfg.get(SERVER_CONTEXT_VERSION_KEY) != SERVER_CONTEXT_VERSION:
                    logger.warning(
                        "Refusing unattested human on_message trigger {}",
                        trigger.id,
                    )
                    return False
                watched_session_id = cfg.get("_watched_session_id")
                watched_user_id = cfg.get("_watched_user_id")
                watched_source_channel = str(
                    cfg.get("_watched_source_channel") or ""
                )
                if watched_source_channel not in {
                    "web",
                    "feishu",
                    "slack",
                    "discord",
                    "wecom",
                    "dingtalk",
                    "wechat",
                    "whatsapp",
                    "teams",
                }:
                    return False
                try:
                    bound_session_id = uuid.UUID(str(watched_session_id))
                    bound_owner_id = uuid.UUID(str(watched_user_id))
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid human conversation binding on trigger {}",
                        trigger.id,
                    )
                    return False

                result = await db.execute(
                    select(ChatMessage)
                    .join(
                        ChatSession,
                        ChatMessage.conversation_id
                        == sa_cast(ChatSession.id, SaString),
                    )
                    .where(
                        ChatSession.id == bound_session_id,
                        ChatSession.agent_id == trigger.agent_id,
                        ChatSession.user_id == bound_owner_id,
                        ChatSession.source_channel == watched_source_channel,
                        ChatSession.is_group.is_(False),
                        ChatMessage.conversation_id == str(bound_session_id),
                        ChatMessage.user_id == bound_owner_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )

                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = from_user_name
                cfg["_matched_conversation_id"] = str(msg.conversation_id)
                cfg["_matched_message_id"] = str(msg.id)
                from app.services.trigger_runtime.config import mark_verified_message_context
                mark_verified_message_context(cfg)
                return True
    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.id}: {e}")
        return False

    return False
