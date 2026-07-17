"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import json
import re
import uuid
from collections import deque
from datetime import datetime, timezone as tz
from time import perf_counter


from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging_config import get_trace_id, set_trace_id
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import (
    access_token_matches_identity,
    decode_access_token,
    extract_websocket_access_token,
    websocket_response_subprotocol,
)
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.artifact_contract import append_authoritative_artifacts, verified_tool_artifacts
from app.services.agentbay_live import detect_agentbay_env, get_browser_snapshot, get_desktop_screenshot
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.chat_session_access import (
    ChatSessionAuthorizationError,
    build_user_tool_authorization_context,
    validate_active_user_chat_lane,
)
from app.services.llm import call_llm_with_failover
from app.services.llm.caller import RouteMeta, validate_inline_media_payload
from app.services.llm.utils import convert_chat_messages_to_llm_format, truncate_messages_with_pair_integrity
from app.services.media_message_content import sanitize_inline_media_content
from app.services.onboarding import is_onboarded, mark_onboarding_phase, resolve_onboarding_prompt
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
    check_agent_llm_quota,
    check_conversation_quota,
    increment_conversation_usage,
    quota_error_payload,
)
from app.services.realtime import PRESENCE_TTL_SECONDS, realtime_router

router = APIRouter(tags=["websocket"])

MAX_LIVE_CODE_STREAM_CHARS = 120_000
LIVE_CODE_TRUNCATED_NOTICE = "\n\n[... live output truncated; execution continues ...]\n"


def generic_llm_failure_user_message() -> str:
    """Return a stable chat error without exposing database/provider details."""

    return "[LLM call error] 系统暂时无法完成模型调用，请稍后重试；若持续出现请联系管理员。"


def extract_partial_content(args_str: str) -> str:
    """Extract the string value of the 'content' field from a partial JSON tool-arguments string.

    When the LLM streams the finish tool call, arguments arrive as an
    incrementally-growing JSON fragment like '{"content": "hello \\\\n wor'.
    This function parses what is available so far, correctly handling JSON
    escape sequences (\\n, \\", \\\\, \\\\uXXXX, etc.) even when the string is
    truncated mid-escape.
    """
    import re as _re

    s = args_str.strip()
    match = _re.search(r'"content"\s*:\s*"', s)
    if not match:
        return ""

    start_idx = match.end()
    val_chars: list[str] = []
    escaped = False
    i = start_idx
    n = len(s)
    while i < n:
        c = s[i]
        if escaped:
            if c == "n":
                val_chars.append("\n")
            elif c == "t":
                val_chars.append("\t")
            elif c == "r":
                val_chars.append("\r")
            elif c == "b":
                val_chars.append("\b")
            elif c == "f":
                val_chars.append("\f")
            elif c == '"':
                val_chars.append('"')
            elif c == "\\":
                val_chars.append("\\")
            elif c == "/":
                val_chars.append("/")
            elif c == "u":
                if i + 4 < n:
                    try:
                        hex_val = int(s[i + 1 : i + 5], 16)
                        val_chars.append(chr(hex_val))
                        i += 4
                    except ValueError:
                        val_chars.append("\\")
                        val_chars.append("u")
                else:
                    # Incomplete \uXXXX — wait for more data
                    val_chars.append("\\")
                    val_chars.append("u")
            else:
                val_chars.append(c)
            escaped = False
        else:
            if c == "\\":
                escaped = True
            elif c == '"':
                # End of the JSON string value
                break
            else:
                val_chars.append(c)
        i += 1
    return "".join(val_chars)


class ConnectionManager:
    """Manage WebSocket connections per agent."""

    def __init__(self):
        # agent_id_str -> list of (WebSocket, session_id_str | None, user_id_str | None)
        self.active_connections: dict[str, list[tuple]] = {}
        self._presence_heartbeat_tasks: dict[int, asyncio.Task] = {}

    async def connect(self, agent_id: str, websocket: WebSocket, session_id: str = None, user_id: str | None = None):
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = []
        self.active_connections[agent_id].append((websocket, session_id, user_id))
        await realtime_router.register_connection(
            agent_id=agent_id,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
        )
        heartbeat_key = id(websocket)
        previous = self._presence_heartbeat_tasks.pop(heartbeat_key, None)
        if previous:
            previous.cancel()
        self._presence_heartbeat_tasks[heartbeat_key] = asyncio.create_task(
            self._presence_heartbeat(agent_id, websocket),
            name=f"ws-presence-{agent_id[:8]}",
        )

    async def _presence_heartbeat(self, agent_id: str, websocket: WebSocket) -> None:
        interval = max(PRESENCE_TTL_SECONDS // 3, 1)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await realtime_router.refresh_connection(
                        agent_id=agent_id,
                        websocket=websocket,
                    )
                except Exception:
                    logger.warning(
                        "[Realtime] Presence heartbeat retry agent_id={}",
                        agent_id,
                    )
        except asyncio.CancelledError:
            raise

    async def disconnect(self, agent_id: str, websocket: WebSocket):
        if agent_id in self.active_connections:
            self.active_connections[agent_id] = [
                (ws, sid, uid) for ws, sid, uid in self.active_connections[agent_id] if ws != websocket
            ]
        heartbeat = self._presence_heartbeat_tasks.pop(id(websocket), None)
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await realtime_router.unregister_connection(agent_id=agent_id, websocket=websocket)

    def _local_connections(self, agent_id: str) -> list[tuple[WebSocket, str | None, str | None]]:
        return self.active_connections.get(agent_id, [])

    async def deliver_pubsub_message(
        self,
        *,
        agent_id: str,
        payload: dict,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if agent_id not in self.active_connections:
            return
        for ws, sid, uid in list(self.active_connections[agent_id]):
            if session_id is not None and sid != session_id:
                continue
            if user_id is not None and uid != user_id:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def send_message(self, agent_id: str, message: dict):
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
        )

    async def send_to_session(self, agent_id: str, session_id: str, message: dict):
        """Send message only to WebSocket connections matching the given session_id."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=session_id,
        )

    async def send_to_user(self, agent_id: str, user_id: str, message: dict):
        """Send message to all live WebSocket sessions of a given platform user for an agent."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            user_id=user_id,
        )

    async def send_to_session_user(
        self,
        agent_id: str,
        session_id: str,
        user_id: str,
        message: dict,
    ) -> bool:
        """Send only to the exact authenticated user/session pair."""
        return await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=session_id,
            user_id=user_id,
            require_target_success=True,
        )

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        """Return distinct session IDs for all active WS connections of an agent."""
        return await realtime_router.get_active_session_ids(agent_id)

    async def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        """Return True if the given platform user currently has this exact session open."""
        return await realtime_router.is_user_viewing_session(
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )


