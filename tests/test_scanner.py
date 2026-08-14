import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("greenlet")

from core.config import settings
from core.database import (
    PositionModel,
    TradeModel,
    get_scanner_rejection_summary,
    list_scanner_audits,
    record_scanner_audit,
)
from core.utils.datetime import utcnow
from models import MarketContext
from services.market_scanner import MarketScannerService, ScannerCandidate, ScannerUniverseItem
from services.scanner_learning import (
    compute_factor_performance,
    compute_outcome_summary,
    compute_walk_forward_thresholds,
    sync_scanner_outcomes,
)
from services.scanner_rules import (
    DEFAULT_ENGINE,
    ScoringContext,
    load_rules_config,
    save_rules_config,
)
from services.synthetic_signal import build_synthetic_signal, market_context_from_bundle
from services.unified_ohlcv import (
    NormalizedCandle,
    OHLCVBundle,
    SymbolMapping,
    UnifiedOHLCVProvider,
    _indicator_snapshot,
)


def _candles(count: int = 80, start: float = 100.0) -> list[NormalizedCandle]:
    now = utcnow().replace(tzinfo=None)
    rows: list[NormalizedCandle] = []
    for idx in range(count):
        price = start + idx * 0.1
        rows.append(
            NormalizedCandle(
                timestamp=now - timedelta(hours=count - idx),
                open=price,
                high=price + 1.0,
                low=price - 1.0,
                close=price,
                volume=1000.0 + idx,
            )
        )
    return rows


def _bundle() -> OHLCVBundle:
    return OHLCVBundle(
        mapping=SymbolMapping(
            watch_symbol="BTCUSDT",
            data_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            exchange_name="binance",
            market_type="swap",
            data_source="ccxt",
        ),
        current_price=100.0,
        volume_24h=50_000_000.0,
        timeframes={"1h": _candles()},
        indicators={
            "1h": {
                "rsi": 30.0,
                "atr_pct": 1.2,
                "ema_fast": 101.0,
                "ema_slow": 99.0,
            }
        },
        data_quality={
            "passed": True,
            "reasons": [],
            "spread_pct": 0.02,
            "primary_timeframe": "1h",
            "orderbook_bid_depth_usdt": 250_000.0,
            "orderbook_ask_depth_usdt": 250_000.0,
        },
    )


def test_ohlcv_bundle_accepts_string_indicator_values():
    indicators = {"1h": _indicator_snapshot(_candles(80))}

    bundle = OHLCVBundle(
        mapping=SymbolMapping(
            watch_symbol="BTCUSDT",
            data_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            exchange_name="binance",
            market_type="swap",
            data_source="ccxt",
        ),
        current_price=100.0,
        timeframes={"1h": _candles(80)},
        indicators=indicators,
        data_quality={"passed": True, "reasons": [], "primary_timeframe": "1h"},
    )

    assert bundle.indicators["1h"]["market_regime"] in {"unknown", "ranging", "transitional", "trending"}


def test_market_context_from_bundle_preserves_scanner_market_data():
    bundle = _bundle()
    bundle.timeframes["15m"] = _candles(80)
    bundle.timeframes["4h"] = _candles(120)
    bundle.indicators["15m"] = dict(bundle.indicators["1h"])
    bundle.indicators["4h"] = dict(bundle.indicators["1h"])
    bundle.indicators["1h"]["market_regime"] = "trending"
    bundle.indicators["1h"]["vwap"] = 101.2
    bundle.indicators["1h"]["volume_profile_poc"] = 100.8
    bundle.funding_rate = 0.0001
    bundle.orderbook_imbalance = 1.25
    bundle.long_short_ratio = 0.92
    bundle.oi_current = 123456.0
    bundle.oi_change_pct = 4.5
    bundle.volume_24h = 50_000_000.0
    bundle.volume_change_pct = 12.5
    bundle.price_change_24h = 2.1
    bundle.high_24h = 110.0
    bundle.low_24h = 95.0

    context = market_context_from_bundle(bundle, ticker="BTCUSDT")

    assert context.funding_rate == pytest.approx(0.0001)
    assert context.orderbook_imbalance == pytest.approx(1.25)
    assert context.open_interest == pytest.approx(123456.0)
    assert context.open_interest_change_pct == pytest.approx(4.5)
    assert context.volume_24h == pytest.approx(50_000_000.0)
    assert context.volume_change_pct == pytest.approx(12.5)
    assert context.price_change_24h == pytest.approx(2.1)
    assert len(context._ohlcv_1h) == 80
    assert len(context._ohlcv_15m) == 80
    assert len(context._ohlcv_4h) == 120
    assert context._scanner_market_regime == "trending"
    assert context._scanner_indicators["1h"]["vwap"] == pytest.approx(101.2)


