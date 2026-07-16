"""Supervision reminder service — periodically sends reminders for supervision tasks.

Checks all supervision-type tasks that are not done and sends Feishu reminders
to the target person based on the configured schedule preset.

Schedule presets: daily, every_2_days, every_3_days, weekly

Runs as a background task inside the FastAPI process.
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.task import Task, TaskLog
from app.models.agent import Agent
from app.services.llm import LLMError
from app.config import get_settings

# Fail closed for v1.10.12 RC5. The previous implementation held database
# transactions across LLM/provider calls and had no durable exactly-once claim,
# so enabling it could duplicate sends, token spend, and Credits settlement.
# Re-enabling requires a separate durable-worker design and explicit release.
SUPERVISION_EXECUTION_ENABLED = get_settings().SUPERVISION_EXECUTION_ENABLED

# Schedule JSON format:
# {"freq": "daily"|"weekly", "interval": N, "time": "HH:MM", "weekdays": [0-6]}
# weekdays: 0=Sun, 1=Mon, ..., 6=Sat


def _parse_schedule(remind_schedule: str) -> dict | None:
    """Parse remind_schedule — supports JSON format or legacy simple presets."""
    if not remind_schedule:
        return None
    try:
        sched = json.loads(remind_schedule)
        if isinstance(sched, dict) and "freq" in sched:
            return sched
    except (json.JSONDecodeError, TypeError):
        pass
    # Legacy simple preset fallback
    legacy_map = {
        "daily": {"freq": "daily", "interval": 1, "time": "09:00"},
        "every_2_days": {"freq": "daily", "interval": 2, "time": "09:00"},
        "every_3_days": {"freq": "daily", "interval": 3, "time": "09:00"},
        "weekly": {"freq": "weekly", "interval": 1, "time": "09:00", "weekdays": [1, 2, 3, 4, 5]},
    }
    return legacy_map.get(remind_schedule)


def _is_reminder_due(remind_schedule: str, last_reminded_at: datetime | None, now_utc: datetime) -> bool:
    """Check if a reminder is due based on the schedule config.
    
    All time calculations are anchored to now_utc (provided by tick loop).
    Default behavior is to use UTC for hour/minute checks unless a timezone is specified.
    """
    sched = _parse_schedule(remind_schedule)
    if not sched:
        return False

    freq = sched.get("freq", "daily")
    interval = sched.get("interval", 1)
    time_str = sched.get("time", "09:00")

    # Parse target hour/minute
    try:
        th, tm = map(int, time_str.split(":"))
    except Exception:
        th, tm = 9, 0

    # For now, we use UTC for the hour/minute check.
    # In the future, we should load agent.timezone and convert now_utc.
    current_time = now_utc

    # Not yet time today
    if current_time.hour < th or (current_time.hour == th and current_time.minute < tm):
        return False

    # Already past the time window (allow 60-min window)
    if current_time.hour > th or (current_time.hour == th and current_time.minute > tm + 59):
        return False

    # Weekly: check if today is a selected weekday
    if freq == "weekly":
        weekdays = sched.get("weekdays", [1, 2, 3, 4, 5])
        # Python: Monday=0, Sunday=6 → convert to our format: Sunday=0, Monday=1, ...
        py_weekday = current_time.weekday()  # Mon=0
        our_weekday = (py_weekday + 1) % 7  # Sun=0
        if our_weekday not in weekdays:
            return False

    # Check interval since last reminder
    if last_reminded_at is None:
        return True

    # Ensure both are timezone-aware for comparison
    if last_reminded_at.tzinfo is None:
        last_reminded_at = last_reminded_at.replace(tzinfo=timezone.utc)

    elapsed = now_utc - last_reminded_at
    min_interval = timedelta(days=interval) - timedelta(hours=2)  # tolerance
    return elapsed >= min_interval


async def _get_agent_reply(
    target_agent,
    message: str,
    db,
    *,
    owner_user_id,
) -> str | None:
    """Call target agent's LLM to generate a reply to a supervision reminder.

    Returns the reply text, or None if the agent can't respond.
    """
    from app.services.agent_context import build_agent_context
    from app.services.llm import (
        create_llm_client,
        get_llm_request_options,
        llm_provider_may_have_accepted,
        LLMMessage,
        prepare_agent_llm_invocation,
        release_llm_round_credits,
        reserve_llm_round_credits,
        settle_agent_llm_invocation,
        settle_llm_round_credits,
    )
    from app.services.quota_guard import QuotaExceeded
    from app.services.token_tracker import (
        estimate_token_usage_from_chars,
        extract_token_usage,
        record_token_usage,
    )

    try:
        invocation = await prepare_agent_llm_invocation(target_agent, action="chat")
    except QuotaExceeded:
        logger.warning(f"Supervision reply skipped for agent {target_agent.id}: quota exceeded")
        return None
    if invocation is None:
        return None
    model = invocation.model

    static_prompt, dynamic_prompt = await build_agent_context(
        target_agent.id, target_agent.name, target_agent.role_description or ""
    )

    messages = [
        LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
        LLMMessage(role="user", content=message),
    ]

    client = create_llm_client(
        provider=model.provider,
        api_key=invocation.api_key,
        model=model.model,
        base_url=invocation.base_url,
        timeout=float(getattr(model, 'request_timeout', None) or 60.0),
    )
    usage = None
    round_reservation_id = None
    request_options = get_llm_request_options(model)
    try:
        round_reservation_id = await reserve_llm_round_credits(
            tenant_id=invocation.tenant_id,
            user_id=owner_user_id,
            agent_id=target_agent.id,
            model=model,
            route_meta=invocation.route_meta,
            messages=messages,
            tools=None,
            max_tokens=512,
        )
        response = await client.complete(
            messages=messages,
            temperature=model.temperature,
            max_tokens=512,
            **request_options,
        )
        content = (response.content or "").strip()
        usage = extract_token_usage(response.usage)
        if usage is None:
            usage = estimate_token_usage_from_chars(
                len(static_prompt) + len(dynamic_prompt) + len(message) + len(content)
            )
        try:
            await settle_llm_round_credits(
                round_reservation_id,
                usage=usage,
                model=model,
                route_meta=invocation.route_meta,
                agent_id=target_agent.id,
                user_id=owner_user_id,
                tenant_id=invocation.tenant_id,
            )
        except Exception:
            # The provider completed; keep the hold recoverable when the exact
            # settlement transition cannot be persisted.
            return None
        return content if content else None
    except asyncio.CancelledError:
        await release_llm_round_credits(
            round_reservation_id,
            model=model,
            route_meta=invocation.route_meta,
            agent_id=target_agent.id,
            user_id=owner_user_id,
            tenant_id=invocation.tenant_id,
            provider_failed=not llm_provider_may_have_accepted(client),
        )
        raise
    except LLMError as e:
        await release_llm_round_credits(
            round_reservation_id,
            model=model,
            route_meta=invocation.route_meta,
            agent_id=target_agent.id,
            user_id=owner_user_id,
            tenant_id=invocation.tenant_id,
            provider_failed=not llm_provider_may_have_accepted(client),
        )
        logger.error("_get_agent_reply LLM error_type={}", type(e).__name__)
    except Exception as e:
        await release_llm_round_credits(
            round_reservation_id,
            model=model,
            route_meta=invocation.route_meta,
            agent_id=target_agent.id,
            user_id=owner_user_id,
            tenant_id=invocation.tenant_id,
            provider_failed=not llm_provider_may_have_accepted(client),
        )
        logger.error("_get_agent_reply LLM call failed error_type={}", type(e).__name__)
    finally:
        try:
            await client.close()
        except Exception as e:
            logger.warning(
                "Failed to close supervision LLM client error_type={}",
                type(e).__name__,
            )
        if usage is not None and usage.total_tokens > 0:
            try:
                await record_token_usage(target_agent.id, usage)
            except Exception as e:
                logger.exception(
                    "Failed to record supervision LLM tokens error_type={}",
                    type(e).__name__,
                )
            try:
                await settle_agent_llm_invocation(
                    invocation,
                    agent_id=target_agent.id,
                    user_id=owner_user_id,
                    usage=usage,
                )
            except Exception as e:
                logger.exception(
                    "Failed to settle supervision LLM Credits error_type={}",
                    type(e).__name__,
                )
    return None


async def _send_supervision_reminder(task: Task, agent_name: str):
    """Send a single supervision reminder. Target can be an Agent or a Member."""
    if not SUPERVISION_EXECUTION_ENABLED:
        raise RuntimeError("Supervision execution is disabled by release policy")
    try:
        from app.models.agent import Agent
        from app.models.org import AgentAgentRelationship, AgentRelationship
        from app.models.channel_config import ChannelConfig
        from app.models.activity_log import AgentActivityLog
        from app.services.feishu_service import feishu_service
        from app.core.permissions import (
            evaluate_agent_relationship_status,
            get_agent_access_level_for_user_id,
        )
        from sqlalchemy.orm import selectinload
        import json as _json

        target_name = task.supervision_target_name
        if not target_name:
            logger.warning(f"Supervision task {task.id} has no target name")
            return

        days_since = (datetime.now(timezone.utc) - task.created_at).days
        reminder_msg = (
            f"📋 督办提醒 — 来自 {agent_name}\n\n"
            f"事项：{task.title}\n"
        )
        if task.description:
            reminder_msg += f"说明：{task.description}\n"
        reminder_msg += f"创建于：{days_since} 天前\n"
        if task.due_date:
            reminder_msg += f"截止日期：{task.due_date.strftime('%Y-%m-%d')}\n"
        reminder_msg += "\n请及时处理，谢谢！"

        async with async_session() as db:
            sent = False
            send_method = ""
            owner_id = task.created_by
            src_agent_r = await db.execute(
                select(Agent).where(Agent.id == task.agent_id)
            )
            src_agent = src_agent_r.scalar_one_or_none()
            if src_agent is None or not await get_agent_access_level_for_user_id(
                db,
                owner_id,
                src_agent,
            ):
                logger.warning(
                    "Supervision requester lost source Agent access task={}",
                    task.id,
                )
                return

            # 1. Resolve an Agent only through the source Agent's exact,
            # same-tenant relationship.  Names alone are not an identity.
            agent_relation_result = await db.execute(
                select(AgentAgentRelationship)
                .join(
                    Agent,
                    Agent.id == AgentAgentRelationship.target_agent_id,
                )
                .where(
                    AgentAgentRelationship.agent_id == task.agent_id,
                    Agent.name == target_name,
                    Agent.tenant_id == src_agent.tenant_id,
                )
                .options(selectinload(AgentAgentRelationship.target_agent))
            )
            agent_relations = agent_relation_result.scalars().all()
            if len(agent_relations) > 1:
                logger.warning(
                    "Supervision target name is ambiguous task={} target={}",
                    task.id,
                    target_name,
                )
                return
            target_relation = agent_relations[0] if agent_relations else None
            target_agent = (
                target_relation.target_agent if target_relation else None
            )
            if target_agent:
                relationship_status = await evaluate_agent_relationship_status(
                    db,
                    target_relation,
                    current_user_id=owner_id,
                )
                if (
                    relationship_status["access_status"] != "active"
                    or not await get_agent_access_level_for_user_id(
                        db,
                        owner_id,
                        target_agent,
                    )
                ):
                    logger.warning(
                        "Supervision target access revoked task={} target={}",
                        task.id,
                        target_agent.id,
                    )
                    return

            if target_agent:
                # Send agent-to-agent message via ChatSession + ChatMessage
                from app.models.audit import ChatMessage
                from app.models.chat_session import ChatSession
                from app.models.participant import Participant

                # Get participant for sender agent
                src_part_r = await db.execute(
                    select(Participant).where(Participant.type == "agent", Participant.ref_id == task.agent_id)
                )
                src_part = src_part_r.scalar_one_or_none()
                tgt_part_r = await db.execute(
                    select(Participant).where(Participant.type == "agent", Participant.ref_id == target_agent.id)
                )
                tgt_part = tgt_part_r.scalar_one_or_none()

                # Find or create ChatSession
                session_agent_id = min(task.agent_id, target_agent.id, key=str)
                session_peer_id = max(task.agent_id, target_agent.id, key=str)
                sess_r = await db.execute(
                    select(ChatSession).where(
                        ChatSession.agent_id == session_agent_id,
                        ChatSession.peer_agent_id == session_peer_id,
                        ChatSession.user_id == owner_id,
                        ChatSession.source_channel == "agent",
                    )
                )
                chat_session = sess_r.scalar_one_or_none()
                if not chat_session:
                    await db.execute(
                        select(Agent.id)
                        .where(Agent.id == session_agent_id)
                        .with_for_update()
                    )
                    sess_r = await db.execute(
                        select(ChatSession).where(
                            ChatSession.agent_id == session_agent_id,
                            ChatSession.peer_agent_id == session_peer_id,
                            ChatSession.user_id == owner_id,
                            ChatSession.source_channel == "agent",
                        )
                    )
                    chat_session = sess_r.scalar_one_or_none()
                if not chat_session:
                    chat_session = ChatSession(
                        agent_id=session_agent_id,
                        user_id=owner_id,
                        title=f"{agent_name} ↔ {target_agent.name}",
                        source_channel="agent",
                        participant_id=src_part.id if src_part else None,
                        peer_agent_id=session_peer_id,
                    )
                    db.add(chat_session)
                    await db.flush()

                session_id = str(chat_session.id)

                # Save reminder message
                db.add(ChatMessage(
                    agent_id=session_agent_id, user_id=owner_id,
                    role="user", content=reminder_msg,
                    conversation_id=session_id,
                    participant_id=src_part.id if src_part else None,
                ))
                await db.flush()
                chat_session.last_message_at = datetime.now(timezone.utc)
                sent = True
                send_method = "agent消息"

                # Trigger target agent's LLM to generate a reply
                try:
                    reply = await _get_agent_reply(
                        target_agent,
                        reminder_msg,
                        db,
                        owner_user_id=owner_id,
                    )
                    if reply:
                        db.add(ChatMessage(
                            agent_id=session_agent_id, user_id=owner_id,
                            role="assistant", content=reply,
                            conversation_id=session_id,
                            participant_id=tgt_part.id if tgt_part else None,
                        ))
                        send_method = "agent消息+回复"
                        logger.info(
                            "📋 Target agent replied agent={} reply_chars={}",
                            target_agent.id,
                            len(reply),
                        )
                except Exception as e:
                    logger.warning(
                        "Target agent reply failed error_type={}",
                        type(e).__name__,
                    )
            else:
                # 2. Fallback: find target as a Member in relationships
                rel_result = await db.execute(
                    select(AgentRelationship)
                    .where(AgentRelationship.agent_id == task.agent_id)
                    .options(selectinload(AgentRelationship.member))
                )
                rels = rel_result.scalars().all()
                target_member = None
                for r in rels:
                    if r.member and r.member.name == target_name:
                        target_member = r.member
                        break

                if target_member:
                    # Try Feishu
                    config_r = await db.execute(
                        select(ChannelConfig).where(
                            ChannelConfig.agent_id == task.agent_id,
                            ChannelConfig.channel_type == "feishu",
                        )
                    )
                    config = config_r.scalar_one_or_none()
                    if config and (target_member.email or target_member.phone):
                        try:
                            resolved = await feishu_service.resolve_open_id(
                                config.app_id, config.app_secret,
                                email=target_member.email, mobile=target_member.phone,
                            )
                            if resolved:
                                content = _json.dumps({"text": reminder_msg}, ensure_ascii=False)
                                resp = await feishu_service.send_message(
                                    config.app_id, config.app_secret,
                                    receive_id=resolved, msg_type="text",
                                    content=content, receive_id_type="open_id",
                                )
                                if resp.get("code") == 0:
                                    sent = True
                                    send_method = "飞书"
                        except Exception:
                            pass

            # Log result to TaskLog
            if sent:
                log = TaskLog(task_id=task.id, content=f"✅ 已向 {target_name} 发送督办提醒（{send_method}）")
            elif target_agent or target_name:
                log = TaskLog(task_id=task.id, content=f"📋 督办提醒已触发，目标：{target_name}")
            else:
                log = TaskLog(task_id=task.id, content=f"⚠️ 提醒失败：未找到联系人 '{target_name}'")
            db.add(log)

            # Log to AgentActivityLog for Activity tab visibility
            activity = AgentActivityLog(
                agent_id=task.agent_id,
                action_type="schedule_run",
                summary=f"📋 督办提醒：{task.title} → {target_name}" + (f"（{send_method}已发送）" if sent else ""),
                detail_json={"task_id": str(task.id), "target": target_name, "sent": sent},
                related_id=task.id,
            )
            db.add(activity)
            await db.commit()

            logger.info(f"📋 Supervision reminder task={task.id} sent={sent}")

    except Exception as e:
        logger.exception(
            "Supervision reminder error for task={} error_type={}",
            task.id,
            type(e).__name__,
        )


async def _supervision_tick():
    """One tick: check all supervision tasks and send due reminders."""
    if not SUPERVISION_EXECUTION_ENABLED:
        logger.info("[supervision] execution disabled by release policy")
        return
    logger.info("[supervision] tick running...")
    from app.services.audit_logger import write_audit_log

    try:
        now = datetime.now(timezone.utc)

        async with async_session() as db:
            # Find active supervision tasks
            result = await db.execute(
                select(Task, Agent.name).join(Agent, Agent.id == Task.agent_id).where(
                    Task.type == "supervision",
                    Task.status.in_(["pending", "doing"]),
                    Task.remind_schedule.isnot(None),
                )
            )
            rows = result.all()
            logger.info(f"[supervision] found {len(rows)} supervision tasks")

            await write_audit_log("supervision_tick", {"tasks_found": len(rows)})

            for task, agent_name in rows:
                try:
                    # Get last reminder log for this task
                    log_result = await db.execute(
                        select(TaskLog)
                        .where(TaskLog.task_id == task.id)
                        .order_by(TaskLog.created_at.desc())
                        .limit(1)
                    )
                    last_log = log_result.scalar_one_or_none()
                    last_reminded = last_log.created_at if last_log else None

                    if _is_reminder_due(task.remind_schedule, last_reminded, now):
                        logger.info(f"[supervision] FIRING reminder task={task.id}")
                        await write_audit_log(
                            "supervision_fire",
                            {"task_id": str(task.id), "title": task.title, "target": task.supervision_target_name},
                            agent_id=task.agent_id,
                        )
                        await _send_supervision_reminder(task, agent_name)

                except Exception as e:
                    logger.error(
                        "Error checking supervision task={} error_type={}",
                        task.id,
                        type(e).__name__,
                    )

    except Exception as e:
        logger.exception("Supervision tick error_type={}", type(e).__name__)
        await write_audit_log("supervision_error", {"error_type": type(e).__name__})


async def start_supervision_reminder():
    """Start the background supervision reminder loop. Call from FastAPI startup."""
    if not SUPERVISION_EXECUTION_ENABLED:
        logger.info("[supervision] service not started: release policy is Code-off/automation-off")
        return
    logger.info("📋 [supervision] Reminder service started (60s tick)")
    logger.info("📋 Supervision reminder service started (60s tick)")
    while True:
        await _supervision_tick()
        await asyncio.sleep(60)
