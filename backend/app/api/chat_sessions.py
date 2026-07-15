"""Chat session management API endpoints."""

import uuid
import re
from datetime import datetime, timezone as tz
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import cast, select, func, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_plan_selection import (
    InvalidAgentPlanSelection,
    resolve_agent_plan_selection,
)
from app.services.entitlements import get_tenant_entitlements

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])

# Session counters represent user-visible conversation turns. Internal system
# events and tool execution records must not make the counter climb while an
# agent is working in the background.
VISIBLE_MESSAGE_ROLES = ("user", "assistant")


def _can_view_all_agent_chat_sessions(user: User) -> bool:
    """Return whether a user may audit other users' chat sessions.

    Agent ownership grants management of the shared Agent configuration, not
    ownership of every user's private conversation with that Agent.  Only
    explicit administrative roles may cross the session-owner boundary.
    """
    return user.role in ("platform_admin", "org_admin", "agent_admin")


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    user_id: str
    username: Optional[str] = None      # display_name ?? username
    source_channel: str = "web"         # web / feishu / discord / slack / agent
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    unread_count: int = 0
    is_primary: bool = False
    model_tier: Optional[str] = None
    model_modality: Optional[str] = None
    # Agent-to-agent session fields
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type: str = "user"       # 'user' | 'agent'
    # Group chat session fields
    is_group: bool = False
    group_name: Optional[str] = None

class CreateSessionIn(BaseModel):
    title: Optional[str] = None
    model_tier: Optional[str] = None
    model_modality: Optional[str] = None


class PatchSessionIn(BaseModel):
    title: Optional[str] = None
    model_tier: Optional[str] = None
    model_modality: Optional[str] = None
    preference_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_update(self):
        if not self.model_fields_set:
            raise ValueError("At least one session field must be provided")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("Session title cannot be null")
        if "preference_revision" in self.model_fields_set and "model_tier" not in self.model_fields_set:
            raise ValueError("preference_revision requires model_tier")
        return self


async def _resolve_session_model_selection(
    agent: Agent,
    requested_tier: str | None,
    requested_modality: str | None,
    *,
    strict: bool,
) -> tuple[str, str]:
    tenant_id = getattr(agent, "tenant_id", None)
    entitlements = await get_tenant_entitlements(tenant_id) if tenant_id else None
    return resolve_agent_plan_selection(
        entitlements,
        requested_tier,
        requested_modality,
        strict=strict,
    )


