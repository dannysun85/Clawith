import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.realtime_runtime import router as router_module


class _DisconnectingPubSub:
    def __init__(self) -> None:
        self.subscribed = asyncio.Event()
        self.closed = False

    async def subscribe(self, _channel: str) -> None:
        self.subscribed.set()

    async def get_message(self, **_kwargs):
        await asyncio.Event().wait()

    async def unsubscribe(self, _channel: str) -> None:
        raise ConnectionError("Redis stopped before the application")

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, pubsub: _DisconnectingPubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _DisconnectingPubSub:
        return self._pubsub


class _TransientFailurePubSub(_DisconnectingPubSub):
    async def get_message(self, **_kwargs):
        raise ConnectionError("temporary Redis disconnect")

    async def unsubscribe(self, _channel: str) -> None:
        return None


class _SequentialRedis:
    def __init__(self, *pubsubs: _DisconnectingPubSub) -> None:
        self._pubsubs = list(pubsubs)

    def pubsub(self) -> _DisconnectingPubSub:
        return self._pubsubs.pop(0)


class _RecordingPipeline:
    def __init__(self) -> None:
        self.operations: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return self

        return record

    async def execute(self):
        return []


class _PresenceRedis:
    def __init__(self) -> None:
        self.pipelines: list[_RecordingPipeline] = []

    def pipeline(self, **_kwargs):
        pipeline = _RecordingPipeline()
        self.pipelines.append(pipeline)
        return pipeline


class _PublishingRedis:
    def __init__(self, result: int | BaseException) -> None:
        self.result = result

    async def publish(self, _channel: str, _payload: str) -> int:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_stop_tolerates_redis_disappearing_during_pubsub_cleanup(monkeypatch):
    pubsub = _DisconnectingPubSub()

    async def fake_get_redis():
        return _FakeRedis(pubsub)

    monkeypatch.setattr(router_module, "get_redis", fake_get_redis)
    realtime_router = router_module.RealtimeRouter()

    async def deliver_local(**_kwargs) -> None:
        return None

    await realtime_router.start(deliver_local)
    await asyncio.wait_for(pubsub.subscribed.wait(), timeout=1)

    await realtime_router.stop()

    assert pubsub.closed is True
    assert realtime_router._subscriber_task is None
    assert realtime_router._started is False


@pytest.mark.asyncio
async def test_subscriber_reconnects_after_transient_redis_failure(monkeypatch):
    first_pubsub = _TransientFailurePubSub()
    recovered_pubsub = _DisconnectingPubSub()
    redis = _SequentialRedis(first_pubsub, recovered_pubsub)

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(router_module, "get_redis", fake_get_redis)
    monkeypatch.setattr(router_module, "PUBSUB_RETRY_MIN_SECONDS", 0.001)
    realtime_router = router_module.RealtimeRouter()

    async def deliver_local(**_kwargs) -> None:
        return None

    await realtime_router.start(deliver_local)
    await asyncio.wait_for(recovered_pubsub.subscribed.wait(), timeout=1)

    assert first_pubsub.closed is True
    await realtime_router.stop()


@pytest.mark.asyncio
async def test_presence_refresh_renews_connection_and_agent_ttls(monkeypatch):
    redis = _PresenceRedis()

    async def fake_get_redis():
        return redis

    websocket = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(router_module, "get_redis", fake_get_redis)
    realtime_router = router_module.RealtimeRouter()

    connection_id = await realtime_router.register_connection(
        agent_id="agent-1",
        websocket=websocket,
        session_id="session-1",
        user_id="user-1",
    )
    refreshed = await realtime_router.refresh_connection(
        agent_id="agent-1",
        websocket=websocket,
    )

    assert refreshed is True
    assert websocket.state.realtime_connection_id == connection_id
    refresh_operations = redis.pipelines[1].operations
    assert [name for name, _args, _kwargs in refresh_operations].count("expire") == 2
    assert any(name == "hset" for name, _args, _kwargs in refresh_operations)
    assert any(name == "sadd" for name, _args, _kwargs in refresh_operations)


@pytest.mark.asyncio
async def test_realtime_route_targets_exact_session_and_user(monkeypatch):
    exact = SimpleNamespace(send_json=AsyncMock())
    wrong_session = SimpleNamespace(send_json=AsyncMock())
    wrong_user = SimpleNamespace(send_json=AsyncMock())
    realtime_router = router_module.RealtimeRouter()
    monkeypatch.setattr(realtime_router, "_list_presence", AsyncMock(return_value=[]))
    payload = {"type": "media_generation_result"}

    await realtime_router.route_message(
        agent_id="agent-1",
        message=payload,
        local_connections=[
            (exact, "session-1", "user-1"),
            (wrong_session, "session-2", "user-1"),
            (wrong_user, "session-1", "user-2"),
        ],
        session_id="session-1",
        user_id="user-1",
    )

    exact.send_json.assert_awaited_once_with(payload)
    wrong_session.send_json.assert_not_awaited()
    wrong_user.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_realtime_route_raises_when_local_target_send_fails(monkeypatch):
    failed = SimpleNamespace(
        send_json=AsyncMock(side_effect=ConnectionError("socket closed")),
    )
    realtime_router = router_module.RealtimeRouter()
    monkeypatch.setattr(realtime_router, "_list_presence", AsyncMock(return_value=[]))

    with pytest.raises(router_module.RealtimeDeliveryError):
        await realtime_router.route_message(
            agent_id="agent-1",
            message={"type": "media_generation_result"},
            local_connections=[(failed, "session-1", "user-1")],
            session_id="session-1",
            user_id="user-1",
            require_target_success=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publish_result",
    [0, ConnectionError("Redis publish failed")],
    ids=["no-subscriber", "publish-error"],
)
async def test_strict_realtime_route_raises_when_remote_publish_fails(
    monkeypatch,
    publish_result,
):
    async def fake_get_redis():
        return _PublishingRedis(publish_result)

    realtime_router = router_module.RealtimeRouter()
    monkeypatch.setattr(router_module, "get_redis", fake_get_redis)
    monkeypatch.setattr(
        realtime_router,
        "_list_presence",
        AsyncMock(
            return_value=[
                {
                    "instance_id": "remote-instance",
                    "session_id": "session-1",
                    "user_id": "user-1",
                }
            ]
        ),
    )

    with pytest.raises(router_module.RealtimeDeliveryError):
        await realtime_router.route_message(
            agent_id="agent-1",
            message={"type": "media_generation_result"},
            local_connections=[],
            session_id="session-1",
            user_id="user-1",
            require_target_success=True,
        )


@pytest.mark.asyncio
async def test_strict_realtime_route_allows_no_online_target(monkeypatch):
    realtime_router = router_module.RealtimeRouter()
    monkeypatch.setattr(realtime_router, "_list_presence", AsyncMock(return_value=[]))

    delivered = await realtime_router.route_message(
        agent_id="agent-1",
        message={"type": "media_generation_result"},
        local_connections=[],
        session_id="session-1",
        user_id="user-1",
        require_target_success=True,
    )

    assert delivered is False
