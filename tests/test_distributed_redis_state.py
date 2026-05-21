"""Redis distributed coordination tests for strategy and AI hot state."""

import pytest

import core.redis_coordination as redis_coordination
from ai_analyzer import _AI_CACHE, _get_cached_analysis, _set_cached_analysis
from core.config import settings
from core.redis_coordination import DistributedLockTimeout, distributed_lock
from models import AIAnalysis
from strategies.dca import DCAConfig, DCAEngine
from strategies.grid import GridConfig, GridEngine


class FakeRedis:
    def __init__(self):
        self.values: dict[str, object] = {}
        self.hashes: dict[str, dict[str, object]] = {}

    async def ping(self):
        return True

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        existed = key in self.values
        self.values.pop(key, None)
        return int(existed)

    async def eval(self, script, numkeys, key, token):
        if self.values.get(key) == token:
            self.values.pop(key, None)
            return 1
        return 0

    async def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value
        return 1

    async def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    async def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    async def hdel(self, name, key):
        existed = key in self.hashes.get(name, {})
        self.hashes.get(name, {}).pop(key, None)
        return int(existed)


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(settings.redis, "enabled", True)
    monkeypatch.setattr(settings.redis, "url", "redis://fake/0")
    monkeypatch.setattr(redis_coordination, "_CLIENT", client)
    _AI_CACHE.clear()
    yield client
    _AI_CACHE.clear()


@pytest.mark.asyncio
async def test_dca_active_position_hydrates_across_engine_instances(fake_redis):
    writer = DCAEngine()
    config = DCAConfig(ticker="BTCUSDT", user_id="user-1", paper_mode=True)
    position = await writer.create_position_async(config, 50000.0)

    reader = DCAEngine()
    assert await reader.load_position_state(position.config_id) is True

    restored = reader.positions[position.config_id]
    assert restored.ticker == "BTCUSDT"
    assert restored.status == "active"
    assert reader.configs[position.config_id].user_id == "user-1"


@pytest.mark.asyncio
async def test_grid_active_position_hydrates_across_engine_instances(fake_redis):
    writer = GridEngine()
    config = GridConfig(
        ticker="ETHUSDT",
        user_id="user-1",
        upper_price=2200.0,
        lower_price=1800.0,
        grid_count=6,
        paper_mode=True,
    )
    position = await writer.create_grid_async(config, 2000.0)

    reader = GridEngine()
    assert await reader.load_position_state(position.config_id) is True

    restored = reader.positions[position.config_id]
    assert restored.ticker == "ETHUSDT"
    assert restored.status == "active"
    assert len(restored.grid_levels) == 6


@pytest.mark.asyncio
async def test_ai_analysis_cache_uses_redis_l2_after_local_eviction(fake_redis):
    analysis = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="ok")

    await _set_cached_analysis("BTCUSDT", "long", analysis, "50000.00", "60", "cfg")
    _AI_CACHE.clear()

    cached = await _get_cached_analysis("BTCUSDT", "long", "50000.00", "60", "cfg")

    assert cached is not None
    assert cached.recommendation == "execute"
    assert cached.reasoning == "ok"


@pytest.mark.asyncio
async def test_distributed_lock_blocks_same_redis_key(fake_redis):
    async with distributed_lock("unit:test-lock", blocking_timeout_seconds=0.1):
        with pytest.raises(DistributedLockTimeout):
            async with distributed_lock("unit:test-lock", blocking_timeout_seconds=0.01, retry_interval_seconds=0.001):
                pass
