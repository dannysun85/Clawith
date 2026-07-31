"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
from collections import deque
from dataclasses import dataclass
import uuid
from datetime import datetime, timezone as tz
from time import monotonic


from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging_config import get_trace_id, new_trace_id, set_trace_id
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import (
    access_token_matches_identity,
    decode_access_token,
    extract_websocket_access_token,
    websocket_response_subprotocol,
)
from app.database import async_session
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.agent_runtime.adapter import RuntimeCommandIntake
from app.services.agent_runtime.chat_intake import (
    ChatRuntimeIntake,
    ChatRuntimeIntakeError,
    enqueue_chat_runtime,
    onboarding_source_execution_id,
)
from app.services.agent_runtime.chat_stream import (
    ChatRuntimeStreamOutcome,
    stream_web_chat_run,
)
from app.services.agent_runtime.contracts import CancelRunCommand, RunHandle, RuntimeEventCursor
from app.services.agent_runtime.run_state_reader import RunStateReadError, open_run_state_reader
from app.services.chat_session_access import (
    ChatSessionAuthorizationError,
    validate_active_user_chat_lane,
)
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.llm import caller as llm_caller
from app.services.llm.caller import RouteMeta
from app.services.llm.model_resolution import active_agent_model_candidates
from app.services.llm.utils import convert_chat_messages_to_llm_format
from app.services.media_message_content import sanitize_inline_media_content
from app.services.onboarding import is_onboarded, mark_onboarding_phase, resolve_onboarding_prompt
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
    check_agent_llm_quota,
    check_conversation_quota,
    increment_conversation_usage,
)
from app.services.realtime import PRESENCE_TTL_SECONDS, realtime_router

router = APIRouter(tags=["websocket"])


def generic_llm_failure_user_message() -> str:
    """Return a stable chat error without exposing provider or database data."""

    return "[LLM call error] 系统暂时无法完成模型调用，请稍后重试；若持续出现请联系管理员。"


def _client_file_names(data: dict) -> str | list[str]:
    """Prefer the unambiguous attachment array and retain legacy fallback."""

    structured = data.get("file_names")
    if isinstance(structured, list) and all(isinstance(name, str) for name in structured):
        return structured
    legacy = data.get("file_name", "")
    return legacy if isinstance(legacy, str) else ""


@dataclass(frozen=True, slots=True)
class WebChatRuntimeIntake:
    """Runtime intake plus the Web-only onboarding phase notification."""

    run: ChatRuntimeIntake
    onboarding_target_phase: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedWebChatMessage:
    """One client message already persisted as a durable Runtime command."""

    runtime: WebChatRuntimeIntake
    user_content: str
    is_onboarding_trigger: bool = False


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


def _websocket_content_log_summary(content: object) -> str:
    """Return payload-free metadata for one inbound WebSocket message."""
    if not isinstance(content, str):
        return f"content_type={type(content).__name__}"
    image_count = content.count("[image_data:data:image/")
    return f"content_chars={len(content)} image_count={image_count}"


