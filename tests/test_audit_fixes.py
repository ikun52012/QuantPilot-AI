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


def test_ai_cost_tracker_extracts_anthropic_usage_fields():
    from core.ai_cost_tracker import extract_usage_from_response

    assert extract_usage_from_response({"usage": {"input_tokens": 12, "output_tokens": 8}}) == (12, 8, 20)


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


@pytest.mark.asyncio
async def test_ai_decision_log_inserts_json_in_sqlite(db_session):
    import json

    from sqlalchemy import text

    from core.ai_decision_log import _insert_into_db

    record = {
        "decision_id": "decision-sqlite",
        "timestamp": "2026-07-23T00:00:00+00:00",
        "ticker": "BTCUSDT",
        "direction": "long",
        "signal_price": 50000.0,
        "timeframe": "1h",
        "strategy": "test",
        "user_id": "user-1",
        "provider": "test",
        "model_id": "test-model",
        "system_prompt": "system",
        "user_prompt": "user",
        "raw_response": "{}",
        "analysis_json": '{"confidence": 0.8}',
        "market_context_json": '{"regime": "trending"}',
        "enhanced_data_json": "{}",
        "recommendation": "execute",
        "confidence": 0.8,
        "risk_score": 0.2,
    }

    await _insert_into_db(record)
    row = (
        await db_session.execute(
            text(
                "SELECT analysis_json, market_context_json, created_at "
                "FROM ai_decision_log WHERE decision_id = :decision_id"
            ),
            {"decision_id": record["decision_id"]},
        )
    ).one()

    assert json.loads(row.analysis_json)["confidence"] == pytest.approx(0.8)
    assert json.loads(row.market_context_json)["regime"] == "trending"
    assert row.created_at is not None


def test_confidence_calibrator_without_data_preserves_raw_value(monkeypatch, tmp_path):
    import core.confidence_calibrator as calibrator

    calibrator._CALIBRATION_CACHE.clear()
    monkeypatch.setattr(calibrator, "_CALIBRATION_FILE", tmp_path / "missing-calibration.json")

    assert calibrator.calibrate_confidence(0.81, "BTCUSDT", "trending") == pytest.approx(0.81)

    calibrator._CALIBRATION_CACHE.clear()


def test_ai_decision_jsonl_retention_removes_expired_files(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    import core.ai_decision_log as decision_log

    old_file = tmp_path / "ai_decisions_2026-01-01.jsonl"
    recent_file = tmp_path / "ai_decisions_2026-07-22.jsonl"
    old_file.write_text("{}\n", encoding="utf-8")
    recent_file.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(decision_log, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(decision_log, "_LAST_LOG_CLEANUP_DATE", "")
    monkeypatch.setenv("AI_DECISION_LOG_RETENTION_DAYS", "90")

    deleted = decision_log._cleanup_expired_jsonl(datetime(2026, 7, 23, tzinfo=UTC))

    assert deleted == 1
    assert not old_file.exists()
    assert recent_file.exists()


def test_scheduler_lock_does_not_expire_while_owner_is_alive(monkeypatch, tmp_path):
    import core.lifespan as lifespan

    lock_path = tmp_path / "data" / "scheduler.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("4242:1", encoding="ascii")

    monkeypatch.setattr(lifespan, "DATA_DIR", lock_path.parent)
    monkeypatch.setattr(lifespan, "_pid_is_running", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lifespan, "_scheduler_lock_fd", None)
    monkeypatch.setattr(lifespan, "_scheduler_lock_path", None)

    assert lifespan._acquire_scheduler_lock() is False
    assert lock_path.exists()


def test_scheduler_pid_reuse_is_detected(monkeypatch):
    import core.lifespan as lifespan

    monkeypatch.setattr(lifespan.sys, "platform", "linux")
    monkeypatch.setattr(lifespan.os, "kill", lambda *_args: None)
    monkeypatch.setattr(lifespan, "_process_start_time", lambda _pid: 2000.0)

    assert lifespan._pid_is_running(42, expected_start_time=1000.0) is False
