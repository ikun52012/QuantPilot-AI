"""Tests for exchange order sizing metadata."""

from unittest.mock import patch

import pytest

from exchange import _simulate_order
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