async def _lock_user_chat_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> User | None:
    """Lock and refresh the tenant User used by chat-tier compare-and-set.

    Authentication loads the same User into this session's identity map before
    this endpoint runs. ``populate_existing`` is therefore required: a second
    request may wait on the row lock after another transaction has incremented
    the revision, and CAS must compare against that newly committed value.
    """

    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for an agent. scope=all for org/platform admins and agent_admin."""
    # Verify agent exists
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await check_agent_access(db, current_user, agent_id)

    if scope == "all":
        if not _can_view_all_agent_chat_sessions(current_user):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        # Fetch all sessions (including agent-to-agent where this agent is peer)
        result = await db.execute(
            select(ChatSession)
            .where(
                (ChatSession.agent_id == agent_id)
                | ((ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent"))
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        out = []

        # --- BULK FETCH: message counts, user names, agent names in 3 queries total ---
        session_ids = [str(s.id) for s in sessions]
        session_uuid_ids = [s.id for s in sessions]

        message_counts: dict[str, int] = {}
        unread_counts: dict[str, int] = {}
        if session_ids:
            count_res = await db.execute(
                select(ChatMessage.conversation_id, func.count(ChatMessage.id))
                .where(
                    ChatMessage.conversation_id.in_(session_ids),
                    ChatMessage.role.in_(VISIBLE_MESSAGE_ROLES),
                )
                .group_by(ChatMessage.conversation_id)
            )
            for row in count_res.all():
                message_counts[row[0]] = row[1]

            own_session_ids = [s.id for s in sessions if str(s.user_id) == str(current_user.id)]
            if own_session_ids:
                unread_res = await db.execute(
                    select(ChatSession.id, func.count(ChatMessage.id))
                    .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
                    .where(
                        ChatSession.id.in_(own_session_ids),
                        ChatSession.source_channel.notin_(["agent", "trigger"]),
                        ChatSession.is_group.is_(False),
                        ChatMessage.role == "assistant",
                        ChatMessage.created_at > func.coalesce(
                            ChatSession.last_read_at_by_user,
                            datetime(1970, 1, 1, tzinfo=tz.utc),
                        ),
                    )
                    .group_by(ChatSession.id)
                )
                for row in unread_res.all():
                    unread_counts[str(row[0])] = int(row[1] or 0)

        # Collect IDs to resolve in bulk
        from app.models.user import Identity
        user_ids = list({s.user_id for s in sessions
                         if not s.is_group and s.source_channel != "agent" and s.user_id})
        user_names: dict[str, str] = {}
        if user_ids:
            user_r = await db.execute(
                select(User.id, func.coalesce(User.display_name, Identity.username))
                .join(Identity, User.identity_id == Identity.id)
                .where(User.id.in_(user_ids))
            )
            for row in user_r.all():
                user_names[str(row[0])] = row[1] or "Unknown"

        agent_ids_to_fetch: set = set()
        for s in sessions:
            if s.source_channel == "agent" and s.peer_agent_id:
                agent_ids_to_fetch.add(s.agent_id)
                agent_ids_to_fetch.add(s.peer_agent_id)
        agent_names: dict[str, str] = {}
        if agent_ids_to_fetch:
            agent_r = await db.execute(
                select(Agent.id, Agent.name).where(Agent.id.in_(list(agent_ids_to_fetch)))
            )
            for row in agent_r.all():
                agent_names[str(row[0])] = row[1] or "Agent"

        for session in sessions:
            count = message_counts.get(str(session.id), 0)
            if count == 0:
                continue  # hide empty sessions

            display = None
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"

            if session.source_channel == "agent" and session.peer_agent_id:
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                a1_name = agent_names.get(str(session.agent_id), "Agent")
                a2_name = agent_names.get(str(session.peer_agent_id), "Agent")
                peer_agent_name = a2_name
                display = f"Agent {a1_name} - {a2_name}"
            elif session.is_group:
                display = session.group_name or session.title or "Group Chat"
            else:
                display = user_names.get(str(session.user_id), "Unknown")

            out.append(SessionOut(
                id=str(session.id),
                agent_id=str(session.agent_id),
                user_id=str(session.user_id),
                username=display,
                source_channel=session.source_channel,
                title=session.title,
                created_at=session.created_at.isoformat(),
                last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                is_primary=bool(getattr(session, "is_primary", False)),
                model_tier=getattr(session, "model_tier", None),
                model_modality=getattr(session, "model_modality", None),
                peer_agent_id=peer_agent_id,
                peer_agent_name=peer_agent_name,
                participant_type="group" if session.is_group else participant_type,
                is_group=session.is_group,
                group_name=session.group_name,
            ))
        return out

    else:  # scope == "mine"
        result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.user_id == current_user.id,
                ChatSession.is_group.is_(False),  # Group sessions are not "mine"
                ChatSession.source_channel.notin_(["agent", "trigger"]),  # Exclude agent-to-agent and reflection sessions
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        out = []

        # --- BULK FETCH: count total messages and unread messages in two compact queries ---
        session_ids = [str(s.id) for s in sessions]
        session_uuid_ids = [s.id for s in sessions]

        total_counts: dict[str, int] = {}
        unread_counts: dict[str, int] = {}
        if session_ids:
            counts_res = await db.execute(
                select(
                    ChatMessage.conversation_id,
                    func.count(ChatMessage.id)
                ).where(
                    ChatMessage.conversation_id.in_(session_ids),
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.role.in_(VISIBLE_MESSAGE_ROLES),
                ).group_by(ChatMessage.conversation_id)
            )
            for row in counts_res.all():
                total_counts[row[0]] = int(row[1] or 0)

            unread_res = await db.execute(
                select(ChatSession.id, func.count(ChatMessage.id))
                .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
                .where(
                    ChatSession.id.in_(session_uuid_ids),
                    ChatMessage.role == "assistant",
                    ChatMessage.created_at > func.coalesce(
                        ChatSession.last_read_at_by_user,
                        datetime(1970, 1, 1, tzinfo=tz.utc),
                    ),
                )
                .group_by(ChatSession.id)
            )
            for row in unread_res.all():
                unread_counts[str(row[0])] = int(row[1] or 0)

        for session in sessions:
            # Hide truly empty / orphan sessions. Onboarding sessions have zero
            # user messages (the agent greets first) but do have assistant
            # turns, so count ALL messages here — not just user ones.
            count = total_counts.get(str(session.id), 0)
            if count == 0:
                continue
            out.append(SessionOut(
                id=str(session.id),
                agent_id=str(session.agent_id),
                user_id=str(session.user_id),
                source_channel=session.source_channel,
                title=session.title,
                created_at=session.created_at.isoformat(),
                last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
                model_tier=getattr(session, "model_tier", None),
                model_modality=getattr(session, "model_modality", None),
            ))
        return out


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID,
    body: CreateSessionIn = CreateSessionIn(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session for the current user."""
    agent, _ = await check_agent_access(db, current_user, agent_id)

    explicit_selection = bool({"model_tier", "model_modality"} & body.model_fields_set)
    # A user's latest explicit chat choice follows them across Agents. Agent
    # preferences remain the fallback and still drive background automation.
    default_tier = (
        getattr(current_user, "preferred_chat_tier", None)
        or getattr(agent, "preferred_tier", None)
    )
    default_modality = getattr(agent, "preferred_modality", None)
    model_tier: str | None = None
    model_modality: str | None = None
    if explicit_selection or default_tier:
        try:
            model_tier, model_modality = await _resolve_session_model_selection(
                agent,
                body.model_tier if body.model_tier is not None else default_tier,
                body.model_modality if body.model_modality is not None else default_modality,
                strict=explicit_selection,
            )
        except InvalidAgentPlanSelection as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    now = datetime.now(tz.utc)
    new_id = uuid.uuid4()
    session = ChatSession(
        id=new_id,
        agent_id=agent_id,
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        source_channel="web",
        is_primary=False,
        model_tier=model_tier,
        model_modality=model_modality,
        created_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=None,
        message_count=0,
        unread_count=0,
        is_primary=False,
        model_tier=getattr(session, "model_tier", None),
        model_modality=getattr(session, "model_modality", None),
        participant_type="user",
        is_group=False,
    )


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a session title or its first-party chat model selection."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    selection_fields = {"model_tier", "model_modality"} & body.model_fields_set
    if selection_fields:
        if str(session.user_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail="Only the session owner can change its model selection")
        if session.source_channel != "web" or bool(getattr(session, "is_group", False)):
            raise HTTPException(status_code=400, detail="Model selection is only available for first-party web chats")

        try:
            current_tier, current_modality = await _resolve_session_model_selection(
                agent,
                getattr(session, "model_tier", None) or getattr(agent, "preferred_tier", None),
                getattr(session, "model_modality", None) or getattr(agent, "preferred_modality", None),
                strict=False,
            )
            model_tier, model_modality = await _resolve_session_model_selection(
                agent,
                body.model_tier if "model_tier" in body.model_fields_set else current_tier,
                body.model_modality if "model_modality" in body.model_fields_set else current_modality,
                strict=True,
            )
        except InvalidAgentPlanSelection as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if "model_tier" in selection_fields:
            owner = await _lock_user_chat_preference(db, current_user.id)
            if owner is None:
                raise HTTPException(status_code=401, detail="User not found or inactive")
            current_revision = int(getattr(owner, "preferred_chat_tier_revision", 0) or 0)
            if (
                body.preference_revision is not None
                and body.preference_revision != current_revision
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "chat_tier_preference_conflict",
                        "message": "Model tier changed in another session; refresh and try again",
                        "preferred_chat_tier": getattr(owner, "preferred_chat_tier", None),
                        "preferred_chat_tier_revision": current_revision,
                    },
                )
            owner.preferred_chat_tier = model_tier
            owner.preferred_chat_tier_revision = current_revision + 1
            current_user.preferred_chat_tier = model_tier
            current_user.preferred_chat_tier_revision = current_revision + 1
        session.model_tier = model_tier
        session.model_modality = model_modality

    if "title" in body.model_fields_set:
        session.title = body.title
    await db.commit()
    response = {"id": str(session.id), "title": session.title}
    if selection_fields:
        response.update({
            "model_tier": session.model_tier,
            "model_modality": session.model_modality,
            "preferred_chat_tier": getattr(current_user, "preferred_chat_tier", None),
            "preferred_chat_tier_revision": int(
                getattr(current_user, "preferred_chat_tier_revision", 0) or 0
            ),
        })
    return response


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a chat session and its messages as its owner or an administrator."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete associated messages first
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(ChatMessage).where(ChatMessage.conversation_id == str(session_id)))
    await db.delete(session)
    await db.commit()
    return None


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=500, description="Number of messages to return"),
    before: str = Query(None, description="Cursor: return messages created before this timestamp (ISO format)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get chat messages for a specific session."""
    if not isinstance(limit, int):
        limit = 20
    if not isinstance(before, str):
        before = None
    await check_agent_access(db, current_user, agent_id)
    # Allow looking up sessions where agent_id OR peer_agent_id matches
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Permission: session owner or an explicit administrator. Agent creators
    # do not implicitly own conversations started by other users.
    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    # Query messages by conversation_id only (agent-to-agent uses session_agent_id)
    # Optimized: use a single query with ORDER BY and LIMIT instead of subquery
    from sqlalchemy import desc
    query = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session_id))
        .order_by(desc(ChatMessage.created_at))
        .limit(limit)
    )
    # Apply cursor filter if `before` timestamp is provided
    if before:
        from datetime import datetime as dt
        try:
            before_dt = dt.fromisoformat(before.replace('Z', '+00:00'))
            query = query.where(ChatMessage.created_at < before_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid `before` timestamp format. Use ISO 8601.")
    msgs_result = await db.execute(query)
    messages = list(reversed(msgs_result.scalars().all()))

    # Reading your own first-party/channel session should clear its unread state.
    if str(session.user_id) == str(current_user.id) and not getattr(session, "is_group", False) and session.source_channel not in ("agent", "trigger"):
        session.last_read_at_by_user = datetime.now(tz.utc)
        await db.commit()

    # Batch fetch participant identity to avoid N+1 queries.  A2A alignment
    # must use the stable Agent id rather than a display-name comparison:
    # names are mutable and are not guaranteed to be unique.
    sender_cache: dict[str, tuple[str, str]] = {}
    if session.source_channel == "agent":
        from app.models.participant import Participant
        participant_ids = list({m.participant_id for m in messages if m.participant_id})
        if participant_ids:
            p_result = await db.execute(
                select(Participant.id, Participant.display_name, Participant.ref_id)
                .where(Participant.id.in_(participant_ids))
            )
            for row in p_result.all():
                sender_cache[str(row[0])] = (row[1] or "Unknown", str(row[2]))

    out = []
    for m in messages:
        sender_info = sender_cache.get(str(m.participant_id)) if m.participant_id else None
        sender_name = sender_info[0] if sender_info else None
        sender_agent_id = sender_info[1] if sender_info else None

        def add_sender_metadata(entry: dict) -> None:
            if sender_name:
                entry["sender_name"] = sender_name
            if m.participant_id:
                entry["participant_id"] = str(m.participant_id)
            if sender_agent_id:
                entry["sender_agent_id"] = sender_agent_id
                entry["is_current_agent"] = sender_agent_id == str(agent_id)

        if m.role == "tool_call":
            import json
            entry: dict = {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            try:
                data = json.loads(m.content)
                entry["content"] = ""
                entry["toolName"] = data.get("name") or data.get("tool_name") or ""
                entry["toolArgs"] = data.get("args") or data.get("arguments")
                entry["toolStatus"] = data.get("status", "done")
                entry["toolResult"] = data.get("result", "")
                entry["toolThinking"] = data.get("reasoning_content", "")
            except Exception:
                pass
            add_sender_metadata(entry)
            out.append(entry)
            continue

        # For agent sessions, parse inline tool_code blocks from assistant messages
        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = _split_inline_tools(m.content)
            for part in parts:
                add_sender_metadata(part)
                out.append(part)
        else:
            entry = {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            if hasattr(m, 'thinking') and m.thinking:
                entry["thinking"] = m.thinking
            add_sender_metadata(entry)
            out.append(entry)

    return out


def _split_inline_tools(content: str) -> list[dict]:
    """Parse assistant content containing inline ```tool_code blocks.

    Splits into alternating text segments and tool_call entries.
    Format: ```tool_code\ntool_name\n``` ```json\n{args}\n```
    """
    # Pattern: ```tool_code\n<name>\n``` optionally followed by ```json\n<args>\n```
    pattern = re.compile(
        r'```tool_code\s*\n\s*(\w+)\s*\n```'        # tool name
        r'(?:\s*```json\s*\n(.*?)\n```)?',            # optional JSON args
        re.DOTALL
    )

    parts: list[dict] = []
    last_end = 0

    for match in pattern.finditer(content):
        # Text before this tool call
        text_before = content[last_end:match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})

        tool_name = match.group(1)
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                import json
                tool_args = json.loads(args_str.strip())
            except Exception:
                tool_args = {"raw": args_str.strip()}

        parts.append({
            "role": "tool_call",
            "content": "",
            "toolName": tool_name,
            "toolArgs": tool_args,
            "toolStatus": "done",
            "toolResult": "",
        })
        last_end = match.end()

    # Trailing text after last tool
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})

    # If no matches found, return the whole content as-is
    if not parts:
        parts.append({"role": "assistant", "content": content})

    return parts
