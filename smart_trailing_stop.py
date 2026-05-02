"""
QuantPilot AI - Smart Trailing Stop Selector

Automatically selects the optimal trailing stop mode based on:
- AI confidence score
- Market condition (trending, ranging, volatile, calm)
- Trend strength
- Timeframe characteristics
- Risk score

The goal is to balance profit protection with profit potential.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrailingStopRecommendation(str, Enum):
    NONE = "none"
    BREAKEVEN_ON_TP1 = "breakeven_on_tp1"
    STEP_TRAILING = "step_trailing"
    MOVING = "moving"


@dataclass
class TrailingStopDecision:
    """Result of smart trailing stop selection."""
    mode: TrailingStopRecommendation
    reasoning: str
    expected_benefit: str
    risk_reduction_pct: float  # Estimated risk reduction after TP1


def select_smart_trailing_stop(
    confidence: float,
    market_condition: str,
    trend_strength: str = "moderate",
    risk_score: float = 0.5,
    timeframe: str = "60",
    num_tp_levels: int = 4,
    atr_pct: float | None = None,
    user_override: str | None = None,
) -> TrailingStopDecision:
    """
    Select optimal trailing stop mode based on market conditions.

    Args:
        confidence: AI confidence score (0-1)
        market_condition: trending_up, trending_down, ranging, volatile, calm
        trend_strength: strong, moderate, weak, none
        risk_score: AI risk assessment (0-1, higher = more risky)
        timeframe: Signal timeframe (15, 60, 240, 1D, etc.)
        num_tp_levels: Number of TP levels configured
        atr_pct: Current ATR percentage (volatility indicator)
        user_override: User's explicit trailing stop mode preference

    Returns:
        TrailingStopDecision with recommended mode and reasoning
    """
    # User override takes priority
    if user_override and user_override.lower() != "none":
        return TrailingStopDecision(
            mode=TrailingStopRecommendation(user_override.lower()),
            reasoning="User explicitly configured this trailing stop mode",
            expected_benefit="Follows user preference for risk management",
            risk_reduction_pct=100.0 if "breakeven" in user_override.lower() else 50.0,
        )

    # Normalize inputs
    condition = str(market_condition or "").lower().strip()
    strength = str(trend_strength or "moderate").lower().strip()
    tf = str(timeframe or "60").lower().strip()

    # Decision logic based on market analysis

    # Case 1: Strong trending market with high confidence
    # - Let profits run, no trailing needed
    # - Best for capturing full trend movement
    if condition in {"trending_up", "trending_down"} and strength == "strong":
        if confidence >= 0.75:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.NONE,
                reasoning="Strong trend + high confidence: let profits run without trailing interference",
                expected_benefit="Maximize profit potential by holding through minor pullbacks",
                risk_reduction_pct=0.0,
            )
        else:
            # Strong trend but lower confidence - protect with step_trailing
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.STEP_TRAILING,
                reasoning="Strong trend but moderate confidence: use step_trailing to lock profits incrementally",
                expected_benefit="Lock profits at each TP while still allowing trend continuation",
                risk_reduction_pct=100.0,  # Full protection after TP1
            )

    # Case 2: Ranging/choppy market
    # - Always use trailing to protect against reversals
    # - Price likely to reverse after hitting TP levels
    if condition in {"ranging", "calm"} or strength in {"weak", "none"}:
        if num_tp_levels >= 3:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.STEP_TRAILING,
                reasoning="Ranging/weak trend: price likely reversals, step_trailing locks each TP",
                expected_benefit="Protect profits at each level in choppy market",
                risk_reduction_pct=100.0,
            )
        else:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.BREAKEVEN_ON_TP1,
                reasoning="Ranging market with few TP levels: move to breakeven after first hit",
                expected_benefit="Zero risk after TP1, protects against range reversals",
                risk_reduction_pct=100.0,
            )

    # Case 3: Volatile market
    # - Use trailing to protect against sudden reversals
    # - But avoid moving trailing (too tight for volatility)
    if condition == "volatile":
        if atr_pct and atr_pct > 3.0:  # High volatility
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.BREAKEVEN_ON_TP1,
                reasoning="High volatility: breakeven_on_tp1 protects without being too tight",
                expected_benefit="Protect capital in volatile conditions, avoid premature stops",
                risk_reduction_pct=100.0,
            )
        else:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.STEP_TRAILING,
                reasoning="Moderate volatility: step_trailing locks profits at each level",
                expected_benefit="Balance profit protection with continuation potential",
                risk_reduction_pct=100.0,
            )

    # Case 4: Moderate trend
    # - Default to step_trailing for balanced approach
    if condition in {"trending_up", "trending_down"} and strength == "moderate":
        if confidence >= 0.7:
            # Higher confidence in moderate trend - can be more aggressive
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.NONE,
                reasoning="Moderate trend + high confidence: hold for trend completion",
                expected_benefit="Capture full trend movement, maximize R:R",
                risk_reduction_pct=0.0,
            )
        else:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.STEP_TRAILING,
                reasoning="Moderate trend + moderate confidence: protect incrementally",
                expected_benefit="Lock profits at each TP level",
                risk_reduction_pct=100.0,
            )

    # Case 5: High risk score
    # - Be conservative regardless of market condition
    if risk_score >= 0.7:
        return TrailingStopDecision(
            mode=TrailingStopRecommendation.BREAKEVEN_ON_TP1,
            reasoning="High risk score: protect capital first with breakeven stop",
            expected_benefit="Minimize risk exposure, protect trading capital",
            risk_reduction_pct=100.0,
        )

    # Case 6: Short timeframe (scalping)
    # - Use tight trailing for quick profit protection
    if tf in {"1", "1m", "3", "5", "5m", "15", "15m"}:
        if confidence >= 0.8:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.STEP_TRAILING,
                reasoning="Short timeframe: quick profit protection with step_trailing",
                expected_benefit="Lock scalping profits at each TP level",
                risk_reduction_pct=100.0,
            )
        else:
            return TrailingStopDecision(
                mode=TrailingStopRecommendation.BREAKEVEN_ON_TP1,
                reasoning="Short timeframe + lower confidence: protect with breakeven",
                expected_benefit="Zero risk after TP1 for quick scalp trades",
                risk_reduction_pct=100.0,
            )

    # Default: step_trailing for balanced profit protection
    return TrailingStopDecision(
        mode=TrailingStopRecommendation.STEP_TRAILING,
        reasoning="Default: step_trailing provides balanced profit protection",
        expected_benefit="Lock profits at each TP level for optimal risk management",
        risk_reduction_pct=100.0,
    )


def get_trailing_stop_description(mode: TrailingStopRecommendation) -> str:
    """Get human-readable description of trailing stop mode."""
    descriptions = {
        TrailingStopRecommendation.NONE: "No trailing stop - original SL remains active. Best for strong trends with high confidence. Allows full profit capture.",
        TrailingStopRecommendation.BREAKEVEN_ON_TP1: "Move SL to entry price after TP1 hit. Protects capital with zero risk after first target. Good for volatile or uncertain markets.",
        TrailingStopRecommendation.STEP_TRAILING: "Move SL to previous TP level after each hit. TP1 -> breakeven, TP2 -> TP1, TP3 -> TP2. Balanced profit protection for multi-TP trades.",
        TrailingStopRecommendation.MOVING: "Classic moving trailing stop that follows price by fixed distance. Activates after profit threshold reached.",
    }
    return descriptions.get(mode, "Unknown trailing stop mode")


def calculate_expected_rr_with_trailing(
    mode: TrailingStopRecommendation,
    tp_distances: tuple[float, float, float, float],
    tp_quantities: tuple[float, float, float, float],
    sl_distance: float,
) -> dict:
    """
    Calculate expected R:R considering trailing stop behavior.

    Returns scenarios for different trailing outcomes.
    """
    if sl_distance <= 0:
        return {"error": "Invalid SL distance"}

    q1, q2, q3, q4 = tp_quantities
    d1, d2, d3, d4 = tp_distances
    total_qty = q1 + q2 + q3 + q4

    if mode == TrailingStopRecommendation.NONE:
        # No trailing - full R:R
        weighted_tp = sum(d * q for d, q in zip(tp_distances, tp_quantities, strict=True)) / max(total_qty, 1)
        return {
            "mode": "none",
            "scenario": "Hold through all TP levels",
            "expected_rr": weighted_tp / sl_distance,
            "worst_case_rr": d1 / sl_distance if q1 > 0 else 0,
            "best_case_rr": d4 / sl_distance if q4 > 0 else d1 / sl_distance,
            "profit_locked": "None - all profit depends on final outcome",
        }

    elif mode == TrailingStopRecommendation.BREAKEVEN_ON_TP1:
        # After TP1, SL = entry (zero risk)
        # Scenario A: Only TP1 hit, then stopped at breakeven
        scenario_a_profit = d1 * q1 / 100  # TP1 profit only

        # Scenario B: All TP hit
        scenario_b_profit = sum(d * q for d, q in zip(tp_distances, tp_quantities, strict=True)) / 100

        return {
            "mode": "breakeven_on_tp1",
            "scenario_a": f"TP1 hit only (stopped at breakeven): profit = {scenario_a_profit:.2f}%",
            "scenario_b": f"All TP hit: profit = {scenario_b_profit:.2f}%",
            "risk_after_tp1": 0.0,
            "guaranteed_profit_after_tp1": d1 * q1 / 100,
            "recommendation": "Good for protecting capital in uncertain markets",
        }

    elif mode == TrailingStopRecommendation.STEP_TRAILING:
        # Progressive profit locking
        # TP1 hit -> SL at entry, profit locked = d1 * q1
        # TP2 hit -> SL at TP1, profit locked = d1 * q1 + d2 * q2
        # TP3 hit -> SL at TP2, profit locked = d1 * q1 + d2 * q2 + d3 * q3
        # TP4 hit -> SL at TP3, profit locked = full

        profit_at_tp1 = d1 * q1 / 100
        profit_at_tp2 = (d1 * q1 + d2 * q2) / 100
        profit_at_tp3 = (d1 * q1 + d2 * q2 + d3 * q3) / 100
        profit_at_tp4 = (d1 * q1 + d2 * q2 + d3 * q3 + d4 * q4) / 100

        return {
            "mode": "step_trailing",
            "profit_locked_after_tp1": f"{profit_at_tp1:.2f}% (SL at entry)",
            "profit_locked_after_tp2": f"{profit_at_tp2:.2f}% (SL at TP1)",
            "profit_locked_after_tp3": f"{profit_at_tp3:.2f}% (SL at TP2)",
            "profit_locked_after_tp4": f"{profit_at_tp4:.2f}% (SL at TP3)",
            "risk_after_tp1": 0.0,
            "worst_case_profit": profit_at_tp1,
            "best_case_profit": profit_at_tp4,
            "recommendation": "Balanced approach - locks profits incrementally",
        }

    else:
        return {
            "mode": str(mode),
            "note": "Moving trailing stop - complex calculation depends on price movement",
        }