@pytest.mark.asyncio
async def test_ohlcv_bundle_marks_missing_market_context_degraded(monkeypatch):
    monkeypatch.setattr(settings.scanner, "bundle_cache_ttl_secs", 0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])

    async def fake_fetch_ohlcv_history(*args, **kwargs):
        return [candle.model_dump() for candle in _candles(80)]

    async def fake_fetch_market_context(symbol):
        return MarketContext(ticker=symbol, current_price=0.0)

    monkeypatch.setattr("services.unified_ohlcv.fetch_ohlcv_history", fake_fetch_ohlcv_history)
    monkeypatch.setattr("services.unified_ohlcv.fetch_market_context", fake_fetch_market_context)

    bundle = await UnifiedOHLCVProvider().get_bundle("BTCUSDT", ["1h"])

    assert bundle.quality_passed is False
    assert "market_context_unavailable" in bundle.quality_reasons
    assert bundle.data_quality["market_context_available"] is False


@pytest.mark.asyncio
async def test_ohlcv_provider_strict_policy_uses_configured_source(monkeypatch):
    calls = []
    monkeypatch.setattr(settings.scanner, "bundle_cache_ttl_secs", 0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])
    monkeypatch.setattr(settings.scanner, "data_source_policy", "strict")

    async def fake_fetch_ohlcv_history(ticker, **kwargs):
        calls.append(("ohlcv", ticker, kwargs))
        return [candle.model_dump() for candle in _candles(80)]

    async def fake_fetch_market_context(symbol, **kwargs):
        calls.append(("context", symbol, kwargs))
        context = MarketContext(ticker=symbol, current_price=_candles(80)[-1].close, volume_24h=20_000_000.0)
        context._market_data_source = "okx"
        return context

    monkeypatch.setattr("services.unified_ohlcv.fetch_ohlcv_history", fake_fetch_ohlcv_history)
    monkeypatch.setattr("services.unified_ohlcv.fetch_market_context", fake_fetch_market_context)

    bundle = await UnifiedOHLCVProvider().get_bundle(
        "BTCUSDT",
        ["1h"],
        source_exchange="okx",
        source_market_type="contract",
        data_source_policy="strict",
    )

    assert bundle.mapping.source_exchange == "okx"
    assert bundle.mapping.actual_data_source == "okx"
    assert bundle.data_quality["data_source_policy"] == "strict"
    assert any(call[2].get("exchange_ids") == ["okx"] for call in calls if call[0] == "ohlcv")
    assert any(call[2].get("exchange_ids") == ["okx"] for call in calls if call[0] == "context")


@pytest.mark.asyncio
async def test_hybrid_universe_uses_exchange_markets_with_include_exclude(monkeypatch):
    class FakeExchange:
        def load_markets(self):
            return {
                "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "contract": True, "swap": True, "active": True},
                "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "contract": True, "swap": True, "active": True},
                "DOGE/USDT:USDT": {"base": "DOGE", "quote": "USDT", "contract": True, "swap": True, "active": True},
            }

        def fetch_tickers(self):
            return {
                "BTC/USDT:USDT": {"quoteVolume": 100_000_000},
                "ETH/USDT:USDT": {"quoteVolume": 80_000_000},
                "DOGE/USDT:USDT": {"quoteVolume": 1_000_000},
            }

    monkeypatch.setattr("exchange._get_or_create_exchange", lambda *args, **kwargs: FakeExchange())
    monkeypatch.setattr(settings.scanner, "source_mode", "hybrid")
    monkeypatch.setattr(settings.scanner, "source_exchange", "binance")
    monkeypatch.setattr(settings.scanner, "source_market_type", "contract")
    monkeypatch.setattr(settings.scanner, "universe_top_n", 2)
    monkeypatch.setattr(settings.scanner, "universe_min_quote_volume", 5_000_000.0)
    monkeypatch.setattr(settings.scanner, "watchlist", ["XRPUSDT"])
    monkeypatch.setattr(settings.scanner, "include_symbols", ["SOLUSDT"])
    monkeypatch.setattr(settings.scanner, "exclude_symbols", ["ETHUSDT"])
    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "market_type", "contract")

    universe = await MarketScannerService()._build_effective_universe()
    symbols = [item.watch_symbol for item in universe]

    assert "BTCUSDT" in symbols
    assert "ETHUSDT" not in symbols
    assert "DOGEUSDT" not in symbols
    assert "XRPUSDT" in symbols
    assert "SOLUSDT" in symbols
    assert all(item.tradable for item in universe)


