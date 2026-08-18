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
    can_auto_contact_company_agent,
    evaluate_agent_relationship_status,
    evaluate_human_relationship_status,
    get_agent_access_level_for_user_id,
    is_agent_expired,
)
from app.services.agent_runtime.a2a_runtime import (
    A2ARuntimeError,
    complete_gateway_a2a_runtime,
    enqueue_gateway_a2a_runtime,
)
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.models.tenant import Tenant
from app.services.a2a_authorization import (
    A2AAuthorizationError,
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
    key_hash = _hash_key(api_key)
    result = await db.execute(
        select(Agent).where(
            Agent.api_key_hash == key_hash,
            Agent.agent_type == "openclaw",
            Agent.deleted_at.is_(None),
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

    # Fetch legacy relationships for the gateway compatibility payload
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    rel_items = []

    # Legacy human relationships (with available channels)
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

    # Legacy agent-to-agent relationships
    a_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    related_agent_ids = set()
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

    c_result = await db.execute(
        select(Agent)
        .where(
            Agent.tenant_id == agent.tenant_id,
            Agent.id != agent.id,
            Agent.access_mode == "company",
            Agent.status.in_(["running", "idle"]),
            Agent.deleted_at.is_(None),
        )
        .order_by(Agent.name.asc(), Agent.created_at.asc())
    )
    for candidate in c_result.scalars().all():
        if candidate.id in related_agent_ids:
            continue
        if can_auto_contact_company_agent(agent, candidate):
            rel_items.append(GatewayRelationshipItem(
                name=candidate.name,
                type="agent",
                role="company",
                description=candidate.role_description or None,
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

    if msg.status == "completed":
        if msg.result != body.result:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "gateway_result_mismatch",
                    "message": "Message already completed with a different result.",
                },
            )
        return {"status": "ok"}

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
            id=uuid.uuid5(msg.id, "gateway-report-result"),
            agent_id=session.agent_id,
            user_id=msg.sender_user_id,
            role="assistant",
            content=body.result,
            conversation_id=msg.conversation_id,
            participant_id=participant.id if participant else None,
        )
        db.add(assistant_msg)

    runtime_completion = None
    if body.result and msg.sender_agent_id:
        try:
            runtime_completion = await complete_gateway_a2a_runtime(
                db,
                gateway_message=msg,
                target_agent=agent,
                result=body.result,
            )
        except A2ARuntimeError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

        # A missing Runtime receipt identifies an ordinary OpenClaw-to-
        # OpenClaw conversation. Preserve its queue reply behavior.
        if runtime_completion is None:
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

    # 1. Try to find target as another Agent.
    from app.models.org import AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    target_agent = None
    if not channel_hint or channel_hint == "agent":
        company_result = await db.execute(
            select(Agent).where(
                Agent.name == target_name,
                Agent.tenant_id == agent.tenant_id,
                Agent.id != agent.id,
                Agent.access_mode == "company",
                Agent.deleted_at.is_(None),
            )
        )
        company_candidate = company_result.scalars().first()
        if company_candidate and can_auto_contact_company_agent(agent, company_candidate):
            target_agent = company_candidate

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
    if exact_agent_matches:
        target_agent = exact_agent_matches[0]

    logger.info(
        "[Gateway] send_message target_found={} target_agent={} agent_type={} channel_hint_present={}",
        target_agent is not None,
        target_agent.id if target_agent else None,
        getattr(target_agent, "agent_type", None) if target_agent else None,
        bool(channel_hint),
    )

    if target_agent and (not channel_hint or channel_hint == "agent"):
        if getattr(target_agent, "agent_type", None) != "openclaw":
            try:
                intake = await enqueue_gateway_a2a_runtime(
                    db,
                    source_agent=agent,
                    target_agent=target_agent,
                    content=content,
                    message_id=body.message_id,
                )
            except A2ARuntimeError as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            if intake is None:
                await db.rollback()
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "runtime_disabled",
                        "message": "Durable Runtime is not enabled for native A2A.",
                    },
                )
            await db.commit()
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
                "message_id": str(intake.gateway_message_id),
                "run_id": str(intake.target_run_id),
            }

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

    # 2. Try to find target as a human via the legacy gateway directory payload
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
            detail=f"Target '{target_name}' not found. Check the gateway directory payload returned by poll."
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
    accept_language: str | None = Header(None, alias="Accept-Language"),
    db: AsyncSession = Depends(get_db),
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Note: we use the raw key from the header since the agent already authenticated.
    from app.services.platform_service import platform_service

    base_url = await platform_service.get_public_base_url(db, request)
    is_zh = (
        accept_language.lower().startswith("zh")
        if isinstance(accept_language, str)
        else False
    )

    skill_content = f"""请在 OpenClaw Agent 中创建技能文件 skills/clawith_sync.md，内容如下；然后把「使用 clawith_sync 技能检查 Astra inbox 并处理待办消息」加入 HEARTBEAT.md：

---
name: clawith_sync
description: Sync with Astra platform — check inbox, submit results, and send messages.
---

# Astra Sync

> 兼容性说明：`clawith_sync` 仅是历史协议的技能标识。当前产品和平台身份均为 Astra；不得在用户回复中将自己描述为其他品牌或其他品牌的云部署。

## 使用场景
在每次 heartbeat 时检查 Astra 平台是否有新消息。
你也可以主动给 gateway 通讯录 payload 中返回的精确目标发送消息。

## 操作说明

### 1. 检查 inbox
发起 HTTP GET 请求：
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

响应中包含 messages 数组。每条消息包括：
- id：消息 ID，回报结果时使用
- content：消息内容
- sender_user_name：发送消息的 Astra 用户名
- sender_user_id：发送者 ID
- conversation_id：消息所属会话
- history：该会话的历史消息，用于理解上下文

为了兼容旧协议，响应中还包含 relationships 数组。请把它当作 gateway 通讯录 payload，用其中的精确 name 作为发送目标：
- name：人或 Agent 的名称
- type："human" 或 "agent"
- role：旧关系标签，不要把它当作访问规则
- channels：可用通信渠道，例如 ["feishu"] 或 ["agent"]

重要：回复前先阅读 history 理解上下文。不同 sender_user_name 代表不同用户，请按对应用户回复。

### 2. 回报处理结果
每处理完一条消息，发起 HTTP POST 请求：
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<messages 中的 id>", "result": "<你的回复>"}}

### 3. 主动发送消息
如果需要主动联系某个人或 Agent，发起 HTTP POST 请求：
- URL: {base_url}/api/gateway/send-message
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"target": "<gateway 通讯录 payload 中的精确 name>", "content": "<消息内容>"}}

系统会自动选择合适渠道。发给 Agent 时，回复会出现在下一次 poll 中；发给人类成员时，会通过可用渠道投递，例如飞书。
""" if is_zh else f"""---
name: clawith_sync
description: Sync with Astra platform — check inbox, submit results, and send messages.
---

# Astra Sync

> Compatibility note: `clawith_sync` is a legacy protocol skill identifier only.
> The current product and platform identity is Astra. Never describe yourself to
> users as another brand or as another brand's cloud deployment.

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

For compatibility, the response also contains a `relationships` array. Treat it as a gateway directory payload for exact target names:
- `name` — the person or agent name
- `type` — "human" or "agent"
- `role` — legacy relationship label; do not use it as an access rule
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
- Body: {{"target": "<exact name from the gateway directory payload>", "content": "<your message>"}}

The system auto-detects the best channel. For agents, the reply appears in your next poll.
For humans, the message is delivered via their available channel (e.g. Feishu).
"""

    heartbeat_line = "- Check Astra inbox using the clawith_sync skill and process any pending messages"

    return {
        "skill_filename": "clawith_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
