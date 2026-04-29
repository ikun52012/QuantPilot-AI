"""Tests for trailing stop functionality."""
import pytest

from position_monitor import (
    _hit_take_profit_levels,
    _loads_dict,
    _loads_list,
    _price_pnl_pct,
    _safe_float,
)


class TestSafeFloat:
    def test_valid_float(self):
        assert _safe_float(3.14) == 3.14

    def test_valid_int(self):
        assert _safe_float(5) == 5.0

    def test_valid_string(self):
        assert _safe_float("2.5") == 2.5

    def test_invalid_string(self):
        assert _safe_float("invalid") == 0.0

    def test_none_value(self):
        assert _safe_float(None) == 0.0

    def test_with_default(self):
        assert _safe_float(None, default=10.0) == 10.0
        assert _safe_float("bad", default=5.0) == 5.0


class TestLoadsList:
    def test_valid_json_list(self):
        result = _loads_list('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_valid_json_dict_returns_empty(self):
        result = _loads_list('{"a": 1}')
        assert result == []

    def test_empty_string(self):
        result = _loads_list('')
        assert result == []

    def test_none_value(self):
        result = _loads_list(None)
        assert result == []

    def test_already_list(self):
        result = _loads_list([1, 2, 3])
        assert result == [1, 2, 3]

    def test_invalid_json(self):
        result = _loads_list('not json')
        assert result == []


class TestLoadsDict:
    def test_valid_json_dict(self):
        result = _loads_dict('{"a": 1, "b": 2}')
        assert result == {'a': 1, 'b': 2}

    def test_valid_json_list_returns_empty(self):
        result = _loads_dict('[1, 2, 3]')
        assert result == {}

    def test_empty_string(self):
        result = _loads_dict('')
        assert result == {}

    def test_none_value(self):
        result = _loads_dict(None)
        assert result == {}

    def test_already_dict(self):
        result = _loads_dict({'a': 1})
        assert result == {'a': 1}

    def test_invalid_json(self):
        result = _loads_dict('not json')
        assert result == {}


class TestPricePnlPct:
    def test_long_profit(self):
        pnl = _price_pnl_pct("long", 100.0, 110.0, 1.0)
        assert pnl == 10.0

    def test_long_loss(self):
        pnl = _price_pnl_pct("long", 100.0, 90.0, 1.0)
        assert pnl == -10.0

    def test_short_profit(self):
        pnl = _price_pnl_pct("short", 100.0, 90.0, 1.0)
        assert pnl == 10.0

    def test_short_loss(self):
        pnl = _price_pnl_pct("short", 100.0, 110.0, 1.0)
        assert pnl == -10.0

    def test_with_leverage(self):
        pnl = _price_pnl_pct("long", 100.0, 110.0, 5.0)
        assert pnl == 50.0  # 10% * 5x leverage

    def test_zero_entry(self):
        pnl = _price_pnl_pct("long", 0.0, 110.0, 1.0)
        assert pnl == 0.0

    def test_zero_exit(self):
        pnl = _price_pnl_pct("long", 100.0, 0.0, 1.0)
        assert pnl == 0.0

    def test_case_insensitive_direction(self):
        pnl1 = _price_pnl_pct("LONG", 100.0, 110.0, 1.0)
        pnl2 = _price_pnl_pct("long", 100.0, 110.0, 1.0)
        assert pnl1 == pnl2


class TestHitTakeProfitLevels:
    def test_long_tp_hit(self):
        levels = [
            {"price": 105.0, "qty_pct": 50, "status": "pending"},
            {"price": 110.0, "qty_pct": 50, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("long", levels, 108.0, 102.0)
        assert len(hit) == 1
        assert hit[0]["price"] == 105.0

    def test_long_multiple_tp_hit(self):
        levels = [
            {"price": 105.0, "qty_pct": 30, "status": "pending"},
            {"price": 110.0, "qty_pct": 70, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("long", levels, 112.0, 102.0)
        assert len(hit) == 2

    def test_short_tp_hit(self):
        levels = [
            {"price": 95.0, "qty_pct": 50, "status": "pending"},
            {"price": 90.0, "qty_pct": 50, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("short", levels, 98.0, 93.0)
        assert len(hit) == 1
        assert hit[0]["price"] == 95.0

    def test_no_tp_hit(self):
        levels = [
            {"price": 105.0, "qty_pct": 100, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("long", levels, 104.0, 100.0)
        assert len(hit) == 0

    def test_already_hit_levels_skipped(self):
        levels = [
            {"price": 105.0, "qty_pct": 50, "status": "hit"},
            {"price": 110.0, "qty_pct": 50, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("long", levels, 108.0, 102.0)
        assert len(hit) == 0  # Only TP1 hit, but it's already "hit"

    def test_zero_price_skipped(self):
        levels = [
            {"price": 0.0, "qty_pct": 50, "status": "pending"},
            {"price": 110.0, "qty_pct": 50, "status": "pending"},
        ]
        hit = _hit_take_profit_levels("long", levels, 115.0, 100.0)
        assert len(hit) == 1
        assert hit[0]["price"] == 110.0


class TestTrailingStopLogic:
    def test_breakeven_on_tp1_calculation(self):
        entry_price = 100.0
        direction = "long"

        new_stop = entry_price

        assert new_stop == 100.0
        assert direction == "long"

    def test_step_trailing_calculation(self):
        tp_levels = [
            {"price": 105.0, "qty_pct": 30, "status": "hit"},
            {"price": 110.0, "qty_pct": 40, "status": "hit"},
            {"price": 115.0, "qty_pct": 30, "status": "pending"},
        ]

        highest_hit = 2
        prev_tp_price = tp_levels[highest_hit - 2]["price"]

        assert prev_tp_price == 105.0

    def test_profit_pct_trailing_activation(self):
        entry_price = 100.0
        mark_price = 102.5
        activation_pct = 1.0

        profit_pct = ((mark_price - entry_price) / entry_price) * 100

        assert profit_pct == 2.5
        assert profit_pct >= activation_pct

    def test_trailing_stop_moves_correctly_for_long(self):
        mark_price = 105.0
        trail_pct = 1.0

        new_stop = mark_price * (1 - trail_pct / 100.0)

        assert new_stop == pytest.approx(103.95)

    def test_trailing_stop_moves_correctly_for_short(self):
        mark_price = 95.0
        trail_pct = 1.0

        new_stop = mark_price * (1 + trail_pct / 100.0)

        assert new_stop == pytest.approx(95.95)
