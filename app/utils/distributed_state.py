# savoury-spud-backend/app/utils/distributed_state.py
#
# Distributed lock + shared cache, backed by Upstash Redis over its REST API.
# Carried over unchanged (except the key prefix) from the real-estate
# backend's audit-fixed version. Both fall back to in-process behavior if
# Upstash isn't configured — fine for a single worker, not safe across
# multiple instances. Set UPSTASH_REDIS_REST_URL/TOKEN before this ever
# runs with more than one worker.
#
# Used here for:
#   - per-customer lock while processing a WhatsApp message (so two rapid
#     messages from the same person can't race on the same cart)
#   - catalog cache (menu doesn't need a DB round trip on every message)
#
# Design choice, inherited deliberately: if Redis is configured but briefly
# unreachable, the lock fails OPEN rather than blocking message processing
# indefinitely. A silent bot is worse than the rare double-processing a
# missed lock could allow — the wa_message_id unique constraint and the
# payments.reference unique constraint are the real backstops either way.

import asyncio
import time
import uuid
import httpx

from app.core.config import get_settings
from app.utils.logger import log

settings = get_settings()

_redis_configured = bool(settings.upstash_redis_rest_url and settings.upstash_redis_rest_token)
_warned_fallback = False

_local_locks: dict[str, asyncio.Lock] = {}
_local_cache: dict[str, tuple[float, str]] = {}


def _warn_fallback_once(context: str) -> None:
    global _warned_fallback
    if not _warned_fallback:
        _warned_fallback = True
        import logging
        logging.getLogger("savoury-spud").warning(
            f"[DISTRIBUTED_STATE] Upstash Redis not configured — falling back to "
            f"in-process-only {context}. Fine for a single worker/instance, not safe "
            f"across more than one. Set UPSTASH_REDIS_REST_URL/TOKEN to fix."
        )


async def _redis(*args) -> object:
    url = settings.upstash_redis_rest_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.upstash_redis_rest_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(url, headers=headers, json=list(args))
        response.raise_for_status()
        return response.json().get("result")


class _DistributedLock:
    def __init__(self, key: str, ttl_seconds: float, max_wait_seconds: float):
        self.key = f"spud:lock:{key}"
        self.ttl_ms = int(ttl_seconds * 1000)
        self.max_wait = max_wait_seconds
        self.token = uuid.uuid4().hex
        self._acquired_via_redis = False
        self._local_lock: asyncio.Lock | None = None

    async def __aenter__(self):
        if not _redis_configured:
            _warn_fallback_once("locking")
            self._local_lock = _local_locks.setdefault(self.key, asyncio.Lock())
            await self._local_lock.acquire()
            return self

        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            try:
                result = await _redis("SET", self.key, self.token, "NX", "PX", str(self.ttl_ms))
                if result == "OK":
                    self._acquired_via_redis = True
                    return self
            except Exception as e:
                await log.warn("DISTRIBUTED_LOCK_REDIS_UNAVAILABLE", metadata={"error": str(e)[:150]})
                return self
            await asyncio.sleep(0.15)

        await log.warn("DISTRIBUTED_LOCK_TIMEOUT", metadata={"key": self.key})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._local_lock is not None:
            self._local_lock.release()
            return
        if self._acquired_via_redis:
            try:
                await _redis(
                    "EVAL",
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    "1", self.key, self.token,
                )
            except Exception as e:
                await log.warn("DISTRIBUTED_LOCK_RELEASE_FAILED", metadata={"error": str(e)[:150]})


def distributed_lock(key: str, ttl_seconds: float = 60.0, max_wait_seconds: float = 20.0) -> _DistributedLock:
    return _DistributedLock(key, ttl_seconds, max_wait_seconds)


async def cache_get(key: str) -> str | None:
    full_key = f"spud:cache:{key}"
    if not _redis_configured:
        _warn_fallback_once("caching")
        entry = _local_cache.get(full_key)
        if not entry:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            _local_cache.pop(full_key, None)
            return None
        return value
    try:
        return await _redis("GET", full_key)
    except Exception as e:
        await log.warn("DISTRIBUTED_CACHE_GET_FAILED", metadata={"error": str(e)[:150]})
        return None


async def cache_set(key: str, value: str, ttl_seconds: float) -> None:
    full_key = f"spud:cache:{key}"
    if not _redis_configured:
        _warn_fallback_once("caching")
        _local_cache[full_key] = (time.time() + ttl_seconds, value)
        return
    try:
        await _redis("SET", full_key, value, "EX", str(int(ttl_seconds)))
    except Exception as e:
        await log.warn("DISTRIBUTED_CACHE_SET_FAILED", metadata={"error": str(e)[:150]})


async def cache_delete(key: str) -> None:
    full_key = f"spud:cache:{key}"
    if not _redis_configured:
        _local_cache.pop(full_key, None)
        return
    try:
        await _redis("DEL", full_key)
    except Exception as e:
        await log.warn("DISTRIBUTED_CACHE_DELETE_FAILED", metadata={"error": str(e)[:150]})
