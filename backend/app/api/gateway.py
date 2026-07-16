"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to poll for messages, report results, send messages, and send heartbeat pings.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.core.permissions import (
    evaluate_agent_relationship_status,
    evaluate_human_relationship_status,
    get_agent_access_level_for_user_id,
    is_agent_expired,
)
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.models.tenant import Tenant
from app.services.a2a_authorization import (
    A2AAuthorizationError,
    build_a2a_tool_authorization_context,
    ensure_private_a2a_session,
    validate_active_a2a_lane,
)
from app.services.chat_session_access import (
    ChatSessionAuthorizationError,
    validate_active_user_chat_lane,
)
from app.schemas.schemas import (
    GatewayPollResponse, GatewayMessageOut, GatewayReportRequest,
    GatewayHistoryItem, GatewayRelationshipItem, GatewaySendMessageRequest,
)

router = APIRouter(prefix="/gateway", tags=["gateway"])

GATEWAY_POLL_BATCH_LIMIT = 50
GATEWAY_DELIVERY_LEASE = timedelta(minutes=5)
GATEWAY_MAX_DELIVERY_ATTEMPTS = 20


def _gateway_message_is_claimable(
    message: GatewayMessage,
    now: datetime,
) -> bool:
    return message.status == "pending" or (
        message.status == "delivered"
        and (
            message.delivery_lease_expires_at is None
            or message.delivery_lease_expires_at <= now
        )
    )


