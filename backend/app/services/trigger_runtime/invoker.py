"""Trigger invocation and delivery orchestration."""

from __future__ import annotations

import asyncio
import json as _json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.trigger import AgentTrigger
from app.core.permissions import get_agent_access_level_for_user_id
from app.services.trigger_runtime import (
    mark_trigger_executions_completed,
    mark_trigger_executions_failed,
    renew_trigger_execution_leases,
)
from app.services.trigger_runtime.config import trigger_delivery_identity

INTERNAL_A2A_TRIGGER_NAMES = {"a2a_wake", "__a2a_wake__"}


async def _capture_invocation_failure(
    agent_id: uuid.UUID,
    error: BaseException,
) -> None:
    """Record a sanitized issue rollup for failures consumed by this invoker."""
    from app.services.production_issue_monitor import record_production_issue

    category = "database" if isinstance(error, (SQLAlchemyError, TimeoutError)) else "trigger"
    await record_production_issue(
        source="trigger_runtime",
        category=category,
        summary="Trigger agent invocation failed",
        severity="error",
        error_code=type(error).__name__,
        operation="invoke_agent",
        agent_id=agent_id,
        metadata={
            "component": "trigger_invoker",
            "error_type": type(error).__name__,
        },
    )


async def _validated_delivery_session(
    db,
    agent: Agent,
    session_id: object,
    *,
    require_agent_channel: bool = False,
) -> ChatSession | None:
    """Resolve a trigger destination only within the Agent's current access boundary."""

    try:
        parsed_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None

    session = await db.get(ChatSession, parsed_session_id)
    if not session:
        return None

    source_channel = str(session.source_channel or "")
    if source_channel == "agent":
        if agent.id not in {session.agent_id, session.peer_agent_id}:
            return None
        peer_agent_id = (
            session.peer_agent_id
            if session.agent_id == agent.id
            else session.agent_id
        )
        if not peer_agent_id:
            return None
        peer_agent = await db.get(Agent, peer_agent_id)
        if not peer_agent or peer_agent.tenant_id != agent.tenant_id:
            return None
    else:
        if require_agent_channel or session.agent_id != agent.id:
            return None

    if not session.user_id:
        return None
    if not await get_agent_access_level_for_user_id(db, session.user_id, agent):
        return None
    return session


async def resolve_trigger_delivery_target(agent: Agent, triggers: list[AgentTrigger]) -> dict | None:
    """Resolve only ordinary user-session delivery.

    A2A delivery is deliberately excluded from this compatibility path.  It is
    accepted only by ``_validated_internal_a2a_target`` for the isolated,
    system-owned wake trigger and is revalidated again before persistence.
    """

    from app.services.chat_session_service import ensure_primary_platform_session

    origin_cfg = None
    for trigger in triggers:
        cfg = trigger.config or {}
        if cfg.get("_origin_session_id") or cfg.get("_origin_user_id"):
            origin_cfg = cfg
            break
    if not origin_cfg:
        return None

    origin_source_channel = origin_cfg.get("_origin_source_channel")
    origin_session_id = origin_cfg.get("_origin_session_id")
    origin_user_id = origin_cfg.get("_origin_user_id")

    try:
        async with async_session() as db:
            origin_session = None
            if origin_session_id:
                origin_session = await _validated_delivery_session(
                    db,
                    agent,
                    origin_session_id,
                )
                if not origin_session:
                    return None
                if (
                    origin_source_channel
                    and origin_source_channel != origin_session.source_channel
                ):
                    return None

            actual_source_channel = (
                origin_session.source_channel
                if origin_session
                else origin_source_channel
            )
            try:
                configured_user_id = (
                    uuid.UUID(str(origin_user_id))
                    if origin_user_id
                    else None
                )
            except (TypeError, ValueError):
                return None
            if (
                origin_session
                and configured_user_id
                and configured_user_id != origin_session.user_id
            ):
                return None
            if actual_source_channel == "agent":
                if not origin_session:
                    return None
                return {
                    "kind": "session",
                    "session_id": str(origin_session.id),
                    "owner_user_id": str(origin_session.user_id),
                    "source_channel": "agent",
                }

            if actual_source_channel == "trigger":
                return None

            if origin_session:
                target_user_id = origin_session.user_id
            else:
                target_user_id = configured_user_id
                if not await get_agent_access_level_for_user_id(
                    db,
                    target_user_id,
                    agent,
                ):
                    return None
            if not target_user_id:
                return None

            primary = await ensure_primary_platform_session(
                db,
                agent.id,
                target_user_id,
            )
            await db.commit()
            return {
                "kind": "primary_user_session",
                "session_id": str(primary.id),
                "owner_user_id": str(primary.user_id),
                "source_channel": primary.source_channel,
            }
    except Exception:
        return None

    return None


