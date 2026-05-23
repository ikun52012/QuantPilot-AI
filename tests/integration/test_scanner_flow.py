import pytest
from sqlalchemy import select

pytest.importorskip("greenlet")

from core.database import TradeModel
from models import AIAnalysis, MarketContext, PreFilterResult, SignalDirection, TradingViewSignal
from services.signal_processor import SignalProcessor


@pytest.mark.asyncio
async def test_scanner_signal_paper_flow_records_auto_source(monkeypatch, db_session):
    signal = TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        price=50000.0,
        timeframe="1h",
        strategy="AI_Auto_Scanner",
        message='{"source":"auto_scanner","score":88}',
    )
    market = MarketContext(
        ticker="BTCUSDT",
        current_price=50000.0,
        price_change_1h=1.0,
        price_change_4h=2.0,
        price_change_24h=3.0,
        volume_24h=1_000_000,
        high_24h=52000.0,
        low_24h=48000.0,
        bid_ask_spread=0.01,
        rsi_1h=42.0,
        atr_pct=1.0,
        ema_fast=50500.0,
        ema_slow=49000.0,
    )

    async def fake_prefilter(self, signal, market, user_id, user_settings=None):
        return PreFilterResult(passed=True, reason="ok", checks={}, score=92.0)

    async def fake_analyze(signal, market, user_settings=None):
        return AIAnalysis(
            confidence=0.82,
            recommendation="execute",
            reasoning="Clean scanner setup with valid levels",
            suggested_stop_loss=49000.0,
            suggested_tp1=53000.0,
            suggested_tp2=55000.0,
            tp1_qty_pct=50.0,
            tp2_qty_pct=50.0,
            tp3_qty_pct=0.0,
            tp4_qty_pct=0.0,
            position_size_pct=0.25,
            recommended_leverage=2.0,
            risk_score=0.3,
            market_condition="trending_up",
            trend_strength="moderate",
        )

    async def fake_execute_trade(decision, exchange_config):
        return {
            "status": "simulated",
            "order_id": "paper-scanner-1",
            "ticker": decision.ticker,
            "quantity": decision.quantity,
        }

    monkeypatch.setattr(SignalProcessor, "_run_prefilter", fake_prefilter)
    monkeypatch.setattr("services.signal_processor.analyze_signal", fake_analyze)
    monkeypatch.setattr("services.signal_processor.execute_trade", fake_execute_trade)

    processor = SignalProcessor(db_session)
    result = await processor.process_scanner_signal(
        signal,
        scanner_mode="paper",
        scanner_payload={
            "alert_id": "scanner-flow-test",
            "signal_source": "auto_scanner",
            "scanner": {"setup_hash": "scanner-flow-test", "score": 88},
        },
        market=market,
    )
    await db_session.commit()

    assert result["status"] == "simulated"
    row = (await db_session.execute(select(TradeModel))).scalars().first()
    assert row is not None
    assert row.signal_source == "auto_scanner"
