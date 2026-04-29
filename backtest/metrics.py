"""
Performance Metrics Calculator for Backtest Results.
Calculates comprehensive trading performance metrics.
"""
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _config_get(config: object, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


@dataclass
class PerformanceResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_trade_pct: float = 0.0
    largest_win_pct: float = 0.0
    largest_loss_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_bars: int = 0
    recovery_factor: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_bars: float = 0.0
    avg_win_holding_bars: float = 0.0
    avg_loss_holding_bars: float = 0.0
    annualized_return_pct: float = 0.0
    volatility_pct: float = 0.0
    risk_reward_ratio: float = 0.0
    kelly_fraction: float = 0.0
    initial_capital: float = 0.0
    final_capital: float = 0.0
    peak_capital: float = 0.0
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    trade_frequency_per_day: float = 0.0
    hit_rate_on_sl: float = 0.0
    hit_rate_on_tp: float = 0.0


class PerformanceMetrics:
    @staticmethod
    def calculate(trades: list[Any], equity_curve: list[dict[str, object]], config: object) -> dict:
        if not trades:
            return PerformanceMetrics._empty_result(config)

        result = PerformanceResult()
        result.initial_capital = _config_get(config, "initial_capital", 10000.0)
        result.total_trades = len(trades)

        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct < 0]

        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0

        result.total_pnl_pct = sum(t.pnl_pct for t in trades)
        result.total_pnl_usdt = sum(t.pnl_usdt for t in trades)
        result.total_fees_usdt = sum(t.fees_usdt for t in trades)

        if wins:
            result.avg_win_pct = sum(t.pnl_pct for t in wins) / len(wins)
            result.largest_win_pct = max(t.pnl_pct for t in wins)
            result.avg_win_holding_bars = sum(t.holding_bars for t in wins) / len(wins)

        if losses:
            result.avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses)
            result.largest_loss_pct = min(t.pnl_pct for t in losses)
            result.avg_loss_holding_bars = sum(t.holding_bars for t in losses) / len(losses)

        result.avg_trade_pct = result.total_pnl_pct / result.total_trades if result.total_trades > 0 else 0
        result.avg_holding_bars = sum(t.holding_bars for t in trades) / result.total_trades if result.total_trades > 0 else 0

        total_wins_usdt = sum(t.pnl_usdt for t in wins) if wins else 0
        total_losses_usdt = abs(sum(t.pnl_usdt for t in losses)) if losses else 0
        result.profit_factor = total_wins_usdt / total_losses_usdt if total_losses_usdt > 0 else float('inf')

        result.expectancy = (result.win_rate / 100 * result.avg_win_pct) - ((100 - result.win_rate) / 100 * abs(result.avg_loss_pct)) if result.total_trades > 0 else 0

        if equity_curve:
            result.max_drawdown_pct, result.max_drawdown_duration_bars = PerformanceMetrics._calculate_drawdown(equity_curve)
            result.peak_capital = max(_as_float(e.get("equity", 0), 0.0) for e in equity_curve)
            result.final_capital = _as_float(equity_curve[-1].get("equity", result.initial_capital), result.initial_capital)

        result.total_return_pct = (result.final_capital - result.initial_capital) / result.initial_capital * 100

        if equity_curve and len(equity_curve) > 1:
            result.sharpe_ratio = PerformanceMetrics._calculate_sharpe(equity_curve)
            result.sortino_ratio = PerformanceMetrics._calculate_sortino(equity_curve)
            result.volatility_pct = PerformanceMetrics._calculate_volatility(equity_curve)

        if result.max_drawdown_pct > 0:
            result.recovery_factor = result.total_return_pct / result.max_drawdown_pct
            result.calmar_ratio = result.annualized_return_pct / result.max_drawdown_pct

        result.max_consecutive_wins, result.max_consecutive_losses = PerformanceMetrics._calculate_consecutive(trades)

        if equity_curve and len(equity_curve) > 1:
            start_ts = datetime.fromisoformat(str(equity_curve[0].get("timestamp", "")).replace("Z", "+00:00"))
            end_ts = datetime.fromisoformat(str(equity_curve[-1].get("timestamp", "")).replace("Z", "+00:00"))
            days = (end_ts - start_ts).days or 1

            result.annualized_return_pct = result.total_return_pct * (365 / days)
            result.cagr_pct = PerformanceMetrics._calculate_cagr(result.initial_capital, result.final_capital, days)
            result.trade_frequency_per_day = result.total_trades / days

        if result.avg_win_pct > 0 and result.avg_loss_pct < 0:
            result.risk_reward_ratio = abs(result.avg_win_pct / result.avg_loss_pct)

        if result.win_rate > 0 and result.avg_win_pct > 0 and result.avg_loss_pct < 0:
            p = result.win_rate / 100
            w = result.avg_win_pct
            loss_abs = abs(result.avg_loss_pct)
            result.kelly_fraction = (p * w - (1 - p) * loss_abs) / w if w > 0 else 0

        sl_trades = [t for t in trades if "stop_loss" in t.exit_reason]
        tp_trades = [t for t in trades if "take_profit" in t.exit_reason]

        result.hit_rate_on_sl = len(sl_trades) / result.total_trades * 100 if result.total_trades > 0 else 0
        result.hit_rate_on_tp = len(tp_trades) / result.total_trades * 100 if result.total_trades > 0 else 0

        return PerformanceMetrics._to_dict(result)

    @staticmethod
    def _calculate_drawdown(equity_curve: list[dict[str, object]]) -> tuple[float, int]:
        peak = 0.0
        max_dd = 0.0
        dd_duration = 0
        current_dd_start = 0

        for i, e in enumerate(equity_curve):
            equity = _as_float(e.get("equity", 0), 0.0)

            if equity > peak:
                peak = equity
                current_dd_start = i

            if peak > 0:
                dd = (peak - equity) / peak * 100
                if dd > max_dd:
                    max_dd = dd
                    dd_duration = i - current_dd_start

        return max_dd, dd_duration

    @staticmethod
    def _calculate_sharpe(equity_curve: list[dict[str, object]], risk_free_rate: float = 0.0) -> float:
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev_equity = _as_float(equity_curve[i - 1].get("equity", 0), 0.0)
            curr_equity = _as_float(equity_curve[i].get("equity", 0), 0.0)
            if prev_equity > 0:
                ret = (curr_equity - prev_equity) / prev_equity
                returns.append(ret)

        if not returns:
            return 0.0

        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)

        if std_dev == 0:
            return 0.0

        return float((avg_return - risk_free_rate) / std_dev * math.sqrt(365 * 24))  # Annualized for hourly

    @staticmethod
    def _calculate_sortino(equity_curve: list[dict[str, object]], risk_free_rate: float = 0.0) -> float:
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev_equity = _as_float(equity_curve[i - 1].get("equity", 0), 0.0)
            curr_equity = _as_float(equity_curve[i].get("equity", 0), 0.0)
            if prev_equity > 0:
                ret = (curr_equity - prev_equity) / prev_equity
                returns.append(ret)

        if not returns:
            return 0.0

        avg_return = sum(returns) / len(returns)

        downside_returns = [r for r in returns if r < 0]
        if not downside_returns:
            return float('inf')

        downside_variance = sum((r - avg_return) ** 2 for r in downside_returns) / len(downside_returns)
        downside_dev = math.sqrt(downside_variance)

        if downside_dev == 0:
            return 0.0

        return float((avg_return - risk_free_rate) / downside_dev * math.sqrt(365 * 24))

    @staticmethod
    def _calculate_volatility(equity_curve: list[dict[str, object]]) -> float:
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev_equity = _as_float(equity_curve[i - 1].get("equity", 0), 0.0)
            curr_equity = _as_float(equity_curve[i].get("equity", 0), 0.0)
            if prev_equity > 0:
                ret = (curr_equity - prev_equity) / prev_equity * 100
                returns.append(ret)

        if not returns:
            return 0.0

        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)

        return float(math.sqrt(variance) * math.sqrt(365 * 24))  # Annualized

    @staticmethod
    def _calculate_consecutive(trades: list[Any]) -> tuple[int, int]:
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0

        for trade in trades:
            if trade.pnl_pct > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)

        return max_wins, max_losses

    @staticmethod
    def _calculate_cagr(initial: float, final: float, days: int) -> float:
        if initial <= 0 or days <= 0:
            return 0.0

        years = days / 365.0
        if years <= 0:
            return 0.0

        cagr = ((final / initial) ** (1 / years) - 1) * 100
        return float(cagr)

    @staticmethod
    def _empty_result(config: object) -> dict:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "total_pnl_pct": 0.0,
            "total_pnl_usdt": 0.0,
            "total_fees_usdt": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "initial_capital": _config_get(config, "initial_capital", 10000.0),
            "final_capital": _config_get(config, "initial_capital", 10000.0),
            "total_return_pct": 0.0,
            "message": "No trades executed",
        }

    @staticmethod
    def _to_dict(result: PerformanceResult) -> dict:
        return {
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": round(result.win_rate, 2),
            "total_pnl_pct": round(result.total_pnl_pct, 2),
            "total_pnl_usdt": round(result.total_pnl_usdt, 2),
            "total_fees_usdt": round(result.total_fees_usdt, 2),
            "avg_win_pct": round(result.avg_win_pct, 4),
            "avg_loss_pct": round(result.avg_loss_pct, 4),
            "avg_trade_pct": round(result.avg_trade_pct, 4),
            "largest_win_pct": round(result.largest_win_pct, 4),
            "largest_loss_pct": round(result.largest_loss_pct, 4),
            "profit_factor": round(result.profit_factor, 4) if result.profit_factor != float('inf') else "inf",
            "expectancy": round(result.expectancy, 4),
            "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            "max_drawdown_duration_bars": result.max_drawdown_duration_bars,
            "recovery_factor": round(result.recovery_factor, 4),
            "sharpe_ratio": round(result.sharpe_ratio, 4),
            "sortino_ratio": round(result.sortino_ratio, 4) if result.sortino_ratio != float('inf') else "inf",
            "calmar_ratio": round(result.calmar_ratio, 4),
            "max_consecutive_wins": result.max_consecutive_wins,
            "max_consecutive_losses": result.max_consecutive_losses,
            "avg_holding_bars": round(result.avg_holding_bars, 2),
            "avg_win_holding_bars": round(result.avg_win_holding_bars, 2),
            "avg_loss_holding_bars": round(result.avg_loss_holding_bars, 2),
            "annualized_return_pct": round(result.annualized_return_pct, 2),
            "cagr_pct": round(result.cagr_pct, 2),
            "volatility_pct": round(result.volatility_pct, 2),
            "risk_reward_ratio": round(result.risk_reward_ratio, 4) if result.risk_reward_ratio > 0 else 0,
            "kelly_fraction": round(result.kelly_fraction, 4),
            "initial_capital": round(result.initial_capital, 2),
            "final_capital": round(result.final_capital, 2),
            "peak_capital": round(result.peak_capital, 2),
            "total_return_pct": round(result.total_return_pct, 2),
            "trade_frequency_per_day": round(result.trade_frequency_per_day, 4),
            "hit_rate_on_sl": round(result.hit_rate_on_sl, 2),
            "hit_rate_on_tp": round(result.hit_rate_on_tp, 2),
        }
