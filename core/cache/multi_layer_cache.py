"""
P1-FIX: Multi-Layer Intelligent Cache Architecture
High-performance caching system with TTL, LRU eviction, and multi-layer fallback.

Architecture:
    L1 (Memory) - Fastest, TTL+LRU eviction, in-process cache
    L2 (Redis) - Distributed cache, shared across instances (optional)
    L3 (Disk) - Persistent cache, for restart recovery

Features:
    - Automatic layer fallback (L1 -> L2 -> L3 -> compute)
    - TTL (Time-to-live) for each layer
    - LRU (Least Recently Used) eviction for L1 memory
    - Async operations for Redis/Disk I/O
    - Cache hit/miss metrics for observability
    - Thread-safe operations with double-check locking
"""
import asyncio
import hashlib
import json
import os
import pickle
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger


class CacheLayer:
    """Cache layer identifier."""
    L1_MEMORY = "L1_memory"
    L2_REDIS = "L2_redis"
    L3_DISK = "L3_disk"
    COMPUTE = "compute"


class MultiLayerCache:
    """Multi-layer intelligent cache with TTL and LRU eviction.
    
    P1-FIX: Performance optimization for AI analysis, market data, and SMC analysis.
    
    Example:
        cache = MultiLayerCache(
            l1_max_size=500,
            l1_base_ttl=60,
            l2_enabled=True,
            l2_ttl=300,
            l3_enabled=True,
            l3_ttl=3600,
        )
        
        # Get or compute
        result = await cache.get_or_compute(
            key="btc_long_analysis",
            compute_fn=lambda: ai_analyze_signal(signal, market),
            compute_fn_is_async=True,
        )
    """
    
    def __init__(
        self,
        cache_name: str = "default",
        l1_max_size: int = 500,
        l1_base_ttl: float = 60.0,
        l2_enabled: bool = False,
        l2_redis_url: str = "redis://localhost:6379/0",
        l2_ttl: float = 300.0,
        l3_enabled: bool = True,
        l3_cache_dir: str = "./data/cache",
        l3_ttl: float = 3600.0,
    ):
        """Initialize multi-layer cache.
        
        Args:
            cache_name: Cache instance name (for logging)
            l1_max_size: Maximum entries in L1 memory cache
            l1_base_ttl: Base TTL for L1 cache (seconds)
            l2_enabled: Enable L2 Redis cache
            l2_redis_url: Redis connection URL
            l2_ttl: TTL for L2 Redis cache (seconds)
            l3_enabled: Enable L3 disk cache
            l3_cache_dir: Disk cache directory
            l3_ttl: TTL for L3 disk cache (seconds)
        """
        self.cache_name = cache_name
        
        # L1 Memory Cache (OrderedDict for LRU)
        self._l1_cache: OrderedDict[str, tuple[float, float, Any]] = OrderedDict()
        self._l1_max_size = l1_max_size
        self._l1_base_ttl = l1_base_ttl
        self._l1_lock_init = asyncio.Lock()
        self._l1_lock: Optional[asyncio.Lock] = None
        
        # L2 Redis Cache (optional)
        self._l2_enabled = l2_enabled
        self._l2_redis_url = l2_redis_url
        self._l2_ttl = l2_ttl
        self._l2_client: Optional[Any] = None  # redis.Redis or None
        
        # L3 Disk Cache
        self._l3_enabled = l3_enabled
        self._l3_cache_dir = Path(l3_cache_dir)
        self._l3_ttl = l3_ttl
        self._l3_lock = asyncio.Lock()
        
        # Metrics
        self._metrics = {
            "l1_hits": 0,
            "l1_misses": 0,
            "l2_hits": 0,
            "l2_misses": 0,
            "l3_hits": 0,
            "l3_misses": 0,
            "computes": 0,
            "evictions": 0,
        }
        self._metrics_lock = asyncio.Lock()
        
        # Initialize L3 cache directory
        if self._l3_enabled:
            self._l3_cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[P1-FIX] L3 disk cache initialized: {self._l3_cache_dir}")
        
        logger.info(
            f"[P1-FIX] Multi-layer cache '{cache_name}' initialized: "
            f"L1(max={l1_max_size}, ttl={l1_base_ttl}s), "
            f"L2(enabled={l2_enabled}, ttl={l2_ttl}s), "
            f"L3(enabled={l3_enabled}, ttl={l3_ttl}s)"
        )
    
    async def _get_l1_lock(self) -> asyncio.Lock:
        """P0-FIX: Double-check locking for L1 cache lock initialization."""
        if self._l1_lock is None:
            async with self._l1_lock_init:
                if self._l1_lock is None:
                    self._l1_lock = asyncio.Lock()
        return self._l1_lock
    
    def _hash_key(self, key: str) -> str:
        """Hash cache key for L3 disk storage (avoid long filenames)."""
        return hashlib.sha256(key.encode()).hexdigest()[:16]
    
    async def get(
        self,
        key: str,
        default: Any = None,
    ) -> Any:
        """Get value from cache (try L1 -> L2 -> L3).
        
        Args:
            key: Cache key
            default: Default value if not found
            
        Returns:
            Cached value or default
        """
        now = time.monotonic()
        
        # Try L1 Memory Cache
        lock = await self._get_l1_lock()
        async with lock:
            if key in self._l1_cache:
                timestamp, ttl, value = self._l1_cache[key]
                if now - timestamp < ttl:
                    # Cache hit - move to end (LRU)
                    self._l1_cache.move_to_end(key)
                    await self._record_hit(CacheLayer.L1_MEMORY)
                    logger.debug(f"[P1-FIX] L1 cache hit: {key}")
                    return value
                else:
                    # Expired - remove
                    del self._l1_cache[key]
        
        await self._record_miss(CacheLayer.L1_MEMORY)
        
        # Try L2 Redis Cache
        if self._l2_enabled and self._l2_client:
            try:
                cached_bytes = await asyncio.to_thread(self._l2_client.get, key)
                if cached_bytes:
                    value = pickle.loads(cached_bytes)
                    await self._record_hit(CacheLayer.L2_REDIS)
                    logger.debug(f"[P1-FIX] L2 Redis cache hit: {key}")
                    
                    # Promote to L1
                    await self._set_l1(key, value, self._l1_base_ttl)
                    return value
            except Exception as e:
                logger.warning(f"[P1-FIX] L2 Redis get error: {e}")
        
        await self._record_miss(CacheLayer.L2_REDIS)
        
        # Try L3 Disk Cache
        if self._l3_enabled:
            try:
                cache_file = self._l3_cache_dir / f"{self._hash_key(key)}.cache"
                if cache_file.exists():
                    async with self._l3_lock:
                        with open(cache_file, "rb") as f:
                            data = pickle.load(f)
                        timestamp, ttl, value = data["timestamp"], data["ttl"], data["value"]
                        
                        if now - timestamp < ttl:
                            await self._record_hit(CacheLayer.L3_DISK)
                            logger.debug(f"[P1-FIX] L3 disk cache hit: {key}")
                            
                            # Promote to L1
                            await self._set_l1(key, value, self._l1_base_ttl)
                            return value
                        else:
                            # Expired - delete
                            cache_file.unlink()
            except Exception as e:
                logger.warning(f"[P1-FIX] L3 disk cache get error: {e}")
        
        await self._record_miss(CacheLayer.L3_DISK)
        
        return default
    
    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable,
        compute_fn_is_async: bool = False,
        ttl_override: Optional[float] = None,
    ) -> Any:
        """Get value from cache or compute if not found.
        
        Args:
            key: Cache key
            compute_fn: Function to compute value if not cached
            compute_fn_is_async: Whether compute_fn is async
            ttl_override: Override TTL (optional)
            
        Returns:
            Cached or computed value
        """
        # Try get from cache
        cached = await self.get(key)
        if cached is not None:
            return cached
        
        # Compute value
        await self._record_hit(CacheLayer.COMPUTE)
        logger.debug(f"[P1-FIX] Computing value for key: {key}")
        
        if compute_fn_is_async:
            value = await compute_fn()
        else:
            value = await asyncio.to_thread(compute_fn)
        
        # Store in all layers
        ttl = ttl_override or self._l1_base_ttl
        await self.set(key, value, ttl=ttl)
        
        return value
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
    ) -> None:
        """Set value in all cache layers.
        
        Args:
            key: Cache key
            value: Value to cache
            ttl: TTL override (optional)
        """
        ttl = ttl or self._l1_base_ttl
        now = time.monotonic()
        
        # Set L1 Memory Cache
        await self._set_l1(key, value, ttl)
        
        # Set L2 Redis Cache
        if self._l2_enabled and self._l2_client:
            try:
                ttl_l2 = ttl * 5  # L2 TTL = 5x L1 TTL
                cached_bytes = pickle.dumps(value)
                await asyncio.to_thread(
                    self._l2_client.setex,
                    key,
                    int(ttl_l2),
                    cached_bytes,
                )
                logger.debug(f"[P1-FIX] L2 Redis cache set: {key} (ttl={ttl_l2}s)")
            except Exception as e:
                logger.warning(f"[P1-FIX] L2 Redis set error: {e}")
        
        # Set L3 Disk Cache
        if self._l3_enabled:
            try:
                cache_file = self._l3_cache_dir / f"{self._hash_key(key)}.cache"
                ttl_l3 = ttl * 60  # L3 TTL = 60x L1 TTL
                async with self._l3_lock:
                    with open(cache_file, "wb") as f:
                        pickle.dump({
                            "key": key,
                            "value": value,
                            "timestamp": now,
                            "ttl": ttl_l3,
                        }, f)
                logger.debug(f"[P1-FIX] L3 disk cache set: {key} (ttl={ttl_l3}s)")
            except Exception as e:
                logger.warning(f"[P1-FIX] L3 disk cache set error: {e}")
    
    async def _set_l1(self, key: str, value: Any, ttl: float) -> None:
        """Set value in L1 memory cache with LRU eviction."""
        lock = await self._get_l1_lock()
        async with lock:
            now = time.monotonic()
            
            # Remove if exists (for LRU ordering)
            if key in self._l1_cache:
                del self._l1_cache[key]
            
            # Add to end
            self._l1_cache[key] = (now, ttl, value)
            
            # Evict oldest if exceed max size
            while len(self._l1_cache) > self._l1_max_size:
                oldest_key = next(iter(self._l1_cache))
                del self._l1_cache[oldest_key]
                await self._record_eviction()
                logger.debug(f"[P1-FIX] L1 cache evicted (LRU): {oldest_key}")
    
    async def invalidate(self, key: str) -> None:
        """Invalidate key from all cache layers."""
        # Remove from L1
        lock = await self._get_l1_lock()
        async with lock:
            if key in self._l1_cache:
                del self._l1_cache[key]
        
        # Remove from L2
        if self._l2_enabled and self._l2_client:
            try:
                await asyncio.to_thread(self._l2_client.delete, key)
            except Exception as e:
                logger.warning(f"[P1-FIX] L2 Redis delete error: {e}")
        
        # Remove from L3
        if self._l3_enabled:
            try:
                cache_file = self._l3_cache_dir / f"{self._hash_key(key)}.cache"
                if cache_file.exists():
                    async with self._l3_lock:
                        cache_file.unlink()
            except Exception as e:
                logger.warning(f"[P1-FIX] L3 disk cache delete error: {e}")
        
        logger.debug(f"[P1-FIX] Cache invalidated: {key}")
    
    async def clear_all(self) -> None:
        """Clear all cache layers."""
        # Clear L1
        lock = await self._get_l1_lock()
        async with lock:
            self._l1_cache.clear()
        
        # Clear L2 (flush Redis DB)
        if self._l2_enabled and self._l2_client:
            try:
                await asyncio.to_thread(self._l2_client.flushdb)
            except Exception as e:
                logger.warning(f"[P1-FIX] L2 Redis flush error: {e}")
        
        # Clear L3
        if self._l3_enabled:
            try:
                async with self._l3_lock:
                    for cache_file in self._l3_cache_dir.glob("*.cache"):
                        cache_file.unlink()
            except Exception as e:
                logger.warning(f"[P1-FIX] L3 disk cache clear error: {e}")
        
        logger.info(f"[P1-FIX] All cache layers cleared for '{self.cache_name}'")
    
    async def cleanup_expired(self) -> None:
        """Cleanup expired entries from all layers."""
        now = time.monotonic()
        
        # Cleanup L1
        lock = await self._get_l1_lock()
        async with lock:
            expired_keys = [
                k for k, (ts, ttl, _) in self._l1_cache.items()
                if now - ts > ttl
            ]
            for key in expired_keys:
                del self._l1_cache[key]
                await self._record_eviction()
            if expired_keys:
                logger.debug(f"[P1-FIX] L1 cache cleaned {len(expired_keys)} expired entries")
        
        # L2 Redis handles expiration automatically via TTL
        
        # Cleanup L3
        if self._l3_enabled:
            try:
                async with self._l3_lock:
                    expired_files = []
                    for cache_file in self._l3_cache_dir.glob("*.cache"):
                        try:
                            with open(cache_file, "rb") as f:
                                data = pickle.load(f)
                            if now - data["timestamp"] > data["ttl"]:
                                expired_files.append(cache_file)
                        except Exception:
                            expired_files.append(cache_file)  # Corrupted file
                    
                    for cache_file in expired_files:
                        cache_file.unlink()
                    
                    if expired_files:
                        logger.debug(f"[P1-FIX] L3 disk cache cleaned {len(expired_files)} expired files")
            except Exception as e:
                logger.warning(f"[P1-FIX] L3 disk cache cleanup error: {e}")
    
    async def get_metrics(self) -> dict:
        """Get cache hit/miss metrics."""
        async with self._metrics_lock:
            total_hits = (
                self._metrics["l1_hits"] +
                self._metrics["l2_hits"] +
                self._metrics["l3_hits"]
            )
            total_requests = total_hits + self._metrics["computes"]
            
            hit_rate = (total_hits / total_requests * 100) if total_requests > 0 else 0.0
            
            return {
                "cache_name": self.cache_name,
                "l1_size": len(self._l1_cache),
                "l1_max_size": self._l1_max_size,
                "l1_hits": self._metrics["l1_hits"],
                "l1_misses": self._metrics["l1_misses"],
                "l2_hits": self._metrics["l2_hits"],
                "l2_misses": self._metrics["l2_misses"],
                "l3_hits": self._metrics["l3_hits"],
                "l3_misses": self._metrics["l3_misses"],
                "computes": self._metrics["computes"],
                "evictions": self._metrics["evictions"],
                "total_hits": total_hits,
                "total_requests": total_requests,
                "hit_rate_pct": round(hit_rate, 2),
            }
    
    async def _record_hit(self, layer: str) -> None:
        """Record cache hit."""
        async with self._metrics_lock:
            if layer == CacheLayer.L1_MEMORY:
                self._metrics["l1_hits"] += 1
            elif layer == CacheLayer.L2_REDIS:
                self._metrics["l2_hits"] += 1
            elif layer == CacheLayer.L3_DISK:
                self._metrics["l3_hits"] += 1
            elif layer == CacheLayer.COMPUTE:
                self._metrics["computes"] += 1
    
    async def _record_miss(self, layer: str) -> None:
        """Record cache miss."""
        async with self._metrics_lock:
            if layer == CacheLayer.L1_MEMORY:
                self._metrics["l1_misses"] += 1
            elif layer == CacheLayer.L2_REDIS:
                self._metrics["l2_misses"] += 1
            elif layer == CacheLayer.L3_DISK:
                self._metrics["l3_misses"] += 1
    
    async def _record_eviction(self) -> None:
        """Record cache eviction."""
        async with self._metrics_lock:
            self._metrics["evictions"] += 1
    
    async def initialize_redis(self) -> bool:
        """Initialize Redis client for L2 cache.
        
        Returns:
            bool: True if Redis initialized successfully
        """
        if not self._l2_enabled:
            return False
        
        try:
            import redis
            self._l2_client = redis.from_url(
                self._l2_redis_url,
                decode_responses=False,  # For pickle compatibility
            )
            
            # Test connection
            await asyncio.to_thread(self._l2_client.ping)
            logger.info(f"[P1-FIX] L2 Redis cache connected: {self._l2_redis_url}")
            return True
            
        except ImportError:
            logger.warning("[P1-FIX] Redis library not installed, L2 cache disabled")
            self._l2_enabled = False
            return False
        except Exception as e:
            logger.warning(f"[P1-FIX] Redis connection failed: {e}, L2 cache disabled")
            self._l2_enabled = False
            return False
    
    async def close(self) -> None:
        """Close cache resources (Redis connection)."""
        if self._l2_client:
            try:
                await asyncio.to_thread(self._l2_client.close)
                logger.info(f"[P1-FIX] L2 Redis connection closed for '{self.cache_name}'")
            except Exception as e:
                logger.warning(f"[P1-FIX] Redis close error: {e}")
        
        # Cleanup expired entries before shutdown
        await self.cleanup_expired()


