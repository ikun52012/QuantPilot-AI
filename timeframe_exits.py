"""
QuantPilot AI - Timeframe-Based SL/TP Configuration

Different timeframes require different stop-loss and take-profit distances:
- Short timeframes (1m, 5m): Tight stops, quick targets
- Medium timeframes (15m, 1h): Moderate distances
- Long timeframes (4h, 1D): Wide stops, extended targets

This module provides intelligent SL/TP sizing based on signal timeframe.
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


# Timeframe configurations with healthy R:R ratios
# Key principle: TP1 minimum >= SL maximum × 1.5 (ensures healthy R:R even in worst case)
# For single-TP mode, this guarantees minimum 1.5:1 R:R ratio
TIMEFRAME_CONFIGS: dict[str, TimeframeExitConfig] = {
    # Very short timeframes - scalping style (R:R: 1.5:1 to 4:1)
    "1": TimeframeExitConfig(
        timeframe="1",
        min_sl_pct=0.15,
        max_sl_pct=0.3,
        default_sl_pct=0.2,
        min_tp_pct=0.45,  # max_sl(0.3) × 1.5 = 0.45
        max_tp_pct=1.2,
        tp1_range=(0.45, 0.6),   # R:R: 1.5:1 ~ 4:1
        tp2_range=(0.6, 0.9),
        tp3_range=(0.9, 1.0),
        tp4_range=(1.0, 1.2),
    ),
    "1m": TimeframeExitConfig(
        timeframe="1m",
        min_sl_pct=0.15,
        max_sl_pct=0.3,
        default_sl_pct=0.2,
        min_tp_pct=0.45,
        max_tp_pct=1.2,
        tp1_range=(0.45, 0.6),
        tp2_range=(0.6, 0.9),
        tp3_range=(0.9, 1.0),
        tp4_range=(1.0, 1.2),
    ),
    # Short timeframes (R:R: 1.5:1 to 3:1)
    "3": TimeframeExitConfig(
        timeframe="3",
        min_sl_pct=0.2,
        max_sl_pct=0.5,
        default_sl_pct=0.3,
        min_tp_pct=0.75,  # max_sl(0.5) × 1.5 = 0.75
        max_tp_pct=1.5,
        tp1_range=(0.75, 1.0),
        tp2_range=(1.0, 1.2),
        tp3_range=(1.2, 1.4),
        tp4_range=(1.4, 1.5),
    ),
    "5": TimeframeExitConfig(
        timeframe="5",
        min_sl_pct=0.25,
        max_sl_pct=0.6,
        default_sl_pct=0.35,
        min_tp_pct=0.9,  # max_sl(0.6) × 1.5 = 0.9
        max_tp_pct=1.8,
        tp1_range=(0.9, 1.2),
        tp2_range=(1.2, 1.4),
        tp3_range=(1.4, 1.6),
        tp4_range=(1.6, 1.8),
    ),
    "5m": TimeframeExitConfig(
        timeframe="5m",
        min_sl_pct=0.25,
        max_sl_pct=0.6,
        default_sl_pct=0.35,
        min_tp_pct=0.9,
        max_tp_pct=1.8,
        tp1_range=(0.9, 1.2),
        tp2_range=(1.2, 1.4),
        tp3_range=(1.4, 1.6),
        tp4_range=(1.6, 1.8),
    ),
    # Medium-short timeframes (R:R: 1.5:1 to 3:1)
    "15": TimeframeExitConfig(
        timeframe="15",
        min_sl_pct=0.4,
        max_sl_pct=1.0,
        default_sl_pct=0.6,
        min_tp_pct=1.5,  # max_sl(1.0) × 1.5 = 1.5
        max_tp_pct=3.0,
        tp1_range=(1.5, 2.0),
        tp2_range=(2.0, 2.5),
        tp3_range=(2.5, 2.8),
        tp4_range=(2.8, 3.0),
    ),
    "15m": TimeframeExitConfig(
        timeframe="15m",
        min_sl_pct=0.4,
        max_sl_pct=1.0,
        default_sl_pct=0.6,
        min_tp_pct=1.5,
        max_tp_pct=3.0,
        tp1_range=(1.5, 2.0),
        tp2_range=(2.0, 2.5),
        tp3_range=(2.5, 2.8),
        tp4_range=(2.8, 3.0),
    ),
    # Medium timeframes (R:R: 1.5:1 to 3:1)
    "30": TimeframeExitConfig(
        timeframe="30",
        min_sl_pct=0.6,
        max_sl_pct=1.5,
        default_sl_pct=0.9,
        min_tp_pct=2.25,  # max_sl(1.5) × 1.5 = 2.25
        max_tp_pct=4.5,
        tp1_range=(2.25, 3.0),
        tp2_range=(3.0, 3.5),
        tp3_range=(3.5, 4.0),
        tp4_range=(4.0, 4.5),
    ),
    "60": TimeframeExitConfig(
        timeframe="60",
        min_sl_pct=0.8,
        max_sl_pct=2.0,
        default_sl_pct=1.2,
        min_tp_pct=3.0,  # max_sl(2.0) × 1.5 = 3.0
        max_tp_pct=6.0,
        tp1_range=(3.0, 4.0),
        tp2_range=(4.0, 4.8),
        tp3_range=(4.8, 5.4),
        tp4_range=(5.4, 6.0),
    ),
    "1h": TimeframeExitConfig(
        timeframe="1h",
        min_sl_pct=0.8,
        max_sl_pct=2.0,
        default_sl_pct=1.2,
        min_tp_pct=3.0,
        max_tp_pct=6.0,
        tp1_range=(3.0, 4.0),
        tp2_range=(4.0, 4.8),
        tp3_range=(4.8, 5.4),
        tp4_range=(5.4, 6.0),
    ),
    # Longer timeframes (R:R: 1.5:1 to 3:1)
    "120": TimeframeExitConfig(
        timeframe="120",
        min_sl_pct=1.2,
        max_sl_pct=3.0,
        default_sl_pct=1.8,
        min_tp_pct=4.5,  # max_sl(3.0) × 1.5 = 4.5
        max_tp_pct=9.0,
        tp1_range=(4.5, 6.0),
        tp2_range=(6.0, 7.0),
        tp3_range=(7.0, 8.0),
        tp4_range=(8.0, 9.0),
    ),
    "240": TimeframeExitConfig(
        timeframe="240",
        min_sl_pct=2.0,
        max_sl_pct=4.0,
        default_sl_pct=2.5,
        min_tp_pct=6.0,  # max_sl(4.0) × 1.5 = 6.0
        max_tp_pct=12.0,
        tp1_range=(6.0, 8.0),
        tp2_range=(8.0, 9.5),
        tp3_range=(9.5, 10.5),
        tp4_range=(10.5, 12.0),
    ),
    "4h": TimeframeExitConfig(
        timeframe="4h",
        min_sl_pct=2.0,
        max_sl_pct=4.0,
        default_sl_pct=2.5,
        min_tp_pct=6.0,
        max_tp_pct=12.0,
        tp1_range=(6.0, 8.0),
        tp2_range=(8.0, 9.5),
        tp3_range=(9.5, 10.5),
        tp4_range=(10.5, 12.0),
    ),
    # Daily timeframe (R:R: 1.5:1 to 3:1)
    "1D": TimeframeExitConfig(
        timeframe="1D",
        min_sl_pct=3.0,
        max_sl_pct=6.0,
        default_sl_pct=4.0,
        min_tp_pct=9.0,  # max_sl(6.0) × 1.5 = 9.0
        max_tp_pct=18.0,
        tp1_range=(9.0, 12.0),
        tp2_range=(12.0, 14.0),
        tp3_range=(14.0, 16.0),
        tp4_range=(16.0, 18.0),
    ),
    "D": TimeframeExitConfig(
        timeframe="D",
        min_sl_pct=3.0,
        max_sl_pct=6.0,
        default_sl_pct=4.0,
        min_tp_pct=9.0,
        max_tp_pct=18.0,
        tp1_range=(9.0, 12.0),
        tp2_range=(12.0, 14.0),
        tp3_range=(14.0, 16.0),
        tp4_range=(16.0, 18.0),
    ),
    # Weekly timeframe (R:R: 1.5:1 to 3:1)
    "1W": TimeframeExitConfig(
        timeframe="1W",
        min_sl_pct=5.0,
        max_sl_pct=8.0,
        default_sl_pct=6.0,
        min_tp_pct=12.0,  # max_sl(8.0) × 1.5 = 12.0
        max_tp_pct=24.0,
        tp1_range=(12.0, 16.0),
        tp2_range=(16.0, 18.0),
        tp3_range=(18.0, 20.0),
        tp4_range=(20.0, 24.0),
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

    return f"""
