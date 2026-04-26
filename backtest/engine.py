"""
Backtest Engine for QuantPilot AI.
Simulates trading strategies on historical data with realistic execution.
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from backtest.metrics import PerformanceMetrics
from backtest.strategies import BaseStrategy, SignalType


def _signal_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read a signal from either a dict or the TradingSignal dataclass."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


class BacktestMode(Enum):
    PAPER = "paper"
    LIVE_SIMULATION = "live_simulation"


@dataclass
class BacktestPosition:
    ticker: str
    direction: str
    entry_price: float
    entry_time: datetime
    quantity: float
    stop_loss: Optional[float] = None
    take_profit_levels: list[dict] = field(default_factory=list)
    trailing_stop_config: dict = field(default_factory=dict)
    leverage: float = 1.0
    entry_idx: int = 0
    remaining_qty: float = 0.0
    realized_pnl_pct: float = 0.0
    fees_paid: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0


@dataclass
class BacktestTrade:
    ticker: str
    direction: str
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    quantity: float
    pnl_pct: float
    pnl_usdt: float
    fees_usdt: float
    leverage: float
    exit_reason: str
    holding_bars: int
    strategy_name: str


@dataclass
class BacktestConfig:
    initial_capital: float = 10000.0
    position_size_pct: float = 10.0
    max_positions: int = 3
    leverage: float = 1.0
    fee_pct: float = 0.04
    slippage_pct: float = 0.01
    use_trailing_stop: bool = True
    trailing_mode: str = "moving"
    trailing_pct: float = 1.5
    trailing_activation_pct: float = 0.5
    multi_tp_enabled: bool = False
    tp_levels: list[dict] = field(default_factory=lambda: [{"price_pct": 3.0, "qty_pct": 100}])
    stop_loss_pct: float = 2.0
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 20.0
    strategy_name: str = "default"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    timeframe: str = "1h"


class BacktestEngine:
    def __init__(self, config: BacktestConfig, strategy: BaseStrategy):
        self.config = config
        self.strategy = strategy
        self.capital = config.initial_capital
        self.initial_capital = config.initial_capital
        self.positions: list[BacktestPosition] = []
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []
        self.daily_pnl: dict[str, float] = {}
        self.current_bar_idx = 0
        self.data: list[dict] = []
        self.timestamps: list[datetime] = []
        self.max_equity = config.initial_capital
        self.daily_start_equity = config.initial_capital
        self.total_fees = 0.0
        self.signals_generated = 0
        self.signals_executed = 0
        self.signals_blocked = 0

    def load_data(self, ohlcv_data: list[dict]) -> None:
        if not ohlcv_data:
            raise ValueError("No OHLCV data provided")

        self.data = []
        self.timestamps = []

        fallback_start = self.config.start_date or datetime.now(timezone.utc)
        for idx, bar in enumerate(ohlcv_data):
            ts = bar.get("timestamp") or bar.get("datetime") or bar.get("time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc)
            elif ts is None:
                ts = fallback_start + timedelta(minutes=idx)

            if self.config.start_date and ts < self.config.start_date:
                continue
            if self.config.end_date and ts > self.config.end_date:
                continue

            self.data.append({
                "open": float(bar.get("open", 0)),
                "high": float(bar.get("high", 0)),
                "low": float(bar.get("low", 0)),
                "close": float(bar.get("close", 0)),
                "volume": float(bar.get("volume", 0)),
            })
            self.timestamps.append(ts)

        logger.info(f"[Backtest] Loaded {len(self.data)} bars from {self.timestamps[0] if self.timestamps else 'N/A'} to {self.timestamps[-1] if self.timestamps else 'N/A'}")

    def run(self) -> dict:
        if not self.data:
            return {"error": "No data loaded", "trades": [], "metrics": {}}

        logger.info(f"[Backtest] Starting backtest with {self.config.strategy_name} strategy")

        for i, bar in enumerate(self.data):
            self.current_bar_idx = i
            ts = self.timestamps[i]

            self._check_position_exits(i, bar, ts)

            if len(self.positions) < self.config.max_positions:
                signal = self.strategy.generate_signal(self.data[:i+1], i)
                if signal and _signal_get(signal, "action") in ["buy", "sell"]:
                    self.signals_generated += 1
                    if self._should_execute_signal(signal, bar, ts):
                        self._execute_signal(signal, bar, ts, i)
                    else:
                        self.signals_blocked += 1

            self._record_equity(i, bar, ts)

        self._close_all_positions(len(self.data) - 1, self.data[-1], self.timestamps[-1])

        metrics = PerformanceMetrics.calculate(self.trades, self.equity_curve, self.config)

        return {
            "trades": [self._trade_to_dict(t) for t in self.trades],
            "equity_curve": self.equity_curve,
            "metrics": metrics,
            "config": self._config_to_dict(),
            "signals": {
                "generated": self.signals_generated,
                "executed": self.signals_executed,
                "blocked": self.signals_blocked,
            },
        }

    def _should_execute_signal(self, signal: dict, bar: dict, ts: datetime) -> bool:
        if self.capital <= 0:
            return False

        daily_pnl = self.daily_pnl.get(ts.strftime("%Y-%m-%d"), 0.0)
        if daily_pnl <= -self.config.max_daily_loss_pct:
            logger.debug(f"[Backtest] Signal blocked: daily loss limit reached ({daily_pnl:.2f}%)")
            return False

        drawdown = (self.max_equity - self.capital) / self.max_equity * 100
        if drawdown >= self.config.max_drawdown_pct:
            logger.debug(f"[Backtest] Signal blocked: max drawdown reached ({drawdown:.2f}%)")
            return False

        return True

    def _execute_signal(self, signal: dict, bar: dict, ts: datetime, idx: int) -> None:
        direction = _signal_get(signal, "action")
        if direction not in ["buy", "sell"]:
            return

        ticker = _signal_get(signal, "ticker", "UNKNOWN")

        side_adjusted = direction == "buy"
        slippage = bar["close"] * self.config.slippage_pct / 100

        if side_adjusted:
            entry_price = bar["close"] + slippage
        else:
            entry_price = bar["close"] - slippage

        position_value = self.capital * self.config.position_size_pct / 100
        quantity = position_value / entry_price

        fee = position_value * self.config.fee_pct / 100
        self.capital -= fee
        self.total_fees += fee

        stop_loss_price = None
        if self.config.stop_loss_pct > 0:
            if side_adjusted:
                stop_loss_price = entry_price * (1 - self.config.stop_loss_pct / 100)
            else:
                stop_loss_price = entry_price * (1 + self.config.stop_loss_pct / 100)

        tp_levels = []
        if self.config.multi_tp_enabled:
            for tp in self.config.tp_levels:
                price_pct = tp.get("price_pct", 3.0)
                if side_adjusted:
                    tp_price = entry_price * (1 + price_pct / 100)
                else:
                    tp_price = entry_price * (1 - price_pct / 100)
                tp_levels.append({
                    "price": tp_price,
                    "qty_pct": tp.get("qty_pct", 100),
                    "status": "pending",
                })

        position = BacktestPosition(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            entry_time=ts,
            quantity=quantity,
            stop_loss=stop_loss_price,
            take_profit_levels=tp_levels,
            trailing_stop_config={
                "mode": self.config.trailing_mode,
                "trail_pct": self.config.trailing_pct,
                "activation_profit_pct": self.config.trailing_activation_pct,
            },
            leverage=self.config.leverage,
            entry_idx=idx,
            remaining_qty=quantity,
        )

        self.positions.append(position)
        self.signals_executed += 1

        logger.debug(f"[Backtest] Opened {direction} position for {ticker} at {entry_price:.4f}, qty={quantity:.4f}")

    def _check_position_exits(self, idx: int, bar: dict, ts: datetime) -> None:
        positions_to_close = []

        for pos in self.positions:
            pos.highest_price = max(pos.highest_price, bar["high"])
            pos.lowest_price = min(pos.lowest_price, bar["low"])

            exit_triggered = False
            exit_reason = ""
            exit_price = 0.0

            if pos.stop_loss and pos.stop_loss > 0:
                if pos.direction == "buy" and bar["low"] <= pos.stop_loss:
                    exit_triggered = True
                    exit_reason = "stop_loss"
                    exit_price = pos.stop_loss
                elif pos.direction == "sell" and bar["high"] >= pos.stop_loss:
                    exit_triggered = True
                    exit_reason = "stop_loss"
                    exit_price = pos.stop_loss

            if not exit_triggered and pos.take_profit_levels:
                for level in pos.take_profit_levels:
                    if level.get("status") != "pending":
                        continue

                    tp_price = level.get("price", 0)
                    if tp_price <= 0:
                        continue

                    if pos.direction == "buy" and bar["high"] >= tp_price:
                        level["status"] = "hit"
                        exit_triggered = True
                        exit_reason = f"take_profit_level_{level.get('level', 1)}"
                        exit_price = tp_price

                        qty_pct = level.get("qty_pct", 100) / 100
                        close_qty = pos.remaining_qty * qty_pct
                        pos.remaining_qty -= close_qty

                        if pos.remaining_qty <= 0.0001:
                            pos.remaining_qty = 0

                        self._adjust_trailing_on_tp(pos, level)

                        if pos.remaining_qty > 0:
                            exit_triggered = False
                            exit_reason = ""
                            exit_price = 0

                        break

                    elif pos.direction == "sell" and bar["low"] <= tp_price:
                        level["status"] = "hit"
                        exit_triggered = True
                        exit_reason = f"take_profit_level_{level.get('level', 1)}"
                        exit_price = tp_price

                        qty_pct = level.get("qty_pct", 100) / 100
                        close_qty = pos.remaining_qty * qty_pct
                        pos.remaining_qty -= close_qty

                        if pos.remaining_qty <= 0.0001:
                            pos.remaining_qty = 0

                        self._adjust_trailing_on_tp(pos, level)

                        if pos.remaining_qty > 0:
                            exit_triggered = False
                            exit_reason = ""
                            exit_price = 0

                        break

            if not exit_triggered and self.config.use_trailing_stop:
                self._adjust_trailing_stop(pos, bar)

            if exit_triggered and exit_price > 0:
                positions_to_close.append((pos, exit_reason, exit_price))

        for pos, reason, price in positions_to_close:
            self._close_position(pos, idx, bar, ts, reason, price)

    def _adjust_trailing_stop(self, pos: BacktestPosition, bar: dict) -> None:
        trailing_config = pos.trailing_stop_config
        mode = trailing_config.get("mode", "none")

        if mode == "none":
            return

        current_price = bar["close"]
        activation_pct = trailing_config.get("activation_profit_pct", 0.5)
        trail_pct = trailing_config.get("trail_pct", 1.5)

        if pos.direction == "buy":
            profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100
        else:
            profit_pct = (pos.entry_price - current_price) / pos.entry_price * 100

        if profit_pct < activation_pct:
            return

        if mode == "moving" or mode == "profit_pct_trailing":
            if pos.direction == "buy":
                new_stop = current_price * (1 - trail_pct / 100)
                if pos.stop_loss and new_stop > pos.stop_loss:
                    pos.stop_loss = new_stop
                    logger.debug(f"[Backtest] Trailing stop adjusted to {new_stop:.4f}")
            else:
                new_stop = current_price * (1 + trail_pct / 100)
                if pos.stop_loss and new_stop < pos.stop_loss:
                    pos.stop_loss = new_stop
                    logger.debug(f"[Backtest] Trailing stop adjusted to {new_stop:.4f}")

    def _adjust_trailing_on_tp(self, pos: BacktestPosition, tp_level: dict) -> None:
        mode = pos.trailing_stop_config.get("mode", "none")

        if mode == "breakeven_on_tp1":
            level_num = tp_level.get("level", 1)
            if level_num == 1 or (not tp_level.get("level") and pos.take_profit_levels.index(tp_level) == 0):
                pos.stop_loss = pos.entry_price
                logger.debug(f"[Backtest] TP1 hit, SL moved to breakeven {pos.entry_price:.4f}")

        elif mode == "step_trailing":
            all_levels = pos.take_profit_levels
            hit_levels = [l for l in all_levels if l.get("status") == "hit"]

            if hit_levels:
                last_hit_idx = max(
                    all_levels.index(l) for l in hit_levels
                )

                if last_hit_idx > 0:
                    prev_tp_price = all_levels[last_hit_idx - 1].get("price", 0)
                    if prev_tp_price > 0:
                        if pos.direction == "buy":
                            new_stop = min(prev_tp_price, pos.entry_price * 1.002)
                        else:
                            new_stop = max(prev_tp_price, pos.entry_price * 0.998)

                        if pos.stop_loss:
                            if pos.direction == "buy" and new_stop > pos.stop_loss:
                                pos.stop_loss = new_stop
                            elif pos.direction == "sell" and new_stop < pos.stop_loss:
                                pos.stop_loss = new_stop

                        logger.debug(f"[Backtest] Step trailing: SL moved to {pos.stop_loss:.4f}")

    def _close_position(self, pos: BacktestPosition, idx: int, bar: dict, ts: datetime, reason: str, exit_price: float) -> None:
        if exit_price <= 0:
            if reason == "stop_loss":
                exit_price = bar["low"] if pos.direction == "buy" else bar["high"]
            else:
                exit_price = bar["close"]

        slippage = exit_price * self.config.slippage_pct / 100
        if pos.direction == "buy":
            exit_price -= slippage
        else:
            exit_price += slippage

        close_qty = pos.remaining_qty if pos.remaining_qty > 0 else pos.quantity

        if pos.direction == "buy":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100 * self.config.leverage
            pnl_usdt = (exit_price - pos.entry_price) * close_qty
        else:
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100 * self.config.leverage
            pnl_usdt = (pos.entry_price - exit_price) * close_qty

        fee = exit_price * close_qty * self.config.fee_pct / 100
        pnl_usdt -= fee
        self.total_fees += fee

        self.capital += pnl_usdt

        trade = BacktestTrade(
            ticker=pos.ticker,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=ts,
            quantity=close_qty,
            pnl_pct=pnl_pct,
            pnl_usdt=pnl_usdt,
            fees_usdt=fee,
            leverage=self.config.leverage,
            exit_reason=reason,
            holding_bars=idx - pos.entry_idx,
            strategy_name=self.config.strategy_name,
        )

        self.trades.append(trade)

        date_key = ts.strftime("%Y-%m-%d")
        self.daily_pnl[date_key] = self.daily_pnl.get(date_key, 0.0) + pnl_pct

        if self.capital > self.max_equity:
            self.max_equity = self.capital

        if pos in self.positions:
            self.positions.remove(pos)

        logger.debug(f"[Backtest] Closed {pos.direction} position for {pos.ticker}: pnl={pnl_pct:.2f}%, reason={reason}")

    def _close_all_positions(self, idx: int, bar: dict, ts: datetime) -> None:
        for pos in self.positions:
            self._close_position(pos, idx, bar, ts, "end_of_backtest", bar["close"])

    def _record_equity(self, idx: int, bar: dict, ts: datetime) -> None:
        unrealized_pnl = 0.0

        for pos in self.positions:
            current_price = bar["close"]
            if pos.direction == "buy":
                unrealized = (current_price - pos.entry_price) * pos.remaining_qty
            else:
                unrealized = (pos.entry_price - current_price) * pos.remaining_qty
            unrealized_pnl += unrealized

        total_equity = self.capital + unrealized_pnl

        self.equity_curve.append({
            "timestamp": ts.isoformat(),
            "equity": round(total_equity, 2),
            "capital": round(self.capital, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "positions": len(self.positions),
            "drawdown_pct": round((self.max_equity - total_equity) / self.max_equity * 100, 2) if self.max_equity > 0 else 0,
        })

    def _trade_to_dict(self, trade: BacktestTrade) -> dict:
        return {
            "ticker": trade.ticker,
            "direction": trade.direction,
            "entry_price": round(trade.entry_price, 6),
            "exit_price": round(trade.exit_price, 6),
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
            "quantity": round(trade.quantity, 6),
            "pnl_pct": round(trade.pnl_pct, 2),
            "pnl_usdt": round(trade.pnl_usdt, 2),
            "fees_usdt": round(trade.fees_usdt, 4),
            "leverage": trade.leverage,
            "exit_reason": trade.exit_reason,
            "holding_bars": trade.holding_bars,
            "strategy_name": trade.strategy_name,
        }

    def _config_to_dict(self) -> dict:
        return {
            "initial_capital": self.config.initial_capital,
            "position_size_pct": self.config.position_size_pct,
            "leverage": self.config.leverage,
            "fee_pct": self.config.fee_pct,
            "slippage_pct": self.config.slippage_pct,
            "trailing_mode": self.config.trailing_mode,
            "stop_loss_pct": self.config.stop_loss_pct,
            "strategy_name": self.config.strategy_name,
            "timeframe": self.config.timeframe,
            "start_date": self.config.start_date.isoformat() if self.config.start_date else None,
            "end_date": self.config.end_date.isoformat() if self.config.end_date else None,
            "bars_processed": len(self.data),
        }
