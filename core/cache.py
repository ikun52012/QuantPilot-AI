"""
Signal Server - Cache Layer
Redis-based caching with fallback to in-memory cache.
"""
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from typing import Any

from loguru import logger

from core.config import settings

# ─────────────────────────────────────────────
# In-Memory Cache (Fallback)
# ─────────────────────────────────────────────

class InMemoryCache:
    """Thread-safe in-memory LRU cache with TTL."""

    def __init__(self, max_size: int = 1000, default_ttl: int = 300):
        self._cache: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            timestamp, ttl, value = entry
            if time.monotonic() - timestamp > ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: int | None = None):
        with self._lock:
            effective_ttl = ttl if ttl is not None else self._default_ttl
            self._cache[key] = (time.monotonic(), effective_ttl, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def delete(self, key: str):
        """Delete a value from cache."""
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> dict:
        """Get cache statistics."""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "default_ttl": self._default_ttl,
            }


# ─────────────────────────────────────────────
# Redis Cache
# ─────────────────────────────────────────────

class RedisCache:
    """Redis-based cache with async support."""

    def __init__(self, url: str, default_ttl: int = 300):
        self._url = url
        self._default_ttl = default_ttl
        self._client = None
        self._connected = False

    async def _get_client(self):
        """Get or create Redis client."""
        if self._client is None:
            try:
                import redis.asyncio as redis
                self._client = redis.from_url(self._url)
                # Test connection
                await self._client.ping()
                self._connected = True
                logger.info("[Cache] Connected to Redis")
            except Exception as e:
                logger.warning(f"[Cache] Redis connection failed: {e}")
                self._connected = False
                return None
        return self._client

    async def get(self, key: str) -> Any | None:
        """Get a value from cache."""
        client = await self._get_client()
        if client is None:
            return None
        try:
            value = await client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except Exception as e:
            logger.debug(f"[Cache] Redis get error: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None):
        """Set a value in cache."""
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.setex(
                key,
                ttl or self._default_ttl,
                json.dumps(value, default=str)
            )
        except Exception as e:
            logger.debug(f"[Cache] Redis set error: {e}")

    async def delete(self, key: str):
        """Delete a value from cache."""
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.delete(key)
        except Exception as e:
            logger.debug(f"[Cache] Redis delete error: {e}")

    async def clear(self):
        """Clear all cache entries."""
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.flushdb()
        except Exception as e:
            logger.debug(f"[Cache] Redis clear error: {e}")

    def is_connected(self) -> bool:
        """Check if Redis is connected."""
        return self._connected


# ─────────────────────────────────────────────
# Cache Manager
# ─────────────────────────────────────────────

class CacheManager:
    """Unified cache manager with Redis fallback."""

    def __init__(self):
        self._redis: RedisCache | None = None
        self._memory: InMemoryCache = InMemoryCache()
        self._initialized = False
        self._init_lock = threading.Lock()

    def init(self):
        """Initialize cache manager (non-blocking, Redis connection is lazy)."""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            if settings.redis.enabled and settings.redis.url:
                self._redis = RedisCache(settings.redis.url, settings.redis.ttl)
                logger.info("[Cache] Redis cache configured (connection will be established on first use)")
            else:
                logger.info("[Cache] Using in-memory cache")

            self._initialized = True

    async def init_async(self):
        """Initialize cache manager and establish Redis connection."""
        if self._initialized:
            if self._redis:
                await self._redis._get_client()
            return

        with self._init_lock:
            if self._initialized:
                return

            if settings.redis.enabled and settings.redis.url:
                self._redis = RedisCache(settings.redis.url, settings.redis.ttl)
                client = await self._redis._get_client()
                if client:
                    logger.info("[Cache] Connected to Redis")
                else:
                    logger.warning("[Cache] Redis connection failed, falling back to in-memory")
            else:
                logger.info("[Cache] Using in-memory cache")

            self._initialized = True

    async def get(self, key: str) -> Any | None:
        """Get a value from cache."""
        if self._redis:
            value = await self._redis.get(key)
            if value is not None:
                return value
        return self._memory.get(key)

    async def set(self, key: str, value: Any, ttl: int | None = None):
        """Set a value in cache."""
        if self._redis:
            await self._redis.set(key, value, ttl)
        self._memory.set(key, value, ttl)

    async def delete(self, key: str):
        """Delete a value from cache."""
        if self._redis:
            await self._redis.delete(key)
        self._memory.delete(key)

    async def clear(self):
        """Clear all cache entries."""
        if self._redis:
            await self._redis.clear()
        self._memory.clear()

    def get_stats(self) -> dict:
        """Get cache statistics."""
        stats = self._memory.get_stats()
        if self._redis:
            stats["redis_connected"] = self._redis.is_connected()
        return stats


# Global cache manager
cache = CacheManager()


# ─────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────

def cached(ttl: int = 300, key_prefix: str = ""):
    """
    Decorator to cache function results.
    Only works for async functions with hashable arguments.
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Build cache key
            key_parts = [key_prefix or func.__name__]
            key_parts.extend(str(arg) for arg in args if not hasattr(arg, '__dict__'))
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()) if not hasattr(v, '__dict__'))
            cache_key = ":".join(key_parts)

            # Try cache
            cached_value = await cache.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Execute function
            result = await func(*args, **kwargs)

            # Cache result
            await cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator
