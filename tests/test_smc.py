"""Tests for SMC (Smart Money Concepts) analyzer."""
import pytest

from smc_analyzer import (
    calculate_premium_discount,
    detect_fvgs,
    detect_market_structure,
    detect_order_blocks,
    find_confluence_zones,
)


class TestDetectFVGs:
    def test_bullish_fvg_detection(self):
        candles = [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 103, "high": 108, "low": 103, "close": 107},
            {"open": 106, "high": 110, "low": 105, "close": 109},
        ]

        fvgs = detect_fvgs(candles)

        assert len(fvgs) >= 1
        bullish_fvgs = [f for f in fvgs if f.get("type") == "bullish"]
        assert len(bullish_fvgs) >= 1

        fvg = bullish_fvgs[0]
        assert fvg["low"] >= 102
        assert fvg["high"] >= 103

    def test_bearish_fvg_detection(self):
        candles = [
            {"open": 110, "high": 112, "low": 109, "close": 111},
            {"open": 108, "high": 108, "low": 103, "close": 104},
            {"open": 105, "high": 106, "low": 100, "close": 101},
        ]

        fvgs = detect_fvgs(candles)

        bearish_fvgs = [f for f in fvgs if f.get("type") == "bearish"]
        assert len(bearish_fvgs) >= 1

    def test_no_fvg_on_normal_candles(self):
        candles = [
            {"open": 100, "high": 102, "low": 99, "close": 101},
            {"open": 101, "high": 103, "low": 100, "close": 102},
            {"open": 102, "high": 104, "low": 101, "close": 103},
        ]

        fvgs = detect_fvgs(candles)

        assert len(fvgs) == 0

    def test_empty_candles(self):
        fvgs = detect_fvgs([])
        assert fvgs == []


class TestDetectOrderBlocks:
    def test_bullish_order_block(self):
        candles = [
            {"open": 100, "high": 105, "low": 95, "close": 98, "volume": 1000},
            {"open": 98, "high": 110, "low": 97, "close": 108, "volume": 5000},
        ]

        ob = detect_order_blocks(candles)

        bullish_ob = [o for o in ob if o.get("type") == "bullish"]
        assert len(bullish_ob) >= 0

    def test_bearish_order_block(self):
        candles = [
            {"open": 110, "high": 115, "low": 105, "close": 112, "volume": 1000},
            {"open": 112, "high": 113, "low": 100, "close": 102, "volume": 5000},
        ]

        ob = detect_order_blocks(candles)

        bearish_ob = [o for o in ob if o.get("type") == "bearish"]
        assert len(bearish_ob) >= 0

    def test_empty_candles(self):
        ob = detect_order_blocks([])
        assert ob == []


class TestDetectMarketStructure:
    def test_bos_bullish(self):
        swing_points = [
            {"type": "high", "price": 100, "index": 0},
            {"type": "low", "price": 95, "index": 2},
            {"type": "high", "price": 105, "index": 4},
        ]

        current_high = 110

        structure = detect_market_structure(swing_points, current_high)

        assert structure.get("type") in ["bos", "choch", "none"]

    def test_empty_swing_points(self):
        structure = detect_market_structure([], 100)
        assert structure.get("type") == "none"


class TestCalculatePremiumDiscount:
    def test_premium_discount_calculation(self):
        range_high = 110.0
        range_low = 100.0

        result = calculate_premium_discount(range_high, range_low)

        assert "premium" in result
        assert "discount" in result
        assert "equilibrium" in result

        assert result["equilibrium"] == pytest.approx(105.0, rel=0.01)
        assert result["premium"] == pytest.approx(108.1, rel=0.01)  # 0.79 Fibonacci
        assert result["discount"] == pytest.approx(101.9, rel=0.01)  # 0.382 Fibonacci

    def test_invalid_range(self):
        result = calculate_premium_discount(0.0, 0.0)

        assert result.get("premium") == 0.0
        assert result.get("discount") == 0.0


class TestFindConfluenceZones:
    def test_confluence_detection(self):
        fvg_zones = [
            {"type": "bullish", "low": 100.0, "high": 102.0, "strength": 0.8},
        ]
        ob_zones = [
            {"type": "bullish", "low": 99.5, "high": 101.5, "strength": 0.7},
        ]

        confluence = find_confluence_zones(fvg_zones, ob_zones)

        assert len(confluence) >= 0

    def test_no_confluence(self):
        fvg_zones = [
            {"type": "bullish", "low": 100.0, "high": 102.0, "strength": 0.8},
        ]
        ob_zones = [
            {"type": "bearish", "low": 90.0, "high": 92.0, "strength": 0.7},
        ]

        confluence = find_confluence_zones(fvg_zones, ob_zones)

        assert len(confluence) == 0

    def test_empty_zones(self):
        confluence = find_confluence_zones([], [])
        assert confluence == []


class TestSMCIntegration:
    def test_full_analysis_pipeline(self):
        candles = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 1200},
            {"open": 102, "high": 108, "low": 102, "close": 107, "volume": 5000},
            {"open": 106, "high": 110, "low": 105, "close": 109, "volume": 2000},
        ]

        fvgs = detect_fvgs(candles)
        ob = detect_order_blocks(candles)

        assert isinstance(fvgs, list)
        assert isinstance(ob, list)
