"""Tests for exchange order sizing metadata."""

from unittest.mock import patch

import pytest

from exchange import _simulate_order, _validate_and_adjust_amount
from models import AIAnalysis, SignalDirection, TradeDecision


def test_paper_order_reports_capped_leverage_for_margin_tracking():
    decision = TradeDecision(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        quantity=20.0,
        entry_price=100.0,
        execute=True,
        ai_analysis=AIAnalysis(recommendation="execute", confidence=0.8, recommended_leverage=50),
    )

    with patch("exchange.get_market_limits", return_value={"contract_size": 1.0}):
        result = _simulate_order(decision, {"max_leverage": 20, "market_type": "contract"})

    assert result["recommended_leverage"] == 20
    assert result["notional_value"] == pytest.approx(2000.0)


def test_validate_amount_raises_when_close_amount_below_minimum():
    class FakeExchange:
        id = "binance"

        def load_markets(self):
            return {
                "BTC/USDT:USDT": {
                    "limits": {"amount": {"min": 1.0}},
                    "precision": {"amount": 3},
                }
            }

    with pytest.raises(ValueError, match="cannot increase for close order"):
        _validate_and_adjust_amount(FakeExchange(), "BTC/USDT:USDT", 0.5, allow_increase=False)