@pytest.mark.asyncio
async def test_empty_manual_watchlist_defaults_to_all_tradable(monkeypatch):
    class FakeExchange:
        def load_markets(self):
            return {
                "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "contract": True, "swap": True, "active": True},
                "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "contract": True, "swap": True, "active": True},
            }

        def fetch_tickers(self):
            return {
                "BTC/USDT:USDT": {"quoteVolume": 100_000_000},
                "ETH/USDT:USDT": {"quoteVolume": 80_000_000},
            }

    monkeypatch.setattr("exchange._get_or_create_exchange", lambda *args, **kwargs: FakeExchange())
    monkeypatch.setattr(settings.scanner, "source_mode", "manual")
    monkeypatch.setattr(settings.scanner, "watchlist", [])
    monkeypatch.setattr(settings.scanner, "universe_top_n", 50)
    monkeypatch.setattr(settings.scanner, "universe_min_quote_volume", 0.0)
    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "market_type", "contract")

    universe = await MarketScannerService()._build_effective_universe()

    assert [item.watch_symbol for item in universe] == ["BTCUSDT", "ETHUSDT"]
    assert all(item.universe_source == "manual_all_tradable" for item in universe)


@pytest.mark.asyncio
async def test_universe_preview_reports_skipped_and_source_health(monkeypatch):
    class FakeExchange:
        def load_markets(self):
            return {
                "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "contract": True, "swap": True, "active": True},
                "DOGE/USDT:USDT": {"base": "DOGE", "quote": "USDT", "contract": True, "swap": True, "active": True},
            }

        def fetch_tickers(self):
            return {
                "BTC/USDT:USDT": {"quoteVolume": 100_000_000},
                "DOGE/USDT:USDT": {"quoteVolume": 100},
            }

    monkeypatch.setattr("exchange._get_or_create_exchange", lambda *args, **kwargs: FakeExchange())
    monkeypatch.setattr(settings.scanner, "source_mode", "follow_exchange")
    monkeypatch.setattr(settings.scanner, "universe_min_quote_volume", 1_000_000.0)
    monkeypatch.setattr(settings.scanner, "universe_top_n", 50)
    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "market_type", "contract")

    preview = await MarketScannerService().preview_universe(force_refresh=True)

    assert preview["summary"]["count"] == 1
    assert preview["items"][0]["watch_symbol"] == "BTCUSDT"
    assert preview["skipped"][0]["tradability_reason"] == "quote_volume_below_universe_minimum"
    assert preview["summary"]["source_health"]


@pytest.mark.asyncio
async def test_live_validation_empty_whitelist_blocks_snapshot_symbol(monkeypatch):
    service = MarketScannerService()
    item = ScannerUniverseItem(
        watch_symbol="BTCUSDT",
        exchange_symbol="BTCUSDT",
        target_exchange="binance",
        target_market_type="contract",
        source_exchange="binance",
        source_market_type="contract",
        source_symbol="BTCUSDT",
        universe_source="manual_all_tradable",
    )
    service._set_live_universe_snapshot([item])
    candidate = _candidate("long")
    candidate.target_exchange = "binance"
    candidate.target_market_type = "contract"

    monkeypatch.setattr(settings.scanner, "mode", "live")
    monkeypatch.setattr(settings.scanner, "live_symbol_whitelist", [])
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr("exchange.get_market_limits", lambda *args, **kwargs: {"symbol": "BTC/USDT:USDT"})

    ok, reason, limits = await service._validate_live_market(candidate)

    assert not ok
    assert "SCANNER_LIVE_SYMBOL_WHITELIST" in reason
    assert limits == {}


def _candidate(direction: str = "long", score: float = 80.0, setup_hash: str = "candidate") -> ScannerCandidate:
    return ScannerCandidate(
        watch_symbol="BTCUSDT",
        exchange_symbol="BTCUSDT",
        exchange_name="binance",
        market_type="contract",
        data_source="ccxt",
        mapped_asset=False,
        direction=direction,
        timeframe="1h",
        current_price=100.0,
        entry_reference=99.5,
        score=score,
        setup_type=f"{direction}_setup",
        price_zone="zone:99.5",
        setup_hash=setup_hash,
        reasons=["test candidate"],
    )


def _scoring_context(**overrides) -> ScoringContext:
    data = {
        "direction": "long",
        "current_price": 100.0,
        "atr_pct": 1.0,
        "atr_price": 1.0,
        "rsi": 45.0,
        "ema_fast": 99.0,
        "ema_slow": 98.0,
        "ema200": 90.0,
        "macd_hist": 1.0,
        "adx": 25.0,
        "volume_ratio": 1.2,
        "vwap": 95.0,
        "vwap_distance_pct": 5.0,
        "poc": 95.0,
        "regime": "trending",
        "oi_change_pct": 5.0,
        "htf_trend": "bullish",
        "smc_trend": "bullish",
        "smc_risk_score": 0.2,
        "smc_timing_score": 0.8,
        "price_zone": "discount",
        "support_zone": {"type": "bullish_fvg", "midpoint": 99.5},
        "premium_zone": 110.0,
        "discount_zone": 101.0,
        "equilibrium": 105.0,
        "spread_pct": 0.05,
        "bid_ask_spread_pct": 0.05,
        "bundle_quality_passed": True,
        "bundle_quality_reasons": [],
        "timeframe": "1h",
        "market_type": "contract",
    }
    data.update(overrides)
    return ScoringContext(**data)