async def _touch_gateway_agent(
    agent_id: uuid.UUID,
    *,
    allow_creating: bool = False,
) -> None:
    """Touch Gateway liveness in one independent, canonically ordered txn.

    Message delivery transactions never dirty an Agent row. Keeping this
    Agent UPDATE -> User SHARE -> Tenant SHARE transaction separate prevents
    reciprocal A2A poll/report requests from forming an Agent/GatewayMessage
    lock cycle.
    """

    async with async_session() as touch_db:
        try:
            result = await touch_db.execute(
                select(Agent)
                .where(Agent.id == agent_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            agent = result.scalar_one_or_none()
            allowed_statuses = {"running", "idle"}
            if allow_creating:
                allowed_statuses.add("creating")
            if (
                agent is None
                or agent.status not in allowed_statuses
                or is_agent_expired(agent)
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Gateway Agent is unavailable",
                )
            owner = (
                await touch_db.execute(
                    select(User)
                    .where(User.id == agent.creator_id)
                    .execution_options(populate_existing=True)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if (
                owner is None
                or not owner.is_active
                or owner.tenant_id != agent.tenant_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Gateway owner is unavailable",
                )
            tenant = (
                await touch_db.execute(
                    select(Tenant)
                    .where(Tenant.id == agent.tenant_id)
                    .execution_options(populate_existing=True)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if tenant is None or not tenant.is_active:
                raise HTTPException(
                    status_code=403,
                    detail="Gateway company is inactive",
                )
            if not await get_agent_access_level_for_user_id(
                touch_db,
                owner.id,
                agent,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Gateway owner lost Agent access",
                )
            agent.openclaw_last_seen = datetime.now(timezone.utc)
            if agent.status in {"creating", "idle"}:
                agent.status = "running"
            await touch_db.commit()
        except BaseException:
            await touch_db.rollback()
            raise


async def _lock_claimable_gateway_message(
    db: AsyncSession,
    *,
    message_id: uuid.UUID,
    agent_id: uuid.UUID,
    now: datetime,
    skip_locked: bool,
) -> GatewayMessage | None:
    query = (
        select(GatewayMessage)
        .where(
            GatewayMessage.id == message_id,
            GatewayMessage.agent_id == agent_id,
            or_(
                GatewayMessage.status == "pending",
                (
                    (GatewayMessage.status == "delivered")
                    & (
                        GatewayMessage.delivery_lease_expires_at.is_(None)
                        | (GatewayMessage.delivery_lease_expires_at <= now)
                    )
                ),
            ),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=skip_locked)
    )
    return (await db.execute(query)).scalar_one_or_none()


async def _authorize_gateway_message(
    db: AsyncSession,
    message: GatewayMessage,
    target_agent: Agent,
    *,
    lock_relationship: bool = False,
):
    """Validate a queued message against its durable current principal."""

    if not message.sender_user_id or not message.conversation_id:
        raise A2AAuthorizationError("Gateway message has no durable owner lane")
    if message.sender_agent_id:
        authorization_source = message.authorization_source_agent_id
        if authorization_source not in {message.sender_agent_id, target_agent.id}:
            raise A2AAuthorizationError("Gateway A2A authorization source is invalid")
        authorization_target = (
            target_agent.id
            if authorization_source == message.sender_agent_id
            else message.sender_agent_id
        )
        return await validate_active_a2a_lane(
            db,
            source_agent_id=authorization_source,
            target_agent_id=authorization_target,
            owner_user_id=message.sender_user_id,
            session_id=message.conversation_id,
            lock_relationship=lock_relationship,
        )

    try:
        lane = await validate_active_user_chat_lane(
            db,
            agent_id=target_agent.id,
            owner_user_id=message.sender_user_id,
            session_id=message.conversation_id,
            lock_authority=lock_relationship,
        )
    except ChatSessionAuthorizationError as exc:
        raise A2AAuthorizationError(
            "Gateway user conversation is no longer authorized"
        ) from exc
    return lane.session


def _quarantine_gateway_message(
    message: GatewayMessage,
    *,
    reason: str,
) -> None:
    message.status = "revoked"
    message.result = reason
    message.delivery_lease_expires_at = None
    message.completed_at = datetime.now(timezone.utc)


def _hash_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


async def _get_agent_by_key(api_key: str, db: AsyncSession) -> Agent:
    """Authenticate an OpenClaw agent by its API key."""
    # First try plaintext (new behavior)
    result = await db.execute(
        select(Agent).where(
            Agent.api_key_hash == api_key,
            Agent.agent_type == "openclaw",
        )
    )
    agent = result.scalar_one_or_none()

    # Fallback to hashed (legacy behavior)
    if not agent:
        key_hash = _hash_key(api_key)
        result = await db.execute(
            select(Agent).where(
                Agent.api_key_hash == key_hash,
                Agent.agent_type == "openclaw",
            )
        )
        agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if (
        getattr(agent, "status", None) in {"stopped", "paused", "error"}
        or is_agent_expired(agent)
    ):
        raise HTTPException(status_code=403, detail="Gateway Agent is unavailable")
    tenant = await db.get(Tenant, agent.tenant_id) if agent.tenant_id else None
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=403, detail="Gateway company is inactive")
    if not agent.creator_id or not await get_agent_access_level_for_user_id(
        db,
        agent.creator_id,
        agent,
    ):
        raise HTTPException(
            status_code=403,
            detail="Gateway owner is inactive or no longer authorized",
        )
    return agent


# ─── Poll for messages ──────────────────────────────────

@router.get("/poll", response_model=GatewayPollResponse)
async def poll_messages(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent polls for pending messages.

    Returns all pending messages and marks them as delivered.
    Also updates openclaw_last_seen for online status tracking.
    """
    logger.info("[Gateway] poll called")
    agent = await _get_agent_by_key(x_api_key, db)
    agent_id = agent.id
    await db.rollback()
    await _touch_gateway_agent(agent_id, allow_creating=True)
    agent = await _get_agent_by_key(x_api_key, db)

    # Claim one bounded at-least-once batch. A process crash after a successful
    # poll no longer strands rows in ``delivered`` forever: an expired lease is
    # reclaimable, and consumers already receive the stable message id needed
    # for idempotent processing.
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(GatewayMessage)
        .where(
            GatewayMessage.agent_id == agent.id,
            or_(
                GatewayMessage.status == "pending",
                (
                    (GatewayMessage.status == "delivered")
                    & (
                        GatewayMessage.delivery_lease_expires_at.is_(None)
                        | (GatewayMessage.delivery_lease_expires_at <= now)
                    )
                ),
            ),
        )
        .order_by(GatewayMessage.created_at.asc(), GatewayMessage.id.asc())
        .limit(GATEWAY_POLL_BATCH_LIMIT)
    )
    candidates = list(result.scalars().all())

    # Mark only currently authorized messages as delivered. Queued work does
    # not survive relationship/access revocation.
    out = []
    for candidate in candidates:
        attempts = candidate.delivery_attempts or 0
        if attempts >= GATEWAY_MAX_DELIVERY_ATTEMPTS:
            msg = await _lock_claimable_gateway_message(
                db,
                message_id=candidate.id,
                agent_id=agent.id,
                now=now,
                skip_locked=True,
            )
            if msg is None:
                continue
            _quarantine_gateway_message(
                msg,
                reason="Gateway delivery retry limit was reached",
            )
            logger.error(
                "[Gateway] Quarantined retry-exhausted message id={} attempts={}",
                msg.id,
                msg.delivery_attempts or 0,
            )
            continue
        try:
            await _authorize_gateway_message(
                db,
                candidate,
                agent,
                lock_relationship=True,
            )
        except A2AAuthorizationError:
            msg = await _lock_claimable_gateway_message(
                db,
                message_id=candidate.id,
                agent_id=agent.id,
                now=now,
                skip_locked=True,
            )
            if msg is None:
                continue
            _quarantine_gateway_message(
                msg,
                reason="Gateway delivery authorization was revoked",
            )
            logger.warning(
                "[Gateway] Quarantined unauthorized pending message id={}",
                msg.id,
            )
            continue
        msg = await _lock_claimable_gateway_message(
            db,
            message_id=candidate.id,
            agent_id=agent.id,
            now=now,
            skip_locked=True,
        )
        if msg is None or not _gateway_message_is_claimable(msg, now):
            continue
        attempts = msg.delivery_attempts or 0
        if attempts >= GATEWAY_MAX_DELIVERY_ATTEMPTS:
            _quarantine_gateway_message(
                msg,
                reason="Gateway delivery retry limit was reached",
            )
            continue
        msg.status = "delivered"
        msg.delivered_at = now
        msg.delivery_lease_expires_at = now + GATEWAY_DELIVERY_LEASE
        msg.delivery_attempts = attempts + 1

        # Resolve sender names
        sender_agent_name = None
        sender_user_name = None
        if msg.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == msg.sender_agent_id))
            sender_agent_name = r.scalar_one_or_none()
        if msg.sender_user_id:
            r = await db.execute(select(User.display_name).where(User.id == msg.sender_user_id))
            sender_user_name = r.scalar_one_or_none()

        # Fetch conversation history (last 10 messages) for context
        history = []
        if msg.conversation_id:
            from app.models.audit import ChatMessage
            history_query = select(ChatMessage).where(
                ChatMessage.conversation_id == msg.conversation_id
            )
            if msg.sender_user_id:
                history_query = history_query.where(
                    ChatMessage.user_id == msg.sender_user_id
                )
            hist_result = await db.execute(
                history_query
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))
            for h in hist_msgs:
                # Resolve sender name for each history message
                h_sender = None
                if h.role == "user" and h.user_id:
                    r = await db.execute(select(User.display_name).where(User.id == h.user_id))
                    h_sender = r.scalar_one_or_none()
                elif h.role == "assistant":
                    h_sender = agent.name
                history.append(GatewayHistoryItem(
                    role=h.role,
                    content=h.content or "",
                    sender_name=h_sender,
                    created_at=h.created_at,
                ))

        out.append(GatewayMessageOut(
            id=msg.id,
            conversation_id=msg.conversation_id,
            sender_agent_name=sender_agent_name,
            sender_user_name=sender_user_name,
            sender_user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
            content=msg.content,
            created_at=msg.created_at,
            delivery_attempt=msg.delivery_attempts,
            history=history,
        ))

    # Fetch agent relationships for context
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    rel_items = []

    # Human relationships (with available channels)
    h_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentRelationship.member))
    )
    for r in h_result.scalars().all():
        status_info = await evaluate_human_relationship_status(db, r, source_agent=agent)
        if r.member and status_info["access_status"] == "active":
            channels = []
            if getattr(r.member, 'external_id', None) or getattr(r.member, 'open_id', None):
                channels.append("feishu")
            if getattr(r.member, 'email', None):
                channels.append("email")
            rel_items.append(GatewayRelationshipItem(
                name=r.member.name,
                type="human",
                role=r.relation,
                description=r.description or None,
                channels=channels,
            ))

    # Agent-to-agent relationships
    a_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    for r in a_result.scalars().all():
        status_info = await evaluate_agent_relationship_status(
            db,
            r,
            current_user_id=agent.creator_id,
        )
        target_access = (
            await get_agent_access_level_for_user_id(
                db,
                agent.creator_id,
                r.target_agent,
            )
            if r.target_agent
            else None
        )
        if (
            r.target_agent
            and target_access
            and status_info["access_status"] == "active"
        ):
            rel_items.append(GatewayRelationshipItem(
                name=r.target_agent.name,
                type="agent",
                role=r.relation,
                description=r.description or None,
                channels=["agent"],
            ))

    await db.commit()
    return GatewayPollResponse(messages=out, relationships=rel_items)


# ─── Report results ─────────────────────────────────────

@router.post("/report")
async def report_result(
    body: GatewayReportRequest,
    x_api_key: str = Header(None, alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent reports the result of a processed message."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    logger.info("[Gateway] report called message_id_present={}", bool(body.message_id))
    agent = await _get_agent_by_key(x_api_key, db)
    agent_id = agent.id
    await db.rollback()
    await _touch_gateway_agent(agent_id)
    agent = await _get_agent_by_key(x_api_key, db)

    result = await db.execute(
        select(GatewayMessage).where(
            GatewayMessage.id == body.message_id,
            GatewayMessage.agent_id == agent.id,
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Message not found")
    expected_attempt = candidate.delivery_attempts or 0
    supplied_attempt = body.delivery_attempt
    compatible_attempt = supplied_attempt == expected_attempt or (
        supplied_attempt is None and expected_attempt == 1
    )
    if (
        candidate.status == "completed"
        and candidate.result == body.result
        and compatible_attempt
    ):
        return {"status": "ok"}

    try:
        authorized_lane = await _authorize_gateway_message(
            db,
            candidate,
            agent,
            lock_relationship=True,
        )
    except A2AAuthorizationError:
        result = await db.execute(
            select(GatewayMessage)
            .where(
                GatewayMessage.id == body.message_id,
                GatewayMessage.agent_id == agent.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        revoked_message = result.scalar_one_or_none()
        if revoked_message and revoked_message.status in {"pending", "delivered"}:
            _quarantine_gateway_message(
                revoked_message,
                reason="Gateway result authorization was revoked",
            )
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail="Message conversation is no longer authorized",
        )

    result = await db.execute(
        select(GatewayMessage)
        .where(
            GatewayMessage.id == body.message_id,
            GatewayMessage.agent_id == agent.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    expected_attempt = msg.delivery_attempts or 0
    supplied_attempt = body.delivery_attempt
    compatible_attempt = supplied_attempt == expected_attempt or (
        supplied_attempt is None and expected_attempt == 1
    )
    if (
        msg.status == "completed"
        and msg.result == body.result
        and compatible_attempt
    ):
        return {"status": "ok"}
    now = datetime.now(timezone.utc)
    if (
        msg.status != "delivered"
        or not compatible_attempt
        or msg.delivery_lease_expires_at is None
        or msg.delivery_lease_expires_at <= now
    ):
        raise HTTPException(
            status_code=409,
            detail="Message delivery lease is stale or no longer active",
        )

    msg.status = "completed"
    msg.result = body.result
    msg.delivery_lease_expires_at = None
    msg.completed_at = now

    # Save result as assistant chat message and push via WebSocket
    # (works for both user-originated and agent-to-agent messages)
    if body.result and msg.conversation_id and msg.sender_user_id:
        from app.models.audit import ChatMessage
        from app.models.participant import Participant
        session = (
            authorized_lane.session
            if hasattr(authorized_lane, "session")
            else authorized_lane
        )
        # Look up OpenClaw agent's participant_id
        part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == agent.id))
        participant = part_r.scalar_one_or_none()
        
        assistant_msg = ChatMessage(
            agent_id=session.agent_id,
            user_id=msg.sender_user_id,
            role="assistant",
            content=body.result,
            conversation_id=msg.conversation_id,
            participant_id=participant.id if participant else None,
        )
        db.add(assistant_msg)

    # If the original message came from another Agent, enqueue its reply in
    # the same transaction and retain the original directed authorization.
    if body.result and msg.sender_agent_id:
        gw_reply = GatewayMessage(
            agent_id=msg.sender_agent_id,
            sender_agent_id=agent.id,
            sender_user_id=msg.sender_user_id,
            authorization_source_agent_id=msg.authorization_source_agent_id,
            content=body.result,
            status="pending",
            conversation_id=msg.conversation_id,
        )
        db.add(gw_reply)

    await db.commit()

    # Push to WebSocket if user is connected
    if body.result and msg.conversation_id and msg.sender_user_id:
        try:
            from app.api.websocket import manager
            await manager.send_to_user(str(agent.id), str(msg.sender_user_id), {
                "type": "done",
                "role": "assistant",
                "content": body.result,
                "session_id": msg.conversation_id,
            })
        except Exception:
            pass  # User may have disconnected

    if body.result and msg.sender_agent_id:
        logger.info(
            "[Gateway] Reply routed back to sender agent {}",
            msg.sender_agent_id,
        )

    return {"status": "ok"}


# ─── Heartbeat ──────────────────────────────────────────

@router.post("/heartbeat")
async def heartbeat(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Pure heartbeat ping — keeps the OpenClaw agent marked as online."""
    agent = await _get_agent_by_key(x_api_key, db)
    agent_id = agent.id
    await db.rollback()
    await _touch_gateway_agent(agent_id, allow_creating=True)
    return {"status": "ok", "agent_id": str(agent_id)}


# ─── Send message ───────────────────────────────────────

async def _ensure_gateway_a2a_session(
    db: AsyncSession,
    *,
    source_agent: Agent,
    target_agent: Agent,
    owner_user_id: uuid.UUID,
):
    """Find/create the canonical private A2A lane for a gateway request."""

    return await ensure_private_a2a_session(
        db,
        source_agent=source_agent,
        target_agent=target_agent,
        owner_user_id=owner_user_id,
    )


async def _send_to_agent_background(
    source_agent_id: str,
    source_agent_name: str,
    target_agent_id: str,
    target_agent_name: str,
    target_role_description: str,
    owner_user_id: str,
    conversation_id: str,
    content: str,
):
    """Background task: invoke target agent LLM and write reply to gateway_messages.
    
    Accepts plain values (not ORM objects) to avoid stale session references
    since this runs after the request's DB session has closed.
    """
    logger.info(f"[Gateway] Background send started source={source_agent_id} target={target_agent_id}")
    try:
        from app.services.llm import call_llm, resolve_agent_model
        from app.models.audit import ChatMessage

        async with async_session() as db:
            owner_id = uuid.UUID(str(owner_user_id))
            lane = await validate_active_a2a_lane(
                db,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                owner_user_id=owner_id,
                session_id=conversation_id,
            )
            target_agent = lane.target_agent

            model, fallback_model, route_meta = await resolve_agent_model(target_agent)
            model = model or fallback_model
            if not model:
                logger.warning(f"Target agent {target_agent_id} has no LLM model")
                return
            # Skip if model is disabled by admin
            if not model.enabled:
                logger.warning(f"Target agent {target_agent_id} model {model.model} is disabled, skipping")
                return

            session = lane.session
            conv_id = str(session.id)

            # Migrate any existing messages from the old gateway-only format.
            old_conv_id = f"gw_agent_{source_agent_id}_{target_agent_id}"
            from sqlalchemy import update
            await db.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.conversation_id == old_conv_id,
                    ChatMessage.user_id == owner_id,
                )
                .values(conversation_id=conv_id)
            )
            await db.commit()

            # Update last_message_at
            from datetime import datetime, timezone
            session.last_message_at = datetime.now(timezone.utc)


            # Agent-to-agent communication context (injected as prefix to user message
            # since call_llm builds the full system prompt internally)
            agent_comm_alert = (
                "--- Agent-to-Agent Communication Alert ---\n"
                f"You are receiving a direct message from another digital employee ({source_agent_name}). "
                "CRITICAL INSTRUCTION: Your direct text reply will automatically be delivered back to them. "
                "DO NOT use the `send_message_to_agent` tool to reply to this conversation. Just reply naturally in text.\n"
                "If they are asking you to create or analyze a file, deliver the file using `send_file_to_agent` after writing it."
            )

            # Load recent conversation history for context
            hist_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))

            from app.services.llm.utils import convert_chat_messages_to_llm_format as _conv
            messages = _conv(hist_msgs)

            # Add the new message with agent communication context
            user_msg = f"{agent_comm_alert}\n\n[Message from agent: {source_agent_name}]\n{content}"
            messages.append({"role": "user", "content": user_msg})

            from app.models.participant import Participant
            
            # Lookup participants for both agents
            src_part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == source_agent_id))
            tgt_part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == target_agent_id))
            src_participant = src_part_r.scalar_one_or_none()
            tgt_participant = tgt_part_r.scalar_one_or_none()

            write_lane = await validate_active_a2a_lane(
                db,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                owner_user_id=owner_id,
                session_id=conv_id,
                lock_relationship=True,
            )
            # Save user message to conversation
            db.add(ChatMessage(
                agent_id=write_lane.session.agent_id,
                conversation_id=conv_id,
                role="user",
                content=user_msg,
                user_id=owner_id,
                participant_id=src_participant.id if src_participant else None,
            ))
            await db.commit()

        # Short provider preflight; no database connection is retained across
        # network/model latency. Each tool and final write has its own fresh,
        # transaction-scoped authority fence.
        async with async_session() as authorization_db:
            await validate_active_a2a_lane(
                authorization_db,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                owner_user_id=owner_id,
                session_id=conv_id,
                lock_relationship=True,
            )
            await authorization_db.commit()
        collected = []

        async def on_chunk(text):
            collected.append(text)

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=target_agent_name,
            role_description=target_role_description,
            agent_id=target_agent_id,
            user_id=owner_id,
            session_id=conv_id,
            on_chunk=on_chunk,
            route_meta=route_meta,
            tool_authorization_context=(
                build_a2a_tool_authorization_context(
                    source_agent_id=source_agent_id,
                    target_agent_id=target_agent_id,
                    owner_user_id=owner_id,
                    session_id=conv_id,
                )
            ),
        )
        final_reply = reply or "".join(collected)

        async with async_session() as db:
            from app.models.participant import Participant
            final_lane = await validate_active_a2a_lane(
                db,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                owner_user_id=owner_id,
                session_id=conv_id,
                lock_relationship=True,
            )
            tgt_part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == target_agent_id))
            tgt_participant = tgt_part_r.scalar_one_or_none()
            
            db.add(ChatMessage(
                agent_id=final_lane.session.agent_id,
                conversation_id=conv_id,
                role="assistant",
                content=final_reply,
                user_id=owner_id,
                participant_id=tgt_participant.id if tgt_participant else None,
            ))

            # Write reply to gateway_messages for source (OpenClaw) to poll
            gw_reply = GatewayMessage(
                agent_id=source_agent_id,
                sender_agent_id=target_agent_id,
                sender_user_id=owner_id,
                authorization_source_agent_id=source_agent_id,
                content=final_reply,
                status="pending",
                conversation_id=conv_id,
            )
            db.add(gw_reply)
            await db.commit()

        logger.info(f"[Gateway] Background send completed source={source_agent_id} target={target_agent_id}")

    except Exception as e:
        logger.error(
            "[Gateway] send_to_agent_background failed error_type={}",
            type(e).__name__,
        )


