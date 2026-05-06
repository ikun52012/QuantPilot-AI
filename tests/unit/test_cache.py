"""
P4-FIX: Unit Tests for Multi-Layer Cache
Tests for L1, L2, L3 cache operations and metrics.
"""
import pytest
import asyncio
import pickle
from unittest.mock import Mock, AsyncMock, patch
from pathlib import Path

from core.cache.multi_layer_cache import MultiLayerCache, CacheLayer


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
    
    async def test_l1_cache_ttl_expiration(self, cache):
        """Test L1 cache TTL expiration."""
        await cache.set("expire_key", "value", ttl=0.5)  # 0.5 second TTL
        
        # Immediate get should succeed
        result1 = await cache.get("expire_key")
        assert result1 == "value"
        
        # Wait for expiration
        await asyncio.sleep(0.6)
        
        # Should be expired now
        result2 = await cache.get("expire_key")
        assert result2 is None
        
        metrics = await cache.get_metrics()
        assert metrics["l1_misses"] >= 1
    
    async def test_l1_cache_lru_eviction(self, cache):
        """Test L1 cache LRU eviction when max size exceeded."""
        # Set more items than max_size (10)
        for i in range(15):
            await cache.set(f"key_{i}", f"value_{i}", ttl=60)
        
        # Check cache size is limited
        metrics = await cache.get_metrics()
        assert metrics["l1_size"] <= cache._l1_max_size
        assert metrics["evictions"] >= 5
        
        # Older keys should be evicted
        result = await cache.get("key_0")
        assert result is None  # Evicted
        
        # Recent keys should still exist
        result = await cache.get("key_14")
        assert result == "value_14"  # Still cached
    
    async def test_l3_disk_cache_persistence(self, cache):
        """Test L3 disk cache persistence."""
        await cache.set("disk_key", {"persistent": True}, ttl=300)
        
        # Clear L1 (simulate restart)
        cache._l1_cache.clear()
        
        # Get should fetch from L3
        result = await cache.get("disk_key")
        
        assert result is not None
        assert result["persistent"] == True
        
        metrics = await cache.get_metrics()
        assert metrics["l3_hits"] >= 1
    
    async def test_cache_promotion(self, cache):
        """Test cache promotion from L3 to L1."""
        # Set in all layers
        await cache.set("promote_key", "test_value", ttl=300)
        
        # Clear L1 only
        cache._l1_cache.clear()
        
        # Get will fetch from L3 and promote to L1
        result = await cache.get("promote_key")
        assert result == "test_value"
        
        # Check promoted to L1
        assert "promote_key" in cache._l1_cache
        
        # Next get should hit L1
        metrics_before = await cache.get_metrics()
        result2 = await cache.get("promote_key")
        metrics_after = await cache.get_metrics()
        
        assert metrics_after["l1_hits"] > metrics_before["l1_hits"]
    
    async def test_get_or_compute_caches_result(self, cache):
        """Test get_or_compute caches computed result."""
        compute_count = 0
        
        async def compute_fn():
            compute_count += 1
            return {"computed": True, "count": compute_count}
        
        # First call computes
        result1 = await cache.get_or_compute(
            "compute_key",
            compute_fn,
            compute_fn_is_async=True,
        )
        
        assert result1["computed"] == True
        assert compute_count == 1
        
        # Second call uses cache
        result2 = await cache.get_or_compute(
            "compute_key",
            compute_fn,
            compute_fn_is_async=True,
        )
        
        assert result2["computed"] == True
        assert compute_count == 1  # Not incremented
        
        metrics = await cache.get_metrics()
        assert metrics["computes"] >= 1
    
    async def test_cache_invalidation(self, cache):
        """Test cache invalidation across all layers."""
        await cache.set("invalidate_key", "value", ttl=300)
        
        # Invalidate
        await cache.invalidate("invalidate_key")
        
        # Should not exist in any layer
        result = await cache.get("invalidate_key")
        assert result is None
        
        # Check L1
        assert "invalidate_key" not in cache._l1_cache
    
    async def test_cache_clear_all(self, cache):
        """Test clearing all cache layers."""
        # Set multiple items
        for i in range(5):
            await cache.set(f"clear_key_{i}", f"value_{i}", ttl=300)
        
        # Clear all
        await cache.clear_all()
        
        # All should be gone
        for i in range(5):
            result = await cache.get(f"clear_key_{i}")
            assert result is None
        
        metrics = await cache.get_metrics()
        assert metrics["l1_size"] == 0
    
    async def test_cleanup_expired_entries(self, cache):
        """Test cleanup of expired entries."""
        # Set short TTL
        await cache.set("cleanup_key_1", "value1", ttl=0.1)
        await cache.set("cleanup_key_2", "value2", ttl=60)  # Long TTL
        
        # Wait for expiration
        await asyncio.sleep(0.2)
        
        # Cleanup
        await cache.cleanup_expired()
        
        # Expired should be removed
        assert "cleanup_key_1" not in cache._l1_cache
        assert "cleanup_key_2" in cache._l1_cache
    
    async def test_cache_hit_rate_calculation(self, cache):
        """Test cache hit rate calculation."""
        # Generate hits and misses
        await cache.set("hit_key", "value", ttl=60)
        
        for _ in range(10):
            await cache.get("hit_key")  # 10 hits
        
        for _ in range(5):
            await cache.get("miss_key")  # 5 misses
        
        metrics = await cache.get_metrics()
        
        assert metrics["total_hits"] >= 10
        assert metrics["total_requests"] >= 15
        
        hit_rate = metrics["hit_rate_pct"]
        assert hit_rate >= 60.0  # At least 60% hit rate


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