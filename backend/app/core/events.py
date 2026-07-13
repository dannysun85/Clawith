"""Redis Pub/Sub events for enterprise info sync."""

import asyncio
import json

import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()

_redis_client: redis.Redis | None = None
_redis_clients_by_loop: dict[int, redis.Redis] = {}


async def get_redis() -> redis.Redis:
    """Get or create the Redis client."""
    global _redis_client
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    client = _redis_clients_by_loop.get(loop_id)
    if client is None:
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        _redis_clients_by_loop[loop_id] = client
        _redis_client = client
    return client


async def publish_event(channel: str, data: dict) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    r = await get_redis()
    await r.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis_client
    clients = list(_redis_clients_by_loop.values())
    _redis_clients_by_loop.clear()
    for client in clients:
        await client.aclose()
    _redis_client = None