manager = ConnectionManager()


async def maybe_mark_session_read_for_active_viewer(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
) -> bool:
    """Advance last_read_at_by_user if the owner is actively viewing this exact session."""
    if not await manager.is_user_viewing_session(str(agent_id), session_id, str(user_id)):
        return False

    session = await db.get(ChatSession, uuid.UUID(session_id))
    if not session:
        return False

    session.last_read_at_by_user = datetime.now(tz.utc)
    return True



@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: str | None = Query(None),
    session_id: str = Query(None),
    lang: str = Query("en"),
):
    """WebSocket endpoint for real-time chat with an agent."""
    access_token = extract_websocket_access_token(websocket, token)
    handler = WebSocketChatHandler(websocket, agent_id, access_token, session_id, lang)
    await handler.run()


class WebSocketChatHandler:
    """Manages connection lifecycle, message polling, LLM orchestration, and persistence for a single user-agent session."""

    def __init__(
        self,
        websocket: WebSocket,
        agent_id: uuid.UUID,
        token: str | None,
        session_id: str | None = None,
        lang: str = "en",
    ):
        self.websocket = websocket
        self.agent_id = agent_id
        self.token = token
        self.session_id_param = session_id
        self.lang = lang

        # State fields initialized during setup
        self.user: User | None = None
        self.agent: Agent | None = None
        self.agent_name: str = ""
        self.agent_type: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.llm_model: LLMModel | None = None
        self.fallback_llm_model: LLMModel | None = None
        self.current_route_meta: RouteMeta | None = None
        self.conv_id: str | None = None
        self.session_model_tier: str | None = None
        self.session_model_modality: str | None = None
        self.history_messages: list[ChatMessage] = []
        self.conversation: list[dict] = []
        self.current_user_text: str = ""
        self.pending_messages: deque[dict] = deque()

    async def run(self):
        """Main entry point for handling the lifecycle of the WebSocket connection."""
        try:
            # 1. Setup session (Authentication, permissions, loading models, history, etc.)
            success = await self.setup()
            if not success:
                return

            # 2. Start the message receiving and processing loop
            await self.message_loop()

        except WebSocketDisconnect:
            logger.info(f"[WS] Client disconnected: {getattr(self.user, 'id', 'unknown')}")
            await manager.disconnect(str(self.agent_id), self.websocket)
        except Exception as e:
            logger.exception(f"[WS] Unexpected error: {e}")
            from app.services.production_issue_monitor import record_production_issue

            await record_production_issue(
                source="websocket",
                category="websocket",
                summary="WebSocket product session terminated unexpectedly",
                severity="error",
                error_code=type(e).__name__,
                route="/ws/chat/{agent_id}",
                operation="session",
                tenant_id=getattr(self.agent, "tenant_id", None),
                user_id=getattr(self.user, "id", None),
                agent_id=self.agent_id,
                trace_id=get_trace_id(),
                metadata={"error_type": type(e).__name__},
            )
            await manager.disconnect(str(self.agent_id), self.websocket)

    async def setup(self) -> bool:
        """Accepts connection, authenticates user, verifies agent access, loads models, resolves session & history."""
        # Accept immediately so browser sees onopen without waiting for DB setup
        await self.websocket.accept(subprotocol=websocket_response_subprotocol(self.websocket))

        # Authenticate
        try:
            payload = decode_access_token(self.token)
            user_id = uuid.UUID(payload["sub"])
        except Exception:
            await self.websocket.send_json({"type": "error", "content": "Authentication failed"})
            await self.websocket.close(code=4001)
            return False

        try:
            async with async_session() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == user_id)
                    .options(selectinload(User.identity))
                )
                self.user = result.scalar_one_or_none()
                if (
                    not self.user
                    or not self.user.is_active
                    or not self.user.identity
                    or not self.user.identity.is_active
                    or not access_token_matches_identity(payload, self.user.identity)
                ):
                    logger.error("[WS] User not found")
                    await self.websocket.send_json({"type": "error", "content": "Account unavailable"})
                    await self.websocket.close(code=4001)
                    return False
                self.auth_version = int(payload["av"])

                tenant = (
                    await db.get(Tenant, self.user.tenant_id)
                    if self.user.tenant_id
                    else None
                )
                if tenant is None or not tenant.is_active:
                    await self.websocket.send_json(
                        {"type": "error", "content": "Company unavailable"}
                    )
                    await self.websocket.close(code=4003)
                    return False

                logger.info(f"[WS] Checking agent access for {self.agent_id}")
                self.agent, _ = await check_agent_access(db, self.user, self.agent_id)
                if (
                    self.agent.tenant_id != self.user.tenant_id
                    or getattr(self.agent, "status", None)
                    in {"stopped", "paused", "error"}
                    or is_agent_expired(self.agent)
                ):
                    await self.websocket.send_json(
                        {
                            "type": "error",
                            "content": "This Agent has expired and is off duty. Please contact your admin to extend its service.",
                        }
                    )
                    await self.websocket.close(code=4003)
                    return False

                self.agent_name = self.agent.name
                self.agent_type = self.agent.agent_type or ""
                self.role_description = self.agent.role_description or ""
                self.welcome_message = self.agent.welcome_message or ""
                self.ctx_size = self.agent.context_window_size or 100
                self.user_display_name = (self.user.display_name or "").strip() or "there"
                logger.info(
                    f"[WS] Agent={self.agent_id} type={self.agent_type} "
                    f"model_id={self.agent.primary_model_id} ctx={self.ctx_size}"
                )

                # Load models
                await self._load_models(db)

                # Resolve or create chat session
                self.conv_id = await self._resolve_chat_session(db, user_id)
                if not self.conv_id:
                    return False
                await validate_active_user_chat_lane(
                    db,
                    agent_id=self.agent_id,
                    owner_user_id=user_id,
                    session_id=self.conv_id,
                    expected_auth_version=self.auth_version,
                )

                # Load history messages
                await self._load_history(db)

        except Exception as e:
            logger.exception(f"[WS] Setup error: {e}")
            await self.websocket.send_json({"type": "error", "content": "Setup failed"})
            await self.websocket.close(code=4002)
            return False

        # Connect connection manager
        agent_id_str = str(self.agent_id)
        await manager.connect(agent_id_str, self.websocket, self.conv_id, str(user_id))
        logger.info(f"[WS] Ready agent={self.agent_id}")

        # Send session_id to frontend
        await self.websocket.send_json({"type": "connected", "session_id": self.conv_id})

        # Build conversation context
        self.conversation = self._build_conversation_context()

        return True

    async def _load_models(self, db: AsyncSession):
        """Loads primary and fallback models for the agent."""
        if self.agent.primary_model_id:
            model_result = await db.execute(select(LLMModel).where(LLMModel.id == self.agent.primary_model_id))
            self.llm_model = model_result.scalar_one_or_none()
            if self.llm_model and not self.llm_model.enabled:
                logger.info(f"[WS] Primary model {self.llm_model.model} is disabled, skipping")
                self.llm_model = None
            else:
                logger.info(f"[WS] Primary model loaded: {self.llm_model.model if self.llm_model else 'None'}")

        if self.agent.fallback_model_id:
            fb_result = await db.execute(select(LLMModel).where(LLMModel.id == self.agent.fallback_model_id))
            self.fallback_llm_model = fb_result.scalar_one_or_none()
            if self.fallback_llm_model and not self.fallback_llm_model.enabled:
                logger.info(f"[WS] Fallback model {self.fallback_llm_model.model} is disabled, skipping")
                self.fallback_llm_model = None
            elif self.fallback_llm_model:
                logger.info(f"[WS] Fallback model loaded: {self.fallback_llm_model.model}")

        if not self.llm_model and self.fallback_llm_model:
            self.llm_model = self.fallback_llm_model
            self.fallback_llm_model = None
            logger.info(f"[WS] Primary model unavailable, using fallback: {self.llm_model.model}")

    async def _resolve_chat_session(self, db: AsyncSession, user_id: uuid.UUID) -> str | None:
        """Resolves existing session or creates a new one."""
        conv_id = self.session_id_param
        selected_session: ChatSession | None = None
        if conv_id:
            try:
                _sid = uuid.UUID(conv_id)
            except (ValueError, TypeError):
                conv_id = None
                _existing = None
            else:
                _sr = await db.execute(select(ChatSession).where(ChatSession.id == _sid))
                _existing = _sr.scalar_one_or_none()
                if not _existing:
                    conv_id = None
                elif (
                    str(_existing.agent_id) != str(self.agent_id)
                    or str(_existing.user_id) != str(user_id)
                    or _existing.source_channel != "web"
                    or bool(getattr(_existing, "is_group", False))
                ):
                    await self.websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                    await self.websocket.close(code=4003)
                    return None
                else:
                    selected_session = _existing
        if not conv_id:
            _sr = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == self.agent_id,
                    ChatSession.user_id == user_id,
                    ChatSession.source_channel == "web",
                    not ChatSession.is_group,
                    ChatSession.is_primary,
                )
                .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
                .limit(1)
            )
            _latest = _sr.scalar_one_or_none()
            if _latest:
                conv_id = str(_latest.id)
                selected_session = _latest
            else:
                _new_session = await ensure_primary_platform_session(db, self.agent_id, user_id)
                await db.commit()
                await db.refresh(_new_session)
                conv_id = str(_new_session.id)
                selected_session = _new_session
                logger.info(f"[WS] Selected primary session {conv_id}")
        if selected_session is not None:
            self.session_model_tier = getattr(selected_session, "model_tier", None)
            self.session_model_modality = getattr(selected_session, "model_modality", None)
        return conv_id

    async def _load_history(self, db: AsyncSession):
        """Loads and prepares history messages for the conversation."""
        try:
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.agent_id == self.agent_id, ChatMessage.conversation_id == self.conv_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(self.ctx_size)
            )
            self.history_messages = list(reversed(history_result.scalars().all()))
            logger.info(f"[WS] Loaded {len(self.history_messages)} history messages for session {self.conv_id}")
        except Exception as e:
            logger.warning(f"[WS] History load failed (non-fatal): {e}")

    def _build_conversation_context(self) -> list[dict]:
        """Translates historical ChatMessages to LLM inputs."""
        return convert_chat_messages_to_llm_format(self.history_messages)

    async def message_loop(self):
        """Core message processing loop."""
        # Send welcome message on new session (no history)
        if self.welcome_message and not self.history_messages:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": self.welcome_message})

        while True:
            data = await self._receive_next_message()
            if not await self._ensure_access_token_current():
                return

            # Set a unique trace ID for this specific message processing.
            set_trace_id(uuid.uuid4().hex[:12])

            content = data.get("content", "")
            display_content = data.get("display_content", "")
            file_name = data.get("file_name", "")
            raw_client_message_id = data.get("client_message_id")
            client_message_id: uuid.UUID | None = None
            if isinstance(raw_client_message_id, str) and len(raw_client_message_id) <= 100:
                try:
                    candidate_message_id = uuid.UUID(raw_client_message_id)
                except (TypeError, ValueError):
                    pass
                else:
                    if candidate_message_id.version == 4:
                        client_message_id = candidate_message_id
            chat_tier = data.get("tier") or self.session_model_tier
            chat_modality = data.get("modality") or self.session_model_modality
            ephemeral_modality = data.get("ephemeral_modality") is True
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(
                "[WS] Received message content_chars={} display_chars={} file_attached={} kind={}",
                len(content),
                len(display_content),
                bool(file_name),
                "onboarding_trigger" if is_onboarding_trigger else "chat",
            )

            if not content and not is_onboarding_trigger:
                continue

            if is_onboarding_trigger:
                if await self._handle_onboarding_trigger_guard():
                    continue
                content = "Please begin the onboarding."

            try:
                validate_inline_media_payload(content)
            except QuotaExceeded as qe:
                await self.websocket.send_json({
                    "type": "done",
                    "role": "assistant",
                    "content": f"⚠️ {qe.message}",
                    "quota_error": quota_error_payload(qe),
                })
                continue

            persisted_user_content = sanitize_inline_media_content(
                content,
                display_content=display_content,
                file_names=file_name,
            )
            self.current_user_text = persisted_user_content

            # Resolve effective model from SaaS tier/modality (legacy model_id no longer trusted)
            try:
                effective_llm_model, _effective_fallback = await self._resolve_route(
                    tier=chat_tier,
                    modality=chat_modality,
                )
            except QuotaExceeded as qe:
                quota_error = quota_error_payload(qe)
                await self.websocket.send_json({
                    "type": "done",
                    "role": "assistant",
                    "content": f"⚠️ {qe.message}",
                    "quota_error": quota_error,
                })
                continue

            if self.current_route_meta is not None:
                chat_tier = self.current_route_meta.saas_tier
                chat_modality = self.current_route_meta.modality
                await self._persist_session_model_selection(
                    chat_tier,
                    None if ephemeral_modality else chat_modality,
                )

            # Quota Checks (use resolved SaaS tier for weighting)
            if not await self._check_quotas(saas_tier=chat_tier):
                continue

            # Add user message to in-memory context
            current_user_turn = {"role": "user", "content": content}
            self.conversation.append(current_user_turn)

            # Save user message to DB
            if not await self._ensure_access_token_current():
                return
            await self._save_user_message(
                content,
                display_content,
                file_name,
                is_onboarding_trigger,
                message_id=client_message_id,
            )

            # OpenClaw routing check
            if self.agent_type == "openclaw":
                current_user_turn["content"] = persisted_user_content
                if not await self._ensure_access_token_current():
                    return
                await self._route_openclaw(persisted_user_content)
                continue

            # Detect task creation intent
            task_match = re.search(
                r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)",
                persisted_user_content,
                re.IGNORECASE,
            )

            # Invoke LLM and stream response
            try:
                if effective_llm_model:
                    assistant_response, thinking_content, queued_messages = await self._run_llm_and_stream(
                        effective_llm_model,
                        is_onboarding_trigger,
                        route_meta=self.current_route_meta,
                    )
                else:
                    assistant_response = (
                        f"⚠️ {self.agent_name} has no LLM model configured. "
                        "Please select a tier in the agent's Settings tab or ask an admin to configure model routes."
                    )
                    thinking_content = []
                    queued_messages = []
            finally:
                # The binary data URL is needed only by the current provider
                # request. Retaining it would resend the same image/video on
                # every later turn and amplify memory, request size and cost.
                current_user_turn["content"] = persisted_user_content

            # If task creation detected, create a real Task record
            if task_match:
                if not await self._ensure_access_token_current():
                    return
                assistant_response = await self._create_task_record(task_match.group(1).strip(), assistant_response)

            # Add assistant response to in-memory conversation
            self.conversation.append({"role": "assistant", "content": assistant_response})

            # Save assistant reply
            if not await self._ensure_access_token_current():
                return
            assistant_message_id = await self._save_assistant_reply(assistant_response, thinking_content)

            # Final 'done' packet
            await self.websocket.send_json(
                {
                    "type": "done",
                    "role": "assistant",
                    "content": assistant_response,
                    "message_id": assistant_message_id,
                }
            )

            # Messages arriving during generation are processed in arrival order on
            # the next loop iterations instead of being silently discarded.
            self.pending_messages.extend(queued_messages)

    async def _receive_next_message(self) -> dict:
        """Return queued input before reading the socket again."""
        if self.pending_messages:
            return self.pending_messages.popleft()
        return await self.websocket.receive_json()

    async def _ensure_access_token_current(self) -> bool:
        """Fence every message and side effect against Identity revocation."""

        try:
            async with async_session() as db:
                await validate_active_user_chat_lane(
                    db,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=self.conv_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                await db.commit()
            return True
        except ChatSessionAuthorizationError:
            await self.websocket.send_json(
                {"type": "error", "content": "Session expired. Please sign in again."}
            )
            await self.websocket.close(code=4001)
            return False

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already onboarded)."""
        async with async_session() as _gdb:
            if await is_onboarded(_gdb, self.agent_id, self.user.id):
                logger.info("[WS] Onboarding trigger ignored — pair already onboarded")
                await self.websocket.send_json(
                    {
                        "type": "onboarded",
                        "agent_id": str(self.agent_id),
                    }
                )
                return True
        return False

    async def _resolve_route(
        self,
        tier: str | None,
        modality: str | None,
    ) -> tuple[LLMModel | None, LLMModel | None]:
        """Resolve effective LLM model(s) from SaaS tier/modality or legacy config.

        Stores resolved models on self.llm_model / self.fallback_llm_model.
        """
        from app.services.llm.caller import resolve_agent_model

        async with async_session() as _mdb:
            _agent_r = await _mdb.execute(select(Agent).where(Agent.id == self.agent_id))
            _agent_cur = _agent_r.scalar_one_or_none()
            if not _agent_cur:
                self.llm_model = None
                self.fallback_llm_model = None
                self.current_route_meta = None
                return None, None

            try:
                primary, fallback, route_meta = await resolve_agent_model(
                    _agent_cur,
                    tier=tier,
                    modality=modality,
                )
            except QuotaExceeded as qe:
                # Propagate as a user-facing message; caller will render it.
                self.llm_model = None
                self.fallback_llm_model = None
                self.current_route_meta = None
                raise qe

            self.llm_model = primary
            self.fallback_llm_model = fallback
            self.current_route_meta = route_meta
            return primary, fallback

    async def _persist_session_model_selection(
        self,
        tier: str,
        modality: str | None,
    ) -> None:
        """Persist an explicit route selection, never an attachment-only modality."""
        if not self.conv_id or not self.user:
            return
        if (
            self.session_model_tier == tier
            and (modality is None or self.session_model_modality == modality)
        ):
            return
        try:
            session_uuid = uuid.UUID(self.conv_id)
        except (TypeError, ValueError):
            return

        try:
            async with async_session() as db:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_uuid,
                        ChatSession.agent_id == self.agent_id,
                        ChatSession.user_id == self.user.id,
                        ChatSession.source_channel == "web",
                        ChatSession.is_group.is_(False),
                    )
                )
                session = result.scalar_one_or_none()
                if not session:
                    return
                session.model_tier = tier
                if modality is not None:
                    session.model_modality = modality
                await db.commit()
            self.session_model_tier = tier
            if modality is not None:
                self.session_model_modality = modality
        except Exception as exc:
            # A persistence outage must not discard a valid chat response. The
            # frontend PATCH path will retry on the next explicit selection.
            logger.warning(f"[WS] Failed to persist session model selection: {exc}")

    async def _check_quotas(self, saas_tier: str | None = None) -> bool:
        """Checks conversation and agent LLM quotas. Sends message and returns False if exceeded."""
        try:
            await check_conversation_quota(self.user.id)
            await check_agent_expired(self.agent_id)
            await check_agent_llm_quota(self.agent_id, model_tier=saas_tier)
            return True
        except QuotaExceeded as qe:
            quota_error = quota_error_payload(qe)
            await self.websocket.send_json({
                "type": "done",
                "role": "assistant",
                "content": f"⚠️ {qe.message}",
                "quota_error": quota_error,
            })
            return False
        except AgentExpired as ae:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {ae.message}"})
            return False

    async def _save_user_message(
        self,
        content: str,
        display_content: str,
        file_name: str,
        is_onboarding_trigger: bool,
        *,
        message_id: uuid.UUID | None = None,
    ) -> str | None:
        """Saves user message to the database and updates session title/time."""
        saved_content = sanitize_inline_media_content(
            content,
            display_content=display_content,
            file_names=file_name,
        )
        if is_onboarding_trigger:
            logger.info("[WS] Onboarding trigger — skipping user-message persistence")
            async with async_session() as _sdb:
                lane = await validate_active_user_chat_lane(
                    _sdb,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=self.conv_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                if lane.session.title.startswith("Session "):
                    lane.session.title = "Onboarding"
                    await _sdb.commit()
            return None
        else:
            persisted_message_id = message_id or uuid.uuid4()
            async with async_session() as db:
                lane = await validate_active_user_chat_lane(
                    db,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=self.conv_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                user_msg = ChatMessage(
                    id=persisted_message_id,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    role="user",
                    content=saved_content,
                    conversation_id=self.conv_id,
                )
                db.add(user_msg)
                # Update session
                _now = datetime.now(tz.utc)
                lane.session.last_message_at = _now
                if not self.history_messages and lane.session.title.startswith("Session "):
                    clean_title = saved_content.replace("[图片] ", "📷 ").strip()
                    lane.session.title = clean_title[:40] if clean_title else "New message"
                await db.commit()
            logger.info("[WS] User message saved")
            return str(persisted_message_id)

    async def _route_openclaw(self, content: str):
        """Enqueues message for OpenClaw edge node poll."""
        from app.models.gateway_message import GatewayMessage as GwMsg

        async with async_session() as db:
            await validate_active_user_chat_lane(
                db,
                agent_id=self.agent_id,
                owner_user_id=self.user.id,
                session_id=self.conv_id,
                lock_authority=True,
                expected_auth_version=self.auth_version,
            )
            gw_msg = GwMsg(
                agent_id=self.agent_id,
                sender_user_id=self.user.id,
                conversation_id=self.conv_id,
                content=content,
                status="pending",
            )
            db.add(gw_msg)
            await db.commit()
        logger.info("[WS] OpenClaw: message queued for gateway poll")
        await self.websocket.send_json(
            {
                "type": "done",
                "role": "assistant",
                "content": "Message forwarded to OpenClaw agent. Waiting for response...",
            }
        )

    async def _run_llm_and_stream(
        self,
        effective_llm_model: LLMModel,
        is_onboarding_trigger: bool,
        route_meta: "RouteMeta | None" = None,
    ) -> tuple[str, list[str], list[dict]]:
        """Calls the LLM and streams response chunks to WebSocket."""
        start_gen = perf_counter()
        try:
            logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")

            # Accumulate partial content for abort handling
            partial_chunks: list[str] = []
            # Track how many characters of finish-tool content have been streamed
            finish_content_sent_len = 0
            completed_artifact_paths: list[str] = []

            # Set inside _call_with_failover when an onboarding prompt was injected
            needs_onboarding_mark = False
            onboarding_target_phase = "completed"
            onboarding_mark_done = False

            async def maybe_mark_onboarding_progress():
                nonlocal onboarding_mark_done
                if needs_onboarding_mark and not onboarding_mark_done:
                    onboarding_mark_done = True
                    try:
                        async with async_session() as _ob_db:
                            await mark_onboarding_phase(
                                _ob_db,
                                self.agent_id,
                                self.user.id,
                                onboarding_target_phase,
                            )
                        # Tell the frontend to refresh its cached agent record
                        await self.websocket.send_json(
                            {
                                "type": "onboarded",
                                "agent_id": str(self.agent_id),
                            }
                        )
                    except Exception as _ob_err:
                        logger.warning(f"[WS] mark_onboarded failed (non-fatal): {_ob_err}")

            async def stream_to_ws(text: str):
                """Send each chunk to client in real-time."""
                partial_chunks.append(text)
                await self.websocket.send_json({"type": "chunk", "content": text})
                await maybe_mark_onboarding_progress()

            async def tool_call_to_ws(data: dict):
                """Send tool call info to client and persist completed ones."""
                if data.get("status") in {"running", "done"}:
                    await maybe_mark_onboarding_progress()
                if data.get("status") == "done":
                    # Inject Live Preview & Workspace Activities
                    await self._inject_live_preview_and_workspace_metadata(data)
                    verified = await verified_tool_artifacts(
                        self.agent_id,
                        str(data.get("name") or ""),
                        data.get("args") if isinstance(data.get("args"), dict) else None,
                        str(data.get("result") or ""),
                    )
                    if verified:
                        data["artifacts"] = [
                            {"path": path, "verified": True}
                            for path in verified
                        ]
                        completed_artifact_paths.extend(
                            path for path in verified if path not in completed_artifact_paths
                        )

                # Persist before publishing the final frame so history hydration and
                # realtime delivery share one stable database message ID.
                if data.get("status") == "done":
                    message_id = await self._save_completed_tool_call_to_db(data)
                    if message_id:
                        data["message_id"] = message_id

                await self.websocket.send_json({"type": "tool_call", **data})

            # Track thinking content for storage
            thinking_content = []

            async def thinking_to_ws(text: str):
                """Send thinking chunks to client for collapsible display."""
                thinking_content.append(text)
                await self.websocket.send_json({"type": "thinking", "content": text})

            _workspace_draft_cache: dict[str, str] = {}

            async def tool_delta_to_ws(data: dict):
                """Stream workspace file-operation drafts while tool args are still arriving."""
                nonlocal finish_content_sent_len
                tool_name = data.get("name", "")

                # Stream finish tool content as real-time chunks
                if tool_name == "finish":
                    raw_args = data.get("arguments", "")
                    if isinstance(raw_args, str) and raw_args:
                        current_content = extract_partial_content(raw_args)
                        if len(current_content) > finish_content_sent_len:
                            delta = current_content[finish_content_sent_len:]
                            finish_content_sent_len = len(current_content)
                            await stream_to_ws(delta)
                    return

                _ws_tools = {
                    "write_file",
                    "edit_file",
                    "move_file",
                    "delete_file",
                    "convert_markdown_to_docx",
                    "convert_csv_to_xlsx",
                    "convert_markdown_to_pdf",
                    "convert_html_to_pdf",
                    "convert_html_to_pptx",
                }
                if tool_name not in _ws_tools:
                    return

                raw_args = data.get("arguments", "")
                if isinstance(raw_args, (dict, list)):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                elif raw_args is None:
                    raw_args = ""
                else:
                    raw_args = str(raw_args)

                draft_id = str(data.get("id") or f"draft-{data.get('index', 0)}")
                if _workspace_draft_cache.get(draft_id) == raw_args:
                    return
                _workspace_draft_cache[draft_id] = raw_args

                await self.websocket.send_json(
                    {
                        "type": "workspace_draft",
                        "id": draft_id,
                        "index": data.get("index", 0),
                        "name": tool_name,
                        "arguments": raw_args,
                    }
                )

            # Run call_llm_with_failover as a cancellable task
            async def _call_with_failover():
                nonlocal needs_onboarding_mark, onboarding_target_phase

                async def _on_failover(reason: str):
                    await self.websocket.send_json({"type": "info", "content": f"Primary model error, {reason}"})

                _truncated = truncate_messages_with_pair_integrity(self.conversation, self.ctx_size)

                # Resolve onboarding prompt
                skip_tools_for_greeting = False
                try:
                    async with async_session() as _ob_db:
                        _onb = await resolve_onboarding_prompt(
                            _ob_db,
                            self.agent,
                            self.user.id,
                            user_name=self.user_display_name,
                            user_locale=self.lang,
                        )
                    if _onb:
                        _truncated = [{"role": "system", "content": _onb.prompt}] + _truncated
                        if _onb.lock_on_first_chunk:
                            needs_onboarding_mark = True
                            onboarding_target_phase = _onb.target_phase
                        if _onb.is_greeting_turn:
                            skip_tools_for_greeting = True
                except Exception as _onb_err:
                    logger.warning(f"[WS] Onboarding prompt resolve failed (non-fatal): {_onb_err}")

                live_code_chars_sent = 0
                live_code_truncated_sent = False

                async def code_output_to_ws(text: str, label: str = "stdout"):
                    """Stream execute_code output chunks to the frontend live panel in real-time."""
                    nonlocal live_code_chars_sent, live_code_truncated_sent
                    try:
                        remaining = MAX_LIVE_CODE_STREAM_CHARS - live_code_chars_sent
                        if remaining <= 0:
                            if not live_code_truncated_sent:
                                live_code_truncated_sent = True
                                await self.websocket.send_json(
                                    {
                                        "type": "agentbay_live",
                                        "env": "code",
                                        "output": LIVE_CODE_TRUNCATED_NOTICE,
                                        "stream": label,
                                    }
                                )
                            return

                        output = text[:remaining]
                        live_code_chars_sent += len(output)
                        await self.websocket.send_json(
                            {
                                "type": "agentbay_live",
                                "env": "code",
                                "output": output,
                                "stream": label,
                            }
                        )
                    except Exception:
                        pass

                async with async_session() as authorization_db:
                    await validate_active_user_chat_lane(
                        authorization_db,
                        agent_id=self.agent_id,
                        owner_user_id=self.user.id,
                        session_id=self.conv_id,
                        lock_authority=True,
                        expected_auth_version=self.auth_version,
                    )
                    await authorization_db.commit()

                return await call_llm_with_failover(
                    primary_model=effective_llm_model,
                    fallback_model=self.fallback_llm_model,
                    messages=_truncated,
                    agent_name=self.agent_name,
                    role_description=self.role_description,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    session_id=self.conv_id,
                    on_chunk=stream_to_ws,
                    on_tool_call=tool_call_to_ws,
                    on_tool_delta=tool_delta_to_ws,
                    on_thinking=thinking_to_ws,
                    supports_vision=getattr(effective_llm_model, "supports_vision", False),
                    on_failover=_on_failover,
                    skip_tools=skip_tools_for_greeting,
                    on_code_output=code_output_to_ws,
                    route_meta=route_meta,
                    tool_authorization_context=(
                        build_user_tool_authorization_context(
                            agent_id=self.agent_id,
                            owner_user_id=self.user.id,
                            session_id=self.conv_id,
                            expected_auth_version=self.auth_version,
                        )
                    ),
                )

            llm_task = asyncio.create_task(_call_with_failover())

            # Listen for abort while LLM is running
            aborted = False
            queued_messages: list[dict] = []
            while not llm_task.done():
                try:
                    msg = await asyncio.wait_for(self.websocket.receive_json(), timeout=0.5)
                    if msg.get("type") == "abort":
                        logger.info("[WS] Abort received, cancelling LLM task")
                        llm_task.cancel()
                        aborted = True
                        break
                    else:
                        queued_messages.append(msg)
                except asyncio.TimeoutError:
                    continue
                except WebSocketDisconnect:
                    llm_task.cancel()
                    raise

            if aborted:
                try:
                    await llm_task
                except (asyncio.CancelledError, Exception):
                    pass
                partial_text = "".join(partial_chunks).strip()
                assistant_response = (
                    (partial_text + "\n\n*[Generation stopped]*") if partial_text else "*[Generation stopped]*"
                )
                logger.info(f"[WS] LLM aborted partial_chars={len(assistant_response)}")
            else:
                assistant_response = await llm_task
                logger.info(f"[WS] LLM response complete chars={len(assistant_response)}")

            assistant_response = append_authoritative_artifacts(
                assistant_response,
                self.agent_id,
                completed_artifact_paths,
            )

            # Raise error on prefix for failover matching
            _llm_error_prefixes = ("[LLM Error]", "[LLM call error]", "[Error]")
            if (
                not aborted
                and assistant_response
                and any(assistant_response.startswith(p) for p in _llm_error_prefixes)
            ):
                raise RuntimeError(assistant_response)

            # Post-success actions (last_active_at, quota usage increments, activity logs)
            await self._update_activity_and_quota(assistant_response)

            return assistant_response, thinking_content, queued_messages

        except WebSocketDisconnect:
            raise
        except QuotaExceeded:
            raise
        except Exception as e:
            gen_duration = perf_counter() - start_gen
            logger.exception(f"[WS] LLM error after {gen_duration:.3f}s: {e}")
            from app.services.production_issue_monitor import record_production_issue

            await record_production_issue(
                source="websocket",
                category="llm",
                summary="Web chat model operation failed",
                severity="error",
                error_code=type(e).__name__,
                route="/ws/chat/{agent_id}",
                operation="chat",
                tenant_id=getattr(self.agent, "tenant_id", None),
                user_id=getattr(self.user, "id", None),
                agent_id=self.agent_id,
                trace_id=get_trace_id(),
                metadata={
                    "error_type": type(e).__name__,
                    "duration_ms": round(gen_duration * 1000),
                    "model": getattr(self.llm_model, "model", None),
                    "provider": getattr(self.llm_model, "provider", None),
                },
            )
            return generic_llm_failure_user_message(), [], []

    async def _inject_live_preview_and_workspace_metadata(self, data: dict):
        """Injects live previews and workspace panel activity tracking into tool results."""
        try:
            tool_name = data.get("name", "")
            env = detect_agentbay_env(tool_name)
            if env == "desktop":
                b64_url = await get_desktop_screenshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "browser":
                b64_url = await get_browser_snapshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "code":
                tool_result = data.get("result", "") or ""
                data["live_preview"] = {"env": "code", "output": tool_result[:5000]}
        except Exception as _lp_err:
            logger.warning(f"[WS][LivePreview] Embed failed: {_lp_err}")

        _workspace_tool_actions = {
            "write_file": "write",
            "edit_file": "edit",
            "move_file": "move",
            "delete_file": "delete",
            "convert_markdown_to_docx": "convert",
            "convert_csv_to_xlsx": "convert",
            "convert_markdown_to_pdf": "convert",
            "convert_html_to_pdf": "convert",
            "convert_html_to_pptx": "convert",
        }
        _done_tool_name = data.get("name", "")
        if _done_tool_name in _workspace_tool_actions:
            _ws_args = data.get("args") or {}
            if isinstance(_ws_args, str):
                try:
                    _ws_args = json.loads(_ws_args)
                except Exception:
                    _ws_args = {}
            _ws_path = _ws_args.get("output_path") or _ws_args.get("destination_path") or _ws_args.get("path", "")
            _ws_result = str(data.get("result") or "")
            _pending_approval = "requires approval" in _ws_result.lower()
            data["workspace_activity"] = {
                "action": _workspace_tool_actions[_done_tool_name],
                "path": _ws_path,
                "tool": _done_tool_name,
                "ok": not _pending_approval,
                "pendingApproval": _pending_approval,
            }
            logger.info(
                "[WS][Workspace] activity path_present={} pending_approval={}",
                bool(_ws_path),
                _pending_approval,
            )

    async def _save_completed_tool_call_to_db(self, data: dict) -> str | None:
        """Persist completed tool calls in ChatMessage DB logs."""
        try:
            from app.services.chat_session_service import save_tool_call_log
            async with async_session() as _tc_db:
                await validate_active_user_chat_lane(
                    _tc_db,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=self.conv_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                message_id = await save_tool_call_log(
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    conversation_id=self.conv_id,
                    tool_name=data.get("name", ""),
                    arguments=data.get("args"),
                    result=(data.get("result") or "")[:500],
                    status="done",
                    tool_call_id=data.get("call_id"),
                    reasoning_content=data.get("reasoning_content"),
                    db=_tc_db,
                )
                if not message_id:
                    return None
                await maybe_mark_session_read_for_active_viewer(
                    _tc_db,
                    agent_id=self.agent_id,
                    session_id=self.conv_id,
                    user_id=self.user.id,
                )
                await _tc_db.commit()
        except Exception as _tc_err:
            logger.warning(
                "[WS] Failed to save tool_call error_type={}",
                type(_tc_err).__name__,
            )
            return None
        return message_id

    async def _update_activity_and_quota(self, assistant_response: str):
        """Update last_active_at, conversation/agent LLM usage, and log activity."""
        try:
            async with async_session() as _db:
                _ar = await _db.execute(select(Agent).where(Agent.id == self.agent_id))
                _agent = _ar.scalar_one_or_none()
                if _agent:
                    _agent.last_active_at = datetime.now(tz.utc)
                    await _db.commit()
        except Exception as e:
            logger.warning(f"[WS] Failed to update last_active_at: {e}")

        try:
            await increment_conversation_usage(self.user.id)
        except Exception:
            pass

        try:
            user_text = getattr(self, "current_user_text", "")
            await log_activity(
                self.agent_id,
                "chat_reply",
                "Replied to web chat",
                detail={
                    "channel": "web",
                    "request_chars": len(user_text),
                    "reply_chars": len(assistant_response),
                },
            )
        except Exception as e:
            logger.warning(f"[WS] Failed to log activity: {e}")

    async def _create_task_record(self, task_title: str, assistant_response: str) -> str:
        """Creates a background execution task from task matching."""
        if not task_title:
            return assistant_response
        try:
            async with async_session() as db:
                await validate_active_user_chat_lane(
                    db,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=self.conv_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                task = Task(
                    agent_id=self.agent_id,
                    title=task_title,
                    created_by=self.user.id,
                    status="pending",
                    priority="medium",
                )
                db.add(task)
                await db.commit()
                await db.refresh(task)
                logger.info(f"[WS] Task created: {task.id}")
            assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
        except Exception as te:
            logger.error(f"[WS] Task creation failed: {te}")
        return assistant_response

    async def _save_assistant_reply(self, assistant_response: str, thinking_content: list[str]) -> str:
        """Saves assistant reply to DB."""
        message_id = uuid.uuid4()
        async with async_session() as db:
            await validate_active_user_chat_lane(
                db,
                agent_id=self.agent_id,
                owner_user_id=self.user.id,
                session_id=self.conv_id,
                lock_authority=True,
                expected_auth_version=self.auth_version,
            )
            assistant_msg = ChatMessage(
                id=message_id,
                agent_id=self.agent_id,
                user_id=self.user.id,
                role="assistant",
                content=assistant_response,
                conversation_id=self.conv_id,
                thinking="".join(thinking_content) if thinking_content else None,
            )
            db.add(assistant_msg)
            await maybe_mark_session_read_for_active_viewer(
                db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user.id,
            )
            await db.commit()
        logger.info("[WS] Assistant message saved")
        return str(message_id)