def _build_trigger_delivery_notification(
    *,
    agent_id: uuid.UUID,
    delivery_target: dict,
    content: str,
    triggers: list[str],
):
    """Bind persisted notification ownership to the validated target session user."""

    from app.services.chat_session_service import build_persisted_trigger_notification

    return build_persisted_trigger_notification(
        agent_id=agent_id,
        user_id=uuid.UUID(str(delivery_target["owner_user_id"])),
        conversation_id=str(delivery_target["session_id"]),
        content=content,
        triggers=triggers,
    )


async def _validated_internal_a2a_target(
    db,
    agent: Agent,
    triggers: list[AgentTrigger],
) -> dict | None:
    """Validate the one durable A2A trigger before any context or LLM work."""

    a2a_triggers = [
        trigger
        for trigger in triggers
        if trigger.type == "a2a" or trigger.name in INTERNAL_A2A_TRIGGER_NAMES
    ]
    if not a2a_triggers:
        return None
    if len(triggers) != 1 or len(a2a_triggers) != 1:
        raise PermissionError("A2A executions must be invoked in an isolated batch")

    trigger = a2a_triggers[0]
    if (
        trigger.type != "a2a"
        or trigger.name not in INTERNAL_A2A_TRIGGER_NAMES
        or not trigger.is_system
    ):
        raise PermissionError("Untrusted A2A trigger")
    session_id = (trigger.config or {}).get("_a2a_session_id")
    session = await _validated_delivery_session(
        db,
        agent,
        session_id,
        require_agent_channel=True,
    )
    if not session:
        raise PermissionError("A2A delivery session is not authorized")
    return {
        "session_id": str(session.id),
        "owner_user_id": str(session.user_id),
    }


async def _persist_validated_a2a_reply(
    *,
    agent: Agent,
    target: dict,
    content: str,
) -> None:
    """Revalidate and persist an A2A reply without trusting runtime config."""

    from app.models.audit import ChatMessage
    from app.models.participant import Participant

    async with async_session() as db:
        validated_session = await _validated_delivery_session(
            db,
            agent,
            target["session_id"],
            require_agent_channel=True,
        )
        if not validated_session:
            raise PermissionError(
                "A2A delivery authorization changed before persistence"
            )
        participant_result = await db.execute(
            select(Participant).where(
                Participant.type == "agent",
                Participant.ref_id == agent.id,
            )
        )
        participant = participant_result.scalar_one_or_none()
        db.add(
            ChatMessage(
                agent_id=agent.id,
                conversation_id=str(validated_session.id),
                role="assistant",
                content=content,
                user_id=validated_session.user_id,
                participant_id=participant.id if participant else None,
            )
        )
        validated_session.last_message_at = datetime.now(timezone.utc)
        await db.commit()


