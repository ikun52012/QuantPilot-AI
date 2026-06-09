import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from models import AIAnalysis, MarketContext, SignalDirection, TradingViewSignal
from services.unified_ohlcv import OHLCVBundle, SymbolMapping


@asynccontextmanager
async def dummy_lock(*args, **kwargs):
    yield

# Auto-use fixture to mock distributed_lock in all tests in this file
@pytest.fixture(autouse=True)
def mock_dca_locks():
    with patch("strategies.dca.distributed_lock", side_effect=dummy_lock):
        yield

# 1. Test dynamic cache ttl when Redis raises exceptions
@pytest.mark.asyncio
async def test_dynamic_cache_ttl_redis_error():
    from ai_analyzer import _VOLATILITY_TRACKER, _get_dynamic_cache_ttl

    # Clean cache and volatility tracker
    _VOLATILITY_TRACKER.clear()

    # Mock settings
    settings.ai.dynamic_cache_ttl_enabled = True
    settings.ai.dynamic_cache_ttl_base = 60

    # Mock redis_get_json to raise ConnectionError
    with patch("ai_analyzer.redis_get_json", side_effect=ConnectionError("Redis down")):
        ttl = await _get_dynamic_cache_ttl("BTCUSDT")
        # Low volatility fallback (0.0% < 2.0%) should return base * low_volatility_multiplier (60 * 2.0 = 120.0)
        assert ttl == 120.0

# 2. Test fallback to local rule analysis when LLMs fail
@pytest.mark.asyncio
async def test_fallback_to_local_rule_analysis():
    from ai_analyzer import analyze_signal

    settings.ai.auto_fallback_to_local = True
    settings.ai.voting_enabled = False
    settings.ai.provider = "openai"

    signal = TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        timeframe="15",
        price=100.0,
    )
    market = MarketContext(
        ticker="BTCUSDT",
        current_price=100.0,
        high_24h=105.0,
        low_24h=95.0,
        atr_pct=1.0,
    )

    # Mock _call_openai to raise an error
    with patch("ai_analyzer._call_openai", side_effect=Exception("OpenAI timeout")), \
         patch("ai_analyzer.redis_set_json"), \
         patch("ai_analyzer.redis_get_json", return_value=None):
        # Mock _local_rule_analysis to return a mock JSON string
        mock_json_response = (
            '{"confidence": 0.5, "recommendation": "execute", "reasoning": "local rule success", '
            '"suggested_direction": "long", "suggested_entry": 100.0, "suggested_stop_loss": 98.0, '
            '"suggested_take_profit": 105.0, "suggested_tp1": 102.0, "suggested_tp2": 104.0, '
            '"suggested_tp3": null, "suggested_tp4": null, "tp1_qty_pct": 50.0, "tp2_qty_pct": 50.0, '
            '"tp3_qty_pct": 0.0, "tp4_qty_pct": 0.0, "position_size_pct": 0.5, "recommended_leverage": 5, '
            '"risk_score": 0.3, "market_condition": "trending_up", "trend_strength": "strong", '
            '"recommended_trailing_stop_mode": "breakeven_on_tp1", "warnings": []}'
        )
        with patch("ai_analyzer._local_rule_analysis", return_value=mock_json_response) as mock_local:
            analysis = await analyze_signal(signal, market)
            mock_local.assert_called_once()
            assert analysis.recommendation == "execute"
            assert any("AI fallback triggered" in warning for warning in analysis.warnings)

# 3. Test walk-forward learning throttling interval
@pytest.mark.asyncio
async def test_walk_forward_learning_throttling():
    from services.market_scanner import MarketScannerService

    settings.scanner.learning_enabled = True
    settings.scanner.learning_refresh_interval_secs = 3600

    scanner = MarketScannerService(scope="test_scope")
    scanner._last_learning_refreshed_at = time.time()
    scanner._learning_summary_cache = {"enabled": True, "win_rate": 0.65}

    # Mock database outcomes sync functions to track if they are called
    with patch("services.market_scanner.sync_scanner_outcomes") as mock_sync:
        result = await scanner._refresh_learning("test_run")
        # Since interval is 3600s and we just refreshed, it should skip database calls
        mock_sync.assert_not_called()
        assert result == {"enabled": True, "win_rate": 0.65}

# 4. Test normalization of manual watchlist symbols
def test_normalization_manual_watchlist_symbols():
    from services.market_scanner import MarketScannerService

    scanner = MarketScannerService(scope="test_scope")

    # Passing symbol with slash and spaces
    item = scanner._coerce_universe_item("BTC / USDT")

    # Should normalize and compact to BTCUSDT
    assert item.watch_symbol == "BTCUSDT"
    assert item.exchange_symbol == "BTCUSDT"
    assert item.source_symbol == "BTCUSDT"

