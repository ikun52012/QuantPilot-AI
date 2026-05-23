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