def test_build_synthetic_signal_marks_auto_scanner():
    candidate = ScannerCandidate(
        watch_symbol="BTCUSDT",
        exchange_symbol="BTCUSDT",
        exchange_name="binance",
        market_type="contract",
        data_source="ccxt",
        mapped_asset=False,
        direction="long",
        timeframe="1h",
        current_price=100.0,
        entry_reference=99.5,
        score=82.0,
        setup_type="bullish_fvg",
        price_zone="discount:99.5",
        setup_hash="abc123",
        reasons=["price in discount zone"],
    )

    signal, raw_body = build_synthetic_signal(candidate)

    assert signal.strategy == "AI_Auto_Scanner"
    assert signal.ticker == "BTCUSDT"
    assert raw_body["signal_source"] == "auto_scanner"
    assert raw_body["scanner"]["setup_hash"] == "abc123"
    assert raw_body["scanner"]["exchange_name"] == "binance"
    assert raw_body["scanner"]["market_type"] == "contract"


def test_penalty_rules_do_not_reward_missing_conflicts(monkeypatch):
    monkeypatch.setattr(settings.scanner, "ema200_enabled", True)
    monkeypatch.setattr(settings.scanner, "htf_conflict_enabled", True)
    monkeypatch.setattr(settings.scanner, "regime_filter_enabled", True)

    _, _, breakdown = DEFAULT_ENGINE.evaluate(_scoring_context())

    assert "ema200_conflict_long" not in breakdown
    assert "htf_conflict" not in breakdown
    assert "volume_penalty" not in breakdown
    assert "no_support_penalty" not in breakdown


def test_scanner_rules_round_trip_persists_to_explicit_path(tmp_path):
    path = tmp_path / "scanner_rules.json"

    save_rules_config(path)

    assert path.exists()
    assert load_rules_config(path) is True


@pytest.mark.asyncio
async def test_enhanced_context_uses_direction_relative_mtf_and_session(monkeypatch):
    import core.quant_indicators as quant_indicators
    import enhanced_market_data as enhanced

    bundle = _bundle()
    bundle.funding_rate = 0.0002
    bundle.timeframes["15m"] = _candles()
    bundle.timeframes["4h"] = _candles()
    bundle.indicators["15m"] = {"rsi": 70.0}
    bundle.indicators["1h"].update({"rsi": 70.0, "ema200": 90.0})
    bundle.indicators["4h"] = {"rsi": 70.0}
    smc = SimpleNamespace(
        fvgs=[],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=95.0,
        equilibrium=100.0,
        risk_score=0.2,
        entry_timing_score=0.8,
    )

    async_empty = AsyncMock(return_value={})
    for name in (
        "calculate_volume_zscore",
        "calculate_atr_percentile",
        "estimate_orderbook_slippage",
        "fetch_btc_dominance",
        "fetch_long_short_ratio",
        "fetch_liquidation_heatmap",
        "detect_volatility_regime",
        "calculate_cvd_divergence",
        "fetch_fear_greed_index",
    ):
        monkeypatch.setattr(enhanced, name, async_empty)
    funding = AsyncMock(return_value={"trend": "rising"})
    monkeypatch.setattr(enhanced, "calculate_funding_term_structure", funding)
    monkeypatch.setattr(enhanced, "detect_active_session", lambda: "asian")
    monkeypatch.setattr(
        quant_indicators,
        "compute_relative_strength_btc",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        enhanced,
        "calculate_mtf_momentum_alignment",
        lambda **kwargs: 0.9,
    )

    service = MarketScannerService()
    long_context = await service._build_scoring_context(
        bundle,
        smc,
        "long",
        bundle.indicators["1h"],
        timeframe="1h",
    )
    short_context = await service._build_scoring_context(
        bundle,
        smc,
        "short",
        bundle.indicators["1h"],
        timeframe="1h",
    )

    assert long_context.mtf_alignment == pytest.approx(0.8)
    assert long_context.mtf_aligned is True
    assert short_context.mtf_alignment == pytest.approx(-0.8)
    assert short_context.mtf_conflicted is True
    assert long_context.active_session == "asian"
    assert long_context.low_liquidity_session is True
    funding.assert_awaited_with("BTCUSDT", pytest.approx(0.0002))


