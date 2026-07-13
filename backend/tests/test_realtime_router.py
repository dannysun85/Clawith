import asyncio

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