async def invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log
    from app.services.llm import call_llm, resolve_agent_model

    execution_claims = [
        (
            uuid.UUID(str((t.config or {}).get("_execution_id"))),
            str((t.config or {}).get("_execution_lease_token")),
        )
        for t in triggers
        if (t.config or {}).get("_execution_id")
        and (t.config or {}).get("_execution_lease_token")
    ]
    invocation_task = asyncio.current_task()
    lease_renewal_task = None
    validated_a2a_target: dict | None = None

    async def _renew_execution_claims() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                renewed = await renew_trigger_execution_leases(execution_claims)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Trigger lease renewal failed agent_id={} error_type={}",
                    agent_id,
                    type(exc).__name__,
                )
                renewed = 0
            if renewed != len(execution_claims):
                logger.error(
                    "Trigger lease fence lost agent_id={} expected={} renewed={}",
                    agent_id,
                    len(execution_claims),
                    renewed,
                )
                if invocation_task is not None:
                    invocation_task.cancel()
                return

    if execution_claims:
        lease_renewal_task = asyncio.create_task(_renew_execution_claims())

    async def _stop_lease_renewal() -> None:
        nonlocal lease_renewal_task
        if lease_renewal_task is None:
            return
        lease_renewal_task.cancel()
        await asyncio.gather(lease_renewal_task, return_exceptions=True)
        lease_renewal_task = None

    try:
        ordinary_identities = {
            trigger_delivery_identity(trigger.config)
            for trigger in triggers
            if trigger.type != "a2a"
            and trigger.name not in INTERNAL_A2A_TRIGGER_NAMES
        }
        if len(ordinary_identities) > 1:
            raise PermissionError(
                "Trigger batch contains multiple delivery principals"
            )
        legacy_unfenced_execution_ids = [
            uuid.UUID(str((t.config or {}).get("_execution_id")))
            for t in triggers
            if (t.config or {}).get("_execution_id")
            and not (t.config or {}).get("_execution_lease_token")
        ]
        if legacy_unfenced_execution_ids:
            raise RuntimeError("Trigger execution is missing its claim-generation fence")
        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent or agent.is_expired:
                if execution_claims:
                    await _stop_lease_renewal()
                    await mark_trigger_executions_failed(
                        execution_claims,
                        "Agent not found or is expired",
                    )
                return

            validated_a2a_target = await _validated_internal_a2a_target(
                db,
                agent,
                triggers,
            )

            primary_model, fallback_model, route_meta = await resolve_agent_model(agent)
            model = primary_model or fallback_model
            if not model:
                logger.warning(f"Agent {agent.id} has no LLM model, skipping trigger invocation")
                if execution_claims:
                    await _stop_lease_renewal()
                    await mark_trigger_executions_failed(
                        execution_claims,
                        "Agent has no LLM model configured",
                    )
                return
            if not model.enabled:
                logger.warning(f"Agent {agent.id} model is unavailable, skipping trigger invocation")
                if execution_claims:
                    await _stop_lease_renewal()
                    await mark_trigger_executions_failed(
                        execution_claims,
                        "Agent model is unavailable or disabled",
                    )
                return

            context_parts = []
            trigger_names = []
            for t in triggers:
                part = f"触发器：{t.name} ({t.type})\n原因：{t.reason}"
                if t.is_system and t.name == "daily_okr_collection":
                    part += (
                        "\n执行要求：先调用 get_okr_settings 确认日报收集是否开启。"
                        "如果开启，只能联系你关系网络中的成员和数字员工来收集今天的最终日报，"
                        "并整理成不超过 2000 字的正式日报；"
                        "如果未开启，则说明本次无需执行并停止。"
                    )
                elif t.is_system and t.name in (
                    "daily_okr_report",
                    "weekly_okr_report",
                    "monthly_okr_report",
                ):
                    part += (
                        "\n执行要求：本次公司级报表由系统自动汇总生成。"
                        "如果你被唤醒，仅补充必要说明，不要再次向成员发起收集。"
                    )
                elif t.is_system and t.name == "biweekly_okr_checkin":
                    part += (
                        "\n执行要求：先调用 get_okr_settings 确认 OKR 是否开启。"
                        "如果开启，检查当前周期公司和成员 OKR，主动提醒尚未设置或进展滞后的相关成员；"
                        "如果未开启，则说明本次无需执行并停止。"
                    )
                if t.focus_ref:
                    part += f"\n关联 Focus：{t.focus_ref}"
                cfg = t.config or {}
                if t.type in {"on_message", "a2a"} and cfg.get("_matched_message"):
                    matched_message = str(cfg["_matched_message"])
                    source_message_id = cfg.get("_source_message_id") or cfg.get(
                        "_matched_message_id"
                    )
                    if source_message_id:
                        try:
                            source_query = select(ChatMessage.content).where(
                                ChatMessage.id == uuid.UUID(str(source_message_id))
                            )
                            expected_conversation_id = (
                                validated_a2a_target["session_id"]
                                if t.type == "a2a" and validated_a2a_target
                                else cfg.get("expected_conversation_id")
                            )
                            if expected_conversation_id:
                                source_query = source_query.where(
                                    ChatMessage.conversation_id
                                    == str(expected_conversation_id)
                                )
                            persisted_message = (
                                await db.execute(source_query)
                            ).scalar_one_or_none()
                            if persisted_message is not None:
                                matched_message = persisted_message[:32000]
                                if len(persisted_message) > 32000:
                                    matched_message += "\n…(message truncated at 32,000 characters)"
                        except (TypeError, ValueError):
                            logger.warning("Ignoring invalid A2A source message id")
                    part += (
                        f"\n收到来自 {cfg.get('_matched_from', '?')} 的消息："
                        f"\n\"{matched_message}\""
                    )
                    if t.type == "a2a":
                        if cfg.get("_a2a_kind") == "task_delegate":
                            part += (
                                "\n执行要求：这是明确委派给你的任务。请完成任务并给出可直接交付给"
                                "发送方的最终结果；不要把它当作无需回复的通知。"
                            )
                        else:
                            part += (
                                "\n执行要求：这是另一位数字员工发送的通知。请确认其影响，更新必要的"
                                "工作状态，并仅在确有后续行动时采取行动。"
                            )
                if t.type == "on_message" and cfg.get("okr_member_id") and cfg.get("okr_report_date"):
                    part += (
                        "\n执行要求：这是一次日报回复入库事件。"
                        f"\n1. 将对方回复整理成一段不超过 2000 字的最终日报。"
                        f"\n2. 立即调用 upsert_member_daily_report(report_date=\"{cfg['okr_report_date']}\", "
                        f"member_type=\"{cfg.get('okr_member_type', 'user')}\", "
                        f"member_id=\"{cfg['okr_member_id']}\", content=\"<整理后的日报>\")。"
                        "\n3. 工具调用成功后，再发送一句简短确认，明确你已收到并已记录。"
                        "\n4. 不要只回复确认而不调用工具，也不要把原始长对话原样存入日报。"
                    )
                if t.type == "webhook" and cfg.get("_webhook_payload"):
                    payload_str = cfg["_webhook_payload"]
                    if len(payload_str) > 2000:
                        payload_str = payload_str[:2000] + "... (truncated)"
                    part += f"\nWebhook Payload:\n{payload_str}"
                context_parts.append(part)
                trigger_names.append(t.name)

            trigger_context = (
                "===== 本次唤醒上下文 =====\n"
                f"唤醒来源：trigger（{'多个触发器同时触发' if len(triggers) > 1 else '触发器触发'}）\n\n"
                + "\n---\n".join(context_parts)
                + "\n==========================="
            )

            title = f"🤖 内心独白：{', '.join(trigger_names)}"
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = ChatSession(
                agent_id=agent_id,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id
            messages = [{"role": "user", "content": trigger_context}]
            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="user",
                content=trigger_context,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            ))
            await db.commit()
            agent_participant_id = agent_participant.id if agent_participant else None

        collected_content: list[str] = []
        delivered_platform_message_via_tool = False

        async def on_chunk(text):
            collected_content.append(text)

        async def on_tool_call(data):
            nonlocal delivered_platform_message_via_tool
            try:
                tool_name = data.get("name")
                tool_status = data.get("status")
                if tool_status == "done" and tool_name == "send_platform_message":
                    result_text = str(data.get("result", ""))
                    if result_text.startswith("✅"):
                        delivered_platform_message_via_tool = True

                async with async_session() as _tc_db:
                    if data["status"] == "running":
                        _tc_db.add(ChatMessage(
                            agent_id=agent_id,
                            conversation_id=str(session_id),
                            role="tool_call",
                            content=_json.dumps({"name": data["name"], "args": data["args"]}, ensure_ascii=False, default=str),
                            user_id=agent.creator_id,
                            participant_id=agent_participant_id,
                        ))
                    elif data["status"] == "done":
                        result_str = str(data.get("result", ""))[:2000]
                        _tc_db.add(ChatMessage(
                            agent_id=agent_id,
                            conversation_id=str(session_id),
                            role="tool_call",
                            content=_json.dumps({"name": data["name"], "result": result_str}, ensure_ascii=False, default=str),
                            user_id=agent.creator_id,
                            participant_id=agent_participant_id,
                        ))
                    await _tc_db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        from_agent_name = None
        for t in triggers:
            cfg = t.config or {}
            if cfg.get("from_agent_name"):
                from_agent_name = cfg.get("from_agent_name")
                break

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=agent.creator_id,
            session_id=str(session_id),
            on_chunk=on_chunk,
            on_tool_call=on_tool_call,
            current_user_name_override=from_agent_name,
            route_meta=route_meta,
        )

        async with async_session() as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()
            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="assistant",
                content=reply or "".join(collected_content),
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            ))
            await db.commit()

        final_reply = reply or "".join(collected_content)
        if validated_a2a_target and final_reply:
            await _persist_validated_a2a_reply(
                agent=agent,
                target=validated_a2a_target,
                content=final_reply,
            )

        is_a2a_internal = validated_a2a_target is not None
        delivery_target = None if is_a2a_internal else await resolve_trigger_delivery_target(agent, triggers)

        if final_reply and delivery_target and not delivered_platform_message_via_tool:
            try:
                from app.api.websocket import manager as ws_manager
                agent_id_str = str(agent_id)
                trigger_reasons = []
                for t in triggers:
                    ns = (t.config or {}).get("_notification_summary", "").strip()
                    if ns:
                        trigger_reasons.append(ns)
                    else:
                        r = (t.reason or "").strip()
                        if r and len(r) <= 80:
                            trigger_reasons.append(r)
                        elif r:
                            trigger_reasons.append(r[:77] + "...")
                summary = trigger_reasons[0] if trigger_reasons else "有新的事件需要处理"
                notification = f"⚡ {summary}\n\n{final_reply}"
                target_session_id = delivery_target["session_id"]
                owner_user_id = delivery_target.get("owner_user_id")

                message, notification_payload = _build_trigger_delivery_notification(
                    agent_id=agent_id,
                    delivery_target=delivery_target,
                    content=notification,
                    triggers=[t.name for t in triggers],
                )

                async with async_session() as db:
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer
                    from app.models.chat_session import ChatSession
                    db.add(message)
                    session_row = await db.get(ChatSession, uuid.UUID(target_session_id))
                    if session_row:
                        session_row.last_message_at = datetime.now(timezone.utc)
                    if owner_user_id:
                        await maybe_mark_session_read_for_active_viewer(
                            db,
                            agent_id=agent_id,
                            session_id=target_session_id,
                            user_id=uuid.UUID(owner_user_id),
                        )
                    await db.commit()

                if owner_user_id:
                    await ws_manager.send_to_user(
                        agent_id_str,
                        owner_user_id,
                        notification_payload,
                    )
            except Exception as e:
                logger.error(f"Failed to push trigger result to WebSocket: {e}")

        await write_audit_log(
            "trigger_fired",
            {"agent_name": agent.name, "triggers": [{"name": t.name, "type": t.type} for t in triggers]},
            agent_id=agent_id,
        )

        if execution_claims:
            await _stop_lease_renewal()
            completed = await mark_trigger_executions_completed(execution_claims)
            if completed != len(execution_claims):
                raise RuntimeError("Trigger execution lease was lost before completion")
    except asyncio.CancelledError:
        if execution_claims:
            try:
                await _stop_lease_renewal()
                await mark_trigger_executions_failed(
                    execution_claims,
                    "Trigger invocation cancelled or lease fence lost",
                )
            except Exception as mark_error:
                logger.error(
                    "Failed to fence cancelled trigger executions error_type={}",
                    type(mark_error).__name__,
                )
        raise
    except Exception as e:
        logger.error(
            "Failed to invoke agent {} for triggers error_type={}",
            agent_id,
            type(e).__name__,
        )
        if execution_claims:
            try:
                await _stop_lease_renewal()
                await mark_trigger_executions_failed(execution_claims, str(e)[:2000])
            except Exception as mark_error:
                logger.error(
                    "Failed to mark trigger executions failed error_type={}",
                    type(mark_error).__name__,
                )
        await _capture_invocation_failure(agent_id, e)
    finally:
        await _stop_lease_renewal()