def _runtime_error_packet(
    *,
    code: str,
    message: str,
    agent_id: uuid.UUID,
    stage: str,
    run_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    **legacy: object,
) -> dict:
    """Build the canonical Runtime error context without breaking legacy WS fields."""
    resolved_trace_id = trace_id or get_trace_id() or new_trace_id()
    run_id_text = str(run_id) if run_id is not None else None
    agent_id_text = str(agent_id)
    error = {
        "code": code,
        "message": message,
        "run_id": run_id_text,
        "agent_id": agent_id_text,
        "stage": stage,
        "trace_id": resolved_trace_id,
    }
    return {
        "type": "error",
        "content": message,
        "message": message,
        "code": code,
        "run_id": run_id_text,
        "agent_id": agent_id_text,
        "stage": stage,
        "trace_id": resolved_trace_id,
        "error": error,
        **legacy,
    }


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
        self.auth_version: int | None = None
        self.session_model_tier: str | None = None
        self.session_model_modality: str | None = None
        self.history_messages: list[ChatMessage] = []
        self.conversation: list[dict] = []
        self.current_user_text: str = ""
        self.pending_messages: deque[dict] = deque()
        self._last_auth_fence_at: float = 0.0

    async def run(self):
        """Main entry point for handling the lifecycle of the WebSocket connection."""
        set_trace_id(uuid.uuid4().hex[:12])
        try:
            # 1. Setup session (Authentication, permissions, loading models, history, etc.)
            success = await self.setup()
            if not success:
                return

            # 2. Start the message receiving and processing loop
            await self.message_loop()

        except WebSocketDisconnect:
            logger.info("[WS] Client disconnected agent_id={}", self.agent_id)
            await manager.disconnect(str(self.agent_id), self.websocket)
        except Exception as exc:
            logger.exception(
                "[WS] Unexpected session error agent_id={} error_type={}",
                self.agent_id,
                type(exc).__name__,
            )
            await manager.disconnect(str(self.agent_id), self.websocket)

    async def setup(self) -> bool:
        """Accepts connection, authenticates user, verifies agent access, loads models, resolves session & history."""
        # Accept immediately so browser sees onopen without waiting for DB setup
        await self.websocket.accept(subprotocol=websocket_response_subprotocol(self.websocket))

        # Authenticate
        try:
            payload = decode_access_token(self.token or "")
            user_id = uuid.UUID(payload["sub"])
            token_auth_version = int(payload["av"])
        except Exception:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="authentication_failed",
                    message="Authentication failed",
                    agent_id=self.agent_id,
                    stage="request",
                )
            )
            await self.websocket.close(code=4001)
            return False

        try:
            async with async_session() as db:
                result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.identity)))
                self.user = result.scalar_one_or_none()
                if (
                    not self.user
                    or not self.user.is_active
                    or not self.user.identity
                    or not self.user.identity.is_active
                    or not access_token_matches_identity(payload, self.user.identity)
                ):
                    logger.warning("[WS] Authentication principal unavailable")
                    await self.websocket.send_json({"type": "error", "content": "Account unavailable"})
                    await self.websocket.close(code=4001)
                    return False
                self.auth_version = token_auth_version

                tenant = await db.get(Tenant, self.user.tenant_id) if self.user.tenant_id else None
                if tenant is None or not tenant.is_active:
                    await self.websocket.send_json({"type": "error", "content": "Company unavailable"})
                    await self.websocket.close(code=4003)
                    return False

                logger.info("[WS] Checking agent access agent_id={}", self.agent_id)
                self.agent, _ = await check_agent_access(db, self.user, self.agent_id)
                if (
                    self.agent.tenant_id != self.user.tenant_id
                    or getattr(self.agent, "status", None) in {"stopped", "paused", "error"}
                    or is_agent_expired(self.agent)
                ):
                    await self.websocket.send_json(
                        _runtime_error_packet(
                            code="agent_expired",
                            message="This Agent has expired and is off duty. Please contact your admin to extend its service.",
                            agent_id=self.agent_id,
                            stage="request",
                        )
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
                    "[WS] Agent ready agent_id={} type={} ctx={}",
                    self.agent_id,
                    self.agent_type,
                    self.ctx_size,
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

        except Exception as exc:
            logger.exception(
                "[WS] Setup failed agent_id={} error_type={}",
                self.agent_id,
                type(exc).__name__,
            )
            await self.websocket.send_json({"type": "error", "content": "Setup failed"})
            await self.websocket.close(code=4002)
            return False

        # Connect connection manager
        agent_id_str = str(self.agent_id)
        await manager.connect(agent_id_str, self.websocket, self.conv_id, str(user_id))
        logger.info("[WS] Ready agent_id={}", self.agent_id)

        # Send session_id to frontend
        await self.websocket.send_json({"type": "connected", "session_id": self.conv_id})

        # Build conversation context
        self.conversation = self._build_conversation_context()

        return True

    async def _load_models(self, db: AsyncSession):
        """Loads primary and fallback models for the agent."""
        candidates = await active_agent_model_candidates(db, self.agent)
        self.llm_model = candidates[0] if candidates else None
        self.fallback_llm_model = candidates[1] if len(candidates) > 1 else None

    async def _resolve_chat_session(self, db: AsyncSession, user_id: uuid.UUID) -> str | None:
        """Resolves existing session or creates a new one."""
        if self.agent is None or self.agent.tenant_id is None:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="chat_connection_not_ready",
                    message="Agent chat scope is unavailable",
                    agent_id=self.agent_id,
                    stage="request",
                )
            )
            await self.websocket.close(code=4002)
            return None
        if self.session_id_param is not None:
            try:
                session_id = uuid.UUID(self.session_id_param)
            except (ValueError, TypeError):
                await self.websocket.send_json(
                    _runtime_error_packet(
                        code="invalid_chat_session",
                        message="Invalid chat session",
                        agent_id=self.agent_id,
                        stage="request",
                    )
                )
                await self.websocket.close(code=4002)
                return None
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.tenant_id == self.agent.tenant_id,
                    ChatSession.agent_id == self.agent_id,
                    ChatSession.user_id == user_id,
                    ChatSession.session_type == "direct",
                    ChatSession.group_id.is_(None),
                    ChatSession.source_channel == "web",
                    ChatSession.is_group.is_(False),
                    ChatSession.deleted_at.is_(None),
                )
            )
            existing = result.scalar_one_or_none()
            if (
                existing is None
                or getattr(existing, "tenant_id", None) != self.agent.tenant_id
                or getattr(existing, "agent_id", None) != self.agent_id
                or getattr(existing, "user_id", None) != user_id
                or getattr(existing, "session_type", "direct") != "direct"
                or getattr(existing, "group_id", None) is not None
                or getattr(existing, "source_channel", None) != "web"
                or bool(getattr(existing, "is_group", False))
                or getattr(existing, "deleted_at", None) is not None
            ):
                await self.websocket.send_json(
                    _runtime_error_packet(
                        code="chat_session_scope_mismatch",
                        message="Not authorized for this session",
                        agent_id=self.agent_id,
                        stage="request",
                    )
                )
                await self.websocket.close(code=4002)
                return None
            self.session_model_tier = getattr(existing, "model_tier", None)
            self.session_model_modality = getattr(existing, "model_modality", None) or "text"
            return str(existing.id)

        result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.tenant_id == self.agent.tenant_id,
                ChatSession.agent_id == self.agent_id,
                ChatSession.user_id == user_id,
                ChatSession.source_channel == "web",
                ChatSession.session_type == "direct",
                ChatSession.group_id.is_(None),
                ChatSession.is_group.is_(False),
                ChatSession.deleted_at.is_(None),
                ChatSession.is_primary,
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        if latest:
            self.session_model_tier = getattr(latest, "model_tier", None)
            self.session_model_modality = getattr(latest, "model_modality", None) or "text"
            return str(latest.id)
        new_session = await ensure_primary_platform_session(db, self.agent_id, user_id)
        await db.commit()
        await db.refresh(new_session)
        self.session_model_tier = getattr(new_session, "model_tier", None)
        self.session_model_modality = getattr(new_session, "model_modality", None) or "text"
        logger.info("[WS] Selected primary session session_id={}", new_session.id)
        return str(new_session.id)

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

        pending_runs: deque[AcceptedWebChatMessage] = deque()
        while True:
            if pending_runs:
                if not await self._ensure_access_token_current():
                    return
                accepted = pending_runs.popleft()
            else:
                data = await self._receive_next_message()
                if not await self._ensure_access_token_current():
                    return
                if data.get("type") == "abort":
                    if self.agent_type == "openclaw":
                        continue
                    await self._handle_cancel_packet(data)
                    continue
                if data.get("type") == "attach_run":
                    attached = await self._attach_runtime_run(data)
                    if attached is None:
                        continue
                    outcome, queued_messages = await self._run_runtime_and_stream(
                        attached,
                        user_content="",
                    )
                    pending_runs.extend(queued_messages)
                    if outcome is not None:
                        self.conversation.append({"role": "assistant", "content": outcome.content})
                    continue
                accepted = await self._accept_client_message(data)
                if accepted is None:
                    continue

            outcome, queued_messages = await self._run_runtime_and_stream(
                accepted.runtime.run,
                user_content=accepted.user_content,
            )
            pending_runs.extend(queued_messages)
            if outcome is not None:
                if not accepted.is_onboarding_trigger:
                    self.conversation.append({"role": "user", "content": accepted.user_content})
                self.conversation.append({"role": "assistant", "content": outcome.content})
                if outcome.status == "completed" and accepted.runtime.onboarding_target_phase is not None:
                    await self._mark_onboarding_runtime_phase(accepted.runtime.onboarding_target_phase)
            continue

    async def _receive_next_message(self) -> dict:
        """Return locally queued input before reading the socket again."""
        if self.pending_messages:
            return self.pending_messages.popleft()
        return await self.websocket.receive_json()

    async def _ensure_access_token_current(self) -> bool:
        """Fence mutable chat authority against account or token revocation."""
        if self.user is None or self.conv_id is None:
            return False
        # Unit-level handler construction does not run setup. Production setup
        # always captures the token auth version and therefore always takes the
        # canonical database fence below.
        if self.auth_version is None:
            return True
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
            self._last_auth_fence_at = monotonic()
            return True
        except ChatSessionAuthorizationError:
            await self.websocket.send_json({"type": "error", "content": "Session expired. Please sign in again."})
            await self.websocket.close(code=4001)
            return False

    @staticmethod
    def _event_cursor(value: object) -> RuntimeEventCursor | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or "|" not in value:
            raise ChatRuntimeIntakeError(
                "invalid_event_cursor",
                "attach_run cursor must be '<created_at>|<event_id>'",
            )
        created_at_raw, event_id_raw = value.rsplit("|", 1)
        try:
            created_at = datetime.fromisoformat(created_at_raw)
            event_id = uuid.UUID(event_id_raw)
        except (TypeError, ValueError) as exc:
            raise ChatRuntimeIntakeError(
                "invalid_event_cursor",
                "attach_run cursor is invalid",
            ) from exc
        if created_at.tzinfo is None:
            raise ChatRuntimeIntakeError(
                "invalid_event_cursor",
                "attach_run cursor timestamp must include a timezone",
            )
        return RuntimeEventCursor(created_at, event_id)

    async def _attach_runtime_run(self, data: dict) -> ChatRuntimeIntake | None:
        """Reattach this exact Direct Chat socket to an already-running Run."""
        if self.user is None or self.agent is None or self.conv_id is None:
            return None
        try:
            run_id = self._optional_client_uuid(data.get("run_id"), field="run_id")
            if run_id is None:
                raise ChatRuntimeIntakeError("missing_run_id", "attach_run requires run_id")
            after = self._event_cursor(data.get("cursor"))
            session_id = uuid.UUID(self.conv_id)
        except (ChatRuntimeIntakeError, ValueError) as exc:
            code = getattr(exc, "code", "invalid_chat_session")
            await self.websocket.send_json(
                _runtime_error_packet(
                    code=code,
                    message=str(exc),
                    agent_id=self.agent_id,
                    stage="intake",
                )
            )
            return None

        async with async_session() as db:
            result = await db.execute(
                select(AgentRun).where(
                    AgentRun.tenant_id == self.agent.tenant_id,
                    AgentRun.id == run_id,
                    AgentRun.agent_id == self.agent_id,
                    AgentRun.session_id == session_id,
                    AgentRun.origin_user_id == self.user.id,
                    AgentRun.source_type == "chat",
                    AgentRun.run_kind == "foreground",
                    AgentRun.runtime_type == "langgraph",
                    AgentRun.runtime_thread_id == str(session_id),
                    AgentRun.scheduling_lane_key == f"direct_chat_thread:{self.agent.tenant_id}:{session_id}",
                    AgentRun.lane_held.is_(True),
                )
            )
            run = result.scalar_one_or_none()
            if run is None:
                await self.websocket.send_json(
                    _runtime_error_packet(
                        code="chat_attach_scope_mismatch",
                        message="Run is not active in this Direct Chat session.",
                        agent_id=self.agent_id,
                        stage="intake",
                        run_id=run_id,
                    )
                )
                return None
            command_result = await db.execute(
                select(AgentRunCommand)
                .where(
                    AgentRunCommand.tenant_id == run.tenant_id,
                    AgentRunCommand.run_id == run.id,
                )
                .order_by(AgentRunCommand.created_at.desc(), AgentRunCommand.id.desc())
                .limit(1)
            )
            command = command_result.scalar_one_or_none()
            if command is None:
                await self.websocket.send_json(
                    _runtime_error_packet(
                        code="chat_attach_command_missing",
                        message="Run command is unavailable.",
                        agent_id=self.agent_id,
                        stage="intake",
                        run_id=run_id,
                    )
                )
                return None
        source_id = run.source_id or ""
        try:
            message_id = uuid.UUID(source_id)
        except ValueError:
            message_id = uuid.uuid5(run.id, "attached-chat-message")
        return ChatRuntimeIntake(
            handle=RunHandle(
                tenant_id=run.tenant_id,
                run_id=run.id,
                thread_id=run.runtime_thread_id,
                command_id=command.id,
                runtime_type="langgraph",
                created=False,
            ),
            message_id=message_id,
            resumed=False,
            stream_after=after,
        )

    async def _accept_client_message(
        self,
        data: dict,
    ) -> AcceptedWebChatMessage | None:
        """Validate and durably enqueue one explicit client input."""
        set_trace_id(uuid.uuid4().hex[:12])
        content = data.get("content", "")
        display_content = data.get("display_content", "")
        file_names = _client_file_names(data)
        is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
        logger.info(
            "[WS] Received input content_chars={} onboarding={}",
            len(content) if isinstance(content, str) else 0,
            is_onboarding_trigger,
        )
        if not isinstance(content, str) or (not content and not is_onboarding_trigger):
            return None
        requested_tier = data.get("tier")
        requested_modality = data.get("modality")
        if requested_tier is not None and not isinstance(requested_tier, str):
            await self.websocket.send_json(
                {
                    "type": "error",
                    "content": "tier must be a string",
                    "code": "invalid_model_tier",
                }
            )
            return None
        if requested_modality is not None and not isinstance(requested_modality, str):
            await self.websocket.send_json(
                {
                    "type": "error",
                    "content": "modality must be a string",
                    "code": "invalid_model_modality",
                }
            )
            return None
        requested_tier = (requested_tier or "").strip().lower() or (
            self.session_model_tier or getattr(self.user, "preferred_chat_tier", None)
        )
        requested_modality = (requested_modality or "").strip().lower() or self.session_model_modality or "text"
        ephemeral_modality = data.get("ephemeral_modality") is True
        onboarding_source_execution: str | None = None
        if is_onboarding_trigger:
            onboarding_source_execution = await self._handle_onboarding_trigger_guard()
            if onboarding_source_execution is None:
                return None
            content = "Please begin the onboarding."

        resume_run_id: uuid.UUID | None = None
        try:
            message_id = self._optional_client_uuid(
                data.get("client_message_id", data.get("message_id")),
                field="message_id",
            )
            resume_run_id = self._optional_client_uuid(
                data.get("run_id"),
                field="run_id",
            )
            work_request_id = self._optional_client_uuid(
                data.get("work_request_id"),
                field="work_request_id",
            )
        except ChatRuntimeIntakeError as exc:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code=exc.code,
                    message=str(exc),
                    agent_id=self.agent_id,
                    stage="intake",
                    run_id=resume_run_id,
                )
            )
            return None
        resume_correlation_id = data.get("correlation_id")
        if resume_correlation_id is not None and not isinstance(
            resume_correlation_id,
            str,
        ):
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="invalid_chat_resume_correlation",
                    message="correlation_id must be a string",
                    agent_id=self.agent_id,
                    stage="intake",
                    run_id=resume_run_id,
                )
            )
            return None

        self.current_user_text = content
        try:
            effective_llm_model = await self._resolve_effective_model(
                None,
                tier=requested_tier,
                modality=requested_modality,
            )
        except QuotaExceeded as exc:
            await self.websocket.send_json(
                {
                    "type": "done",
                    "role": "assistant",
                    "content": f"⚠️ {exc.message}",
                    "quota_error": {
                        "quota_type": exc.quota_type,
                        "action": "upgrade",
                    },
                }
            )
            return None
        resolved_tier = self.current_route_meta.saas_tier if self.current_route_meta is not None else requested_tier
        resolved_modality = (
            self.current_route_meta.modality if self.current_route_meta is not None else requested_modality
        )
        if self.current_route_meta is not None:
            await self._persist_session_model_selection(
                resolved_tier,
                None if ephemeral_modality else resolved_modality,
            )
        if not await self._check_quotas(saas_tier=resolved_tier):
            return None
        if self.agent_type == "openclaw":
            if work_request_id is not None:
                await self.websocket.send_json(
                    {
                        "type": "error",
                        "content": "Structured deliverable requests require a native Agent.",
                        "code": "deliverable_runtime_unsupported",
                    }
                )
                return None
            saved_content = sanitize_inline_media_content(
                content,
                display_content=display_content,
                file_names=file_names,
            )
            self.conversation.append({"role": "user", "content": saved_content})
            await self._save_user_message(
                content,
                display_content,
                file_names,
                is_onboarding_trigger,
                message_id=message_id,
            )
            await self._route_openclaw(saved_content)
            return None
        if effective_llm_model is None:
            await self.websocket.send_json(
                {
                    "type": "error",
                    "content": (
                        f"{self.agent_name} has no enabled LLM model configured. "
                        "Select a tier in Agent Settings or ask an administrator "
                        "to configure the company model routes."
                    ),
                    "code": "model_unavailable",
                }
            )
            return None

        try:
            web_intake = await self._enqueue_runtime_chat(
                content=content,
                display_content=display_content,
                file_name=file_names,
                model_id=effective_llm_model.id,
                fallback_model_id=(
                    self.fallback_llm_model.id
                    if self.fallback_llm_model is not None
                    else None
                ),
                saas_tier=resolved_tier,
                model_modality=resolved_modality,
                message_id=message_id,
                resume_run_id=resume_run_id,
                resume_correlation_id=resume_correlation_id,
                work_request_id=work_request_id,
                is_onboarding_trigger=is_onboarding_trigger,
                onboarding_source_execution_id=onboarding_source_execution,
            )
        except ChatRuntimeIntakeError as exc:
            logger.warning("[WS] Runtime chat intake rejected code={}", exc.code)
            await self.websocket.send_json(
                _runtime_error_packet(
                    code=exc.code,
                    message=str(exc),
                    agent_id=self.agent_id,
                    stage="intake",
                    run_id=resume_run_id,
                )
            )
            return None
        except Exception as exc:
            error_code = getattr(exc, "code", "runtime_intake_failed")
            if is_onboarding_trigger and error_code in {
                "source_idempotency_mismatch",
                "command_idempotency_mismatch",
            }:
                # A concurrent socket for the same pair won the durable source
                # identity. Re-read it and acknowledge the stale trigger
                # instead of surfacing a false chat failure.
                await self._handle_onboarding_trigger_guard()
                return None
            logger.exception(
                "[WS] Runtime chat intake failed code={} error_type={}",
                error_code,
                type(exc).__name__,
            )
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="runtime_intake_failed",
                    message="Message could not be accepted by the durable Runtime.",
                    agent_id=self.agent_id,
                    stage="intake",
                    run_id=resume_run_id,
                )
            )
            return None
        if web_intake is None:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="runtime_disabled",
                    message="Durable Runtime is not enabled for native Web Chat.",
                    agent_id=self.agent_id,
                    stage="intake",
                    run_id=resume_run_id,
                )
            )
            return None
        return AcceptedWebChatMessage(
            runtime=web_intake,
            user_content=content,
            is_onboarding_trigger=is_onboarding_trigger,
        )

    @staticmethod
    def _optional_client_uuid(value: object, *, field: str) -> uuid.UUID | None:
        if value is None or value == "":
            return None
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise ChatRuntimeIntakeError(
                f"invalid_{field}",
                f"{field} must be a UUID",
            ) from exc

    async def _enqueue_runtime_chat(
        self,
        *,
        content: str,
        display_content: str,
        file_name: str | list[str],
        model_id: uuid.UUID,
        message_id: uuid.UUID | None,
        resume_run_id: uuid.UUID | None,
        resume_correlation_id: str | None,
        is_onboarding_trigger: bool,
        fallback_model_id: uuid.UUID | None = None,
        saas_tier: str | None = None,
        model_modality: str | None = None,
        work_request_id: uuid.UUID | None = None,
        onboarding_source_execution_id: str | None = None,
    ) -> WebChatRuntimeIntake | None:
        """Revalidate mutable ingress scope and commit one durable input."""
        if self.user is None or self.conv_id is None:
            raise ChatRuntimeIntakeError(
                "chat_connection_not_ready",
                "Web Chat connection has no authenticated session",
            )
        try:
            session_id = uuid.UUID(self.conv_id)
        except ValueError as exc:
            raise ChatRuntimeIntakeError(
                "invalid_chat_session",
                "Web Chat session ID is invalid",
            ) from exc

        async with async_session() as db:
            async with db.begin():
                if self.auth_version is not None:
                    try:
                        lane = await validate_active_user_chat_lane(
                            db,
                            agent_id=self.agent_id,
                            owner_user_id=self.user.id,
                            session_id=session_id,
                            lock_authority=True,
                            expected_auth_version=self.auth_version,
                        )
                    except ChatSessionAuthorizationError as exc:
                        raise ChatRuntimeIntakeError(
                            "chat_authorization_revoked",
                            "Web Chat authorization is no longer active",
                        ) from exc
                    user = lane.owner
                    agent = lane.agent
                    session = lane.session
                else:
                    user = await db.get(User, self.user.id)
                    if user is None or not user.is_active:
                        raise ChatRuntimeIntakeError(
                            "chat_user_unavailable",
                            "Authenticated Chat user is unavailable",
                        )
                    agent, _ = await check_agent_access(db, user, self.agent_id)
                    session = await db.get(ChatSession, session_id)
                model = await db.get(LLMModel, model_id)
                if session is None:
                    raise ChatRuntimeIntakeError(
                        "chat_session_not_found",
                        "Web Chat session no longer exists",
                    )
                if model is None:
                    raise ChatRuntimeIntakeError(
                        "model_unavailable",
                        "Selected Chat model no longer exists",
                    )
                onboarding = (
                    None
                    if resume_run_id is not None
                    else await resolve_onboarding_prompt(
                        db,
                        agent,
                        user.id,
                        user_name=(user.display_name or "").strip() or "there",
                        user_locale=self.lang,
                        allow_greeting_turn=is_onboarding_trigger,
                    )
                )
                target_phase = (
                    onboarding.target_phase if onboarding is not None and onboarding.lock_on_first_chunk else None
                )
                async with open_run_state_reader(db) as run_state_reader:
                    intake = await enqueue_chat_runtime(
                        db,
                        agent=agent,
                        user=user,
                        session=session,
                        model=model,
                        fallback_model_id=fallback_model_id,
                        content=content,
                        display_content=display_content,
                        file_name=file_name,
                        saas_tier=saas_tier,
                        model_modality=model_modality,
                        message_id=message_id,
                        resume_run_id=resume_run_id,
                        resume_correlation_id=resume_correlation_id,
                        work_request_id=work_request_id,
                        runtime_instruction=(onboarding.prompt if onboarding is not None else ""),
                        onboarding_target_phase=target_phase or "",
                        persist_user_message=not is_onboarding_trigger,
                        source_execution_id_override=(
                            onboarding_source_execution_id if is_onboarding_trigger else None
                        ),
                        application_tools_enabled=not (
                            is_onboarding_trigger
                            and onboarding is not None
                            and onboarding.is_greeting_turn
                        ),
                        run_state_reader=run_state_reader,
                    )
                if intake is None:
                    return None
                if is_onboarding_trigger and session.title.startswith("Session "):
                    session.title = "Onboarding"
                return WebChatRuntimeIntake(
                    run=intake,
                    onboarding_target_phase=target_phase,
                )

    async def _cancel_runtime_run(self, run_id: uuid.UUID) -> RunHandle:
        if self.user is None or self.conv_id is None:
            raise ChatRuntimeIntakeError(
                "chat_connection_not_ready",
                "Web Chat connection has no authenticated session",
            )
        try:
            session_id = uuid.UUID(self.conv_id)
        except ValueError as exc:
            raise ChatRuntimeIntakeError(
                "invalid_chat_session",
                "Web Chat session ID is invalid",
            ) from exc
        idempotency_key = f"cancel:web:{run_id}"
        async with async_session() as db:
            async with db.begin():
                user = await db.get(User, self.user.id)
                if user is None or not user.is_active:
                    raise ChatRuntimeIntakeError(
                        "chat_user_unavailable",
                        "Authenticated Chat user is unavailable",
                    )
                agent, _ = await check_agent_access(db, user, self.agent_id)
                session = await db.get(ChatSession, session_id)
                if (
                    session is None
                    or session.tenant_id != agent.tenant_id
                    or session.agent_id != agent.id
                    or session.user_id != user.id
                    or session.session_type != "direct"
                    or session.group_id is not None
                    or session.source_channel != "web"
                    or session.deleted_at is not None
                ):
                    raise ChatRuntimeIntakeError(
                        "chat_cancel_scope_mismatch",
                        "Cancel target is outside this Direct Chat Session",
                    )
                run_result = await db.execute(
                    select(AgentRun).where(
                        AgentRun.tenant_id == agent.tenant_id,
                        AgentRun.id == run_id,
                    )
                )
                run = run_result.scalar_one_or_none()
                if (
                    run is None
                    or run.agent_id != agent.id
                    or run.session_id != session.id
                    or run.origin_user_id != user.id
                    or run.source_type != "chat"
                    or run.run_kind != "foreground"
                    or run.runtime_type != "langgraph"
                    or run.runtime_thread_id != str(session.id)
                    or run.scheduling_lane_key != f"direct_chat_thread:{agent.tenant_id}:{session.id}"
                ):
                    raise ChatRuntimeIntakeError(
                        "chat_cancel_scope_mismatch",
                        "Cancel target is not a Run in this Direct Chat Session",
                    )
                existing_result = await db.execute(
                    select(AgentRunCommand).where(
                        AgentRunCommand.tenant_id == agent.tenant_id,
                        AgentRunCommand.run_id == run.id,
                        AgentRunCommand.command_type == "cancel",
                        AgentRunCommand.idempotency_key == idempotency_key,
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if not run.lane_held and existing is None:
                    raise ChatRuntimeIntakeError(
                        "chat_cancel_not_lane_holder",
                        "Cancel target is no longer the active Direct Chat Run",
                    )
                return await RuntimeCommandIntake(db).cancel_run(
                    CancelRunCommand(
                        tenant_id=agent.tenant_id,
                        run_id=run.id,
                        idempotency_key=idempotency_key,
                        reason="cancelled_by_user",
                        actor_user_id=user.id,
                    )
                )

    async def _handle_cancel_packet(
        self,
        data: dict,
        *,
        expected_run_id: uuid.UUID | None = None,
    ) -> None:
        run_id: uuid.UUID | None = None
        try:
            run_id = self._optional_client_uuid(data.get("run_id"), field="run_id")
            if run_id is None:
                raise ChatRuntimeIntakeError(
                    "missing_cancel_run_id",
                    "Cancellation requires an explicit run_id",
                )
            if expected_run_id is not None and run_id != expected_run_id:
                raise ChatRuntimeIntakeError(
                    "chat_cancel_run_mismatch",
                    "Cancellation does not target the currently attached Run",
                )
            handle = await self._cancel_runtime_run(run_id)
        except ChatRuntimeIntakeError as exc:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code=exc.code,
                    message=str(exc),
                    agent_id=self.agent_id,
                    stage="execution",
                    run_id=run_id,
                )
            )
            return
        except Exception as exc:
            logger.warning(f"[WS] Runtime cancel enqueue failed: {exc}")
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="runtime_cancel_failed",
                    message="Cancellation could not be accepted.",
                    agent_id=self.agent_id,
                    stage="execution",
                    run_id=run_id,
                )
            )
            return
        await self.websocket.send_json(
            {
                "type": "runtime_status",
                "run_id": str(handle.run_id),
                "event": "cancel_requested",
                "status": "cancelling",
            }
        )

    async def _run_runtime_and_stream(
        self,
        intake: ChatRuntimeIntake,
        *,
        user_content: str,
    ) -> tuple[ChatRuntimeStreamOutcome | None, list[AcceptedWebChatMessage]]:
        """Keep the socket responsive while durable work continues off-request."""
        if self.user is None or self.conv_id is None:
            raise ChatRuntimeIntakeError(
                "chat_connection_not_ready",
                "Web Chat connection has no authenticated session",
            )
        session_id = uuid.UUID(self.conv_id)

        async def send_authorized_packet(packet: dict) -> None:
            force_fence = packet.get("type") in {
                "done",
                "error",
                "quota_exceeded",
            }
            if force_fence or monotonic() - self._last_auth_fence_at >= 2.0:
                if not await self._ensure_access_token_current():
                    raise WebSocketDisconnect()
            await self.websocket.send_json(packet)

        stream_task = asyncio.create_task(
            stream_web_chat_run(
                handle=intake.handle,
                session_factory=async_session,
                send_packet=send_authorized_packet,
                agent_id=self.agent_id,
                session_id=session_id,
                user_id=self.user.id,
                after=intake.stream_after,
                trace_id=get_trace_id() or None,
            ),
            name=f"web-chat-runtime-{intake.handle.run_id}",
        )
        queued_messages: list[AcceptedWebChatMessage] = []
        try:
            while not stream_task.done():
                try:
                    message = await asyncio.wait_for(
                        self.websocket.receive_json(),
                        timeout=0.25,
                    )
                except asyncio.TimeoutError:
                    continue
                if not await self._ensure_access_token_current():
                    raise WebSocketDisconnect()
                if message.get("type") == "abort":
                    await self._handle_cancel_packet(
                        message,
                        expected_run_id=intake.handle.run_id,
                    )
                    continue
                accepted = await self._accept_client_message(message)
                if accepted is None:
                    continue
                queued_messages.append(accepted)
                await self.websocket.send_json(
                    {
                        "type": "runtime_status",
                        "run_id": str(accepted.runtime.run.handle.run_id),
                        "event": "queued",
                        "status": "queued",
                    }
                )
            outcome = await stream_task
        except WebSocketDisconnect:
            stream_task.cancel()
            try:
                await stream_task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        except Exception as exc:
            logger.exception(f"[WS] Runtime event stream failed: {exc}")
            if not stream_task.done():
                stream_task.cancel()
                try:
                    await stream_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self.websocket.send_json(
                _runtime_error_packet(
                    code=getattr(exc, "code", "runtime_stream_failed"),
                    message="Runtime execution continues, but its live event stream was interrupted.",
                    agent_id=self.agent_id,
                    stage="stream",
                    run_id=intake.handle.run_id,
                )
            )
            return None, queued_messages

        self.current_user_text = user_content
        await self._update_activity_and_quota(outcome.content)
        async with async_session() as db:
            await maybe_mark_session_read_for_active_viewer(
                db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user.id,
            )
            await db.commit()
        return outcome, queued_messages

    async def _handle_onboarding_trigger_guard(self) -> str | None:
        """Reserve the next pair-scoped onboarding attempt or reject a stale trigger."""
        if self.user is None or self.agent is None or self.agent.tenant_id is None:
            raise ChatRuntimeIntakeError(
                "chat_connection_not_ready",
                "Web Chat connection has no authenticated onboarding scope",
            )
        tenant_id = self.agent.tenant_id
        first_execution_id = onboarding_source_execution_id(
            tenant_id,
            self.agent_id,
            self.user.id,
            attempt=1,
        )
        source_prefix = first_execution_id.rsplit(":", 1)[0]
        async with async_session() as db:
            if await is_onboarded(db, self.agent_id, self.user.id):
                logger.info("[WS] Onboarding trigger ignored — pair already onboarded")
                await self.websocket.send_json(
                    {
                        "type": "onboarded",
                        "agent_id": str(self.agent_id),
                    }
                )
                return None
            result = await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.agent_id == self.agent_id,
                    AgentRun.origin_user_id == self.user.id,
                    AgentRun.source_type == "chat",
                    AgentRun.source_execution_id.like(f"{source_prefix}:%"),
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            )
            runs = list(result.scalars().all())
            attempts: list[tuple[int, AgentRun]] = []
            for run in runs:
                raw_attempt = (run.source_execution_id or "").removeprefix(f"{source_prefix}:")
                if raw_attempt.isdigit() and int(raw_attempt) > 0:
                    attempts.append((int(raw_attempt), run))
            if not attempts:
                return first_execution_id
            attempt, latest = max(attempts, key=lambda item: item[0])
            try:
                async with open_run_state_reader(db) as reader:
                    view = await reader.get_run_state(tenant_id, latest.id)
            except RunStateReadError as exc:
                logger.warning(f"[WS] Onboarding trigger held by unreadable Run {latest.id}: {exc.code}")
                view = None
            except Exception as exc:
                logger.exception(f"[WS] Onboarding trigger held while Run {latest.id} state is unavailable: {exc}")
                view = None

            status = view.execution_status if view is not None else None
            if status in {"failed", "cancelled"}:
                return onboarding_source_execution_id(
                    tenant_id,
                    self.agent_id,
                    self.user.id,
                    attempt=attempt + 1,
                )
            if status == "completed":
                # Completion normally reconciles this row in the worker. Repair
                # the narrow crash window so future mounts also stop triggering.
                await mark_onboarding_phase(
                    db,
                    self.agent_id,
                    self.user.id,
                    "greeted",
                )
                await self.websocket.send_json({"type": "onboarded", "agent_id": str(self.agent_id)})
                return None
            await self.websocket.send_json(
                {
                    "type": "onboarding_pending",
                    "agent_id": str(self.agent_id),
                    "run_id": str(latest.id),
                }
            )
            return None

    async def _mark_onboarding_runtime_phase(self, target_phase: str) -> None:
        """Advance the visible socket immediately; the worker also reconciles it."""
        if self.user is None:
            return
        try:
            async with async_session() as db:
                await mark_onboarding_phase(
                    db,
                    self.agent_id,
                    self.user.id,
                    target_phase,
                )
            await self.websocket.send_json(
                {
                    "type": "onboarded",
                    "agent_id": str(self.agent_id),
                }
            )
        except Exception as exc:
            logger.warning(f"[WS] Runtime onboarding phase update failed: {exc}")

    async def _resolve_route(
        self,
        *,
        tier: str | None,
        modality: str | None,
    ) -> tuple[LLMModel | None, LLMModel | None]:
        """Resolve the current Agent through the unified SaaS route table."""
        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == self.agent_id))
            agent = result.scalar_one_or_none()
        if agent is None:
            self.llm_model = None
            self.fallback_llm_model = None
            self.current_route_meta = None
            return None, None

        primary, fallback, route_meta = await llm_caller.resolve_agent_model(
            agent,
            tier=tier,
            modality=modality,
        )
        self.agent = agent
        self.llm_model = primary
        self.fallback_llm_model = fallback
        self.current_route_meta = route_meta
        return primary, fallback

    async def _persist_session_model_selection(
        self,
        tier: str | None,
        modality: str | None,
    ) -> None:
        """Persist the tier and only a durable, explicitly selected modality."""
        if not tier or self.user is None or self.conv_id is None:
            return
        if self.session_model_tier == tier and (modality is None or self.session_model_modality == modality):
            return
        try:
            session_id = uuid.UUID(self.conv_id)
        except (TypeError, ValueError):
            return

        async with async_session() as db:
            if self.auth_version is not None:
                lane = await validate_active_user_chat_lane(
                    db,
                    agent_id=self.agent_id,
                    owner_user_id=self.user.id,
                    session_id=session_id,
                    lock_authority=True,
                    expected_auth_version=self.auth_version,
                )
                session = lane.session
            else:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.agent_id == self.agent_id,
                        ChatSession.user_id == self.user.id,
                        ChatSession.source_channel == "web",
                        ChatSession.is_group.is_(False),
                    )
                )
                session = result.scalar_one_or_none()
            if session is None:
                return
            session.model_tier = tier
            if modality is not None:
                session.model_modality = modality
            await db.commit()
            self.session_model_tier = tier
            self.session_model_modality = getattr(session, "model_modality", None) or "text"

    async def _resolve_effective_model(
        self,
        override_model_id: str | None,
        *,
        tier: str | None = None,
        modality: str | None = None,
    ) -> LLMModel | None:
        """Compatibility wrapper around tier/modality routing.

        Concrete client-supplied model IDs are intentionally ignored: model
        authorization and billing are controlled by the company route table.
        """
        if override_model_id:
            logger.warning("[WS] Ignored deprecated client model_id override")
        primary, _fallback = await self._resolve_route(
            tier=tier,
            modality=modality,
        )
        return primary

    async def _check_quotas(self, saas_tier: str | None = None) -> bool:
        """Checks conversation and agent LLM quotas. Sends message and returns False if exceeded."""
        try:
            await check_conversation_quota(self.user.id)
            await check_agent_expired(self.agent_id)
            await check_agent_llm_quota(self.agent_id, model_tier=saas_tier)
            return True
        except QuotaExceeded as qe:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="quota_exceeded",
                    message=f"⚠️ {qe.message}",
                    agent_id=self.agent_id,
                    stage="intake",
                    type="done",
                    role="assistant",
                )
            )
            return False
        except AgentExpired as ae:
            await self.websocket.send_json(
                _runtime_error_packet(
                    code="agent_expired",
                    message=f"⚠️ {ae.message}",
                    agent_id=self.agent_id,
                    stage="intake",
                    type="done",
                    role="assistant",
                )
            )
            return False

    async def _save_user_message(
        self,
        content: str,
        display_content: str,
        file_names: str | list[str],
        is_onboarding_trigger: bool,
        *,
        message_id: uuid.UUID | None = None,
    ) -> str | None:
        """Persist the legacy OpenClaw ingress under the same chat fences."""
        if self.user is None or self.conv_id is None:
            return None
        saved_content = sanitize_inline_media_content(
            content,
            display_content=display_content,
            file_names=file_names,
        )
        async with async_session() as db:
            lane = await validate_active_user_chat_lane(
                db,
                agent_id=self.agent_id,
                owner_user_id=self.user.id,
                session_id=self.conv_id,
                lock_authority=True,
                expected_auth_version=self.auth_version,
            )
            if is_onboarding_trigger:
                if lane.session.title.startswith("Session "):
                    lane.session.title = "Onboarding"
                await db.commit()
                return None

            persisted_message_id = message_id or uuid.uuid4()
            db.add(
                ChatMessage(
                    id=persisted_message_id,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    role="user",
                    content=saved_content,
                    conversation_id=self.conv_id,
                )
            )
            lane.session.last_message_at = datetime.now(tz.utc)
            if not self.history_messages and lane.session.title.startswith("Session "):
                lane.session.title = saved_content[:40] or "New message"
            await db.commit()
        logger.info("[WS] User message persisted")
        return str(persisted_message_id)

    async def _save_assistant_reply(
        self,
        assistant_response: str,
        thinking_content: list[str],
    ) -> str:
        """Persist a compatibility-path assistant reply with an exact lane fence."""
        if self.user is None or self.conv_id is None:
            raise ChatSessionAuthorizationError("Chat session is unavailable")
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
            db.add(
                ChatMessage(
                    id=message_id,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    role="assistant",
                    content=assistant_response,
                    conversation_id=self.conv_id,
                    thinking="".join(thinking_content) if thinking_content else None,
                )
            )
            await maybe_mark_session_read_for_active_viewer(
                db,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user.id,
            )
            await db.commit()
        logger.info("[WS] Assistant message persisted")
        return str(message_id)

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

    async def _update_activity_and_quota(self, assistant_response: str):
        """Update activity and message usage after Runtime delivery.

        Per-invocation LLM quota/Credits are already settled by the Runtime LLM
        call boundary and must not be consumed a second time here.
        """
        try:
            async with async_session() as _db:
                _ar = await _db.execute(
                    select(Agent).where(
                        Agent.id == self.agent_id,
                        Agent.deleted_at.is_(None),
                    )
                )
                _agent = _ar.scalar_one_or_none()
                if _agent:
                    _agent.last_active_at = datetime.now(tz.utc)
                    await _db.commit()
        except Exception as exc:
            logger.warning(
                "[WS] Failed to update Agent activity error_type={}",
                type(exc).__name__,
            )

        try:
            await increment_conversation_usage(self.user.id)
        except Exception:
            pass

        try:
            await log_activity(
                self.agent_id,
                "chat_reply",
                "Replied to web chat",
                detail={
                    "channel": "web",
                    "input_chars": len(getattr(self, "current_user_text", "")),
                    "reply_chars": len(assistant_response),
                },
            )
        except Exception as exc:
            logger.warning(
                "[WS] Failed to log activity error_type={}",
                type(exc).__name__,
            )
