"""Canonical authorization for user-owned Agent chat sessions."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.core.permissions import get_agent_access_level_for_user_id, is_agent_expired


CHAT_SESSION_AUDIT_ROLES = frozenset(
    {"platform_admin", "org_admin", "agent_admin"}
)


class ChatSessionAuthorizationError(PermissionError):
    """An exact user-owned conversation lane is no longer authorized."""


@dataclass(frozen=True)
class AuthorizedUserChatLane:
    session: ChatSession
    agent: Agent
    owner: User


def build_user_tool_authorization_context(
    *,
    agent_id: object,
    owner_user_id: object,
    session_id: object,
):
    """Return short pre/post exact-lane fences for one tool call.

    No pooled database connection or row lock is held while a provider/tool is
    running.  The preflight is the authorization linearization point; the
    postflight prevents a result from being accepted after revocation.
    """

    async def _validate() -> None:
        from app.database import async_session

        async with async_session() as db:
            try:
                await validate_active_user_chat_lane(
                    db,
                    agent_id=agent_id,
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    lock_authority=True,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def _authorize(_tool_name: str, _args: dict):
        await _validate()
        yield
        await _validate()

    return _authorize


def _as_uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ChatSessionAuthorizationError(f"Invalid {field}") from exc


async def validate_active_user_chat_lane(
    db: AsyncSession,
    *,
    agent_id: object,
    owner_user_id: object,
    session_id: object,
    lock_authority: bool = False,
) -> AuthorizedUserChatLane:
    """Validate an exact non-A2A user conversation at the side-effect fence.

    When ``lock_authority`` is true, the returned lane remains protected until
    the caller commits or rolls back. Agent ACL replacement, account/company
    deactivation and chat-session deletion therefore serialize behind this
    short transaction instead of racing the final write or external action.
    """

    parsed_agent_id = _as_uuid(agent_id, "Agent")
    parsed_owner_id = _as_uuid(owner_user_id, "owner")
    parsed_session_id = _as_uuid(session_id, "chat session")

    agent_query = select(Agent).where(Agent.id == parsed_agent_id)
    owner_query = select(User).where(User.id == parsed_owner_id)
    session_query = select(ChatSession).where(ChatSession.id == parsed_session_id)
    if lock_authority:
        agent_query = agent_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
        owner_query = owner_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
        session_query = session_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)

    agent = (await db.execute(agent_query)).scalar_one_or_none()
    owner = (await db.execute(owner_query)).scalar_one_or_none()
    if agent is None or owner is None or not owner.is_active:
        raise ChatSessionAuthorizationError("Chat principal is unavailable")
    if agent.tenant_id is None or owner.tenant_id != agent.tenant_id:
        raise ChatSessionAuthorizationError("Chat principal tenant is invalid")
    if getattr(agent, "status", None) in {"stopped", "paused", "error"}:
        raise ChatSessionAuthorizationError("Agent is unavailable")
    if is_agent_expired(agent):
        raise ChatSessionAuthorizationError("Agent is expired")

    tenant_query = select(Tenant).where(Tenant.id == agent.tenant_id)
    if lock_authority:
        tenant_query = tenant_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
    tenant = (await db.execute(tenant_query)).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise ChatSessionAuthorizationError("Company is inactive")

    session = (await db.execute(session_query)).scalar_one_or_none()
    if (
        session is None
        or session.agent_id != agent.id
        or session.user_id != owner.id
        or session.source_channel in {"agent", "trigger"}
        or bool(getattr(session, "is_group", False))
    ):
        raise ChatSessionAuthorizationError("Chat session principal is not authorized")
    if not await get_agent_access_level_for_user_id(db, owner.id, agent):
        raise ChatSessionAuthorizationError("Chat owner lost Agent access")

    return AuthorizedUserChatLane(session=session, agent=agent, owner=owner)


def can_audit_agent_chat_sessions(user: User) -> bool:
    """Return whether a user may cross another session owner's boundary."""

    return user.role in CHAT_SESSION_AUDIT_ROLES


async def require_authorized_agent_chat_session(
    db: AsyncSession,
    *,
    user: User,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> ChatSession:
    """Load one session only inside the requested Agent and owner boundary.

    Callers must first run the canonical Agent-access check.  This helper adds
    the narrower conversation boundary: the requested Agent must participate,
    and only the session owner or an explicit chat auditor may read or mutate
    it.  A UUID from an unrelated Agent therefore remains indistinguishable
    from a missing session.
    """

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            or_(
                ChatSession.agent_id == agent_id,
                ChatSession.peer_agent_id == agent_id,
            ),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    requires_auditor = bool(getattr(session, "is_group", False)) or (
        session.source_channel == "trigger"
    )
    if requires_auditor and not can_audit_agent_chat_sessions(user):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to view this managed session",
        )
    if str(session.user_id) != str(user.id) and not can_audit_agent_chat_sessions(
        user
    ):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to view this session",
        )
    return session
