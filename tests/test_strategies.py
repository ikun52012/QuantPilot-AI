"""Tests for DCA and Grid Strategies."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from models import SignalDirection
from strategies.dca import DCAConfig, DCAEngine, DCAEntry
from strategies.grid import GridConfig, GridEngine, GridLevel


class TestDCAConfig:
    def test_default_config(self):
        config = DCAConfig()
        assert config.max_entries == 5
        assert config.entry_spacing_pct == 2.0
        assert config.stop_loss_pct == 10.0
        assert config.take_profit_pct == 5.0
        assert config.mode == "average_down"

    def test_custom_config(self):
        config = DCAConfig(
            ticker="ETHUSDT",
            max_entries=7,
            entry_spacing_pct=3.0,
            sizing_method="martingale",
        )
        assert config.ticker == "ETHUSDT"
        assert config.max_entries == 7
        assert config.entry_spacing_pct == 3.0
        assert config.sizing_method == "martingale"


class TestDCAEngine:
    @pytest.fixture
    def engine(self):
        return DCAEngine()

    @pytest.fixture
    def config(self):
        return DCAConfig(
            ticker="BTCUSDT",
            direction="long",
            initial_capital_usdt=1000.0,
            max_entries=5,
            entry_spacing_pct=2.0,
            stop_loss_pct=10.0,
            take_profit_pct=5.0,
            sizing_method="fixed",
            activation_loss_pct=1.0,
        )

    def test_create_position(self, engine, config):
        position = engine.create_position(config, 50000.0)

        assert position.ticker == "BTCUSDT"
        assert position.direction == "long"
        assert len(position.entries) == 1
        assert position.entries_remaining == 4

    def test_position_sizing_fixed(self, engine, config):
        config.sizing_method = "fixed"
        position = engine.create_position(config, 50000.0)

        assert position.total_quantity > 0

    @pytest.mark.asyncio
    async def test_position_sizing_martingale(self, engine):
        config = DCAConfig(
            ticker="BTCUSDT",
            initial_capital_usdt=1000.0,
            sizing_method="martingale",
            sizing_multiplier=1.5,
        )
        position = engine.create_position(config, 50000.0)

        base_qty = position.entries[0].quantity

        await engine._add_entry(position.config_id, config, 49000.0)

        new_qty = position.entries[-1].quantity
        assert new_qty >= base_qty

    @pytest.mark.asyncio
    async def test_average_entry_calculation(self, engine, config):
        position = engine.create_position(config, 50000.0)

        initial_avg = position.average_entry_price

        await engine._add_entry(position.config_id, config, 49000.0)

        new_avg = position.average_entry_price

        assert new_avg < initial_avg

    def test_stop_loss_calculation(self, engine, config):
        position = engine.create_position(config, 50000.0)

        assert position.stop_loss_price > 0
        assert position.stop_loss_price < position.average_entry_price

    def test_take_profit_calculation(self, engine, config):
        position = engine.create_position(config, 50000.0)

        assert position.take_profit_price > 0
        assert position.take_profit_price > position.average_entry_price

    def test_dca_trigger_on_loss(self, engine, config):
        position = engine.create_position(config, 50000.0)

        result = engine._should_add_entry(position, config, 49000.0)

        assert result is True

    def test_dca_respects_next_entry_spacing(self, engine, config):
        position = engine.create_position(config, 50000.0)

        result = engine._should_add_entry(position, config, 49400.0)

        assert result is False

    @pytest.mark.asyncio
    async def test_max_entries_limit(self, engine):
        config = DCAConfig(
            ticker="BTCUSDT",
            max_entries=3,
            activation_loss_pct=0.5,
        )
        position = engine.create_position(config, 50000.0)

        await engine._add_entry(position.config_id, config, 49750.0)
        await engine._add_entry(position.config_id, config, 49500.0)

        assert len(position.entries) == 3
        assert position.entries_remaining == 0

    @pytest.mark.asyncio
    async def test_close_position(self, engine, config):
        position = engine.create_position(config, 50000.0)

        await engine._close_position(position.config_id, 52000.0, "take_profit")

        assert position.status == "closed"
        assert position.close_reason == "take_profit"

    @pytest.mark.asyncio
    async def test_live_close_failure_keeps_dca_position_active(self, engine, config, monkeypatch):
        position = engine.create_position(config, 50000.0)
        config.paper_mode = False
        engine.configs[position.config_id] = config

        execute_trade = AsyncMock(return_value={"status": "error", "reason": "not closed"})
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        with pytest.raises(RuntimeError):
            await engine._close_position(position.config_id, 49000.0, "stop_loss", {"live_trading": True})

        assert position.status == "active"
        assert position.close_reason == ""

    @pytest.mark.asyncio
    async def test_live_close_rejects_partial_closed_status(self, engine, config, monkeypatch):
        position = engine.create_position(config, 50000.0)
        config.paper_mode = False
        engine.configs[position.config_id] = config

        execute_trade = AsyncMock(return_value={"status": "partial_closed", "order_id": "close-1"})
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        with pytest.raises(RuntimeError):
            await engine._close_position(position.config_id, 49000.0, "stop_loss", {"live_trading": True})

        assert position.status == "active"
        decision = execute_trade.await_args.args[0]
        assert decision.idempotency_key == f"dca:{position.config_id}:close:stop_loss"

    @pytest.mark.asyncio
    async def test_live_close_cancels_all_dca_protection_orders(self, engine, config, monkeypatch):
        position = engine.create_position(config, 50000.0)
        config.paper_mode = False
        engine.configs[position.config_id] = config
        position.stop_loss_order_id = "sl-main"
        position.stop_loss_order_ids = ["sl-main", "sl-orphan"]
        position.take_profit_order_id = "tp-main"
        position.take_profit_order_ids = ["tp-main", "tp-orphan"]

        execute_trade = AsyncMock(return_value={"status": "closed", "order_id": "close-1"})
        cancel_order = AsyncMock(return_value={"status": "cancelled"})
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr("exchange.cancel_order", cancel_order)

        await engine._close_position(
            position.config_id,
            51000.0,
            "manual",
            {"live_trading": True},
        )

        assert position.status == "closed"
        assert position.stop_loss_order_id == ""
        assert position.take_profit_order_id == ""
        assert position.stop_loss_order_ids == []
        assert position.take_profit_order_ids == []
        assert {call.args[0] for call in cancel_order.await_args_list} == {
            "sl-main", "sl-orphan", "tp-main", "tp-orphan",
        }

    @pytest.mark.asyncio
    async def test_live_close_retries_cleanup_without_closing_position_twice(self, engine, config, monkeypatch):
        position = engine.create_position(config, 50000.0)
        config.paper_mode = False
        engine.configs[position.config_id] = config
        position.stop_loss_order_id = "sl-main"
        position.stop_loss_order_ids = ["sl-main"]

        execute_trade = AsyncMock(return_value={"status": "closed", "order_id": "close-1"})
        cancel_order = AsyncMock(return_value={"status": "error", "reason": "offline"})
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr("exchange.cancel_order", cancel_order)

        await engine._close_position(
            position.config_id,
            51000.0,
            "manual",
            {"live_trading": True},
        )

        assert position.status == "cleanup_required"
        assert position.stop_loss_order_id == "sl-main"

        cancel_order.return_value = {"status": "cancelled"}
        await engine._close_position(
            position.config_id,
            52000.0,
            "cleanup_retry",
            {"live_trading": True},
        )

        assert position.status == "closed"
        assert position.close_price == 51000.0
        execute_trade.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_add_entry_replaces_aggregate_protection(self, engine, config, monkeypatch):
        position = engine.create_position(config, 50000.0)
        position.stop_loss_order_id = "sl-old"
        position.take_profit_order_id = "tp-old"
        config.paper_mode = False
        engine.configs[position.config_id] = config

        monkeypatch.setattr("exchange.get_market_limits", lambda *args: {})
        monkeypatch.setattr(
            "exchange.execute_trade",
            AsyncMock(return_value={
                "status": "filled",
                "order_id": "entry-2",
                "entry_price": 49000.0,
                "filled_quantity": 0.02,
                "stop_loss_order_id": "sl-entry",
                "take_profit_order_id": "tp-entry",
            }),
        )
        place_stop = AsyncMock(return_value={"status": "placed", "order_id": "sl-new"})
        place_take_profit = AsyncMock(return_value={"status": "placed", "order_id": "tp-new"})
        cancel_order = AsyncMock(return_value={"status": "cancelled"})
        monkeypatch.setattr("exchange.place_protective_stop", place_stop)
        monkeypatch.setattr("exchange.place_protective_take_profit", place_take_profit)
        monkeypatch.setattr("exchange.cancel_order", cancel_order)

        result = await engine._add_entry(
            position.config_id,
            config,
            49000.0,
            {"live_trading": True},
        )

        assert result["success"] is True
        assert position.stop_loss_order_id == "sl-new"
        assert position.take_profit_order_id == "tp-new"
        assert place_stop.await_args.kwargs["existing_order_id"] == "sl-old"
        assert place_take_profit.await_args.kwargs["existing_order_id"] == "tp-old"
        cancelled_ids = {call.args[0] for call in cancel_order.await_args_list}
        assert cancelled_ids == {"sl-entry", "tp-entry"}


class TestGridConfig:
    def test_default_config(self):
        config = GridConfig()
        assert config.grid_count == 10
        assert config.grid_spacing_pct == 1.0
        assert config.spacing_mode == "arithmetic"
        assert config.mode == "neutral"

    def test_custom_config(self):
        config = GridConfig(
            ticker="ETHUSDT",
            grid_count=20,
            grid_spacing_pct=0.5,
            spacing_mode="geometric",
        )
        assert config.grid_count == 20
        assert config.grid_spacing_pct == 0.5
        assert config.spacing_mode == "geometric"


class TestGridEngine:
    @pytest.fixture
    def engine(self):
        return GridEngine()

    @pytest.fixture
    def config(self):
        return GridConfig(
            ticker="BTCUSDT",
            upper_price=52000.0,
            lower_price=48000.0,
            grid_count=10,
            total_capital_usdt=1000.0,
            spacing_mode="arithmetic",
        )

    def test_create_grid(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        assert grid.ticker == "BTCUSDT"
        assert grid.upper_price == 52000.0
        assert grid.lower_price == 48000.0
        assert len(grid.grid_levels) == 10

    def test_arithmetic_spacing(self, engine, config):
        config.spacing_mode = "arithmetic"
        grid = engine.create_grid(config, 50000.0)

        prices = [level.price for level in grid.grid_levels]

        diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]

        for diff in diffs:
            assert abs(diff - (52000 - 48000) / 10) < 1

    def test_geometric_spacing(self, engine):
        config = GridConfig(
            ticker="BTCUSDT",
            upper_price=52000.0,
            lower_price=48000.0,
            grid_count=10,
            spacing_mode="geometric",
        )
        grid = engine.create_grid(config, 50000.0)

        assert len(grid.grid_levels) == 10

    def test_buy_sell_distribution(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        buy_levels = [level for level in grid.grid_levels if level.side == "buy"]
        sell_levels = [level for level in grid.grid_levels if level.side == "sell"]

        for level in buy_levels:
            assert level.price < 50000.0

        for level in sell_levels:
            assert level.price > 50000.0

    def test_grid_trigger_on_price_movement(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        triggered = engine._find_triggered_levels(grid, 48500.0)

        buy_triggered = [level for level in triggered if level.side == "buy"]
        assert len(buy_triggered) > 0

    @pytest.mark.asyncio
    async def test_execute_grid_level(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        for level in grid.grid_levels[:3]:
            level.status = "pending"

        result = await engine._execute_grid_level(grid.config_id, grid.grid_levels[0], 49000.0, config)

        assert result["success"]

    @pytest.mark.asyncio
    async def test_grid_pairs_opposite_fills_once(self, engine):
        config = GridConfig(
            ticker="BTCUSDT",
            upper_price=110.0,
            lower_price=90.0,
            grid_count=2,
            total_capital_usdt=1000.0,
        )
        grid = engine.create_grid(config, 100.0)

        buy_level = next(level for level in grid.grid_levels if level.side == "buy")
        sell_level = next(level for level in grid.grid_levels if level.side == "sell")

        await engine._execute_grid_level(grid.config_id, buy_level, 95.0, config)
        await engine._execute_grid_level(grid.config_id, sell_level, 105.0, config)

        assert buy_level.status == "paired"
        assert sell_level.status == "paired"
        assert grid.total_trades == 1
        assert grid.realized_pnl_usdt > 0

        engine._update_pnl(grid, 100.0)
        assert grid.unrealized_pnl_usdt == 0.0

    @pytest.mark.asyncio
    async def test_close_grid_keeps_realized_pnl_without_double_counting_fees(self, engine):
        config = GridConfig(
            ticker="BTCUSDT",
            upper_price=110.0,
            lower_price=90.0,
            grid_count=2,
            total_capital_usdt=1000.0,
        )
        grid = engine.create_grid(config, 100.0)

        buy_level = next(level for level in grid.grid_levels if level.side == "buy")
        sell_level = next(level for level in grid.grid_levels if level.side == "sell")

        await engine._execute_grid_level(grid.config_id, buy_level, 95.0, config)
        await engine._execute_grid_level(grid.config_id, sell_level, 105.0, config)
        realized_before_close = grid.realized_pnl_usdt

        await engine._close_grid(grid.config_id, 100.0, "manual_close")

        assert grid.realized_pnl_usdt == pytest.approx(realized_before_close)

    def test_pnl_calculation(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        engine._update_pnl(grid, 50000.0)

        assert isinstance(grid.unrealized_pnl_usdt, float)

    @pytest.mark.asyncio
    async def test_close_grid(self, engine, config):
        grid = engine.create_grid(config, 50000.0)

        await engine._close_grid(grid.config_id, 53000.0, "out_of_range")

        assert grid.status == "closed"

    @pytest.mark.asyncio
    async def test_live_grid_fill_skips_duplicate_market_order_for_existing_limit(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        level = grid.grid_levels[0]
        level.order_id = "limit-1"
        level.exchange_order_status = "open"

        execute_trade = AsyncMock()
        get_recent_orders = AsyncMock(return_value=[{
            "id": "limit-1",
            "status": "closed",
            "filled": level.quantity,
            "average": level.price,
        }])
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr("exchange.get_recent_orders", get_recent_orders)

        result = await engine._execute_grid_level(grid.config_id, level, level.price, config, {"live_trading": True})

        assert result["success"] is True
        execute_trade.assert_not_awaited()
        get_recent_orders.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_grid_limit_order_must_be_confirmed_filled(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        level = grid.grid_levels[0]
        level.order_id = "limit-1"
        level.exchange_order_status = "open"

        execute_trade = AsyncMock()
        get_recent_orders = AsyncMock(return_value=[{"id": "limit-1", "status": "open", "filled": 0.0}])
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr("exchange.get_recent_orders", get_recent_orders)

        result = await engine._execute_grid_level(grid.config_id, level, level.price, config, {"live_trading": True})

        assert result["success"] is False
        assert level.status == "pending"
        execute_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_grid_existing_order_id_never_falls_back_to_market(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        level = grid.grid_levels[0]
        level.order_id = "limit-closed"
        level.exchange_order_status = "closed"

        execute_trade = AsyncMock()
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr(
            "exchange.get_recent_orders",
            AsyncMock(return_value=[{
                "id": "limit-closed",
                "status": "closed",
                "filled": level.quantity,
                "average": level.price,
            }]),
        )

        result = await engine._execute_grid_level(grid.config_id, level, level.price, config, {"live_trading": True})

        assert result["success"] is True
        execute_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_grid_protection_failure_closes_unprotected_fill(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        level = grid.grid_levels[0]
        level.order_id = "limit-closed"
        level.exchange_order_status = "closed"

        monkeypatch.setattr(
            "exchange.get_recent_orders",
            AsyncMock(return_value=[{
                "id": "limit-closed",
                "status": "closed",
                "filled": level.quantity,
                "average": level.price,
            }]),
        )
        monkeypatch.setattr(
            engine,
            "_place_live_fill_protection",
            AsyncMock(side_effect=RuntimeError("protection failed")),
        )
        execute_trade = AsyncMock(return_value={
            "status": "partial_closed",
            "order_id": "fail-safe-close",
            "closed_quantity": level.quantity,
            "exit_price": level.price,
        })
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        result = await engine._execute_grid_level(grid.config_id, level, level.price, config, {"live_trading": True})

        assert result["success"] is True
        assert result["closed_fail_safe"] is True
        assert level.status == "closed"
        assert grid.total_buy_quantity == 0
        assert grid.total_sell_quantity == 0
        decision = execute_trade.await_args.args[0]
        expected_direction = SignalDirection.CLOSE_LONG if level.side == "buy" else SignalDirection.CLOSE_SHORT
        assert decision.direction == expected_direction
        assert decision.quantity == level.quantity

    @pytest.mark.asyncio
    async def test_close_live_grid_cancels_pending_orders_instead_of_market_closing(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        pending_levels = grid.grid_levels[:2]
        for idx, level in enumerate(pending_levels, start=1):
            level.status = "pending"
            level.order_id = f"order-{idx}"
            level.exchange_order_status = "open"

        cancel_order = AsyncMock(return_value={"status": "cancelled"})
        execute_trade = AsyncMock()
        monkeypatch.setattr("exchange.cancel_order", cancel_order)
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        await engine._close_grid(grid.config_id, 53000.0, "out_of_range", {"live_trading": True})

        assert cancel_order.await_count == 2
        execute_trade.assert_not_awaited()
        assert all(level.exchange_order_status == "cancelled" for level in pending_levels)

    @pytest.mark.asyncio
    async def test_close_live_grid_closes_net_exposure_before_marking_closed(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        buy_level = next(level for level in grid.grid_levels if level.side == "buy")
        await engine._execute_grid_level(grid.config_id, buy_level, buy_level.price, config)
        config.paper_mode = False
        engine.configs[grid.config_id] = config

        cancel_order = AsyncMock(return_value={"status": "cancelled"})
        execute_trade = AsyncMock(return_value={"status": "closed", "order_id": "close-net"})
        monkeypatch.setattr("exchange.cancel_order", cancel_order)
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        await engine._close_grid(grid.config_id, 53000.0, "out_of_range", {"live_trading": True})

        execute_trade.assert_awaited_once()
        assert grid.status == "closed"
        decision = execute_trade.await_args.args[0]
        assert decision.idempotency_key == f"grid:{grid.config_id}:close:out_of_range"

    @pytest.mark.asyncio
    async def test_close_live_grid_cancels_entry_and_protection_orders(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        engine.configs[grid.config_id] = config
        pending_level = grid.grid_levels[0]
        pending_level.order_id = "entry-open"
        filled_level = grid.grid_levels[1]
        filled_level.status = "filled"
        filled_level.stop_loss_order_id = "sl-open"
        filled_level.take_profit_order_id = "tp-open"

        cancel_order = AsyncMock(return_value={"status": "cancelled"})
        monkeypatch.setattr("exchange.cancel_order", cancel_order)
        monkeypatch.setattr("exchange.execute_trade", AsyncMock())

        await engine._close_grid(
            grid.config_id,
            53000.0,
            "manual",
            {"live_trading": True},
        )

        assert grid.status == "closed"
        assert pending_level.order_id == ""
        assert filled_level.stop_loss_order_id == ""
        assert filled_level.take_profit_order_id == ""
        assert {call.args[0] for call in cancel_order.await_args_list} == {
            "entry-open", "sl-open", "tp-open",
        }

    @pytest.mark.asyncio
    async def test_close_live_grid_stops_before_flatten_when_cancel_fails(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        config.paper_mode = False
        engine.configs[grid.config_id] = config
        pending_level = grid.grid_levels[0]
        pending_level.order_id = "entry-open"

        cancel_order = AsyncMock(return_value={
            "status": "error",
            "reason": "offline",
            "reconciliation_id": "recon-1",
        })
        execute_trade = AsyncMock()
        monkeypatch.setattr("exchange.cancel_order", cancel_order)
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        await engine._close_grid(
            grid.config_id,
            53000.0,
            "manual",
            {"live_trading": True},
        )

        assert grid.status == "cleanup_required"
        assert pending_level.order_id == "entry-open"
        assert grid.cleanup_errors[0]["reconciliation_id"] == "recon-1"
        execute_trade.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_live_grid_rejects_partial_closed_net_exposure(self, engine, config, monkeypatch):
        grid = engine.create_grid(config, 50000.0)
        buy_level = next(level for level in grid.grid_levels if level.side == "buy")
        await engine._execute_grid_level(grid.config_id, buy_level, buy_level.price, config)
        config.paper_mode = False
        engine.configs[grid.config_id] = config

        monkeypatch.setattr("exchange.cancel_order", AsyncMock(return_value={"status": "cancelled"}))
        monkeypatch.setattr("exchange.execute_trade", AsyncMock(return_value={"status": "partial_closed", "order_id": "close-net"}))

        await engine._close_grid(grid.config_id, 53000.0, "out_of_range", {"live_trading": True})

        assert grid.status == "cleanup_required"
        assert grid.close_requires_manual_review is True
        assert grid.cleanup_errors[0]["kind"] == "net_close"

    @pytest.mark.asyncio
    async def test_live_grid_rollback_retains_order_id_when_cancel_fails(self, engine, monkeypatch):
        @asynccontextmanager
        async def fake_lock(*args, **kwargs):
            yield

        config = GridConfig(
            ticker="BTCUSDT",
            upper_price=110.0,
            lower_price=90.0,
            grid_count=4,
            total_capital_usdt=1000.0,
            paper_mode=False,
        )
        execute_trade = AsyncMock(side_effect=[
            {"status": "pending", "order_id": "live-order-1"},
            {"status": "error", "reason": "rejected"},
            {"status": "error", "reason": "rejected"},
            {"status": "error", "reason": "rejected"},
        ])
        monkeypatch.setattr("strategies.grid.distributed_lock", fake_lock)
        monkeypatch.setattr("exchange.execute_trade", execute_trade)
        monkeypatch.setattr(
            "exchange.cancel_order",
            AsyncMock(return_value={"status": "error", "reason": "exchange unavailable"}),
        )

        with pytest.raises(RuntimeError, match="Rollback incomplete"):
            await engine.create_grid_async(config, 100.0, {"live_trading": True})

        position = engine.positions[config.strategy_id]
        placed_level = next(level for level in position.grid_levels if level.order_id)
        assert placed_level.order_id == "live-order-1"
        assert placed_level.status == "pending"


class TestDCAEntry:
    def test_entry_creation(self):
        entry = DCAEntry(
            entry_price=50000.0,
            quantity=0.02,
            capital_usdt=1000.0,
            entry_time=datetime.now(UTC),
            entry_idx=1,
            reason="initial_entry",
        )

        assert entry.entry_price == 50000.0
        assert entry.quantity == 0.02
        assert entry.entry_idx == 1


class TestGridLevel:
    def test_level_creation(self):
        level = GridLevel(
            price=49500.0,
            quantity=0.01,
            side="buy",
        )

        assert level.price == 49500.0
        assert level.side == "buy"
        assert level.status == "pending"

    def test_level_filled(self):
        level = GridLevel(
            price=49500.0,
            quantity=0.01,
            side="buy",
        )

        level.status = "filled"
        level.filled_price = 49450.0
        level.filled_at = datetime.now(UTC)

        assert level.status == "filled"
        assert level.filled_price == 49450.0
