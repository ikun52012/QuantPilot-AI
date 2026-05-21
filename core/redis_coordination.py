"""
Redis-backed coordination primitives for distributed workers.

Keeps hot trading state and cross-process locks out of a single Python
process while falling back to local locks if Redis is disabled/unavailable.
"""
import asyncio
import inspect
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger

from core.config import settings

KEY_PREFIX = "quantpilot"
_CLIENT: Any | None = None
_CLIENT_LOCK = asyncio.Lock()
_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}
_LOCAL_LOCKS_GUARD = asyncio.Lock()
_REDIS_UNAVAILABLE_LOGGED = False

_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class DistributedLockTimeout(TimeoutError):
    """Raised when a distributed lock cannot be acquired before timeout."""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def make_key(*parts: object) -> str:
    """Build a namespaced Redis key."""
    clean_parts = [str(part).strip(":") for part in parts if str(part or "").strip(":")]
    return ":".join([KEY_PREFIX, *clean_parts])


async def _get_client() -> Any | None:
    """Return a shared async Redis client if Redis is enabled and reachable."""
    global _CLIENT, _REDIS_UNAVAILABLE_LOGGED
    if not settings.redis.enabled or not settings.redis.url:
        return None

    if _CLIENT is not None:
        return _CLIENT

    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis.url, decode_responses=False)
            await _maybe_await(client.ping())
            _CLIENT = client
            logger.info("[RedisCoordination] Connected to Redis")
            return _CLIENT
        except Exception as exc:
            if not _REDIS_UNAVAILABLE_LOGGED:
                logger.warning(f"[RedisCoordination] Redis unavailable, using local process fallback: {exc}")
                _REDIS_UNAVAILABLE_LOGGED = True
            _CLIENT = None
            return None


async def _local_lock(name: str) -> asyncio.Lock:
    async with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(name)
        if lock is None:
            lock = asyncio.Lock()
            _LOCAL_LOCKS[name] = lock
        return lock


@asynccontextmanager
async def distributed_lock(
    name: str,
    *,
    ttl_seconds: int = 30,
    blocking_timeout_seconds: float = 10.0,
    retry_interval_seconds: float = 0.05,
) -> AsyncIterator[None]:
    """Acquire a Redis SETNX lock, with local fallback when Redis is absent."""
    lock_key = make_key("lock", name)
    token = uuid.uuid4().hex
    client = await _get_client()

    if client is None:
        lock = await _local_lock(lock_key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=blocking_timeout_seconds)
        except TimeoutError as exc:
            raise DistributedLockTimeout(f"Timed out acquiring local lock {lock_key}") from exc
        try:
            yield
        finally:
            lock.release()
        return

    deadline = time.monotonic() + max(blocking_timeout_seconds, 0.0)
    acquired = False
    while True:
        try:
            acquired = bool(await _maybe_await(client.set(lock_key, token, nx=True, ex=ttl_seconds)))
        except Exception as exc:
            logger.warning(f"[RedisCoordination] Redis lock acquire failed for {lock_key}: {exc}")
            raise

        if acquired:
            break
        if time.monotonic() >= deadline:
            raise DistributedLockTimeout(f"Timed out acquiring Redis lock {lock_key}")
        await asyncio.sleep(retry_interval_seconds)

    try:
        yield
    finally:
        try:
            await _maybe_await(client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token))
        except Exception:
            try:
                current = await _maybe_await(client.get(lock_key))
                if current is not None and _decode(current) == token:
                    await _maybe_await(client.delete(lock_key))
            except Exception as exc:
                logger.warning(f"[RedisCoordination] Redis lock release failed for {lock_key}: {exc}")


async def redis_get_json(key: str) -> Any | None:
    client = await _get_client()
    if client is None:
        return None
    try:
        raw = await _maybe_await(client.get(key))
        if raw is None:
            return None
        return json.loads(_decode(raw))
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis get error for {key}: {exc}")
        return None


async def redis_set_json(key: str, value: Any, *, ttl_seconds: int | None = None) -> bool:
    client = await _get_client()
    if client is None:
        return False
    payload = json.dumps(value, ensure_ascii=False, default=str)
    try:
        if ttl_seconds and ttl_seconds > 0:
            await _maybe_await(client.setex(key, int(ttl_seconds), payload))
        else:
            await _maybe_await(client.set(key, payload))
        return True
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis set error for {key}: {exc}")
        return False


async def redis_delete(key: str) -> bool:
    client = await _get_client()
    if client is None:
        return False
    try:
        await _maybe_await(client.delete(key))
        return True
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis delete error for {key}: {exc}")
        return False


async def redis_hget_json(hash_key: str, field: str) -> Any | None:
    client = await _get_client()
    if client is None:
        return None
    try:
        raw = await _maybe_await(client.hget(hash_key, field))
        if raw is None:
            return None
        return json.loads(_decode(raw))
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis hget error for {hash_key}/{field}: {exc}")
        return None


async def redis_hset_json(hash_key: str, field: str, value: Any) -> bool:
    client = await _get_client()
    if client is None:
        return False
    payload = json.dumps(value, ensure_ascii=False, default=str)
    try:
        await _maybe_await(client.hset(hash_key, field, payload))
        return True
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis hset error for {hash_key}/{field}: {exc}")
        return False


async def redis_hdel(hash_key: str, field: str) -> bool:
    client = await _get_client()
    if client is None:
        return False
    try:
        await _maybe_await(client.hdel(hash_key, field))
        return True
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis hdel error for {hash_key}/{field}: {exc}")
        return False


async def redis_hgetall_json(hash_key: str) -> dict[str, Any]:
    client = await _get_client()
    if client is None:
        return {}
    try:
        raw_map = await _maybe_await(client.hgetall(hash_key))
        result: dict[str, Any] = {}
        for raw_field, raw_value in dict(raw_map or {}).items():
            try:
                result[_decode(raw_field)] = json.loads(_decode(raw_value))
            except (TypeError, json.JSONDecodeError):
                continue
        return result
    except Exception as exc:
        logger.debug(f"[RedisCoordination] Redis hgetall error for {hash_key}: {exc}")
        return {}
