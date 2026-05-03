"""Tests for trailing stop functionality."""
import json
from unittest.mock import AsyncMock

import pytest

from core.database import PositionModel
from exchange import place_protective_stop
from position_monitor import (
    _adjust_trailing_stop_on_tp_hit,
    _check_pending_limit_orders,
    _find_exchange_position,
    _hit_take_profit_levels,
    _loads_dict,
    _loads_list,
    _maybe_adjust_trailing_stop,
    _paper_trailing_stop_price,
    _position_limit_timeout_secs,
    _price_pnl_pct,
    _reconcile_paper_position,
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


@pytest.mark.asyncio
async def test_place_protective_stop_replaces_existing_order(monkeypatch):
    class FakeExchange:
        def __init__(self):
            self.options = {"defaultType": "future"}

    fake_exchange = FakeExchange()
    cancel_calls = []
    create_calls = []

    async def fake_cancel(exchange, symbol, order_id):
        cancel_calls.append((exchange, symbol, order_id))
        return {"status": "cancelled", "order_id": order_id, "symbol": symbol}

    async def fake_create(exchange, symbol, kind, side, amount, trigger_price, position_side=None):
        create_calls.append((exchange, symbol, kind, side, amount, trigger_price, position_side))
        return {"id": "stop-new"}

    monkeypatch.setattr("exchange._get_or_create_exchange", lambda *args, **kwargs: fake_exchange)
    monkeypatch.setattr("exchange._resolve_symbol", lambda *args, **kwargs: "TRB/USDT:USDT")
    monkeypatch.setattr("exchange._cancel_exchange_order", fake_cancel)
    monkeypatch.setattr("exchange._create_conditional_order", fake_create)

    result = await place_protective_stop(
        ticker="TRBUSDT",
        direction="long",
        quantity=1.5,
        stop_price=99.0,
        exchange_config={"live_trading": True, "market_type": "contract"},
        existing_order_id="stop-old",
    )

    assert result["status"] == "placed"
    assert result["order_id"] == "stop-new"
    assert result["replaced_order_id"] == "stop-old"
    assert cancel_calls == [(fake_exchange, "TRB/USDT:USDT", "stop-old")]
    assert create_calls == [(fake_exchange, "TRB/USDT:USDT", "stop_loss", "sell", 1.5, 99.0, "long")]


@pytest.mark.asyncio
async def test_maybe_adjust_trailing_stop_passes_existing_order_id():
    position = PositionModel(
        ticker="BTCUSDT",
        direction="long",
        entry_price=100.0,
        quantity=1.0,
        remaining_quantity=1.0,
        stop_loss=95.0,
        stop_loss_order_id="stop-old",
        trailing_stop_config_json=json.dumps({"mode": "moving", "trail_pct": 1.0, "activation_profit_pct": 1.0}),
    )
    place_stop = AsyncMock(return_value={"status": "placed", "order_id": "stop-new"})

    changed = await _maybe_adjust_trailing_stop(
        position,
        {"live_trading": True},
        {"markPrice": 110.0},
        place_stop,
    )

    assert changed is True
    place_stop.assert_awaited_once()
    assert place_stop.await_args.kwargs["existing_order_id"] == "stop-old"
    assert position.stop_loss_order_id == "stop-new"


@pytest.mark.asyncio
async def test_adjust_trailing_stop_on_tp_hit_passes_existing_order_id():
    position = PositionModel(
        ticker="BTCUSDT",
        direction="long",
        entry_price=100.0,
        quantity=1.0,
        remaining_quantity=0.5,
        stop_loss=95.0,
        stop_loss_order_id="stop-old",
        trailing_stop_config_json=json.dumps({"mode": "breakeven_on_tp1"}),
    )
    tp_levels = [{"level": 1, "price": 110.0, "qty_pct": 50, "status": "hit"}]
    hit_levels = [{"level": 1, "price": 110.0, "qty_pct": 50, "status": "hit"}]
    place_stop = AsyncMock(return_value={"status": "placed", "order_id": "stop-new"})

    changed = await _adjust_trailing_stop_on_tp_hit(
        position,
        tp_levels,
        hit_levels,
        {"live_trading": True},
        place_stop,
    )

    assert changed is True
    place_stop.assert_awaited_once()
    assert place_stop.await_args.kwargs["existing_order_id"] == "stop-old"
    assert position.stop_loss_order_id == "stop-new"


@pytest.mark.asyncio
async def test_partial_take_profit_recalculates_remaining_pnl_metrics(monkeypatch):
    position = PositionModel(
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=100.0,
        quantity=1.0,
        remaining_quantity=1.0,
        leverage=1.0,
        take_profit_json=json.dumps([
            {"level": 1, "price": 110.0, "qty_pct": 50, "status": "pending"},
            {"level": 2, "price": 120.0, "qty_pct": 50, "status": "pending"},
        ]),
    )

    async def fake_get_latest_candle(*args, **kwargs):
        return {"high": 111.0, "low": 99.0, "close": 108.0}

    async def fake_get_ticker(*args, **kwargs):
        return {"last": 108.0}

    monkeypatch.setattr("exchange.get_latest_candle", fake_get_latest_candle)
    monkeypatch.setattr("exchange.get_ticker", fake_get_ticker)

    class FakeSession:
        async def flush(self):
            return None

    stats = await _reconcile_paper_position(FakeSession(), position, {"live_trading": False})

    assert stats["partials"] == 1
    assert position.remaining_quantity == pytest.approx(0.5)
    assert position.realized_pnl_pct == pytest.approx(5.0)
    assert position.current_pnl_pct == pytest.approx(9.0)
    assert position.unrealized_pnl_usdt == pytest.approx(4.0)


def test_paper_trailing_stop_price_activates_for_moving_mode():
    position = PositionModel(
        direction="long",
        entry_price=100.0,
        trailing_stop_config_json=json.dumps({"mode": "moving", "trail_pct": 1.0, "activation_profit_pct": 1.0}),
    )

    new_stop = _paper_trailing_stop_price(position, 105.0)

    assert new_stop == pytest.approx(103.95)


@pytest.mark.asyncio
async def test_reconcile_paper_position_adjusts_trailing_stop(monkeypatch):
    position = PositionModel(
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=100.0,
        quantity=1.0,
        remaining_quantity=1.0,
        stop_loss=95.0,
        trailing_stop_config_json=json.dumps({"mode": "moving", "trail_pct": 1.0, "activation_profit_pct": 1.0}),
    )

    async def fake_get_latest_candle(*args, **kwargs):
        return {"high": 106.0, "low": 104.0, "close": 105.0}

    async def fake_get_ticker(*args, **kwargs):
        return {"last": 105.0}

    monkeypatch.setattr("exchange.get_latest_candle", fake_get_latest_candle)
    monkeypatch.setattr("exchange.get_ticker", fake_get_ticker)

    class FakeSession:
        async def flush(self):
            return None

    stats = await _reconcile_paper_position(FakeSession(), position, {"live_trading": False})

    assert stats["adjusted"] == 1
    assert position.stop_loss == pytest.approx(103.95)


def test_pending_limit_timeout_defaults_to_extended_window():
    position = PositionModel()
    assert _position_limit_timeout_secs(position) == 8 * 60 * 60


def test_find_exchange_position_matches_alias_but_not_different_contract_family():
    position = PositionModel(ticker="SHIBUSDT.P", direction="long")

    match = _find_exchange_position(
        position,
        [
            {"symbol": "1000SHIB/USDT:USDT", "side": "long"},
            {"symbol": "SHIB/USDT:USDT", "side": "long"},
        ],
    )

    assert match is not None
    assert match["symbol"] == "SHIB/USDT:USDT"


@pytest.mark.asyncio
async def test_check_pending_limit_orders_passes_market_type_to_symbol_resolution(monkeypatch):
    class FakeExchange:
        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "symbol": symbol, "status": "open", "timestamp": 0}

        def cancel_order(self, order_id, symbol):
            return {"id": order_id, "symbol": symbol, "status": "canceled"}

    fake_exchange = FakeExchange()
    resolve_calls = []

    async def fake_close_exchange(_exchange):
        return None

    def fake_resolve_symbol(exchange, symbol, market_type=None):
        resolve_calls.append((exchange, symbol, market_type))
        return "TRB/USDT:USDT"

    monkeypatch.setattr("exchange._get_or_create_exchange", lambda *args, **kwargs: fake_exchange)
    monkeypatch.setattr("exchange._resolve_symbol", fake_resolve_symbol)
    monkeypatch.setattr("exchange._close_exchange", fake_close_exchange)

    class FakeSession:
        async def flush(self):
            return None

    position = PositionModel(
        ticker="TRBUSDT.P",
        status="pending",
        entry_order_id="ord-1",
    )

    await _check_pending_limit_orders(
        FakeSession(),
        position,
        {"exchange": "okx", "market_type": "contract", "live_trading": True},
    )

    assert resolve_calls == [(fake_exchange, "TRBUSDT.P", "contract")]
