"""Cross-process Take Control lock backed by Redis."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from app.core.events import get_redis


LOCK_PREFIX = "agentbay-take-control"
LOCK_TTL_SECONDS = 600
TOOL_LEASE_PREFIX = "agentbay-tool-execution"
TOOL_LEASE_TTL_SECONDS = 600
TOOL_QUARANTINE_PREFIX = "agentbay-tool-verification"
# Provider SDK calls may continue in a worker thread after asyncio cancellation.
# Keep human control and further tool calls blocked for the maximum supported
# provider-operation window unless the owning coroutine proves normal completion.
TOOL_QUARANTINE_TTL_SECONDS = 1800
INTERACTION_PREFIX = "agentbay-control-interaction"
INTERACTION_TTL_SECONDS = 600
AGENT_DELETION_PREFIX = "agentbay-agent-deletion"

_ACQUIRE_HUMAN_IF_IDLE = """
if redis.call('exists', KEYS[4]) == 1 then
  return -3
end
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return -2
end
local current = redis.call('get', KEYS[1])
if not current then
  redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
local decoded = cjson.decode(current)
if decoded['user_id'] ~= ARGV[1] then
  return -1
end
redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 2
"""

_RELEASE_IF_OWNER = """
local current = redis.call('get', KEYS[1])
if not current then
  return 0
end
local decoded = cjson.decode(current)
if decoded['user_id'] ~= ARGV[1] then
  return -1
end
return redis.call('del', KEYS[1])
"""

_ACQUIRE_TOOL_IF_NO_HUMAN = """
if redis.call('exists', KEYS[4]) == 1 then
  return -2
end
if redis.call('exists', KEYS[1]) == 1 then
  return -1
end
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[2])
redis.call('set', KEYS[3], ARGV[1], 'EX', ARGV[3])
return 1
"""

_REFRESH_TOOL_FENCE = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
if redis.call('get', KEYS[2]) ~= ARGV[1] then
  return 0
end
redis.call('expire', KEYS[1], ARGV[2])
redis.call('expire', KEYS[2], ARGV[3])
return 1
"""

_RELEASE_TOOL_FENCE = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
if redis.call('get', KEYS[2]) ~= ARGV[1] then
  return 0
end
redis.call('del', KEYS[1])
redis.call('del', KEYS[2])
return 1
"""

_REFRESH_TOKEN = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call('expire', KEYS[1], ARGV[2])
return 1
"""