@pytest.mark.asyncio
async def test_outcome_summary_filters_closed_time_mode_and_strategy_slice(db_session):
    now = utcnow()
    common = {
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "direction": "long",
        "score": 80.0,
        "pnl_pct": 1.0,
    }
    await record_scanner_audit(
        db_session,
        scope="admin",
        event_type="outcome_label",
        setup_hash="paper-current",
        payload={
            **common,
            "execution_mode": "paper",
            "closed_at": now.isoformat(),
        },
    )
    await record_scanner_audit(
        db_session,
        scope="admin",
        event_type="outcome_label",
        setup_hash="live-current",
        payload={
            **common,
            "execution_mode": "live",
            "closed_at": now.isoformat(),
        },
    )
    await record_scanner_audit(
        db_session,
        scope="admin",
        event_type="outcome_label",
        setup_hash="paper-old",
        payload={
            **common,
            "execution_mode": "paper",
            "closed_at": (now - timedelta(days=60)).isoformat(),
        },
    )
    await db_session.commit()

    summary = await compute_outcome_summary(
        db_session,
        scope="admin",
        days=30,
        include_recent=False,
        symbol="BTCUSDT",
        timeframe="1h",
        direction="long",
        execution_mode="paper",
    )

    assert summary["total"] == 1
    assert summary["expectancy_pct"] == pytest.approx(1.0)


def test_scanner_feature_toggles_disable_optional_rule_groups(monkeypatch):
    monkeypatch.setattr(settings.scanner, "ema200_enabled", False)
    monkeypatch.setattr(settings.scanner, "htf_conflict_enabled", False)
    monkeypatch.setattr(settings.scanner, "regime_filter_enabled", False)

    _, _, breakdown = DEFAULT_ENGINE.evaluate(
        _scoring_context(ema200=110.0, htf_trend="bearish", regime="ranging")
    )

    assert not any(name.startswith("ema200_") for name in breakdown)
    assert "htf_conflict" not in breakdown
    assert not any(name.startswith("regime_") for name in breakdown)


@pytest.mark.asyncio
async def test_hard_filter_requires_support_zone(monkeypatch):
    fake_ctx = SimpleNamespace(
        fvgs=[],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", lambda *args, **kwargs: fake_ctx)
    monkeypatch.setattr(settings.scanner, "hard_filters_enabled", True)
    monkeypatch.setattr(settings.scanner, "require_support_zone", True)
    monkeypatch.setattr(settings.scanner, "min_score", 0.0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])

    assert await MarketScannerService()._build_candidates(_bundle()) == []


@pytest.mark.asyncio
async def test_hard_filter_rejects_poor_risk_reward(monkeypatch):
    fake_ctx = SimpleNamespace(
        fvgs=[SimpleNamespace(type="bullish", bottom=99.0, top=101.0, midpoint=100.0, filled=False, effectiveness=1.0)],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=100.4,
        discount_zone=101.0,
        equilibrium=100.3,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", lambda *args, **kwargs: fake_ctx)
    monkeypatch.setattr(settings.scanner, "hard_filters_enabled", True)
    monkeypatch.setattr(settings.scanner, "min_rr_ratio", 1.5)
    monkeypatch.setattr(settings.scanner, "min_score", 0.0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])

    assert await MarketScannerService()._build_candidates(_bundle()) == []


@pytest.mark.asyncio
async def test_mtf_consensus_returns_neutral_when_margin_too_small(monkeypatch):
    class FakeFVG:
        def __init__(self, direction: str):
            self.type = "bullish" if direction == "long" else "bearish"
            self.bottom = 99.0
            self.top = 101.0
            self.midpoint = 100.0
            self.filled = False
            self.effectiveness = 1.0

    def fake_analyze(rows, timeframe, current_price, direction, atr_pct):
        return SimpleNamespace(
            fvgs=[FakeFVG(direction)],
            order_blocks=[],
            structure=SimpleNamespace(trend="bullish" if direction == "long" else "bearish"),
            premium_zone=99.0 if direction == "short" else 110.0,
            discount_zone=101.0,
            equilibrium=100.5,
            risk_score=0.2,
            entry_timing_score=0.9,
            timing_recommendation="Good",
        )

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "mtf_consensus_enabled", True)
    monkeypatch.setattr(settings.scanner, "mtf_consensus_min_margin", 100.0)
    monkeypatch.setattr(settings.scanner, "min_score", 0.0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])

    assert await MarketScannerService()._build_candidates(_bundle()) == []