# 5. Test DCA initial entry size and cost limit validations
@pytest.mark.asyncio
async def test_dca_initial_entry_size_limits():
    from strategies.dca import DCAConfig, DCAEngine

    engine = DCAEngine()
    config = DCAConfig(
        ticker="BTCUSDT",
        initial_capital_usdt=5.0, # extremely small size
        paper_mode=False,
    )

    # Mock get_market_limits to enforce a minimum cost of 10.0 USDT
    mock_limits = {
        "min_amount": 0.001,
        "min_cost": 10.0,
        "contract_size": 1.0,
    }

    with patch("exchange.get_market_limits", return_value=mock_limits):
        with pytest.raises(ValueError) as excinfo:
            await engine.create_position_async(config, current_price=50000.0)
        assert "size_below_exchange_minimum" in str(excinfo.value)

# 6. Test DCA entry addition limits validation
@pytest.mark.asyncio
async def test_dca_add_entry_size_limits():
    from core.utils.datetime import utcnow
    from strategies.dca import DCAConfig, DCAEngine, DCAEntry, DCAPosition

    engine = DCAEngine()
    config = DCAConfig(
        ticker="BTCUSDT",
        fixed_size_usdt=5.0, # extremely small size
        paper_mode=False,
        max_entries=3,
        entry_spacing_pct=2.0,
    )

    # Pre-build a position with 1 entry
    position = DCAPosition(
        config_id="test_position",
        ticker="BTCUSDT",
        direction="long",
        entries=[
            DCAEntry(
                entry_price=50000.0,
                quantity=0.0002,
                capital_usdt=10.0,
                entry_time=utcnow(),
                entry_idx=1,
            )
        ],
        total_quantity=0.0002,
        total_capital_usdt=10.0,
        average_entry_price=50000.0,
        entries_remaining=2,
    )
    engine.positions["test_position"] = position
    engine.configs["test_position"] = config

    # Mock get_market_limits to enforce a minimum cost of 10.0 USDT
    mock_limits = {
        "min_amount": 0.0001,
        "min_cost": 10.0,
        "contract_size": 1.0,
    }

    with patch("exchange.get_market_limits", return_value=mock_limits):
        res = await engine._add_entry("test_position", config, current_price=49000.0)
        # Should return success=False and reason=size_below_exchange_minimum
        assert res["success"] is False
        assert res["reason"] == "size_below_exchange_minimum"
        # Position entries should not increase
        assert len(position.entries) == 1


def test_ai_parse_response_keeps_omitted_later_tp_quantities_zero():
    from ai_analyzer import _parse_response

    analysis = _parse_response(
        '{"confidence": 0.8, "recommendation": "execute", "reasoning": "ok", '
        '"suggested_stop_loss": 95.0, "suggested_tp1": 105.0, "suggested_tp2": 110.0, '
        '"tp1_qty_pct": 60.0, "tp2_qty_pct": 40.0}'
    )

    assert analysis.recommendation == "execute"
    assert analysis.tp1_qty_pct == 60.0
    assert analysis.tp2_qty_pct == 40.0
    assert analysis.tp3_qty_pct == 0.0
    assert analysis.tp4_qty_pct == 0.0


def test_ai_cost_tracker_prefers_longest_model_match():
    from core.ai_cost_tracker import AICostTracker

    tracker = AICostTracker()

    assert tracker._estimate_cost("gpt-4o-mini", 1_000_000, 0) == 0.15


@pytest.mark.asyncio
async def test_ai_memory_cache_returns_copies():
    from ai_analyzer import _AI_CACHE, _get_cached_analysis, _set_cached_analysis

    _AI_CACHE.clear()
    analysis = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="cached", warnings=[])

    with patch("ai_analyzer.redis_get_json", new=AsyncMock(return_value=None)), \
         patch("ai_analyzer.redis_set_json", new=AsyncMock()), \
         patch("ai_analyzer.distributed_lock", side_effect=dummy_lock):
        await _set_cached_analysis("BTCUSDT", "long", analysis, price_bucket="100")
        cached = await _get_cached_analysis("BTCUSDT", "long", price_bucket="100")
        assert cached is not None
        cached.warnings.append("mutated")

        cached_again = await _get_cached_analysis("BTCUSDT", "long", price_bucket="100")

    assert cached_again is not None
    assert cached_again.warnings == []


@pytest.mark.asyncio
async def test_scanner_backtest_does_not_pass_unsupported_cache_flag_or_mutate_default_weights():
    from services.scanner_backtest import ScannerBacktester
    from services.scanner_rules import DEFAULT_ENGINE

    class FakeProvider:
        def __init__(self):
            self.calls = []

        async def get_bundle(self, symbol, timeframes=None, **kwargs):
            self.calls.append((symbol, timeframes, kwargs))
            return OHLCVBundle(
                mapping=SymbolMapping(
                    watch_symbol=symbol,
                    data_symbol=symbol,
                    exchange_symbol=symbol,
                ),
                current_price=100.0,
                timeframes={},
            )

    original_weights = dict(DEFAULT_ENGINE.weights)
    provider = FakeProvider()
    backtester = ScannerBacktester(provider=provider)

    summary = await backtester.run_backtest(
        ["BTCUSDT"],
        timeframes=["1h"],
        weights_override={"ema_alignment": 99.0},
    )

    assert summary.total_signals == 0
    assert provider.calls == [("BTCUSDT", ["1h"], {})]
    assert DEFAULT_ENGINE.weights == original_weights
