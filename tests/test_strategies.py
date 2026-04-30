"""Tests for DCA and Grid Strategies."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

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
        monkeypatch.setattr("exchange.execute_trade", execute_trade)

        result = await engine._execute_grid_level(grid.config_id, level, level.price, config, {"live_trading": True})

        assert result["success"] is True
        execute_trade.assert_not_awaited()

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


class TestDCAEntry:
    def test_entry_creation(self):
        entry = DCAEntry(
            entry_price=50000.0,
            quantity=0.02,
            capital_usdt=1000.0,
            entry_time=datetime.now(timezone.utc),
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
        level.filled_at = datetime.now(timezone.utc)

        assert level.status == "filled"
        assert level.filled_price == 49450.0