def test_liquidity_filter_rejects_thin_orderbook(monkeypatch):
    bundle = _bundle()
    bundle.volume_24h = 10_000_000.0
    bundle.data_quality["orderbook_ask_depth_usdt"] = 100.0
    bundle.data_quality["orderbook_bid_depth_usdt"] = 100.0

    monkeypatch.setattr(settings.scanner, "liquidity_filter_enabled", True)
    monkeypatch.setattr(settings.scanner, "liquidity_order_size_usdt", 1000.0)
    monkeypatch.setattr(settings.scanner, "min_orderbook_depth_usdt", 500.0)

    ok, reason, payload = MarketScannerService()._liquidity_filter(bundle, "long")

    assert not ok
    assert reason == "liquidity_filter:orderbook_depth_too_low"
    assert payload["side_depth_usdt"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_pre_scan_generates_smc_candidate(monkeypatch):
    class FakeFVG:
        type = "bullish"
        bottom = 99.0
        top = 101.0
        midpoint = 100.0
        filled = False
        effectiveness = 1.0

    fake_ctx = SimpleNamespace(
        fvgs=[FakeFVG()],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 60.0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])

    service = MarketScannerService()
    candidates = await service._build_candidates(_bundle())

    assert candidates
    assert candidates[0].direction == "long"
    assert candidates[0].setup_type == "bullish_fvg"
    assert candidates[0].score >= 60.0


@pytest.mark.asyncio
async def test_pre_scan_fuses_multiple_timeframes_into_one_signal(monkeypatch):
    class FakeFVG:
        type = "bullish"
        bottom = 99.0
        top = 101.0
        midpoint = 100.0
        filled = False
        effectiveness = 1.0

    fake_ctx = SimpleNamespace(
        fvgs=[FakeFVG()],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    bundle = _bundle()
    bundle.timeframes["4h"] = _candles()
    bundle.indicators["4h"] = dict(bundle.indicators["1h"])
    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 60.0)
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h", "4h"])
    monkeypatch.setattr(settings.scanner, "mtf_confirmation_bonus", 6.0)

    candidates = await MarketScannerService()._build_candidates(bundle)

    assert len(candidates) == 1
    assert candidates[0].direction == "long"
    assert set(candidates[0].fused_timeframes) == {"1h", "4h"}
    assert candidates[0].fusion_summary["confirmations"] == 2


@pytest.mark.asyncio
async def test_direction_conflict_keeps_highest_score(monkeypatch):
    audits = []

    async def fake_audit(run_id, event_type, **kwargs):
        audits.append((event_type, kwargs))

    service = MarketScannerService()
    monkeypatch.setattr(service, "_audit", fake_audit)
    bundle = _bundle()
    long_candidate = _candidate("long", 72.0, "long-low")
    short_candidate = _candidate("short", 84.0, "short-high")

    kept, dropped = await service._resolve_direction_conflicts(
        "conflict-run",
        [(long_candidate, bundle), (short_candidate, bundle)],
    )

    assert dropped == 1
    assert len(kept) == 1
    assert kept[0][0].direction == "short"
    assert audits[0][0] == "direction_conflict"
    assert audits[0][1]["direction"] == "long"


@pytest.mark.asyncio
async def test_portfolio_risk_blocks_correlated_same_direction(monkeypatch, db_session):
    now = utcnow()
    db_session.add(PositionModel(
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=100.0,
        quantity=1.0,
        opened_at=now,
    ))
    await db_session.flush()
    audits = []

    async def fake_audit(run_id, event_type, **kwargs):
        audits.append((event_type, kwargs))

    service = MarketScannerService()
    monkeypatch.setattr(service, "_audit", fake_audit)
    monkeypatch.setattr(settings.scanner, "portfolio_risk_enabled", True)
    monkeypatch.setattr(settings.scanner, "max_same_direction_exposure", 1)
    monkeypatch.setattr(settings.scanner, "max_correlated_signals_per_run", 2)

    kept, dropped = await service._apply_portfolio_risk_filters("portfolio-run", [(_candidate("long", 90.0), _bundle())])

    assert kept == []
    assert dropped == 1
    assert audits[0][0] == "portfolio_risk"


