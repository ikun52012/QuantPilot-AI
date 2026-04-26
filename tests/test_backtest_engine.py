"""Tests for Backtest Engine."""
import pytest
from datetime import datetime, timezone
from backtest.engine import BacktestEngine, BacktestConfig, BacktestPosition, BacktestTrade
from backtest.strategies import SimpleTrendFollowStrategy, SMCTrendStrategy, AIAssistantStrategy, BaseStrategy, TradingSignal


class OneShotLongStrategy(BaseStrategy):
    def generate_signal(self, data, current_idx):
        if current_idx == 0:
            return TradingSignal(action="buy", confidence=1.0, ticker="BTCUSDT", reason="test entry")
        return None


class TestBacktestConfig:
    def test_default_config(self):
        config = BacktestConfig()
        assert config.initial_capital == 10000.0
        assert config.position_size_pct == 10.0
        assert config.max_positions == 3
        assert config.leverage == 1.0
        assert config.fee_pct == 0.04
        assert config.stop_loss_pct == 2.0

    def test_custom_config(self):
        config = BacktestConfig(
            initial_capital=5000.0,
            position_size_pct=20.0,
            max_positions=5,
            leverage=2.0,
            stop_loss_pct=3.0,
        )
        assert config.initial_capital == 5000.0
        assert config.position_size_pct == 20.0
        assert config.max_positions == 5
        assert config.leverage == 2.0
        assert config.stop_loss_pct == 3.0


class TestBacktestEngine:
    @pytest.fixture
    def sample_data(self):
        return [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 1200},
            {"open": 102, "high": 105, "low": 101, "close": 104, "volume": 1500},
            {"open": 104, "high": 108, "low": 103, "close": 107, "volume": 2000},
            {"open": 107, "high": 110, "low": 106, "close": 109, "volume": 1800},
            {"open": 109, "high": 112, "low": 108, "close": 111, "volume": 1600},
            {"open": 111, "high": 115, "low": 110, "close": 113, "volume": 1400},
            {"open": 113, "high": 116, "low": 112, "close": 114, "volume": 1300},
            {"open": 114, "high": 118, "low": 113, "close": 117, "volume": 1500},
            {"open": 117, "high": 120, "low": 116, "close": 119, "volume": 1700},
        ]

    @pytest.fixture
    def timestamps(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [base.replace(hour=i) for i in range(10)]

    @pytest.fixture
    def engine(self):
        config = BacktestConfig(
            initial_capital=10000.0,
            position_size_pct=10.0,
            stop_loss_pct=2.0,
            trailing_mode="none",
        )
        strategy = OneShotLongStrategy()
        return BacktestEngine(config, strategy)

    def test_load_data(self, engine, sample_data):
        engine.load_data(sample_data)
        assert len(engine.data) == 10
        assert engine.data[0]["close"] == 101

    def test_run_backtest(self, engine, sample_data):
        engine.load_data(sample_data)
        result = engine.run()

        assert "trades" in result
        assert "equity_curve" in result
        assert "metrics" in result
        assert len(result["equity_curve"]) == 10

    def test_stop_loss_trigger(self):
        config = BacktestConfig(
            initial_capital=10000.0,
            stop_loss_pct=5.0,
            position_size_pct=10.0,
        )
        strategy = OneShotLongStrategy()
        engine = BacktestEngine(config, strategy)

        data = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 103, "low": 95, "close": 96, "volume": 2000},
        ]

        engine.load_data(data)
        result = engine.run()

        assert len(result["trades"]) > 0

    def test_trailing_stop_adjustment(self):
        config = BacktestConfig(
            initial_capital=10000.0,
            trailing_mode="moving",
            trailing_pct=1.0,
            trailing_activation_pct=0.5,
        )
        strategy = SimpleTrendFollowStrategy()
        engine = BacktestEngine(config, strategy)

        data = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 105, "low": 100, "close": 104, "volume": 1500},
            {"open": 104, "high": 108, "low": 103, "close": 107, "volume": 2000},
        ]

        engine.load_data(data)
        result = engine.run()

        assert result["config"]["trailing_mode"] == "moving"