_RELEASE_TOKEN = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
return redis.call('del', KEYS[1])
"""


@dataclass(frozen=True)
class TakeControlLock:
    user_id: str
    env_type: str


class AgentBayToolExecutionActive(RuntimeError):
    pass


class AgentBayTakeControlActive(RuntimeError):
    pass


class AgentBayInteractionBusy(RuntimeError):
    pass


class AgentBayAgentDeleting(RuntimeError):
    pass


def _key(agent_id: uuid.UUID | str, session_id: str) -> str:
    return f"{LOCK_PREFIX}:{_canonical_uuidish(agent_id)}:{_canonical_uuidish(session_id)}"


def _tool_key(agent_id: uuid.UUID | str, session_id: str) -> str:
    return f"{TOOL_LEASE_PREFIX}:{_canonical_uuidish(agent_id)}:{_canonical_uuidish(session_id)}"


def _interaction_key(agent_id: uuid.UUID | str, session_id: str) -> str:
    return f"{INTERACTION_PREFIX}:{_canonical_uuidish(agent_id)}:{_canonical_uuidish(session_id)}"


def _tool_quarantine_key(agent_id: uuid.UUID | str, session_id: str) -> str:
    return f"{TOOL_QUARANTINE_PREFIX}:{_canonical_uuidish(agent_id)}:{_canonical_uuidish(session_id)}"


def agentbay_agent_deletion_key(agent_id: uuid.UUID | str) -> str:
    return f"{AGENT_DELETION_PREFIX}:{_canonical_uuidish(agent_id)}"


def _canonical_uuidish(value: uuid.UUID | str) -> str:
    """Collapse all textual UUID aliases while retaining legacy non-UUID lanes."""

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return str(value)


def _payload(user_id: uuid.UUID | str, env_type: str) -> str:
    return json.dumps(
        {"user_id": _canonical_uuidish(user_id), "env_type": env_type},
        separators=(",", ":"),
        sort_keys=True,
    )


async def get_take_control_lock(
    agent_id: uuid.UUID | str,
    session_id: str,
) -> TakeControlLock | None:
    redis = await get_redis()
    raw = await redis.get(_key(agent_id, session_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        user_id = str(uuid.UUID(str(value["user_id"])))
        env_type = str(value.get("env_type") or "browser")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Take Control lock state is invalid") from exc
    if env_type not in {"browser", "computer", "code"}:
        raise RuntimeError("Take Control lock environment is invalid")
    return TakeControlLock(user_id=user_id, env_type=env_type)


async def acquire_take_control_lock(
    agent_id: uuid.UUID | str,
    session_id: str,
    *,
    user_id: uuid.UUID | str,
    env_type: str,
) -> tuple[bool, TakeControlLock | None]:
    """Atomically acquire or refresh a lock owned by the same user."""

    redis = await get_redis()
    key = _key(agent_id, session_id)
    value = _payload(user_id, env_type)
    acquire_result = int(
        await redis.eval(
            _ACQUIRE_HUMAN_IF_IDLE,
            4,
            key,
            _tool_key(agent_id, session_id),
            _tool_quarantine_key(agent_id, session_id),
            agentbay_agent_deletion_key(agent_id),
            _canonical_uuidish(user_id),
            value,
            str(LOCK_TTL_SECONDS),
        )
    )
    if acquire_result == -3:
        raise AgentBayAgentDeleting("AgentBay Agent deletion is in progress")
    if acquire_result == -2:
        raise AgentBayToolExecutionActive(
            "AgentBay tool execution is still active for this session"
        )
    if acquire_result in {1, 2}:
        return True, TakeControlLock(_canonical_uuidish(user_id), env_type)
    return False, await get_take_control_lock(agent_id, session_id)


async def release_take_control_lock(
    agent_id: uuid.UUID | str,
    session_id: str,
    *,
    user_id: uuid.UUID | str,
) -> bool:
    redis = await get_redis()
    result = await redis.eval(
        _RELEASE_IF_OWNER,
        1,
        _key(agent_id, session_id),
        _canonical_uuidish(user_id),
    )
    if int(result) < 0:
        raise PermissionError("Take Control lock belongs to another user")
    return bool(result)


async def is_take_control_locked(
    agent_id: uuid.UUID | str,
    session_id: str,
) -> bool:
    """Return shared lock state; Redis failures propagate and fail closed."""

    return await get_take_control_lock(agent_id, session_id) is not None


@asynccontextmanager
async def agentbay_tool_execution_lease(
    agent_id: uuid.UUID | str,
    session_id: str,
):
    """Hold a renewable tool lease mutually exclusive with Take Control."""

    redis = await get_redis()
    tool_key = _tool_key(agent_id, session_id)
    quarantine_key = _tool_quarantine_key(agent_id, session_id)
    token = str(uuid.uuid4())
    result = int(
        await redis.eval(
            _ACQUIRE_TOOL_IF_NO_HUMAN,
            4,
            _key(agent_id, session_id),
            tool_key,
            quarantine_key,
            agentbay_agent_deletion_key(agent_id),
            token,
            str(TOOL_LEASE_TTL_SECONDS),
            str(TOOL_QUARANTINE_TTL_SECONDS),
        )
    )
    if result == -2:
        raise AgentBayAgentDeleting("AgentBay Agent deletion is in progress")
    if result < 0:
        raise AgentBayTakeControlActive(
            "AgentBay session is under human control"
        )
    if result == 0:
        raise AgentBayToolExecutionActive(
            "Another AgentBay tool execution is active for this session"
        )

    stop = asyncio.Event()
    fence_lost = asyncio.Event()
    renewal_lost = asyncio.Event()
    owner_task = asyncio.current_task()

    async def _renew() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                return
            except TimeoutError:
                pass
            try:
                refreshed = await redis.eval(
                    _REFRESH_TOOL_FENCE,
                    2,
                    tool_key,
                    quarantine_key,
                    token,
                    str(TOOL_LEASE_TTL_SECONDS),
                    str(TOOL_QUARANTINE_TTL_SECONDS),
                )
                if int(refreshed) == 1:
                    continue
            except Exception:
                pass
            renewal_lost.set()
            fence_lost.set()
            if owner_task is not None and not owner_task.done():
                owner_task.cancel("AgentBay tool lease could not be renewed")
            return

    renewal_task = asyncio.create_task(_renew())
    try:
        try:
            yield
        except asyncio.CancelledError as exc:
            # A provider SDK call may still be running in a worker thread. Keep
            # the quarantine fence for its TTL, but preserve ordinary task
            # cancellation so shutdown and request cancellation cannot be
            # mistaken for a recoverable application error.
            fence_lost.set()
            if renewal_lost.is_set():
                raise AgentBayToolExecutionActive(
                    "AgentBay operation completion requires verification"
                ) from exc
            raise
        except BaseException:
            fence_lost.set()
            raise
    finally:
        stop.set()
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task
        if not fence_lost.is_set():
            await redis.eval(
                _RELEASE_TOOL_FENCE,
                2,
                tool_key,
                quarantine_key,
                token,
            )


@asynccontextmanager
async def agentbay_control_interaction_mutex(
    agent_id: uuid.UUID | str,
    session_id: str,
):
    """Serialize control scripts across API workers for one exact lane."""

    redis = await get_redis()
    key = _interaction_key(agent_id, session_id)
    token = str(uuid.uuid4())
    acquired = False
    for _attempt in range(40):
        acquired = bool(
            await redis.set(
                key,
                token,
                ex=INTERACTION_TTL_SECONDS,
                nx=True,
            )
        )
        if acquired:
            break
        await asyncio.sleep(0.05)
    if not acquired:
        raise AgentBayInteractionBusy(
            "Another Take Control interaction is still running"
        )
    stop = asyncio.Event()
    fence_lost = asyncio.Event()
    renewal_lost = asyncio.Event()
    owner_task = asyncio.current_task()

    async def _renew() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                return
            except TimeoutError:
                pass
            try:
                refreshed = await redis.eval(
                    _REFRESH_TOKEN,
                    1,
                    key,
                    token,
                    str(INTERACTION_TTL_SECONDS),
                )
                if int(refreshed) == 1:
                    continue
            except Exception:
                pass
            renewal_lost.set()
            fence_lost.set()
            if owner_task is not None and not owner_task.done():
                owner_task.cancel("AgentBay interaction fence could not be renewed")
            return

    renewal_task = asyncio.create_task(_renew())
    try:
        try:
            yield
        except asyncio.CancelledError as exc:
            # Retain the mutex until its TTL when the remote interaction may
            # still be in flight, while preserving normal cancellation.
            fence_lost.set()
            if renewal_lost.is_set():
                raise AgentBayInteractionBusy(
                    "AgentBay interaction completion requires verification"
                ) from exc
            raise
        except BaseException:
            fence_lost.set()
            raise
    finally:
        stop.set()
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task
        if not fence_lost.is_set():
            await redis.eval(_RELEASE_TOKEN, 1, key, token)