@pytest.mark.asyncio
async def test_scan_once_dispatches_candidate_and_records_audit(monkeypatch, db_session):
    candidate = ScannerCandidate(
        watch_symbol="BTCUSDT",
        exchange_symbol="BTCUSDT",
        exchange_name="binance",
        market_type="contract",
        data_source="ccxt",
        mapped_asset=False,
        direction="long",
        timeframe="1h",
        current_price=100.0,
        entry_reference=99.5,
        score=88.0,
        setup_type="bullish_fvg",
        price_zone="discount:99.5",
        setup_hash="scan-once-candidate",
        reasons=["test candidate"],
    )

    class FakeProvider:
        async def get_bundle(self, watch_symbol, timeframes=None):
            return _bundle()

    async def fake_process(self, signal, **kwargs):
        return {"status": "observed", "reason": "test", "analysis": {"confidence": 0.8}}

    monkeypatch.setattr(settings.scanner, "enabled", True)
    monkeypatch.setattr(settings.scanner, "mode", "observe")
    monkeypatch.setattr(settings.scanner, "watchlist", ["BTCUSDT"])
    monkeypatch.setattr(settings.scanner, "timeframes", ["1h"])
    monkeypatch.setattr(settings.scanner, "max_candidates_per_run", 1)
    monkeypatch.setattr(settings.scanner, "max_ai_calls_per_day", 10)
    monkeypatch.setattr(settings.scanner, "max_signals_per_day", 10)
    monkeypatch.setattr("services.signal_processor.SignalProcessor.process_scanner_signal", fake_process)

    service = MarketScannerService(provider=FakeProvider())

    async def fake_build_candidates(bundle):
        return [candidate]

    monkeypatch.setattr(service, "_build_candidates", fake_build_candidates)

    result = await service.scan_once()

    assert result["status"] == "ok"
    assert result["processed"][0]["status"] == "observed"
    audits = await list_scanner_audits(db_session, event_type="sent_to_ai")
    assert audits
    assert audits[0].setup_hash == "scan-once-candidate"


@pytest.mark.asyncio
async def test_live_market_validation_blocks_missing_market(monkeypatch):
    candidate = ScannerCandidate(
        watch_symbol="XAUUSD",
        exchange_symbol="PAXGUSDT",
        exchange_name="binance",
        market_type="spot",
        data_source="yfinance",
        mapped_asset=True,
        direction="long",
        timeframe="1h",
        current_price=3000.0,
        entry_reference=2990.0,
        score=88.0,
        setup_type="bullish_fvg",
        price_zone="discount:2990",
        setup_hash="missing-live-market",
        reasons=["test candidate"],
    )

    monkeypatch.setattr(settings.scanner, "mode", "live")
    monkeypatch.setattr(settings.scanner, "live_symbol_whitelist", ["PAXGUSDT"])
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr("exchange.get_market_limits", lambda *args, **kwargs: {})

    ok, reason, limits = await MarketScannerService()._validate_live_market(candidate)

    assert not ok
    assert limits == {}
    assert "not available" in reason


@pytest.mark.asyncio
async def test_scanner_rejection_summary_counts_ai_reject(db_session):
    await record_scanner_audit(
        db_session,
        scope="admin",
        run_id="summary-test",
        event_type="result",
        watch_symbol="BTCUSDT",
        exchange_symbol="BTCUSDT",
        direction="long",
        score=75.0,
        setup_hash="summary-hash",
        reason="AI rejected",
        payload={
            "result": {
                "status": "observed",
                "would_execute": False,
                "reason": "AI rejected",
                "analysis": {"recommendation": "reject", "reasoning": "No clean edge"},
            }
        },
    )
    await db_session.commit()

    summary = await get_scanner_rejection_summary(db_session, scope="admin")

    assert summary["rejected_or_held"] >= 1
    assert summary["reject"] >= 1
    assert summary["symbols"]["BTCUSDT"] >= 1


@pytest.mark.asyncio
async def test_ema200_alignment_adds_score_counter_with_penalty(monkeypatch):
    class FakeFVG:
        type = "bullish"
        bottom = 99.0
        top = 101.0
        midpoint = 100.0
        filled = False
        effectiveness = 1.0

    fake_ctx = SimpleNamespace(
        fvgs=[FakeFVG()],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 55.0)
    monkeypatch.setattr(settings.scanner, "ema200_enabled", True)

    bundle = _bundle()
    bundle.indicators["1h"]["ema200"] = 90.0  # current=100, ema200=90 => long above ema200
    bundle.indicators["1h"]["ema_slow"] = 95.0
    bundle.indicators["1h"]["ema_fast"] = 98.0

    candidates = await MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "EMA200 bullish alignment" in reasons


@pytest.mark.asyncio
async def test_ema200_counter_trend_penalizes_score(monkeypatch):
    fake_ctx = SimpleNamespace(
        fvgs=[SimpleNamespace(type="bullish", bottom=99.0, top=101.0, midpoint=100.0, filled=False, effectiveness=1.0)],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 0.0)
    monkeypatch.setattr(settings.scanner, "ema200_enabled", True)
    monkeypatch.setattr(settings.scanner, "rsi_lower", 35.0)
    monkeypatch.setattr(settings.scanner, "rsi_upper", 65.0)

    bundle = _bundle()
    bundle.indicators["1h"]["ema200"] = 110.0   # long signal, price=100 below ema200=110 => EMA200 conflict
    bundle.indicators["1h"]["rsi"] = 30.0        # <35 => long
    bundle.indicators["1h"]["ema_slow"] = 98.0
    bundle.indicators["1h"]["ema_fast"] = 102.0  # ema_fast(102) > ema_slow(98) => ema bullish

    candidates = await MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "EMA200 conflict penalized" in reasons