# Global cache instances (initialized lazily)
_AI_ANALYSIS_CACHE: Optional[MultiLayerCache] = None
_MARKET_DATA_CACHE: Optional[MultiLayerCache] = None
_SMC_ANALYSIS_CACHE: Optional[MultiLayerCache] = None


async def get_ai_analysis_cache() -> MultiLayerCache:
    """Get or create AI analysis cache instance."""
    global _AI_ANALYSIS_CACHE
    if _AI_ANALYSIS_CACHE is None:
        from core.config import settings
        
        _AI_ANALYSIS_CACHE = MultiLayerCache(
            cache_name="ai_analysis",
            l1_max_size=500,
            l1_base_ttl=settings.ai.dynamic_cache_ttl_base,
            l2_enabled=settings.redis.enabled,
            l2_redis_url=settings.redis.url,
            l2_ttl=settings.redis.ttl,
            l3_enabled=True,
            l3_cache_dir="./data/cache/ai",
            l3_ttl=3600,
        )
        
        # Initialize Redis if enabled
        if settings.redis.enabled:
            await _AI_ANALYSIS_CACHE.initialize_redis()
    
    return _AI_ANALYSIS_CACHE


async def get_market_data_cache() -> MultiLayerCache:
    """Get or create market data cache instance."""
    global _MARKET_DATA_CACHE
    if _MARKET_DATA_CACHE is None:
        from core.config import settings
        
        _MARKET_DATA_CACHE = MultiLayerCache(
            cache_name="market_data",
            l1_max_size=200,
            l1_base_ttl=30.0,  # Market data expires quickly
            l2_enabled=settings.redis.enabled,
            l2_redis_url=settings.redis.url,
            l2_ttl=60.0,
            l3_enabled=False,  # No disk cache for market data (too volatile)
        )
        
        if settings.redis.enabled:
            await _MARKET_DATA_CACHE.initialize_redis()
    
    return _MARKET_DATA_CACHE


async def get_smc_analysis_cache() -> MultiLayerCache:
    """Get or create SMC analysis cache instance."""
    global _SMC_ANALYSIS_CACHE
    if _SMC_ANALYSIS_CACHE is None:
        from core.config import settings
        
        _SMC_ANALYSIS_CACHE = MultiLayerCache(
            cache_name="smc_analysis",
            l1_max_size=200,
            l1_base_ttl=120.0,  # SMC structure changes slower
            l2_enabled=settings.redis.enabled,
            l2_redis_url=settings.redis.url,
            l2_ttl=600.0,
            l3_enabled=True,
            l3_cache_dir="./data/cache/smc",
            l3_ttl=7200,
        )
        
        if settings.redis.enabled:
            await _SMC_ANALYSIS_CACHE.initialize_redis()
    
    return _SMC_ANALYSIS_CACHE