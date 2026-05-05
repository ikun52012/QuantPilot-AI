"""
QuantPilot AI - Timeframe-Based SL/TP Configuration

Different timeframes require different stop-loss and take-profit distances:
- Short timeframes (1m, 5m): Tight stops, quick targets
- Medium timeframes (15m, 1h): Moderate distances
- Long timeframes (4h, 1D): Wide stops, extended targets

This module provides intelligent SL/TP sizing based on signal timeframe.

Key principle for multi-TP R:R:
- Weighted Average TP >= SL × 1.5 (ensures healthy overall R:R)
- TP1 >= SL × 1.5 (first target must be healthy for single-TP mode)
- Consider different TP quantity distributions (25:25:25:25, 50:30:20:0, etc.)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TimeframeExitConfig:
    """SL/TP configuration for a specific timeframe."""
    timeframe: str
    min_sl_pct: float      # Minimum stop-loss distance (%)
    max_sl_pct: float      # Maximum stop-loss distance (%)
    default_sl_pct: float  # Default stop-loss distance (%)
    min_tp_pct: float      # Minimum take-profit distance (%)
    max_tp_pct: float      # Maximum take-profit distance (%)
    tp1_range: tuple[float, float]  # TP1 target range (min%, max%)
    tp2_range: tuple[float, float]  # TP2 target range
    tp3_range: tuple[float, float]  # TP3 target range
    tp4_range: tuple[float, float]  # TP4 target range
    tp_qty_default: tuple[float, float, float, float] = (25.0, 25.0, 25.0, 25.0)  # Default TP quantities
    min_weighted_rr: float = 1.5  # Minimum weighted average R:R ratio


def calculate_weighted_rr(
    tp_distances: tuple[float, float, float, float],
    tp_quantities: tuple[float, float, float, float],
    sl_distance: float,
) -> float:
    """
    Calculate weighted average R:R ratio for multi-TP setup.

    Args:
        tp_distances: (TP1%, TP2%, TP3%, TP4%) distances from entry
        tp_quantities: (TP1_qty, TP2_qty, TP3_qty, TP4_qty) percentages
        sl_distance: SL distance from entry (%)

    Returns:
        Weighted average R:R ratio
    """
    if sl_distance <= 0:
        return 0.0

    total_qty = sum(tp_quantities)
    if total_qty <= 0:
        return 0.0

    weighted_tp = sum(d * q for d, q in zip(tp_distances, tp_quantities, strict=True)) / total_qty
    return weighted_tp / sl_distance


def validate_multi_tp_rr(
    config: TimeframeExitConfig,
    tp_quantities: tuple[float, float, float, float] | None = None,
) -> dict:
    """
    Validate that multi-TP configuration meets minimum R:R requirements.

    Returns analysis of R:R for different scenarios.
    """
    quantities = tp_quantities or config.tp_qty_default

    # Calculate R:R for worst case (SL at max, TP at min)
    tp_min_distances = (
        config.tp1_range[0],
        config.tp2_range[0],
        config.tp3_range[0],
        config.tp4_range[0],
    )
    worst_case_rr = calculate_weighted_rr(tp_min_distances, quantities, config.max_sl_pct)

    # Calculate R:R for best case (SL at min, TP at max)
    tp_max_distances = (
        config.tp1_range[1],
        config.tp2_range[1],
        config.tp3_range[1],
        config.tp4_range[1],
    )
    best_case_rr = calculate_weighted_rr(tp_max_distances, quantities, config.min_sl_pct)

    # Calculate R:R for typical case (SL at default, TP at mid)
    tp_mid_distances = tuple((low + high) / 2 for low, high in [
        config.tp1_range, config.tp2_range, config.tp3_range, config.tp4_range
    ])
    typical_case_rr = calculate_weighted_rr(tp_mid_distances, quantities, config.default_sl_pct)

    # Single TP mode R:R (TP1 only)
    single_tp_min_rr = config.tp1_range[0] / config.max_sl_pct
    single_tp_max_rr = config.tp1_range[1] / config.min_sl_pct

    return {
        "timeframe": config.timeframe,
        "quantities": quantities,
        "worst_case_rr": worst_case_rr,
        "typical_case_rr": typical_case_rr,
        "best_case_rr": best_case_rr,
        "single_tp_min_rr": single_tp_min_rr,
        "single_tp_max_rr": single_tp_max_rr,
        "meets_minimum": worst_case_rr >= config.min_weighted_rr,
        "analysis": _generate_rr_analysis_text(config, quantities, worst_case_rr, typical_case_rr),
    }


def _generate_rr_analysis_text(
    config: TimeframeExitConfig,
    quantities: tuple[float, float, float, float],
    worst_case_rr: float,
    typical_case_rr: float,
) -> str:
    """Generate human-readable R:R analysis."""
    q1, q2, q3, q4 = quantities
    total = q1 + q2 + q3 + q4

    lines = [
        f"=== {config.timeframe} Timeframe R:R Analysis ===",
        f"SL Range: {config.min_sl_pct}% ~ {config.max_sl_pct}% (default: {config.default_sl_pct}%)",
        f"TP Distribution: TP1={q1}%, TP2={q2}%, TP3={q3}%, TP4={q4}% (total={total}%)",
        "",
        "R:R Scenarios:",
        f"  Worst Case (SL=max, TP=min): {worst_case_rr:.2f}:1",
        f"  Typical Case (SL=default, TP=mid): {typical_case_rr:.2f}:1",
        f"  Best Case (SL=min, TP=max): {worst_case_rr * 2:.2f}:1 (estimated)",
        "",
        "Single TP Mode:",
        f"  Min R:R: {config.tp1_range[0] / config.max_sl_pct:.2f}:1",
        f"  Max R:R: {config.tp1_range[1] / config.min_sl_pct:.2f}:1",
    ]

    if worst_case_rr >= config.min_weighted_rr:
        lines.append(f"\n[PASS] Weighted R:R >= {config.min_weighted_rr}:1 requirement")
    else:
        lines.append(f"\n[FAIL] Weighted R:R {worst_case_rr:.2f}:1 < {config.min_weighted_rr}:1 requirement")

    return "\n".join(lines)


# Timeframe configurations with healthy weighted R:R ratios
# Design principles:
# 1. Single TP mode: TP1_min >= SL_max × 1.5 (worst case R:R = 1.5:1)
# 2. Multi TP mode (25:25:25:25): weighted TP >= SL_max × 2.5 (better R:R)
# 3. TP ranges scale progressively: TP1 < TP2 < TP3 < TP4
# 4. Supports various quantity distributions (100:0:0:0 to 25:25:25:25)
TIMEFRAME_CONFIGS: dict[str, TimeframeExitConfig] = {
    # Very short timeframes - scalping (tight but healthy R:R)
    # Single TP: 1.5:1 ~ 4:1, Multi TP avg: 2.5:1 ~ 5:1
    "1": TimeframeExitConfig(
        timeframe="1",
        min_sl_pct=0.15,
        max_sl_pct=0.3,
        default_sl_pct=0.2,
        min_tp_pct=0.45,
        max_tp_pct=2.0,
        tp1_range=(0.45, 0.6),    # Single TP R:R: 1.5:1 ~ 4:1
        tp2_range=(0.7, 0.9),
        tp3_range=(1.0, 1.2),
        tp4_range=(1.4, 2.0),     # Multi TP weighted avg: 0.45~0.6 avg ~2.0:1
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "1m": TimeframeExitConfig(
        timeframe="1m",
        min_sl_pct=0.15,
        max_sl_pct=0.3,
        default_sl_pct=0.2,
        min_tp_pct=0.45,
        max_tp_pct=2.0,
        tp1_range=(0.45, 0.6),
        tp2_range=(0.7, 0.9),
        tp3_range=(1.0, 1.2),
        tp4_range=(1.4, 2.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 3m timeframe
    "3": TimeframeExitConfig(
        timeframe="3",
        min_sl_pct=0.2,
        max_sl_pct=0.4,
        default_sl_pct=0.25,
        min_tp_pct=0.6,
        max_tp_pct=2.5,
        tp1_range=(0.6, 0.8),
        tp2_range=(1.0, 1.2),
        tp3_range=(1.5, 1.8),
        tp4_range=(2.0, 2.5),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 5m timeframe - popular for quick trades
    "5": TimeframeExitConfig(
        timeframe="5",
        min_sl_pct=0.25,
        max_sl_pct=0.5,
        default_sl_pct=0.35,
        min_tp_pct=0.75,
        max_tp_pct=3.0,
        tp1_range=(0.75, 1.0),    # Single TP: 1.5:1 ~ 4:1
        tp2_range=(1.2, 1.5),
        tp3_range=(2.0, 2.5),
        tp4_range=(2.8, 3.0),     # Multi TP weighted: ~1.8:1 avg
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "5m": TimeframeExitConfig(
        timeframe="5m",
        min_sl_pct=0.25,
        max_sl_pct=0.5,
        default_sl_pct=0.35,
        min_tp_pct=0.75,
        max_tp_pct=3.0,
        tp1_range=(0.75, 1.0),
        tp2_range=(1.2, 1.5),
        tp3_range=(2.0, 2.5),
        tp4_range=(2.8, 3.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 15m timeframe - most popular for intraday trading
    # Single TP: 1.5:1 ~ 5:1, Multi TP avg: 2.5:1 ~ 6:1
    "15": TimeframeExitConfig(
        timeframe="15",
        min_sl_pct=0.4,
        max_sl_pct=1.0,
        default_sl_pct=0.6,
        min_tp_pct=1.5,
        max_tp_pct=5.0,
        tp1_range=(1.5, 2.0),     # Single TP R:R: 1.5:1 ~ 5:1
        tp2_range=(2.5, 3.0),
        tp3_range=(3.5, 4.0),
        tp4_range=(4.5, 5.0),     # Multi TP weighted avg: (1.5+2.5+3.5+4.5)/4=3.0% vs 1.0% = 3:1
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "15m": TimeframeExitConfig(
        timeframe="15m",
        min_sl_pct=0.4,
        max_sl_pct=1.0,
        default_sl_pct=0.6,
        min_tp_pct=1.5,
        max_tp_pct=5.0,
        tp1_range=(1.5, 2.0),
        tp2_range=(2.5, 3.0),
        tp3_range=(3.5, 4.0),
        tp4_range=(4.5, 5.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 30m timeframe
    "30": TimeframeExitConfig(
        timeframe="30",
        min_sl_pct=0.6,
        max_sl_pct=1.5,
        default_sl_pct=0.9,
        min_tp_pct=2.25,
        max_tp_pct=7.0,
        tp1_range=(2.25, 3.0),    # Single TP: 1.5:1 ~ 5:1
        tp2_range=(4.0, 5.0),
        tp3_range=(5.5, 6.0),
        tp4_range=(6.5, 7.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 1h timeframe - standard swing trading
    # Single TP: 1.5:1 ~ 5:1, Multi TP avg: 3:1 ~ 8:1
    "60": TimeframeExitConfig(
        timeframe="60",
        min_sl_pct=0.8,
        max_sl_pct=2.0,
        default_sl_pct=1.2,
        min_tp_pct=3.0,
        max_tp_pct=10.0,
        tp1_range=(3.0, 4.0),     # Single TP R:R: 1.5:1 ~ 5:1
        tp2_range=(5.0, 6.0),
        tp3_range=(7.0, 8.0),
        tp4_range=(9.0, 10.0),    # Multi TP weighted: (3+5+7+9)/4=6.25% vs 2.0% = 3.125:1
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "1h": TimeframeExitConfig(
        timeframe="1h",
        min_sl_pct=0.8,
        max_sl_pct=2.0,
        default_sl_pct=1.2,
        min_tp_pct=3.0,
        max_tp_pct=10.0,
        tp1_range=(3.0, 4.0),
        tp2_range=(5.0, 6.0),
        tp3_range=(7.0, 8.0),
        tp4_range=(9.0, 10.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 2h timeframe
    "120": TimeframeExitConfig(
        timeframe="120",
        min_sl_pct=1.2,
        max_sl_pct=3.0,
        default_sl_pct=1.8,
        min_tp_pct=4.5,
        max_tp_pct=15.0,
        tp1_range=(4.5, 6.0),
        tp2_range=(8.0, 10.0),
        tp3_range=(12.0, 13.0),
        tp4_range=(14.0, 15.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # 4h timeframe - position trading
    "240": TimeframeExitConfig(
        timeframe="240",
        min_sl_pct=2.0,
        max_sl_pct=4.0,
        default_sl_pct=2.5,
        min_tp_pct=6.0,
        max_tp_pct=20.0,
        tp1_range=(6.0, 8.0),     # Single TP: 1.5:1 ~ 4:1
        tp2_range=(10.0, 12.0),
        tp3_range=(15.0, 17.0),
        tp4_range=(18.0, 20.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "4h": TimeframeExitConfig(
        timeframe="4h",
        min_sl_pct=2.0,
        max_sl_pct=4.0,
        default_sl_pct=2.5,
        min_tp_pct=6.0,
        max_tp_pct=20.0,
        tp1_range=(6.0, 8.0),
        tp2_range=(10.0, 12.0),
        tp3_range=(15.0, 17.0),
        tp4_range=(18.0, 20.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # Daily timeframe - macro trading
    # Single TP: 1.5:1 ~ 4:1, Multi TP avg: 3:1 ~ 8:1
    "1D": TimeframeExitConfig(
        timeframe="1D",
        min_sl_pct=3.0,
        max_sl_pct=6.0,
        default_sl_pct=4.0,
        min_tp_pct=9.0,
        max_tp_pct=30.0,
        tp1_range=(9.0, 12.0),    # Single TP R:R: 1.5:1 ~ 4:1
        tp2_range=(15.0, 18.0),
        tp3_range=(22.0, 25.0),
        tp4_range=(28.0, 30.0),   # Multi TP weighted: (9+15+22+28)/4=18.5% vs 6% = 3.08:1
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    "D": TimeframeExitConfig(
        timeframe="D",
        min_sl_pct=3.0,
        max_sl_pct=6.0,
        default_sl_pct=4.0,
        min_tp_pct=9.0,
        max_tp_pct=30.0,
        tp1_range=(9.0, 12.0),
        tp2_range=(15.0, 18.0),
        tp3_range=(22.0, 25.0),
        tp4_range=(28.0, 30.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
    # Weekly timeframe - long-term macro
    "1W": TimeframeExitConfig(
        timeframe="1W",
        min_sl_pct=5.0,
        max_sl_pct=8.0,
        default_sl_pct=6.0,
        min_tp_pct=12.0,
        max_tp_pct=40.0,
        tp1_range=(12.0, 16.0),   # Single TP: 1.5:1 ~ 3.2:1
        tp2_range=(20.0, 25.0),
        tp3_range=(30.0, 35.0),
        tp4_range=(38.0, 40.0),
        tp_qty_default=(25.0, 25.0, 25.0, 25.0),
        min_weighted_rr=1.5,
    ),
}


def get_timeframe_config(timeframe: str) -> TimeframeExitConfig:
    """
    Get SL/TP configuration for a specific timeframe.

    Args:
        timeframe: Timeframe string (e.g., "15", "60", "1h", "4h", "1D")

    Returns:
        TimeframeExitConfig with appropriate min/max distances
    """
    # Normalize timeframe string
    tf_normalized = str(timeframe or "60").strip().upper()

    # Handle common variations
    if tf_normalized.endswith("M"):
        tf_normalized = tf_normalized[:-1]  # "15M" -> "15"
    elif tf_normalized.endswith("H"):
        tf_normalized = tf_normalized[:-1] + "60"  # "1H" -> "60"
    elif tf_normalized == "H":
        tf_normalized = "60"
    elif tf_normalized == "D" or tf_normalized == "1D":
        tf_normalized = "1D"
    elif tf_normalized == "W" or tf_normalized == "1W":
        tf_normalized = "1W"

    # Try exact match first
    if tf_normalized in TIMEFRAME_CONFIGS:
        return TIMEFRAME_CONFIGS[tf_normalized]

    # Try lowercase version
    if tf_normalized.lower() in TIMEFRAME_CONFIGS:
        return TIMEFRAME_CONFIGS[tf_normalized.lower()]

    # Fallback to 1h (60m) configuration for unknown timeframes
    return TIMEFRAME_CONFIGS["60"]


def get_min_sl_for_timeframe(timeframe: str) -> float:
    """Get minimum SL percentage for timeframe."""
    return get_timeframe_config(timeframe).min_sl_pct


def get_max_sl_for_timeframe(timeframe: str) -> float:
    """Get maximum SL percentage for timeframe."""
    return get_timeframe_config(timeframe).max_sl_pct


def get_default_sl_for_timeframe(timeframe: str) -> float:
    """Get default SL percentage for timeframe (for AI fallback)."""
    return get_timeframe_config(timeframe).default_sl_pct


def get_min_tp_for_timeframe(timeframe: str) -> float:
    """Get minimum TP percentage for timeframe."""
    return get_timeframe_config(timeframe).min_tp_pct


def get_max_tp_for_timeframe(timeframe: str) -> float:
    """Get maximum TP percentage for timeframe."""
    return get_timeframe_config(timeframe).max_tp_pct


def get_tp_ranges_for_timeframe(timeframe: str) -> dict[str, tuple[float, float]]:
    """Get TP target ranges for timeframe."""
    config = get_timeframe_config(timeframe)
    return {
        "tp1": config.tp1_range,
        "tp2": config.tp2_range,
        "tp3": config.tp3_range,
        "tp4": config.tp4_range,
    }


def format_timeframe_exit_instructions(timeframe: str) -> str:
    """
    Generate AI instruction text for timeframe-appropriate SL/TP.

    Returns instructions for the AI to generate proper exit levels.
    """
    config = get_timeframe_config(timeframe)
    ranges = get_tp_ranges_for_timeframe(timeframe)

    # Calculate R:R examples for different TP distribution scenarios
    single_tp_rr_min = config.tp1_range[0] / config.max_sl_pct
    single_tp_rr_max = config.tp1_range[1] / config.min_sl_pct

    # Standard 25:25:25:25 distribution
    std_qty = config.tp_qty_default
    std_analysis = validate_multi_tp_rr(config, std_qty)

    # Conservative 50:30:20:0 distribution (2 TP levels)
    conservative_qty = (50.0, 30.0, 20.0, 0.0)
    conservative_analysis = validate_multi_tp_rr(config, conservative_qty)

    return f"""
