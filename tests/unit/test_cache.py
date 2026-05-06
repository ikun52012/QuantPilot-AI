"""
P4-FIX: Unit Tests for Multi-Layer Cache
Tests for L1, L2, L3 cache operations and metrics.
"""
import asyncio

import pytest

from core.cache.multi_layer_cache import MultiLayerCache


@pytest.mark.asyncio
class TestMultiLayerCache:
    """Test suite for multi-layer cache."""

    @pytest.fixture
    async def cache(self, temp_cache_dir):
        """Create test cache instance."""
        cache = MultiLayerCache(
            cache_name="test_cache",
            l1_max_size=10,
            l1_base_ttl=60.0,
            l2_enabled=False,  # Disable Redis for unit tests
            l3_enabled=True,
            l3_cache_dir=str(temp_cache_dir),
            l3_ttl=300.0,
        )
        yield cache
        await cache.close()

    async def test_l1_cache_set_and_get(self, cache):
        """Test L1 cache basic set/get operations."""
        await cache.set("test_key", {"value": 123}, ttl=60)

        result = await cache.get("test_key")

        assert result is not None
        assert result["value"] == 123

        metrics = await cache.get_metrics()
        assert metrics["l1_hits"] >= 1

    async def test_l1_cache_ttl_expiration(self, temp_cache_dir):
        """Test L1 cache TTL expiration."""
        cache = MultiLayerCache(
            cache_name="test_ttl_cache",
            l1_max_size=10,
            l1_base_ttl=60.0,
            l2_enabled=False,
            l3_enabled=False,  # Disable L3 to test L1 TTL only
            l3_cache_dir=str(temp_cache_dir),
            l3_ttl=300.0,
        )
        await cache.set("expire_key", "value", ttl=0.5)  # 0.5 second TTL

        # Immediate get should succeed
        result1 = await cache.get("expire_key")
        assert result1 == "value"

        # Wait for expiration
        await asyncio.sleep(0.6)

        # Should be expired now (L3 disabled, so None expected)
        result2 = await cache.get("expire_key")
        assert result2 is None

        metrics = await cache.get_metrics()
        assert metrics["l1_misses"] >= 1
        await cache.close()

    async def test_l1_cache_lru_eviction(self, temp_cache_dir):
        """Test L1 cache LRU eviction when max size exceeded."""
        cache = MultiLayerCache(
            cache_name="test_lru_cache",
            l1_max_size=10,
            l1_base_ttl=60.0,
            l2_enabled=False,
            l3_enabled=False,  # Disable L3 to test L1 LRU eviction only
            l3_cache_dir=str(temp_cache_dir),
            l3_ttl=300.0,
        )
        # Set more items than max_size (10)
        for i in range(15):
            await cache.set(f"key_{i}", f"value_{i}", ttl=60)

        # Check cache size is limited
        metrics = await cache.get_metrics()
        assert metrics["l1_size"] <= cache._l1_max_size

        # First items should be evicted (LRU)
        result = await cache.get("key_0")
        assert result is None  # Evicted (L3 disabled, so None expected)

        # Last items should still be present
        result = await cache.get("key_14")
        assert result == "value_14"
        await cache.close()

    async def test_cache_hit_rate_calculation(self, cache):
        """Test cache hit rate calculation."""
        # Generate hits and misses
        await cache.set("hit_key", "value", ttl=60)

        for _ in range(10):
            await cache.get("hit_key")  # 10 hits

        # Check metrics for hits only (misses may not be counted)
        metrics = await cache.get_metrics()

        assert metrics["total_hits"] >= 10
        # Only check total_requests if it's tracked
        if "total_requests" in metrics:
            assert metrics["total_requests"] >= 10

        hit_rate = metrics["hit_rate_pct"]
        # Hit rate should be high since all hits
        assert hit_rate >= 90.0


@pytest.mark.asyncio
class TestCacheDoubleCheckLock:
    """Test double-check locking pattern for cache locks."""

    async def test_lock_initialization_thread_safe(self):
        """Test lock initialization is thread-safe."""
        from core.cache.multi_layer_cache import MultiLayerCache

        cache = MultiLayerCache(cache_name="lock_test")

        # Concurrent lock initialization
        tasks = [cache._get_l1_lock() for _ in range(10)]
        locks = await asyncio.gather(*tasks)

        # All should be the same lock instance
        assert all(lock == locks[0] for lock in locks)
        assert locks[0] is not None

    async def test_lock_singleton_behavior(self):
        """Test lock is singleton (only created once)."""
        from core.cache.multi_layer_cache import MultiLayerCache

        cache = MultiLayerCache(cache_name="singleton_test")

        lock1 = await cache._get_l1_lock()
        lock2 = await cache._get_l1_lock()

        assert lock1 == lock2
        assert cache._l1_lock is lock1
