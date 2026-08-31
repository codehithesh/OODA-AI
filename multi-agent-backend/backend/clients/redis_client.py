"""Redis client wrapper — caching and pub/sub only (never Celery).

Every method degrades gracefully: if Redis is unavailable the caller gets a
warning and a sensible default instead of an exception, so core agent flows
never depend on Redis being up.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
import structlog

from config import get_settings

logger = structlog.get_logger(__name__)


class RedisClient:
    """Thin async wrapper around redis-py's asyncio client."""

    def __init__(self, url: str) -> None:
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:
            logger.warning("redis_ping_failed", error=str(exc))
            return False

    async def set_json(self, key: str, value: Any, ttl_s: int = 86400) -> bool:
        try:
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl_s)
            return True
        except Exception as exc:
            logger.warning("redis_set_failed", key=key, error=str(exc))
            return False

    async def get_json(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("redis_get_failed", key=key, error=str(exc))
            return None

    async def publish(self, channel: str, event: dict[str, Any]) -> bool:
        """Publish an agent lifecycle event for subscribers (dashboards, n8n)."""
        try:
            await self._redis.publish(channel, json.dumps(event, default=str))
            return True
        except Exception as exc:
            logger.warning("redis_publish_failed", channel=channel, error=str(exc))
            return False

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception as exc:
            logger.warning("redis_close_failed", error=str(exc))


_default_client: RedisClient | None = None


def get_redis_client() -> RedisClient:
    """Process-wide Redis client singleton."""
    global _default_client
    if _default_client is None:
        _default_client = RedisClient(get_settings().redis_url)
    return _default_client