## Timeframe-Based Exit Requirements ({config.timeframe})

This signal is on a **{config.timeframe} timeframe**. Use these distance guidelines:

### Stop-Loss Rules
- MINIMUM distance: {config.min_sl_pct}% from entry
- MAXIMUM distance: {config.max_sl_pct}% from entry (strictly enforced)
- RECOMMENDED: {config.default_sl_pct}% from entry

### Take-Profit Targets (Healthy R:R Required)
- TP1: {ranges['tp1'][0]}% to {ranges['tp1'][1]}% from entry
- TP2: {ranges['tp2'][0]}% to {ranges['tp2'][1]}% from entry
- TP3: {ranges['tp3'][0]}% to {ranges['tp3'][1]}% from entry
- TP4: {ranges['tp4'][0]}% to {ranges['tp4'][1]}% from entry

### Risk-Reward Ratio Requirements
- **MINIMUM R:R: 1.5:1** for any TP level (even in worst-case SL scenario)
- If using single TP mode, TP1 must be at least {config.min_tp_pct}% to ensure healthy R:R
- Expected R:R range: 1.5:1 to 3:1 depending on actual SL distance

### Critical Rules
1. DO NOT set SL above {config.max_sl_pct}% - will be rejected (oversized risk)
2. DO NOT set TP1 below {config.min_tp_pct}% - will be auto-adjusted
3. Place SL at logical invalidation point (support/resistance break), NOT random distance
4. For single-TP mode: ensure TP1 >= SL × 1.5 for healthy profit potential
"""
