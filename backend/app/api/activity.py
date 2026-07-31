"""Activity log API — view agent work history."""

import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.core.permissions import check_agent_access
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.user import User
from app.services.chat_session_access import (
    can_audit_agent_chat_sessions,
    require_authorized_agent_chat_session,
)

router = APIRouter(tags=["activity"])


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: uuid.UUID,
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recent activity logs for an agent."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    can_audit_all = can_audit_agent_chat_sessions(
        current_user,
        agent=agent,
        agent_access_level=access_level,
    )
    safe_member_summaries = {
        "heartbeat": "Heartbeat completed",
        "oneshot_task": "One-time task completed",
        "schedule_run": "Scheduled work completed",
        "task_updated": "Task updated",
        "tool_call": "Tool executed",
        "file_written": "Generated file",
    }

    query = select(AgentActivityLog).where(AgentActivityLog.agent_id == agent_id)
    if not can_audit_all:
        # Activity rows have no durable owner attribution yet.  Preserve the
        # useful status feed for ordinary Agent users, but fail closed for
        # conversation/file-transfer records and strip all unstructured detail.
        query = query.where(
            AgentActivityLog.action_type.in_(safe_member_summaries)
        )
    result = await db.execute(
        query
        .order_by(AgentActivityLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    return [
        {
            "id": str(log.id),
            "action_type": log.action_type,
            "summary": (
                log.summary
                if can_audit_all
                else safe_member_summaries[log.action_type]
            ),
            "detail": log.detail_json if can_audit_all else None,
            "related_id": str(log.related_id) if log.related_id else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


# ─── Chat History (per-agent) ─────────────────────────────────

@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(
    agent_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List a bounded authorized conversation inventory.

    Message bodies stay in PostgreSQL: the two window queries below return at
    most one preview row per selected conversation.  This avoids loading an
    Agent's complete chat history merely to render the activity sidebar.
    """
    agent, access_level = await check_agent_access(db, current_user, agent_id)

    from app.models.audit import ChatMessage
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession

    can_audit_all = can_audit_agent_chat_sessions(
        current_user,
        agent=agent,
        agent_access_level=access_level,
    )
    session_query = select(ChatSession).where(
        or_(
            ChatSession.agent_id == agent_id,
            (
                (ChatSession.peer_agent_id == agent_id)
                & (ChatSession.source_channel == "agent")
            ),
        )
    )
    if not can_audit_all:
        session_query = session_query.where(
            ChatSession.user_id == current_user.id,
            ChatSession.is_group.is_(False),
            ChatSession.source_channel != "trigger",
        )
    session_query = session_query.order_by(
        func.coalesce(ChatSession.last_message_at, ChatSession.created_at).desc(),
        ChatSession.id.desc(),
    ).limit(limit)
    sessions = list((await db.execute(session_query)).scalars().all())
    session_ids = [str(session.id) for session in sessions]

    stats: dict[str, tuple[int, object]] = {}
    last_content: dict[str, str] = {}
    if session_ids:
        stat_rows = (
            await db.execute(
                select(
                    ChatMessage.conversation_id,
                    func.count(ChatMessage.id),
                    func.max(ChatMessage.created_at),
                )
                .where(ChatMessage.conversation_id.in_(session_ids))
                .group_by(ChatMessage.conversation_id)
            )
        ).all()
        stats = {
            str(conversation_id): (int(count or 0), last_at)
            for conversation_id, count, last_at in stat_rows
        }
        ranked_messages = (
            select(
                ChatMessage.conversation_id.label("conversation_id"),
                ChatMessage.content.label("content"),
                func.row_number()
                .over(
                    partition_by=ChatMessage.conversation_id,
                    order_by=(
                        ChatMessage.created_at.desc(),
                        ChatMessage.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(ChatMessage.conversation_id.in_(session_ids))
            .subquery()
        )
        latest_rows = (
            await db.execute(
                select(
                    ranked_messages.c.conversation_id,
                    ranked_messages.c.content,
                )
                .where(ranked_messages.c.row_number == 1)
            )
        ).all()
        last_content = {
            str(conversation_id): content or ""
            for conversation_id, content in latest_rows
        }

    user_ids = {session.user_id for session in sessions if session.user_id}
    user_names = {}
    if user_ids:
        user_names = {
            user_id: display_name or "未知用户"
            for user_id, display_name in (
                await db.execute(
                    select(User.id, User.display_name).where(User.id.in_(user_ids))
                )
            ).all()
        }
    related_agent_ids = {
        candidate
        for session in sessions
        for candidate in (session.agent_id, session.peer_agent_id)
        if candidate is not None
    }
    agent_names = {}
    if related_agent_ids:
        agent_names = {
            related_id: name or "未知数字员工"
            for related_id, name in (
                await db.execute(
                    select(Agent.id, Agent.name).where(Agent.id.in_(related_agent_ids))
                )
            ).all()
        }

    channel_labels = {
        "web": ("👤", "Web"),
        "feishu": ("📱", "Feishu"),
        "wecom": ("💬", "WeCom"),
        "wechat": ("💬", "WeChat"),
        "dingtalk": ("💬", "DingTalk"),
        "slack": ("💬", "Slack"),
        "discord": ("🎮", "Discord"),
        "whatsapp": ("💬", "WhatsApp"),
        "teams": ("💬", "Teams"),
        "trigger": ("⚙️", "Internal trigger"),
    }
    conversations = []
    for session in sessions:
        conv_id = str(session.id)
        count, last_at = stats.get(conv_id, (0, None))
        if count == 0:
            continue
        if session.source_channel == "agent" and session.peer_agent_id:
            partner_id = (
                session.peer_agent_id
                if session.agent_id == agent_id
                else session.agent_id
            )
            partner_type = "agent"
            partner_name = f"🤖 {agent_names.get(partner_id, '未知数字员工')}"
            public_partner_id = str(partner_id)
        elif session.is_group:
            icon, label = channel_labels.get(
                session.source_channel,
                ("👥", session.source_channel.title()),
            )
            partner_type = session.source_channel
            partner_name = f"{icon} {session.group_name or session.title or label}"
            public_partner_id = session.external_conv_id or conv_id
        else:
            icon, label = channel_labels.get(
                session.source_channel,
                ("💬", session.source_channel.title()),
            )
            owner_name = user_names.get(session.user_id, "未知用户")
            partner_type = (
                "user" if session.source_channel == "web" else session.source_channel
            )
            partner_name = (
                f"{icon} {owner_name}"
                if session.source_channel == "web"
                else f"{icon} {session.title or label}"
            )
            public_partner_id = session.external_conv_id or str(session.user_id)
        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": partner_type,
                "partner_id": public_partner_id,
                "partner_name": partner_name,
                "last_message": last_content.get(conv_id, "")[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    # Isolated compatibility for legacy non-UUID Web conversations. Other
    # connector prefixes used creator placeholders for groups and cannot be
    # safely exposed to ordinary users without a durable ChatSession owner.
    legacy_filters = [ChatMessage.conversation_id.like("web_%")]
    if can_audit_all:
        legacy_filters.extend(
            ChatMessage.conversation_id.like(f"{prefix}_%")
            for prefix in (
                "feishu",
                "wecom",
                "wechat",
                "dingtalk",
                "slack",
                "discord",
                "whatsapp",
                "teams",
                "agent",
            )
        )
    remaining = max(0, limit - len(conversations))
    legacy_predicates = [
        ChatMessage.agent_id == agent_id,
        or_(*legacy_filters),
    ]
    if not can_audit_all:
        legacy_predicates.append(ChatMessage.user_id == current_user.id)
    legacy_rows = []
    if remaining:
        legacy_stats = (
            select(
                ChatMessage.conversation_id.label("conversation_id"),
                func.count(ChatMessage.id).label("message_count"),
                func.max(ChatMessage.created_at).label("last_at"),
            )
            .where(*legacy_predicates)
            .group_by(ChatMessage.conversation_id)
            .order_by(
                func.max(ChatMessage.created_at).desc(),
                ChatMessage.conversation_id,
            )
            .limit(remaining)
            .subquery()
        )
        legacy_ranked = (
            select(
                ChatMessage.conversation_id.label("conversation_id"),
                ChatMessage.content.label("content"),
                func.row_number()
                .over(
                    partition_by=ChatMessage.conversation_id,
                    order_by=(
                        ChatMessage.created_at.desc(),
                        ChatMessage.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .where(*legacy_predicates)
            .subquery()
        )
        legacy_rows = (
            await db.execute(
                select(
                    legacy_stats.c.conversation_id,
                    legacy_stats.c.message_count,
                    legacy_stats.c.last_at,
                    legacy_ranked.c.content,
                ).join(
                    legacy_ranked,
                    and_(
                        legacy_ranked.c.conversation_id
                        == legacy_stats.c.conversation_id,
                        legacy_ranked.c.row_number == 1,
                    ),
                )
            )
        ).all()
    known_ids = {conversation["conv_id"] for conversation in conversations}
    for conv_id, count, last_at, latest in legacy_rows:
        if conv_id in known_ids:
            continue
        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": "legacy",
                "partner_id": conv_id,
                "partner_name": "🗃️ Legacy conversation",
                "last_message": (latest or "")[:80],
                "message_count": int(count or 0),
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    # Sort by last_at desc
    conversations.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return conversations


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a specific conversation."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)

    messages = []
    can_audit_all = can_audit_agent_chat_sessions(
        current_user,
        agent=agent,
        agent_access_level=access_level,
    )

    legacy_prefixes = (
        "web_",
        "feishu_",
        "wecom_",
        "wechat_",
        "dingtalk_",
        "slack_",
        "discord_",
        "whatsapp_",
        "teams_",
    )
    if conv_id.startswith(legacy_prefixes):
        if not can_audit_all and not conv_id.startswith("web_"):
            raise HTTPException(status_code=404, detail="Conversation not found")
        from app.models.audit import ChatMessage
        owner_filters = (
            ()
            if can_audit_all
            else (ChatMessage.user_id == current_user.id,)
        )
        result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conv_id,
                *owner_filters,
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        for m in result.scalars().all():
            content = m.content
            # Strip [发送者: xxx] prefix for display (identity shown in UI)
            if content.startswith("[发送者:"):
                import re
                content = re.sub(r'^\[发送者:[^\]]*\]\s*', '', content)
            messages.append({
                "id": str(m.id),
                "role": m.role,
                "content": content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })
    elif len(conv_id) == 36:
        # Every current Web/channel/A2A conversation uses an authorized
        # ChatSession UUID. Never treat a caller-supplied UUID as a capability.
        from app.models.audit import ChatMessage
        from app.models.participant import Participant

        try:
            session_id = uuid.UUID(conv_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        await require_authorized_agent_chat_session(
            db,
            user=current_user,
            agent_id=agent_id,
            session_id=session_id,
        )

        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        name_cache = {}
        for m in result.scalars().all():
            # Determine sender name from participant_id
            sender_name = "未知"
            if m.participant_id:
                pid_str = str(m.participant_id)
                if pid_str not in name_cache:
                    p_r = await db.execute(select(Participant.display_name).where(Participant.id == m.participant_id))
                    name_cache[pid_str] = p_r.scalar_one_or_none() or "未知"
                sender_name = name_cache[pid_str]
            messages.append({
                "id": str(m.id),
                "role": m.role,
                "sender_name": sender_name,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })

    elif conv_id.startswith("agent_"):
        # Legacy non-UUID A2A identifiers have no ChatSession authorization
        # row. Keep compatibility only inside the exact Agent and message-owner
        # boundary; explicit auditors retain the historical all-session view.
        from app.models.audit import ChatMessage

        owner_filters = (
            ()
            if can_audit_all
            else (ChatMessage.user_id == current_user.id,)
        )
        result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conv_id,
                *owner_filters,
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        for message in result.scalars().all():
            messages.append({
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "created_at": (
                    message.created_at.isoformat()
                    if message.created_at
                    else None
                ),
            })

    else:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return messages
