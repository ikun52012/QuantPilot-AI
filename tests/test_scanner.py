from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("greenlet")

from core.config import settings
from core.database import get_scanner_rejection_summary, list_scanner_audits, record_scanner_audit
from core.utils.datetime import utcnow
from services.market_scanner import MarketScannerService, ScannerCandidate
from services.synthetic_signal import build_synthetic_signal
from services.unified_ohlcv import NormalizedCandle, OHLCVBundle, SymbolMapping


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
        timeframes={"1h": _candles()},
        indicators={
            "1h": {
                "rsi": 30.0,
                "atr_pct": 1.2,
                "ema_fast": 101.0,
                "ema_slow": 99.0,
            }
        },
        data_quality={"passed": True, "reasons": [], "spread_pct": 0.02, "primary_timeframe": "1h"},
    )


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


def test_pre_scan_generates_smc_candidate(monkeypatch):
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
    candidates = service._build_candidates(_bundle())

    assert candidates
    assert candidates[0].direction == "long"
    assert candidates[0].setup_type == "bullish_fvg"
    assert candidates[0].score >= 60.0


def test_pre_scan_fuses_multiple_timeframes_into_one_signal(monkeypatch):
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

    candidates = MarketScannerService()._build_candidates(bundle)

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
    monkeypatch.setattr(service, "_build_candidates", lambda bundle: [candidate])

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


def test_ema200_alignment_adds_score_counter_with_penalty(monkeypatch):
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

    candidates = MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "EMA200 bullish alignment" in reasons


def test_ema200_counter_trend_penalizes_score(monkeypatch):
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

    candidates = MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "EMA200 conflict penalized" in reasons


def test_oi_confirmation_adds_score(monkeypatch):
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

    candidates = MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "OI rising" in reasons


def test_ranging_regime_penalizes_score(monkeypatch):
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

    candidates = MarketScannerService()._build_candidates(bundle)
    assert candidates
    reasons = " ".join(candidates[0].reasons)
    assert "ranging market regime" in reasons
