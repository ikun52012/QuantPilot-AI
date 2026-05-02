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


# Timeframe configurations with realistic SL/TP distances
TIMEFRAME_CONFIGS: dict[str, TimeframeExitConfig] = {
    # Very short timeframes - scalping style
    "1": TimeframeExitConfig(
        timeframe="1",
        min_sl_pct=0.15,
        max_sl_pct=0.5,
        default_sl_pct=0.25,
        min_tp_pct=0.2,
        max_tp_pct=0.8,
        tp1_range=(0.2, 0.4),
        tp2_range=(0.4, 0.6),
        tp3_range=(0.6, 0.8),
        tp4_range=(0.8, 1.0),
    ),
    "1m": TimeframeExitConfig(
        timeframe="1m",
        min_sl_pct=0.15,
        max_sl_pct=0.5,
        default_sl_pct=0.25,
        min_tp_pct=0.2,
        max_tp_pct=0.8,
        tp1_range=(0.2, 0.4),
        tp2_range=(0.4, 0.6),
        tp3_range=(0.6, 0.8),
        tp4_range=(0.8, 1.0),
    ),
    # Short timeframes - quick trades
    "3": TimeframeExitConfig(
        timeframe="3",
        min_sl_pct=0.2,
        max_sl_pct=0.8,
        default_sl_pct=0.35,
        min_tp_pct=0.3,
        max_tp_pct=1.2,
        tp1_range=(0.3, 0.5),
        tp2_range=(0.5, 0.8),
        tp3_range=(0.8, 1.0),
        tp4_range=(1.0, 1.5),
    ),
    "5": TimeframeExitConfig(
        timeframe="5",
        min_sl_pct=0.25,
        max_sl_pct=1.0,
        default_sl_pct=0.4,
        min_tp_pct=0.35,
        max_tp_pct=1.5,
        tp1_range=(0.4, 0.7),
        tp2_range=(0.7, 1.0),
        tp3_range=(1.0, 1.3),
        tp4_range=(1.3, 1.8),
    ),
    "5m": TimeframeExitConfig(
        timeframe="5m",
        min_sl_pct=0.25,
        max_sl_pct=1.0,
        default_sl_pct=0.4,
        min_tp_pct=0.35,
        max_tp_pct=1.5,
        tp1_range=(0.4, 0.7),
        tp2_range=(0.7, 1.0),
        tp3_range=(1.0, 1.3),
        tp4_range=(1.3, 1.8),
    ),
    # Medium-short timeframes
    "15": TimeframeExitConfig(
        timeframe="15",
        min_sl_pct=0.35,
        max_sl_pct=2.0,
        default_sl_pct=0.6,
        min_tp_pct=0.5,
        max_tp_pct=2.5,
        tp1_range=(0.6, 1.0),
        tp2_range=(1.0, 1.5),
        tp3_range=(1.5, 2.0),
        tp4_range=(2.0, 2.5),
    ),
    "15m": TimeframeExitConfig(
        timeframe="15m",
        min_sl_pct=0.35,
        max_sl_pct=2.0,
        default_sl_pct=0.6,
        min_tp_pct=0.5,
        max_tp_pct=2.5,
        tp1_range=(0.6, 1.0),
        tp2_range=(1.0, 1.5),
        tp3_range=(1.5, 2.0),
        tp4_range=(2.0, 2.5),
    ),
    # Medium timeframes - standard swing trades
    "30": TimeframeExitConfig(
        timeframe="30",
        min_sl_pct=0.5,
        max_sl_pct=3.0,
        default_sl_pct=0.8,
        min_tp_pct=0.7,
        max_tp_pct=3.5,
        tp1_range=(0.8, 1.3),
        tp2_range=(1.3, 2.0),
        tp3_range=(2.0, 2.8),
        tp4_range=(2.8, 3.5),
    ),
    "60": TimeframeExitConfig(
        timeframe="60",
        min_sl_pct=0.7,
        max_sl_pct=4.0,
        default_sl_pct=1.2,
        min_tp_pct=1.0,
        max_tp_pct=5.0,
        tp1_range=(1.2, 2.0),
        tp2_range=(2.0, 3.0),
        tp3_range=(3.0, 4.0),
        tp4_range=(4.0, 5.0),
    ),
    "1h": TimeframeExitConfig(
        timeframe="1h",
        min_sl_pct=0.7,
        max_sl_pct=4.0,
        default_sl_pct=1.2,
        min_tp_pct=1.0,
        max_tp_pct=5.0,
        tp1_range=(1.2, 2.0),
        tp2_range=(2.0, 3.0),
        tp3_range=(3.0, 4.0),
        tp4_range=(4.0, 5.0),
    ),
    # Longer timeframes - position trades
    "120": TimeframeExitConfig(
        timeframe="120",
        min_sl_pct=1.0,
        max_sl_pct=5.0,
        default_sl_pct=1.8,
        min_tp_pct=1.5,
        max_tp_pct=6.0,
        tp1_range=(1.8, 2.5),
        tp2_range=(2.5, 3.5),
        tp3_range=(3.5, 4.5),
        tp4_range=(4.5, 6.0),
    ),
    "240": TimeframeExitConfig(
        timeframe="240",
        min_sl_pct=1.5,
        max_sl_pct=7.0,
        default_sl_pct=2.5,
        min_tp_pct=2.0,
        max_tp_pct=8.0,
        tp1_range=(2.5, 3.5),
        tp2_range=(3.5, 5.0),
        tp3_range=(5.0, 6.5),
        tp4_range=(6.5, 8.0),
    ),
    "4h": TimeframeExitConfig(
        timeframe="4h",
        min_sl_pct=1.5,
        max_sl_pct=7.0,
        default_sl_pct=2.5,
        min_tp_pct=2.0,
        max_tp_pct=8.0,
        tp1_range=(2.5, 3.5),
        tp2_range=(3.5, 5.0),
        tp3_range=(5.0, 6.5),
        tp4_range=(6.5, 8.0),
    ),
    # Daily timeframe - macro trades
    "1D": TimeframeExitConfig(
        timeframe="1D",
        min_sl_pct=2.5,
        max_sl_pct=10.0,
        default_sl_pct=4.0,
        min_tp_pct=3.5,
        max_tp_pct=12.0,
        tp1_range=(4.0, 5.5),
        tp2_range=(5.5, 7.5),
        tp3_range=(7.5, 9.5),
        tp4_range=(9.5, 12.0),
    ),
    "D": TimeframeExitConfig(
        timeframe="D",
        min_sl_pct=2.5,
        max_sl_pct=10.0,
        default_sl_pct=4.0,
        min_tp_pct=3.5,
        max_tp_pct=12.0,
        tp1_range=(4.0, 5.5),
        tp2_range=(5.5, 7.5),
        tp3_range=(7.5, 9.5),
        tp4_range=(9.5, 12.0),
    ),
    # Weekly timeframe
    "1W": TimeframeExitConfig(
        timeframe="1W",
        min_sl_pct=4.0,
        max_sl_pct=15.0,
        default_sl_pct=6.0,
        min_tp_pct=5.0,
        max_tp_pct=15.0,
        tp1_range=(6.0, 8.0),
        tp2_range=(8.0, 10.0),
        tp3_range=(10.0, 12.0),
        tp4_range=(12.0, 15.0),
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
- MAXIMUM distance: {config.max_sl_pct}% from entry
- RECOMMENDED: {config.default_sl_pct}% from entry (adjust based on volatility)

### Take-Profit Targets
- TP1: {ranges['tp1'][0]}% to {ranges['tp1'][1]}% from entry (closest target)
- TP2: {ranges['tp2'][0]}% to {ranges['tp2'][1]}% from entry
- TP3: {ranges['tp3'][0]}% to {ranges['tp3'][1]}% from entry
- TP4: {ranges['tp4'][0]}% to {ranges['tp4'][1]}% from entry (furthest target)

### Critical Rules
1. DO NOT set SL below {config.min_sl_pct}% - will be auto-adjusted
2. DO NOT set SL above {config.max_sl_pct}% - will be rejected (oversized risk)
3. DO NOT set TP1 below {config.min_tp_pct}% - will be auto-adjusted
4. Place SL at logical invalidation point (support/resistance break), NOT random distance
5. Scale TP levels appropriately - first target should be achievable within timeframe horizon
"""
