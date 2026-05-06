"""
P1-FIX: Multi-layer Cache Architecture
L1: In-memory cache (fastest, TTL+LRU)
L2: Redis cache (distributed, shared across instances)
L3: Disk cache (persistent, for restart recovery)
"""

from .multi_layer_cache import MultiLayerCache, CacheLayer

__all__ = ["MultiLayerCache", "CacheLayer"]