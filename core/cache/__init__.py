"""
P1-FIX: Multi-layer Cache Architecture
L1: In-memory cache (fastest, TTL+LRU)
L2: Redis cache (distributed, shared across instances)
L3: Disk cache (persistent, for restart recovery)
"""

from .multi_layer_cache import CacheLayer, MultiLayerCache

# Default global cache instance
cache = MultiLayerCache(
    cache_name="quantpilot_cache",
    l1_max_size=1000,
    l1_base_ttl=300.0,
    l2_enabled=False,  # Redis disabled by default
    l3_enabled=True,
    l3_ttl=3600.0,
)

__all__ = ["MultiLayerCache", "CacheLayer", "cache"]
