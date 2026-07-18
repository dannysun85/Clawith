"""Canonical, revocation-aware authorization for private A2A lanes."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    evaluate_agent_relationship_status,
    get_agent_access_level_for_user_id,
    is_agent_expired,
)
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.org import AgentAgentRelationship
from app.models.tenant import Tenant
from app.models.user import User


class A2AAuthorizationError(PermissionError):
    """The durable A2A principal is missing, ambiguous, or no longer active."""


@dataclass(frozen=True)
class AuthorizedA2ALane:
    session: ChatSession
    source_agent: Agent
    target_agent: Agent
    owner_user_id: uuid.UUID
    relationship: AgentAgentRelationship


async def ensure_private_a2a_session(
    db: AsyncSession,
    *,
    source_agent: Agent,
    target_agent: Agent,
    owner_user_id: object,
    participant_id: uuid.UUID | None = None,
) -> ChatSession:
    """Find or create the one private A2A lane without locking Agent rows.

    Tool execution may already hold ``FOR SHARE`` authority fences on both
    Agents.  Taking a nested ``FOR UPDATE`` Agent lock here self-deadlocks on
    the same request.  A pair/owner advisory transaction lock serializes the
    absent-row case instead, while the partial unique index remains the final
    database invariant.  The savepoint fallback also converts a residual
    unique race into a deterministic reselect.
    """

    owner_id = _uuid(owner_user_id, "owner")
    source_id = _uuid(source_agent.id, "source Agent")
    target_id = _uuid(target_agent.id, "target Agent")
    if source_id == target_id:
        raise A2AAuthorizationError("A2A source and target must differ")
    tenant_id = source_agent.tenant_id
    if tenant_id is None or tenant_id != target_agent.tenant_id:
        raise A2AAuthorizationError("A2A Agents are not in one tenant")
    session_agent_id = min(source_id, target_id, key=str)
    session_peer_id = max(source_id, target_id, key=str)
    query = select(ChatSession).where(
        ChatSession.tenant_id == tenant_id,
        ChatSession.session_type == "a2a",
        ChatSession.agent_id == session_agent_id,
        ChatSession.peer_agent_id == session_peer_id,
        ChatSession.user_id == owner_id,
        ChatSession.source_channel == "agent",
        ChatSession.deleted_at.is_(None),
    )

    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        lock_key = f"clawith:a2a-session:v1:{session_agent_id}:{session_peer_id}:{owner_id}"
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        )

    session = (await db.execute(query)).scalar_one_or_none()
    if session is not None:
        return session

    candidate = ChatSession(
        tenant_id=tenant_id,
        session_type="a2a",
        group_id=None,
        agent_id=session_agent_id,
        peer_agent_id=session_peer_id,
        user_id=owner_id,
        created_by_participant_id=participant_id,
        title=f"{source_agent.name} ↔ {target_agent.name}",
        source_channel="agent",
        is_group=False,
        participant_id=participant_id,
        is_primary=False,
        deleted_at=None,
    )
    try:
        async with db.begin_nested():
            db.add(candidate)
            await db.flush()
    except IntegrityError:
        session = (await db.execute(query)).scalar_one_or_none()
        if session is None:
            raise
        return session
    return candidate


def build_a2a_tool_authorization_context(
    *,
    source_agent_id: object,
    target_agent_id: object,
    owner_user_id: object,
    session_id: object,
):
    """Return short pre/post authority fences for one exact A2A tool call."""

    async def _validate() -> None:
        from app.database import async_session

        async with async_session() as db:
            try:
                await validate_active_a2a_lane(
                    db,
                    source_agent_id=source_agent_id,
                    target_agent_id=target_agent_id,
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    lock_relationship=True,
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


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise A2AAuthorizationError(f"Invalid {field}") from exc


async def validate_active_a2a_lane(
    db: AsyncSession,
    *,
    source_agent_id: object,
    target_agent_id: object,
    owner_user_id: object,
    session_id: object,
    lock_relationship: bool = False,
) -> AuthorizedA2ALane:
    """Validate one exact directed relationship and canonical owner lane.

    The caller must invoke this immediately before every queued or external
    side effect and again before final persistence.  It deliberately rejects
    duplicate relationship rows instead of selecting an arbitrary one.
    """

    source_id = _uuid(source_agent_id, "source Agent")
    target_id = _uuid(target_agent_id, "target Agent")
    owner_id = _uuid(owner_user_id, "owner")
    parsed_session_id = _uuid(session_id, "A2A session")
    if source_id == target_id:
        raise A2AAuthorizationError("A2A source and target must differ")

    if lock_relationship:
        # User/Agent rows are the authority fence used by access mutations.
        # Every permission replacement explicitly locks its Agent row FOR UPDATE,
        # so these ordered FOR SHARE locks linearize final delivery against both
        # account and ACL revocation, including same-mode custom-list changes.
        agent_result = await db.execute(
            select(Agent)
            .where(Agent.id.in_([source_id, target_id]))
            .order_by(Agent.id)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        agents = {agent.id: agent for agent in agent_result.scalars().all()}
        source_agent = agents.get(source_id)
        target_agent = agents.get(target_id)
        owner_result = await db.execute(
            select(User).where(User.id == owner_id).execution_options(populate_existing=True).with_for_update(read=True)
        )
        if owner_result.scalar_one_or_none() is None:
            raise A2AAuthorizationError("A2A owner is unavailable")
    else:
        source_agent = await db.get(Agent, source_id)
        target_agent = await db.get(Agent, target_id)
    if (
        source_agent is None
        or target_agent is None
        or source_agent.tenant_id != target_agent.tenant_id
        or source_agent.tenant_id is None
    ):
        raise A2AAuthorizationError("A2A Agents are not in one tenant")
    if any(
        getattr(agent, "status", None) in {"stopped", "paused", "error"} or is_agent_expired(agent)
        for agent in (source_agent, target_agent)
    ):
        raise A2AAuthorizationError("A2A Agent is unavailable")
    tenant_query = select(Tenant).where(Tenant.id == source_agent.tenant_id)
    if lock_relationship:
        tenant_query = tenant_query.execution_options(populate_existing=True).with_for_update(read=True)
    tenant = (await db.execute(tenant_query)).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise A2AAuthorizationError("A2A company is inactive")

    session_query = select(ChatSession).where(ChatSession.id == parsed_session_id)
    if lock_relationship:
        session_query = session_query.execution_options(populate_existing=True).with_for_update(read=True)
    session = (await db.execute(session_query)).scalar_one_or_none()
    canonical_agent_id = min(source_id, target_id, key=str)
    canonical_peer_id = max(source_id, target_id, key=str)
    if (
        session is None
        or session.tenant_id != source_agent.tenant_id
        or session.session_type != "a2a"
        or session.source_channel != "agent"
        or session.user_id != owner_id
        or session.agent_id != canonical_agent_id
        or session.peer_agent_id != canonical_peer_id
    ):
        raise A2AAuthorizationError("A2A session principal is not authorized")

    if not await get_agent_access_level_for_user_id(db, owner_id, source_agent):
        raise A2AAuthorizationError("A2A owner lost source Agent access")
    if not await get_agent_access_level_for_user_id(db, owner_id, target_agent):
        raise A2AAuthorizationError("A2A owner lost target Agent access")

    relationship_query = select(AgentAgentRelationship).where(
        AgentAgentRelationship.agent_id == source_id,
        AgentAgentRelationship.target_agent_id == target_id,
    )
    if lock_relationship:
        relationship_query = relationship_query.execution_options(populate_existing=True).with_for_update(read=True)
    relationship_result = await db.execute(relationship_query)
    relationships = list(relationship_result.scalars().all())
    if len(relationships) != 1:
        raise A2AAuthorizationError("A2A relationship is missing or ambiguous")
    relationship = relationships[0]
    relationship_status = await evaluate_agent_relationship_status(
        db,
        relationship,
        current_user_id=owner_id,
    )
    if relationship_status.get("access_status") != "active":
        raise A2AAuthorizationError("A2A relationship is no longer active")

    return AuthorizedA2ALane(
        session=session,
        source_agent=source_agent,
        target_agent=target_agent,
        owner_user_id=owner_id,
        relationship=relationship,
    )
