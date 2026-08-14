"""Scanner Backtesting Framework - Evaluate scanner rules on historical data.

This module provides:
1. Historical backtest simulation using OHLCV data
2. Rule configuration A/B testing
3. Factor contribution analysis
4. Parameter optimization suggestions
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import DATA_DIR, settings
from core.utils.datetime import utcnow
from services.scanner_rules import DEFAULT_ENGINE, ScoringContext, ScoringEngine, ScoringRule
from services.unified_ohlcv import OHLCVBundle, UnifiedOHLCVProvider, _indicator_snapshot
from smc_analyzer import analyze_smc_single_tf


@dataclass
class BacktestResult:
    """Result of a single backtest run."""
    run_id: str
    symbol: str
    timeframe: str
    direction: str
    timestamp: datetime
    score: float
    score_breakdown: dict[str, float]
    price_entry: float
    price_exit_simulated: float
    outcome: str
    pnl_pct: float
    holding_bars: int
    rules_config: dict[str, Any]
    weights_config: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class BacktestSummary:
    """Summary statistics for a backtest session."""
    run_id: str
    total_signals: int
    winning_signals: int
    losing_signals: int
    neutral_signals: int
    win_rate: float
    avg_pnl_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    factor_contribution: dict[str, float]
    optimal_threshold: float
    best_rules: list[str]
    worst_rules: list[str]
    suggested_weights: dict[str, float]
    timeframe_breakdown: dict[str, dict[str, Any]]
    direction_breakdown: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScannerBacktester:
    """Scanner backtesting engine."""

    def __init__(
        self,
        provider: UnifiedOHLCVProvider | None = None,
        lookback_days: int = 30,
        simulation_bars: int = 24,
        min_score_threshold: float = 65.0,
    ):
        self.provider = provider or UnifiedOHLCVProvider()
        self.lookback_days = lookback_days
        self.simulation_bars = simulation_bars
        self.min_score_threshold = min_score_threshold
        self._results: list[BacktestResult] = []
        self._engine = ScoringEngine(
            rules=[ScoringRule.from_dict(rule) for rule in DEFAULT_ENGINE.get_rules()],
            weights=dict(DEFAULT_ENGINE.weights),
        )

    async def run_backtest(
        self,
        symbols: list[str],
        timeframes: list[str] | None = None,
        rules_override: list[ScoringRule] | None = None,
        weights_override: dict[str, float] | None = None,
        min_score_override: float | None = None,
    ) -> BacktestSummary:
        """Run backtest for given symbols and return summary."""
        run_id = uuid.uuid4().hex[:12]
        timeframes = timeframes or list(settings.scanner.timeframes)
        min_score = min_score_override if min_score_override is not None else self.min_score_threshold

        rules = rules_override or [ScoringRule.from_dict(rule) for rule in DEFAULT_ENGINE.get_rules()]
        weights = dict(DEFAULT_ENGINE.weights)
        if weights_override:
            weights.update(weights_override)
        self._engine = ScoringEngine(rules=rules, weights=weights)

        self._results = []

        for symbol in symbols:
            try:
                bundle = await self.provider.get_bundle(symbol, timeframes)
            except Exception as exc:
                logger.warning(f"[Backtest] Failed to fetch bundle for {symbol}: {exc}")
                continue

            results = self._simulate_signal_candidates(bundle, run_id, min_score)
            self._results.extend(results)

        return self._compute_summary(run_id)

    async def run_walk_forward_backtest(
        self,
        symbols: list[str],
        timeframes: list[str] | None = None,
        num_folds: int = 5,
        oos_ratio: float = 0.3,
        min_score_override: float | None = None,
    ) -> dict:
        """Walk-forward backtest with out-of-sample validation.

        Round-4 audit P0 fix: the previous ``run_backtest`` ran once over all
        data and reported aggregate stats — easy to overfit. This method:

          1. Splits each symbol's historical data into ``num_folds`` windows.
          2. For each fold, trains (finds optimal min_score threshold) on the
             in-sample portion and reports performance on the next out-of-sample
             portion (rolling-origin cross-validation).
          3. Aggregates OOS performance as the trusted estimate.

        Returns a dict with per-fold + aggregate OOS metrics.
        """
        run_id = uuid.uuid4().hex[:12]
        timeframes = timeframes or list(settings.scanner.timeframes)
        min_score = min_score_override if min_score_override is not None else self.min_score_threshold

        rules = [ScoringRule.from_dict(rule) for rule in DEFAULT_ENGINE.get_rules()]
        weights = dict(DEFAULT_ENGINE.weights)
        self._engine = ScoringEngine(rules=rules, weights=weights)

        fold_results: list[dict] = []
        for symbol in symbols:
            try:
                bundle = await self.provider.get_bundle(symbol, timeframes)
            except Exception as exc:
                logger.warning(f"[WalkForward] Failed to fetch bundle for {symbol}: {exc}")
                continue

            for tf, candles in bundle.candles.items():
                rows = self._ohlcv_to_rows(candles)
                n = len(rows)
                if n < 200:
                    continue
                # Split into folds
                fold_size = max(50, n // num_folds)
                oos_size = max(20, int(fold_size * oos_ratio))

                for fold_idx in range(num_folds):
                    is_start = fold_idx * fold_size
                    is_end = min(is_start + fold_size - oos_size, n - oos_size)
                    oos_start = is_end
                    oos_end = min(oos_start + oos_size, n)
                    if is_end <= is_start + 50 or oos_end <= oos_start + 20:
                        continue

                    # In-sample: find best min_score threshold by scanning
                    is_results = self._simulate_on_slice(
                        candles[:is_end], rows[:is_end], tf, bundle, symbol, run_id, min_score, fold_idx
                    )
                    best_threshold = self._find_best_threshold(is_results)
                    # OOS: evaluate at that threshold
                    oos_results = self._simulate_on_slice(
                        candles[oos_start:oos_end], rows[oos_start:oos_end], tf, bundle, symbol, run_id, fold_idx, best_threshold
                    )
                    oos_summary = self._summarize_results(oos_results)
                    is_summary = self._summarize_results(is_results)
                    fold_results.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "fold": fold_idx,
                        "is_size": is_end - is_start,
                        "oos_size": oos_end - oos_start,
                        "is_win_rate": is_summary.get("win_rate", 0),
                        "oos_win_rate": oos_summary.get("win_rate", 0),
                        "is_profit_factor": is_summary.get("profit_factor", 0),
                        "oos_profit_factor": oos_summary.get("profit_factor", 0),
                        "is_count": is_summary.get("total_signals", 0),
                        "oos_count": oos_summary.get("total_signals", 0),
                        "best_threshold": best_threshold,
                        "oos_degradation": max(0.0, is_summary.get("win_rate", 0) - oos_summary.get("win_rate", 0)),
                    })

        # Aggregate OOS metrics
        all_oos_wr = [f["oos_win_rate"] for f in fold_results if f["oos_count"] > 0]
        all_oos_pf = [f["oos_profit_factor"] for f in fold_results if f["oos_count"] > 0]
        avg_oos_wr = sum(all_oos_wr) / len(all_oos_wr) if all_oos_wr else 0.0
        avg_oos_pf = sum(all_oos_pf) / len(all_oos_pf) if all_oos_pf else 0.0
        avg_deg = sum(f["oos_degradation"] for f in fold_results) / len(fold_results) if fold_results else 0.0

        return {
            "run_id": run_id,
            "num_folds": num_folds,
            "oos_ratio": oos_ratio,
            "folds": fold_results,
            "aggregate_oos_win_rate": round(avg_oos_wr, 4),
            "aggregate_oos_profit_factor": round(avg_oos_pf, 4),
            "avg_oos_degradation": round(avg_deg, 4),
            "note": "OOS = out-of-sample. Low degradation (<5%) suggests strategy is robust to overfitting.",
        }

    def _simulate_on_slice(
        self,
        candles_slice: list,
        rows_slice: list,
        timeframe: str,
        bundle: OHLCVBundle,
        symbol: str,
        run_id: str,
        min_score: float,
        fold_idx: int,
        threshold_override: float | None = None,
    ) -> list:
        """Simulate signals on a slice of historical data."""
        results: list = []
        effective_threshold = threshold_override if threshold_override is not None else min_score
        min_history_bars = 50
        if len(rows_slice) < min_history_bars + 10:
            return results
        for bar_idx in range(min_history_bars, len(rows_slice) - 5):
            history_candles = candles_slice[:bar_idx]
            history_rows = rows_slice[:bar_idx]
            if len(history_candles) < min_history_bars:
                continue
            bar = rows_slice[bar_idx]
            bar_close = bar[1]
            try:
                indicators = _indicator_snapshot(history_candles)
            except Exception:
                continue
            atr_pct = self._safe_float(indicators.get("atr_pct")) or 0.5
            atr_price = max(bar_close * max(atr_pct, 0.05) / 100.0, bar_close * 0.001)
            for direction in ["long", "short"]:
                try:
                    smc_ctx = analyze_smc_single_tf(history_rows, timeframe, bar_close, direction, atr_pct)
                except Exception:
                    continue
                scoring_ctx = self._build_backtest_context(
                    bundle, smc_ctx, direction, indicators, timeframe, bar_close, atr_pct, atr_price
                )
                score, reasons, breakdown = self._engine.evaluate(scoring_ctx)
                if score < effective_threshold:
                    continue
                outcome, pnl_pct, holding = self._simulate_outcome(
                    rows_slice, bar_idx, direction, bar_close, 5, atr_pct
                )
                results.append({
                    "score": score,
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "direction": direction,
                })
        return results

    def _find_best_threshold(self, results: list) -> float:
        """Find the min_score threshold that maximises expectancy on the in-sample."""
        if not results:
            return self.min_score_threshold
        # Try thresholds from 50 to 90 in steps of 5
        best_threshold = 70.0
        best_expectancy = -1e9
        for threshold in range(50, 95, 5):
            subset = [r for r in results if r["score"] >= threshold]
            if not subset:
                continue
            wins = sum(1 for r in subset if r["outcome"] == "win")
            losses = sum(1 for r in subset if r["outcome"] == "loss")
            total = wins + losses
            if total < 3:
                continue
            avg_pnl = sum(r["pnl_pct"] for r in subset) / len(subset)
            if avg_pnl > best_expectancy:
                best_expectancy = avg_pnl
                best_threshold = float(threshold)
        return best_threshold

    def _summarize_results(self, results: list) -> dict:
        if not results:
            return {"total_signals": 0, "win_rate": 0.0, "profit_factor": 0.0}
        wins = sum(1 for r in results if r["outcome"] == "win")
        losses = sum(1 for r in results if r["outcome"] == "loss")
        total = wins + losses
        gross_profit = sum(r["pnl_pct"] for r in results if r["pnl_pct"] > 0)
        gross_loss = abs(sum(r["pnl_pct"] for r in results if r["pnl_pct"] < 0))
        return {
            "total_signals": len(results),
            "win_rate": round(wins / total, 4) if total > 0 else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
        }

    def _simulate_signal_candidates(
        self,
        bundle: OHLCVBundle,
        run_id: str,
        min_score: float,
    ) -> list[BacktestResult]:
        """Simulate signals from historical OHLCV and evaluate outcomes."""
        results: list[BacktestResult] = []
        mapping = bundle.mapping
        current = float(bundle.current_price or 0.0)

        if current <= 0:
            return results

        for timeframe, candles in bundle.candles.items():
            min_history_bars = 50
            if len(candles) < self.simulation_bars + min_history_bars:
                continue

            rows = self._ohlcv_to_rows(candles)

            for bar_idx in range(min_history_bars, len(rows) - self.simulation_bars):
                bar = rows[bar_idx]
                bar_time = datetime.fromtimestamp(bar[0] / 1000.0, tz=UTC)
                # CRITICAL: To avoid look-ahead bias, indicators / SMC must be
                # computed on history STRICTLY BEFORE the decision bar.
                # Entry price = open of bar_idx (executable on next bar after
                # close of bar_idx-1). Outcome simulation begins at bar_idx.
                history_candles = candles[:bar_idx]
                history_rows = rows[:bar_idx]
                if len(history_candles) < min_history_bars:
                    continue
                bar_close = bar[1]  # use OPEN of decision bar as entry price
                indicators = _indicator_snapshot(history_candles)
                atr_pct = self._safe_float(indicators.get("atr_pct"))
                atr_price = max(bar_close * max(atr_pct, 0.05) / 100.0, bar_close * 0.001)

                for direction in ["long", "short"]:
                    try:
                        smc_ctx = analyze_smc_single_tf(
                            history_rows,
                            timeframe,
                            bar_close,
                            direction,
                            atr_pct,
                        )
                    except Exception:
                        continue

                    scoring_ctx = self._build_backtest_context(
                        bundle, smc_ctx, direction, indicators, timeframe, bar_close, atr_pct, atr_price
                    )

                    score, reasons, breakdown = self._engine.evaluate(scoring_ctx)

                    if score < min_score:
                        continue

                    outcome, pnl_pct, holding = self._simulate_outcome(
                        rows, bar_idx, direction, bar_close, self.simulation_bars, atr_pct
                    )

                    result = BacktestResult(
                        run_id=run_id,
                        symbol=mapping.exchange_symbol,
                        timeframe=timeframe,
                        direction=direction,
                        timestamp=bar_time,
                        score=score,
                        score_breakdown=breakdown,
                        price_entry=bar_close,
                        price_exit_simulated=rows[bar_idx + holding][4] if bar_idx + holding < len(rows) else bar_close,
                        outcome=outcome,
                        pnl_pct=pnl_pct,
                        holding_bars=holding,
                        rules_config={"rules_count": len(self._engine.rules)},
                        weights_config=self._engine.weights.copy(),
                    )
                    results.append(result)

        return results

    def _simulate_outcome(
        self,
        rows: list[list[float]],
        entry_idx: int,
        direction: str,
        entry_price: float,
        max_bars: int,
        atr_pct: float = 0.5,
        taker_fee_pct: float = 0.05,
        slippage_pct: float | None = None,
        funding_rate_8h: float = 0.0001,
        bar_seconds: int = 3600,
    ) -> tuple[str, float, int]:
        """Simulate trade outcome from historical data.

        Returns (outcome, pnl_pct, holding_bars).
        Outcome is 'win', 'loss', or 'neutral'.

        Realism additions over the original implementation:
          * Dynamic slippage proportional to ATR%% (薄盘币种更高).
          * Funding cost accrued every 8h while holding (perpetual contract).
          * Round-trip taker fees on entry and exit.
        """
        atr_price = entry_price * atr_pct / 100.0
        tp_distance = atr_price * 2.0
        sl_distance = atr_price * 1.5

        tp_price = entry_price + tp_distance if direction == "long" else entry_price - tp_distance
        sl_price = entry_price - sl_distance if direction == "long" else entry_price + sl_distance

        # Dynamic slippage: scales with volatility, with a 0.02% floor.
        if slippage_pct is None:
            slippage_pct = max(0.02, atr_pct * 0.05)

        round_trip_fee_pct = taker_fee_pct * 2.0 + slippage_pct

        def _funding_cost(holding_bars: int) -> float:
            """Funding cost % of notional. Long pays positive funding; short receives.

            We assume the per-period funding rate is paid every 8 hours.
            """
            hours_held = holding_bars * bar_seconds / 3600.0
            funding_periods = hours_held / 8.0
            cost = funding_rate_8h * funding_periods * 100.0  # percent of notional
            return cost if direction == "long" else -cost

        for i in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(rows))):
            high = rows[i][2]
            low = rows[i][3]
            holding = i - entry_idx

            if direction == "long":
                hit_tp = high >= tp_price
                hit_sl = low <= sl_price
                if hit_sl:
                    pnl = (sl_price - entry_price) / entry_price * 100.0 - round_trip_fee_pct - _funding_cost(holding)
                    return "loss", round(pnl, 2), holding
                if hit_tp:
                    pnl = (tp_price - entry_price) / entry_price * 100.0 - round_trip_fee_pct - _funding_cost(holding)
                    return "win", round(pnl, 2), holding
            else:
                hit_tp = low <= tp_price
                hit_sl = high >= sl_price
                if hit_sl:
                    pnl = (entry_price - sl_price) / entry_price * 100.0 - round_trip_fee_pct - _funding_cost(holding)
                    return "loss", round(pnl, 2), holding
                if hit_tp:
                    pnl = (entry_price - tp_price) / entry_price * 100.0 - round_trip_fee_pct - _funding_cost(holding)
                    return "win", round(pnl, 2), holding

        final_price = rows[min(entry_idx + max_bars, len(rows) - 1)][4]
        pnl = (final_price - entry_price) / entry_price * 100.0 if direction == "long" else (entry_price - final_price) / entry_price * 100.0
        pnl = round(pnl - round_trip_fee_pct - _funding_cost(max_bars), 2)

        if abs(pnl) < 0.5:
            return "neutral", pnl, max_bars
        if pnl > 0:
            return "win", pnl, max_bars
        return "loss", pnl, max_bars

    def _build_backtest_context(
        self,
        bundle: OHLCVBundle,
        smc_ctx: Any,
        direction: str,
        indicators: dict[str, Any],
        timeframe: str,
        current_price: float,
        atr_pct: float,
        atr_price: float,
    ) -> ScoringContext:
        """Build scoring context for backtest simulation."""
        rsi = self._safe_float(indicators.get("rsi"), 50.0)
        ema_fast = self._safe_float(indicators.get("ema_fast"))
        ema_slow = self._safe_float(indicators.get("ema_slow"))
        ema200 = self._safe_float(indicators.get("ema200"))
        macd_hist = self._safe_float(indicators.get("macd_hist"))
        adx = self._safe_float(indicators.get("adx"))
        volume_ratio = self._safe_float(indicators.get("volume_ratio"))
        vwap = self._safe_float(indicators.get("vwap"))
        vwap_dist = self._safe_float(indicators.get("vwap_distance_pct"))
        poc = self._safe_float(indicators.get("volume_profile_poc"))
        regime = str(indicators.get("market_regime") or "unknown").lower()

        structure = getattr(smc_ctx, "structure", None)
        smc_trend = str(getattr(structure, "trend", "ranging") or "ranging").lower()
        premium = self._safe_float(getattr(smc_ctx, "premium_zone", 0.0))
        discount = self._safe_float(getattr(smc_ctx, "discount_zone", 0.0))
        equilibrium = self._safe_float(getattr(smc_ctx, "equilibrium", 0.0))
        risk_score = self._safe_float(getattr(smc_ctx, "risk_score", 0.5), 0.5)
        timing_score = self._safe_float(getattr(smc_ctx, "entry_timing_score", 0.5), 0.5)

        support = self._find_backtest_support(smc_ctx, direction, current_price, atr_price)

        if direction == "long":
            if discount and current_price <= discount:
                price_zone = "discount"
            elif equilibrium and current_price <= equilibrium:
                price_zone = "below_equilibrium"
            else:
                price_zone = "premium_or_neutral"
        else:
            if premium and current_price >= premium:
                price_zone = "premium"
            elif equilibrium and current_price >= equilibrium:
                price_zone = "above_equilibrium"
            else:
                price_zone = "discount_or_neutral"

        return ScoringContext(
            direction=direction,
            current_price=current_price,
            atr_pct=atr_pct,
            atr_price=atr_price,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema200=ema200,
            macd_hist=macd_hist,
            adx=adx,
            volume_ratio=volume_ratio,
            vwap=vwap,
            vwap_distance_pct=vwap_dist,
            poc=poc,
            regime=regime,
            oi_change_pct=None,
            htf_trend=None,
            smc_trend=smc_trend,
            smc_risk_score=risk_score,
            smc_timing_score=timing_score,
            price_zone=price_zone,
            support_zone=support,
            premium_zone=premium,
            discount_zone=discount,
            equilibrium=equilibrium,
            spread_pct=0.1,
            bid_ask_spread_pct=0.1,
            bundle_quality_passed=True,
            bundle_quality_reasons=[],
            timeframe=timeframe,
            market_type=bundle.mapping.market_type or settings.exchange.market_type,
        )

    def _find_backtest_support(
        self,
        smc_ctx: Any,
        direction: str,
        current: float,
        atr_price: float,
    ) -> dict[str, Any] | None:
        """Find nearest support zone for backtest."""
        desired_type = "bullish" if direction == "long" else "bearish"
        matches: list[dict[str, Any]] = []

        for fvg in getattr(smc_ctx, "fvgs", []) or []:
            if str(getattr(fvg, "type", "")).lower() != desired_type:
                continue
            if bool(getattr(fvg, "filled", False)):
                continue
            low = self._safe_float(getattr(fvg, "bottom", 0.0))
            high = self._safe_float(getattr(fvg, "top", 0.0))
            midpoint = self._safe_float(getattr(fvg, "midpoint", 0.0))
            distance = min(abs(current - low), abs(current - high), abs(current - midpoint))
            if distance <= atr_price * 2.0:
                matches.append({
                    "type": f"{desired_type}_fvg",
                    "low": min(low, high),
                    "high": max(low, high),
                    "midpoint": midpoint,
                    "distance": distance,
                })

        for ob in getattr(smc_ctx, "order_blocks", []) or []:
            if str(getattr(ob, "type", "")).lower() != desired_type:
                continue
            status = str(getattr(ob, "mitigation_status", "") or "").lower()
            if status in {"mitigated", "broken"}:
                continue
            low = self._safe_float(getattr(ob, "low", 0.0))
            high = self._safe_float(getattr(ob, "high", 0.0))
            midpoint = self._safe_float(getattr(ob, "midpoint", 0.0))
            distance = min(abs(current - low), abs(current - high), abs(current - midpoint))
            if distance <= atr_price * 2.0:
                matches.append({
                    "type": f"{desired_type}_order_block",
                    "low": min(low, high),
                    "high": max(low, high),
                    "midpoint": midpoint,
                    "distance": distance,
                })

        if not matches:
            return None
        matches.sort(key=lambda x: x["distance"])
        return matches[0]

    def _compute_summary(self, run_id: str) -> BacktestSummary:
        """Compute summary statistics from backtest results."""
        if not self._results:
            return BacktestSummary(
                run_id=run_id,
                total_signals=0,
                winning_signals=0,
                losing_signals=0,
                neutral_signals=0,
                win_rate=0.0,
                avg_pnl_pct=0.0,
                avg_win_pct=0.0,
                avg_loss_pct=0.0,
                profit_factor=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                factor_contribution={},
                optimal_threshold=0.0,
                best_rules=[],
                worst_rules=[],
                suggested_weights={},
                timeframe_breakdown={},
                direction_breakdown={},
            )

        wins = [r for r in self._results if r.outcome == "win"]
        losses = [r for r in self._results if r.outcome == "loss"]
        neutrals = [r for r in self._results if r.outcome == "neutral"]

        win_rate = len(wins) / len(self._results) * 100.0 if self._results else 0.0
        avg_pnl = sum(r.pnl_pct for r in self._results) / len(self._results)
        avg_win = sum(r.pnl_pct for r in wins) / len(wins) if wins else 0.0
        avg_loss = sum(r.pnl_pct for r in losses) / len(losses) if losses else 0.0

        total_profit = sum(r.pnl_pct for r in wins)
        total_loss = abs(sum(r.pnl_pct for r in losses))
        profit_factor = total_profit / total_loss if total_loss > 0 else 0.0

        cumulative_pnl = 0.0
        max_cumulative = 0.0
        max_drawdown = 0.0
        for r in sorted(self._results, key=lambda x: x.timestamp):
            cumulative_pnl += r.pnl_pct
            max_cumulative = max(max_cumulative, cumulative_pnl)
            drawdown = max_cumulative - cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)

        pnls = [r.pnl_pct for r in self._results]
        avg_pnl_for_sharpe = sum(pnls) / len(pnls) if pnls else 0.0
        variance = sum((p - avg_pnl_for_sharpe) ** 2 for p in pnls) / len(pnls) if pnls else 0.0
        std_dev = math.sqrt(variance) if variance > 0 else 0.001
        sharpe = avg_pnl_for_sharpe / std_dev if std_dev > 0 else 0.0

        factor_contribution = self._compute_factor_contribution()
        optimal_threshold = self._find_optimal_threshold()
        best_rules, worst_rules = self._identify_best_worst_rules()
        suggested_weights = self._suggest_weights_from_backtest()
        tf_breakdown = self._compute_timeframe_breakdown()
        dir_breakdown = self._compute_direction_breakdown()

        return BacktestSummary(
            run_id=run_id,
            total_signals=len(self._results),
            winning_signals=len(wins),
            losing_signals=len(losses),
            neutral_signals=len(neutrals),
            win_rate=round(win_rate, 2),
            avg_pnl_pct=round(avg_pnl, 2),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            sharpe_ratio=round(sharpe, 2),
            factor_contribution=factor_contribution,
            optimal_threshold=round(optimal_threshold, 2),
            best_rules=best_rules,
            worst_rules=worst_rules,
            suggested_weights=suggested_weights,
            timeframe_breakdown=tf_breakdown,
            direction_breakdown=dir_breakdown,
        )

    def _compute_factor_contribution(self) -> dict[str, float]:
        """Compute average contribution of each factor to winning vs losing signals."""
        if not self._results:
            return {}

        wins = [r for r in self._results if r.outcome == "win"]
        losses = [r for r in self._results if r.outcome == "loss"]

        contribution: dict[str, float] = {}
        all_factors: set[str] = set()

        for r in self._results:
            all_factors.update(r.score_breakdown.keys())

        for factor in all_factors:
            win_avg = sum(r.score_breakdown.get(factor, 0.0) for r in wins) / len(wins) if wins else 0.0
            loss_avg = sum(r.score_breakdown.get(factor, 0.0) for r in losses) / len(losses) if losses else 0.0
            contribution[factor] = round(win_avg - loss_avg, 2)

        return contribution

    def _find_optimal_threshold(self) -> float:
        """Find score threshold that maximizes win rate."""
        if len(self._results) < 10:
            return self.min_score_threshold

        thresholds = [50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0]
        best_threshold = self.min_score_threshold
        best_win_rate = 0.0

        for threshold in thresholds:
            filtered = [r for r in self._results if r.score >= threshold]
            if len(filtered) < 5:
                continue
            wins = [r for r in filtered if r.outcome == "win"]
            wr = len(wins) / len(filtered) * 100.0
            if wr > best_win_rate:
                best_win_rate = wr
                best_threshold = threshold

        return best_threshold

    def _identify_best_worst_rules(self) -> tuple[list[str], list[str]]:
        """Identify rules with highest positive and negative contribution."""
        contribution = self._compute_factor_contribution()
        if not contribution:
            return [], []

        sorted_factors = sorted(contribution.items(), key=lambda x: x[1], reverse=True)
        best = [f[0] for f in sorted_factors[:5] if f[1] > 0]
        worst = [f[0] for f in sorted_factors[-5:] if f[1] < 0]
        return best, worst

    def _suggest_weights_from_backtest(self) -> dict[str, float]:
        """Suggest weight adjustments based on backtest results."""
        contribution = self._compute_factor_contribution()
        suggestions: dict[str, float] = {}

        from services.scanner_rules import DEFAULT_ENGINE
        name_to_weight_key = {r.name: r.weight_key for r in DEFAULT_ENGINE.rules if hasattr(r, 'weight_key')}

        for factor, contrib in contribution.items():
            wk = name_to_weight_key.get(factor, factor)
            if contrib > 2.0:
                suggestions[wk] = 1.2
            elif contrib > 0:
                suggestions[wk] = 1.0
            elif contrib < -2.0:
                suggestions[wk] = 0.5
            else:
                suggestions[wk] = 0.8

        return suggestions

    def _compute_timeframe_breakdown(self) -> dict[str, dict[str, Any]]:
        """Compute statistics per timeframe."""
        breakdown: dict[str, dict[str, Any]] = {}

        tfs = {r.timeframe for r in self._results}
        for tf in tfs:
            tf_results = [r for r in self._results if r.timeframe == tf]
            wins = [r for r in tf_results if r.outcome == "win"]
            wr = len(wins) / len(tf_results) * 100.0 if tf_results else 0.0
            avg_pnl = sum(r.pnl_pct for r in tf_results) / len(tf_results) if tf_results else 0.0
            breakdown[tf] = {
                "count": len(tf_results),
                "win_rate": round(wr, 2),
                "avg_pnl_pct": round(avg_pnl, 2),
            }

        return breakdown

    def _compute_direction_breakdown(self) -> dict[str, dict[str, Any]]:
        """Compute statistics per direction."""
        breakdown: dict[str, dict[str, Any]] = {}

        for direction in ["long", "short"]:
            dir_results = [r for r in self._results if r.direction == direction]
            wins = [r for r in dir_results if r.outcome == "win"]
            wr = len(wins) / len(dir_results) * 100.0 if dir_results else 0.0
            avg_pnl = sum(r.pnl_pct for r in dir_results) / len(dir_results) if dir_results else 0.0
            breakdown[direction] = {
                "count": len(dir_results),
                "win_rate": round(wr, 2),
                "avg_pnl_pct": round(avg_pnl, 2),
            }

        return breakdown

    def _ohlcv_to_rows(self, candles: list[Any]) -> list[list[float]]:
        """Convert candle objects to OHLCV rows."""
        rows: list[list[float]] = []
        for candle in candles:
            ts = getattr(candle, "timestamp", None)
            if hasattr(ts, "timestamp"):
                ts_ms = int(ts.timestamp() * 1000)
            else:
                ts_ms = 0
            rows.append([
                ts_ms,
                float(getattr(candle, "open", 0.0) or 0.0),
                float(getattr(candle, "high", 0.0) or 0.0),
                float(getattr(candle, "low", 0.0) or 0.0),
                float(getattr(candle, "close", 0.0) or 0.0),
                float(getattr(candle, "volume", 0.0) or 0.0),
            ])
        return rows

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        """Safe float conversion."""
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def save_results(self, path: str | Path | None = None) -> None:
        """Save backtest results to JSON file."""
        if path is None:
            path = DATA_DIR / "backtest_results.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "results": [r.to_dict() for r in self._results],
            "timestamp": utcnow().isoformat(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"[Backtest] Saved results to {path}")

    def load_results(self, path: str | Path | None = None) -> bool:
        """Load backtest results from JSON file."""
        if path is None:
            path = DATA_DIR / "backtest_results.json"
        path = Path(path)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._results = []
            for item in data.get("results", []):
                self._results.append(BacktestResult(
                    run_id=item["run_id"],
                    symbol=item["symbol"],
                    timeframe=item["timeframe"],
                    direction=item["direction"],
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    score=item["score"],
                    score_breakdown=item["score_breakdown"],
                    price_entry=item["price_entry"],
                    price_exit_simulated=item["price_exit_simulated"],
                    outcome=item["outcome"],
                    pnl_pct=item["pnl_pct"],
                    holding_bars=item["holding_bars"],
                    rules_config=item["rules_config"],
                    weights_config=item["weights_config"],
                ))
            logger.info(f"[Backtest] Loaded {len(self._results)} results from {path}")
            return True
        except Exception as e:
            logger.warning(f"[Backtest] Failed to load results: {e}")
            return False


_BACKTESTER: ScannerBacktester | None = None


def get_backtester() -> ScannerBacktester:
    """Get global backtester instance."""
    global _BACKTESTER
    if _BACKTESTER is None:
        _BACKTESTER = ScannerBacktester()
    return _BACKTESTER
