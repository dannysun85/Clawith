"""Redis-backed websocket presence and cross-instance message routing."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import WebSocket
from loguru import logger

from app.config import get_settings
from app.core.events import get_redis

settings = get_settings()

PRESENCE_TTL_SECONDS = 180
PUBSUB_PREFIX = "realtime:ws"
PUBSUB_RETRY_MIN_SECONDS = 1.0
PUBSUB_RETRY_MAX_SECONDS = 30.0


class RealtimeDeliveryError(RuntimeError):
    """A known realtime target could not be reached."""


class RealtimeRouter:
    def __init__(self) -> None:
        self.instance_id = settings.INSTANCE_ID
        self._subscriber_task: asyncio.Task | None = None
        self._started = False

    def _connection_key(self, connection_id: str) -> str:
        return f"{PUBSUB_PREFIX}:conn:{connection_id}"

    def _agent_index_key(self, agent_id: str) -> str:
        return f"{PUBSUB_PREFIX}:agent:{agent_id}"

    def _instance_channel(self) -> str:
        return f"{PUBSUB_PREFIX}:instance:{self.instance_id}"

    async def register_connection(
        self,
        *,
        agent_id: str,
        websocket: WebSocket,
        session_id: str | None,
        user_id: str | None,
    ) -> str:
        connection_id = uuid.uuid4().hex
        redis = await get_redis()
        payload = {
            "agent_id": agent_id,
            "session_id": session_id or "",
            "user_id": user_id or "",
            "instance_id": self.instance_id,
        }
        async with redis.pipeline(transaction=True) as pipe:
            pipe.sadd(self._agent_index_key(agent_id), connection_id)
            pipe.hset(self._connection_key(connection_id), mapping=payload)
            pipe.expire(self._connection_key(connection_id), PRESENCE_TTL_SECONDS)
            pipe.expire(self._agent_index_key(agent_id), PRESENCE_TTL_SECONDS)
            await pipe.execute()
        setattr(websocket.state, "realtime_connection_id", connection_id)
        setattr(websocket.state, "realtime_presence_payload", payload)
        return connection_id

    async def refresh_connection(self, *, agent_id: str, websocket: WebSocket) -> bool:
        """Renew presence for long-lived idle sockets used by async workers."""
        connection_id = getattr(websocket.state, "realtime_connection_id", None)
        payload = getattr(websocket.state, "realtime_presence_payload", None)
        if not connection_id or not isinstance(payload, dict):
            return False
        redis = await get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            pipe.sadd(self._agent_index_key(agent_id), connection_id)
            pipe.hset(self._connection_key(connection_id), mapping=payload)
            pipe.expire(self._connection_key(connection_id), PRESENCE_TTL_SECONDS)
            pipe.expire(self._agent_index_key(agent_id), PRESENCE_TTL_SECONDS)
            await pipe.execute()
        return True

    async def unregister_connection(self, *, agent_id: str, websocket: WebSocket) -> None:
        connection_id = getattr(websocket.state, "realtime_connection_id", None)
        if not connection_id:
            return
        redis = await get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            pipe.srem(self._agent_index_key(agent_id), connection_id)
            pipe.delete(self._connection_key(connection_id))
            await pipe.execute()

    async def is_user_viewing_session(self, *, agent_id: str, session_id: str, user_id: str) -> bool:
        for record in await self._list_presence(agent_id):
            if record.get("session_id") == session_id and record.get("user_id") == user_id:
                return True
        return False

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        seen: set[str] = set()
        for record in await self._list_presence(agent_id):
            session_id = (record.get("session_id") or "").strip()
            if session_id:
                seen.add(session_id)
        return list(seen)

    async def route_message(
        self,
        *,
        agent_id: str,
        message: dict,
        local_connections: list[tuple[WebSocket, str | None, str | None]],
        session_id: str | None = None,
        user_id: str | None = None,
        require_target_success: bool = False,
    ) -> bool:
        local_sent = 0
        local_failures = 0
        for ws, local_session_id, local_user_id in list(local_connections):
            if session_id is not None and local_session_id != session_id:
                continue
            if user_id is not None and local_user_id != user_id:
                continue
            try:
                await ws.send_json(message)
                local_sent += 1
            except Exception:
                local_failures += 1

        remote_targets: dict[str, int] = {}
        for record in await self._list_presence(agent_id):
            if record.get("instance_id") == self.instance_id:
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            target_instance = record.get("instance_id")
            if target_instance:
                remote_targets[target_instance] = remote_targets.get(target_instance, 0) + 1

        if not remote_targets:
            if require_target_success and local_failures:
                raise RealtimeDeliveryError("local realtime target delivery failed")
            return bool(local_sent)

        redis = await get_redis()
        envelope = json.dumps(
            {
                "message": message,
                "agent_id": agent_id,
                "session_id": session_id,
                "user_id": user_id,
                "origin_instance_id": self.instance_id,
            }
        )
        publish_tasks = [
            redis.publish(f"{PUBSUB_PREFIX}:instance:{instance_id}", envelope)
            for instance_id in remote_targets
        ]
        publish_results = await asyncio.gather(*publish_tasks, return_exceptions=True)
        remote_sent = 0
        remote_failures = 0
        for result in publish_results:
            if isinstance(result, BaseException) or not isinstance(result, int) or result < 1:
                remote_failures += 1
            else:
                remote_sent += 1
        logger.debug(
            "[Realtime] Routed agent={} local={} remote_instance_count={}",
            agent_id,
            local_sent,
            len(remote_targets),
        )
        if require_target_success and (local_failures or remote_failures):
            raise RealtimeDeliveryError("realtime target delivery failed")
        return bool(local_sent or remote_sent)

    async def start(self, deliver_local) -> None:
        if self._started:
            return
        self._started = True
        self._subscriber_task = asyncio.create_task(self._subscriber_loop(deliver_local), name="realtime-subscriber")

    async def stop(self) -> None:
        if self._subscriber_task:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
            self._subscriber_task = None
        self._started = False

    async def _subscriber_loop(self, deliver_local) -> None:
        retry_delay = PUBSUB_RETRY_MIN_SECONDS
        while True:
            pubsub = None
            try:
                redis = await get_redis()
                pubsub = redis.pubsub()
                await pubsub.subscribe(self._instance_channel())
                retry_delay = PUBSUB_RETRY_MIN_SECONDS

                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message:
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        data = json.loads(message["data"])
                        await deliver_local(
                            agent_id=data["agent_id"],
                            payload=data["message"],
                            session_id=data.get("session_id"),
                            user_id=data.get("user_id"),
                        )
                    except Exception as exc:
                        logger.warning(f"[Realtime] Failed to deliver pubsub message: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[Realtime] Pubsub connection lost: {exc}; retrying in {retry_delay:.1f}s"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, PUBSUB_RETRY_MAX_SECONDS)
            finally:
                if pubsub is not None:
                    # Redis may already be unavailable during orchestrated
                    # shutdown. Cleanup is best effort; the supervisor will
                    # either reconnect or let cancellation finish normally.
                    try:
                        await pubsub.unsubscribe(self._instance_channel())
                    except Exception as exc:
                        logger.debug(f"[Realtime] Pubsub unsubscribe skipped: {exc}")
                    finally:
                        try:
                            await pubsub.aclose()
                        except Exception as exc:
                            logger.debug(f"[Realtime] Pubsub close skipped: {exc}")

    async def _list_presence(self, agent_id: str) -> list[dict[str, str]]:
        redis = await get_redis()
        connection_ids = await redis.smembers(self._agent_index_key(agent_id))
        if not connection_ids:
            return []
        records: list[dict[str, str]] = []
        stale_ids: list[str] = []
        for connection_id in connection_ids:
            data = await redis.hgetall(self._connection_key(connection_id))
            if not data:
                stale_ids.append(connection_id)
                continue
            records.append(data)
        if stale_ids:
            await redis.srem(self._agent_index_key(agent_id), *stale_ids)
        return records


realtime_router = RealtimeRouter()