## Timeframe-Based Exit Requirements ({config.timeframe})

This signal is on a **{config.timeframe} timeframe**. Use these distance guidelines:

### Stop-Loss Guidance
- ADVISORY range: {config.min_sl_pct}% to {config.max_sl_pct}% from entry
- RECOMMENDED baseline: {config.default_sl_pct}% from entry
- Prefer the multi-timeframe structural invalidation level when it differs from the advisory range

### Take-Profit Targets
- TP1: {ranges['tp1'][0]}% to {ranges['tp1'][1]}% from entry
- TP2: {ranges['tp2'][0]}% to {ranges['tp2'][1]}% from entry
- TP3: {ranges['tp3'][0]}% to {ranges['tp3'][1]}% from entry
- TP4: {ranges['tp4'][0]}% to {ranges['tp4'][1]}% from entry

### Risk-Reward Analysis (Multiple Scenarios)

**Single TP Mode (TP1 only, 100% position):**
- Min R:R: {single_tp_rr_min:.2f}:1 (worst case)
- Max R:R: {single_tp_rr_max:.2f}:1 (best case)
- Recommended for: High confidence signals, quick exits

**Standard Multi-TP (25%:25%:25%:25%):**
- Weighted R:R: {std_analysis['worst_case_rr']:.2f}:1 to {std_analysis['best_case_rr']:.2f}:1
- Weighted avg TP: {(ranges['tp1'][0] + ranges['tp2'][0] + ranges['tp3'][0] + ranges['tp4'][0]) / 4:.2f}%
- Recommended for: Normal trading, gradual profit extraction

**Conservative Multi-TP (50%:30%:20%:0%):**
- Weighted R:R: {conservative_analysis['worst_case_rr']:.2f}:1 (worst case)
- Recommended for: Lower confidence, want early profits

### Critical Rules
1. Place SL at the logical invalidation point (support/resistance break, OB/FVG boundary, swing failure)
2. Treat the SL range above as guidance, not a hard rejection range
3. DO NOT set TP1 below {config.min_tp_pct}% - will be auto-adjusted
4. For single TP: TP1 should be >= {config.tp1_range[0]}% where structure allows
5. For multi TP: ensure weighted average TP >= SL × 1.5 where realistic
"""