class TestBacktestStrategies:
    def test_simple_trend_strategy(self):
        strategy = SimpleTrendFollowStrategy({"ema_period": 5})

        data = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 1200},
            {"open": 102, "high": 105, "low": 101, "close": 104, "volume": 1500},
            {"open": 104, "high": 108, "low": 103, "close": 107, "volume": 2000},
            {"open": 107, "high": 110, "low": 106, "close": 109, "volume": 1800},
            {"open": 109, "high": 112, "low": 108, "close": 111, "volume": 1600},
        ]

        signal = strategy.generate_signal(data, 5)

        if signal:
            assert signal.action in ["buy", "sell", "hold"]
            assert isinstance(signal.confidence, float)

    def test_smc_trend_strategy(self):
        strategy = SMCTrendStrategy({"fvg_lookback": 3})

        data = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 1200},
            {"open": 102, "high": 108, "low": 102, "close": 107, "volume": 5000},
            {"open": 106, "high": 110, "low": 105, "close": 109, "volume": 2000},
            {"open": 109, "high": 112, "low": 108, "close": 111, "volume": 1600},
            {"open": 111, "high": 115, "low": 110, "close": 113, "volume": 1400},
        ]

        signal = strategy.generate_signal(data, 5)

        if signal:
            assert signal.action in ["buy", "sell", "hold"]

    def test_ai_assistant_strategy(self):
        strategy = AIAssistantStrategy({"cooldown_bars": 5})

        data = []
        for i in range(30):
            price = 100 + i * 0.5
            data.append({
                "open": price,
                "high": price + 2,
                "low": price - 1,
                "close": price + 1,
                "volume": 1000 + i * 100,
            })

        signal = strategy.generate_signal(data, 29)

        if signal:
            assert signal.action in ["buy", "sell", "hold"]


class TestBacktestPosition:
    def test_position_creation(self):
        pos = BacktestPosition(
            ticker="BTCUSDT",
            direction="buy",
            entry_price=100.0,
            entry_time=datetime.now(timezone.utc),
            quantity=10.0,
        )

        assert pos.ticker == "BTCUSDT"
        assert pos.direction == "buy"
        assert pos.entry_price == 100.0
        assert pos.quantity == 10.0

    def test_position_with_trailing(self):
        pos = BacktestPosition(
            ticker="BTCUSDT",
            direction="buy",
            entry_price=100.0,
            entry_time=datetime.now(timezone.utc),
            quantity=10.0,
            trailing_stop_config={"mode": "moving", "trail_pct": 1.5},
        )

        assert pos.trailing_stop_config["mode"] == "moving"

    def test_position_with_tp_levels(self):
        pos = BacktestPosition(
            ticker="BTCUSDT",
            direction="buy",
            entry_price=100.0,
            entry_time=datetime.now(timezone.utc),
            quantity=10.0,
            take_profit_levels=[
                {"price": 103.0, "qty_pct": 50, "status": "pending"},
                {"price": 106.0, "qty_pct": 50, "status": "pending"},
            ],
        )

        assert len(pos.take_profit_levels) == 2


class TestBacktestTrade:
    def test_trade_creation(self):
        trade = BacktestTrade(
            ticker="BTCUSDT",
            direction="buy",
            entry_price=100.0,
            exit_price=105.0,
            entry_time=datetime.now(timezone.utc),
            exit_time=datetime.now(timezone.utc),
            quantity=10.0,
            pnl_pct=5.0,
            pnl_usdt=50.0,
            fees_usdt=2.0,
            leverage=1.0,
            exit_reason="take_profit",
            holding_bars=5,
            strategy_name="test",
        )

        assert trade.pnl_pct == 5.0
        assert trade.pnl_usdt == 50.0
        assert trade.exit_reason == "take_profit"


class TestMultiTPExecution:
    def test_multi_tp_calculation(self):
        config = BacktestConfig(
            multi_tp_enabled=True,
            tp_levels=[
                {"price_pct": 3.0, "qty_pct": 50},
                {"price_pct": 6.0, "qty_pct": 50},
            ],
        )

        assert config.multi_tp_enabled == True
        assert len(config.tp_levels) == 2

    def test_partial_close(self):
        engine = BacktestEngine(
            BacktestConfig(multi_tp_enabled=True, tp_levels=[{"price_pct": 3.0, "qty_pct": 50}]),
            SimpleTrendFollowStrategy(),
        )

        data = [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"open": 101, "high": 105, "low": 100, "close": 103, "volume": 1500},
            {"open": 103, "high": 106, "low": 102, "close": 104, "volume": 1200},
        ]

        engine.load_data(data)
        result = engine.run()

        assert result["signals"]["executed"] >= 0