@pytest.mark.asyncio
async def test_oi_confirmation_adds_score(monkeypatch):
    class FakeFVG:
        type = "bullish"
        bottom = 99.0
        top = 101.0
        midpoint = 100.0
        filled = False
        effectiveness = 1.0

    fake_ctx = SimpleNamespace(
        fvgs=[FakeFVG()],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 55.0)

    bundle = _bundle()
    bundle.oi_change_pct = 5.0  # rising OI confirms long

    candidates = await MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "OI rising" in reasons


@pytest.mark.asyncio
async def test_ranging_regime_penalizes_score(monkeypatch):
    class FakeFVG:
        type = "bullish"
        bottom = 99.0
        top = 101.0
        midpoint = 100.0
        filled = False
        effectiveness = 1.0

    fake_ctx = SimpleNamespace(
        fvgs=[FakeFVG()],
        order_blocks=[],
        structure=SimpleNamespace(trend="bullish"),
        premium_zone=110.0,
        discount_zone=101.0,
        equilibrium=105.0,
        risk_score=0.2,
        entry_timing_score=0.9,
        timing_recommendation="Good",
    )

    def fake_analyze(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("smc_analyzer.analyze_smc_single_tf", fake_analyze)
    monkeypatch.setattr(settings.scanner, "min_score", 0.0)
    monkeypatch.setattr(settings.scanner, "regime_filter_enabled", True)

    bundle = _bundle()
    bundle.indicators["1h"]["market_regime"] = "ranging"
    bundle.indicators["1h"]["adx"] = 15.0

    candidates = await MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "ranging market regime" in reasons


@pytest.mark.asyncio
async def test_sync_scanner_outcomes_records_real_pnl_label(monkeypatch, db_session):
    now = utcnow()
    open_trade = TradeModel(
        id="open-scanner-trade",
        timestamp=now - timedelta(hours=3),
        ticker="BTCUSDT",
        direction="long",
        execute=True,
        order_status="filled",
        strategy_name="AI_Auto_Scanner",
        signal_source="auto_scanner",
        payload_json=json.dumps({
            "signal": {"strategy": "AI_Auto_Scanner", "price": 100.0, "timeframe": "1h"},
            "scanner": {
                "setup_hash": "outcome-setup",
                "score": 82.0,
                "timeframe": "1h",
                "score_breakdown": {"smc_trend": 18.0},
            },
        }),
    )
    position = PositionModel(
        id="scanner-position",
        ticker="BTCUSDT",
        direction="long",
        status="closed",
        entry_price=101.0,
        exit_price=104.0,
        quantity=1.0,
        remaining_quantity=0.0,
        opened_at=now - timedelta(hours=3),
        closed_at=now - timedelta(hours=1),
        open_trade_id="open-scanner-trade",
        strategy_name="AI_Auto_Scanner",
        pnl_pct=3.0,
        close_reason="take_profit",
    )
    db_session.add(open_trade)
    db_session.add(position)
    await db_session.flush()

    result = await sync_scanner_outcomes(db_session, include_path_metrics=False)
    summary = await compute_outcome_summary(db_session)
    factors = await compute_factor_performance(db_session)

    assert result["synced"] == 1
    assert summary["wins"] == 1
    assert summary["avg_entry_slippage_pct"] == pytest.approx(1.0)
    assert factors["factors"]["smc_trend"]["expectancy_pct"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_walk_forward_thresholds_use_real_outcome_labels(db_session, monkeypatch):
    # Pin the minimum sample count so the 12 labels below are sufficient even in
    # environments without a .env file (CI), where the default is higher.
    monkeypatch.setattr(settings.scanner, "walk_forward_min_samples", 12)
    now = utcnow()
    for idx in range(12):
        score = 80.0 + idx if idx >= 6 else 55.0 + idx
        pnl = 2.0 if idx >= 6 else -1.0
        await record_scanner_audit(
            db_session,
            scope="admin",
            run_id="wf-test",
            event_type="outcome_label",
            watch_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            direction="long",
            score=score,
            setup_hash=f"wf-{idx}",
            reason="win" if pnl > 0 else "loss",
            payload={
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "direction": "long",
                "score": score,
                "pnl_pct": pnl,
                "closed_at": (now - timedelta(minutes=12 - idx)).isoformat(),
            },
        )
    await db_session.flush()

    thresholds = await compute_walk_forward_thresholds(db_session)
    exact = thresholds["thresholds"]["BTCUSDT|1h|long"]

    assert exact["threshold"] >= 65.0
    assert exact["expectancy_pct"] > 0