@router.post("/send-message")
async def send_message(
    body: GatewaySendMessageRequest,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent sends a message to a person or another agent.

    Routes automatically based on target type:
    - Agent target: triggers LLM processing, reply returned via next poll
    - Human target: sends via available channel (feishu, etc.)
    """
    agent = await _get_agent_by_key(x_api_key, db)
    agent_id = agent.id
    await db.rollback()
    await _touch_gateway_agent(agent_id)
    agent = await _get_agent_by_key(x_api_key, db)
    owner_user_id = agent.creator_id

    target_name = body.target.strip()
    content = body.content.strip()
    channel_hint = (body.channel or "").strip().lower()

    # 1. Try to find target as another Agent, limited to active relationships.
    from app.models.org import AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    rel_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    exact_agent_matches: list[Agent] = []
    for rel in rel_result.scalars().all():
        candidate = rel.target_agent
        if not candidate:
            continue
        status_info = await evaluate_agent_relationship_status(
            db,
            rel,
            current_user_id=owner_user_id,
        )
        if status_info["access_status"] != "active":
            continue
        if not await get_agent_access_level_for_user_id(
            db,
            owner_user_id,
            candidate,
        ):
            continue
        if candidate.name.casefold() == target_name.casefold():
            exact_agent_matches.append(candidate)

    if len(exact_agent_matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Agent target name is ambiguous; use a unique relationship name",
        )
    target_agent = exact_agent_matches[0] if exact_agent_matches else None

    logger.info(
        "[Gateway] send_message target_found={} target_agent={} agent_type={} channel_hint_present={}",
        target_agent is not None,
        target_agent.id if target_agent else None,
        getattr(target_agent, "agent_type", None) if target_agent else None,
        bool(channel_hint),
    )

    if target_agent and (not channel_hint or channel_hint == "agent"):
        chat_session = await _ensure_gateway_a2a_session(
            db,
            source_agent=agent,
            target_agent=target_agent,
            owner_user_id=owner_user_id,
        )
        conv_id = str(chat_session.id)
        await validate_active_a2a_lane(
            db,
            source_agent_id=agent.id,
            target_agent_id=target_agent.id,
            owner_user_id=owner_user_id,
            session_id=conv_id,
            lock_relationship=True,
        )
        from app.models.audit import ChatMessage
        from app.models.participant import Participant

        source_participant_result = await db.execute(
            select(Participant).where(
                Participant.type == "agent",
                Participant.ref_id == agent.id,
            )
        )
        source_participant = source_participant_result.scalar_one_or_none()
        request_idempotency_key = (body.idempotency_key or "").strip()
        source_message_id = (
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"clawith:gateway-send:v1:{agent.id}:{request_idempotency_key}",
            )
            if request_idempotency_key
            else uuid.uuid4()
        )
        existing_source_message = await db.get(ChatMessage, source_message_id)
        if existing_source_message is not None and (
            existing_source_message.agent_id != chat_session.agent_id
            or existing_source_message.conversation_id != conv_id
            or existing_source_message.user_id != owner_user_id
            or existing_source_message.content != content
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was already used for another message",
            )
        if existing_source_message is None:
            db.add(
                ChatMessage(
                    id=source_message_id,
                    agent_id=chat_session.agent_id,
                    conversation_id=conv_id,
                    role="user",
                    content=content,
                    user_id=owner_user_id,
                    participant_id=(
                        source_participant.id if source_participant else None
                    ),
                )
            )
        chat_session.last_message_at = datetime.now(timezone.utc)

        if getattr(target_agent, 'agent_type', None) == 'openclaw':
            # OpenClaw-to-OpenClaw: write to gateway_messages directly
            gateway_message_id = (
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"clawith:gateway-delivery:v1:{source_message_id}:{target_agent.id}",
                )
                if request_idempotency_key
                else uuid.uuid4()
            )
            existing_gateway_message = await db.get(
                GatewayMessage,
                gateway_message_id,
            )
            if existing_gateway_message is None:
                db.add(
                    GatewayMessage(
                        id=gateway_message_id,
                        agent_id=target_agent.id,
                        sender_agent_id=agent.id,
                        sender_user_id=owner_user_id,
                        authorization_source_agent_id=agent.id,
                        content=content,
                        status="pending",
                        conversation_id=conv_id,
                    )
                )
            await db.commit()
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "openclaw_agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }
        else:
            # Native Agent: source history and durable execution are committed
            # atomically. A failed enqueue therefore cannot leave a visible
            # half-delivery, and a caller-supplied key makes retries safe.
            from app.services.trigger_daemon import (
                enqueue_agent_wake_with_context,
            )

            accepted = await enqueue_agent_wake_with_context(
                db,
                target_agent.id,
                f"[From {agent.name}] {content}",
                from_agent_id=agent.id,
                a2a_session_id=conv_id,
                message_kind="notify",
                idempotency_key=(
                    f"gateway-a2a:{source_message_id}"
                ),
                source_message_id=source_message_id,
            )
            if not accepted:
                raise HTTPException(
                    status_code=503,
                    detail="Native Agent delivery could not be queued",
                )
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }

    # 2. Try to find target as a human (via relationships)
    from app.models.org import AgentRelationship
    from sqlalchemy.orm import selectinload

    rel_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentRelationship.member))
    )
    rels = rel_result.scalars().all()

    exact_member_matches = []
    for r in rels:
        status_info = await evaluate_human_relationship_status(db, r, source_agent=agent)
        if (
            r.member
            and status_info["access_status"] == "active"
            and r.member.name.casefold() == target_name.casefold()
        ):
            exact_member_matches.append((r, r.member))

    if len(exact_member_matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Human target name is ambiguous; choose a unique relationship",
        )
    target_member = exact_member_matches[0][1] if exact_member_matches else None

    if not target_member:
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail=f"Target '{target_name}' not found. Check your relationships list."
        )

    # A direct provider call cannot make retries idempotent: a timeout can mean
    # either "not sent" or "sent but response lost". Keep Gateway-to-human
    # delivery fail-closed until it is backed by a durable provider outbox and
    # provider message UUID. Agent-to-Agent and first-party chat remain active.
    await db.rollback()
    raise HTTPException(
        status_code=503,
        detail=(
            "Gateway-to-human external delivery is temporarily paused until "
            "durable idempotent delivery is available"
        ),
    )


# ─── Setup guide ────────────────────────────────────────

@router.get("/setup-guide/{agent_id}")
async def get_setup_guide(
    agent_id: uuid.UUID,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Note: we use the raw key from the header since the agent already authenticated.
    from app.services.platform_service import platform_service

    base_url = await platform_service.get_public_base_url(db, request)

    skill_content = f"""---
name: clawith_sync
description: Sync with Astra platform — check inbox, submit results, and send messages.
---

# Astra Sync

## When to use
Check for new messages from the Astra platform during every heartbeat cycle.
You can also proactively send messages to people and agents in your relationships.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

The response contains a `messages` array. Each message includes:
- `id` — unique message ID (use this for reporting)
- `delivery_attempt` — current delivery generation (return it unchanged when reporting)
- `content` — the message text
- `sender_user_name` — name of the Astra user who sent it
- `sender_user_id` — unique ID of the sender
- `conversation_id` — the conversation this message belongs to
- `history` — array of previous messages in this conversation for context

The response also contains a `relationships` array describing your colleagues:
- `name` — the person or agent name
- `type` — "human" or "agent"
- `role` — relationship type (e.g. collaborator, supervisor)
- `channels` — available communication channels (e.g. ["feishu"], ["agent"])

**IMPORTANT**: Use the `history` array to understand conversation context before replying.
Different `sender_user_name` values mean different people — address them accordingly.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<id from the message>", "delivery_attempt": <delivery_attempt from the message>, "result": "<your response>"}}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: {base_url}/api/gateway/send-message
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"target": "<name of person or agent>", "content": "<your message>"}}

The system auto-detects the best channel. For agents, the reply appears in your next poll.
For humans, the message is delivered via their available channel (e.g. Feishu).
"""

    heartbeat_line = "- Check Astra inbox using the clawith_sync skill and process any pending messages"

    return {
        "skill_filename": "clawith_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
