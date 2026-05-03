"""
QuantPilot AI - Smart Money Concepts (SMC) & Fair Value Gap (FVG) Analyzer

Provides multi-timeframe structural analysis for optimal entry detection:
  - FVG (Fair Value Gap): Imbalance zones where price moved too fast, leaving gaps
  - Order Blocks: Last opposing candle before a strong move (institutional footprint)
  - Break of Structure (BOS): Higher-high / lower-low confirmation
  - Change of Character (CHoCH): First sign of trend reversal
  - Premium / Discount zones: Fibonacci-based value areas

These concepts are used by the AI to suggest better entry prices when the
original TradingView signal price is suboptimal.

OPTIMIZATIONS:
  - Timeframe weights (HTF priority over LTF)
  - FVG/OB aging decay (freshness scoring)
  - Structure break strength analysis
  - Structure risk scoring system
  - Entry timing optimization
  - Confluence zone quality classification
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

# ─────────────────────────────────────────────
# Timeframe weights and priority system (P0-1)
# ─────────────────────────────────────────────

TIMEFRAME_WEIGHTS = {
    "4h": 3.0,   # HTF - Highest weight, structure conflicts should reject signals
    "1d": 4.0,   # Daily - Even higher for position trading
    "1h": 2.0,   # MTF - Medium weight, confirms HTF structure
    "30m": 1.5,  # STF - Sub-medium, fills gap between 1h and 15m
    "15m": 1.0,  # LTF - Lower timeframe, precise entry but shouldn't override HTF
    "5m": 0.8,   # Very low timeframe, only for scalping
}

TIMEFRAME_LABELS = {
    "4h": "HTF",
    "1d": "HTF",
    "1h": "MTF",
    "30m": "STF",
    "15m": "LTF",
    "5m": "VLTF",
}


def get_timeframe_weight(timeframe: str) -> float:
    """Get weight for a timeframe."""
    return TIMEFRAME_WEIGHTS.get(timeframe, 1.0)


def get_timeframe_label(timeframe: str) -> str:
    """Get label (HTF/MTF/STF/LTF) for a timeframe."""
    return TIMEFRAME_LABELS.get(timeframe, "MTF")


# ─────────────────────────────────────────────
# Dynamic timeframe selection (P0-3)
# ─────────────────────────────────────────────

def select_timeframes_for_signal(signal_timeframe: str) -> dict[str, str]:
    """Select appropriate SMC analysis timeframes based on signal timeframe.

    Scalping (<=15m): Use smaller timeframes for precision
    Swing (1h-4h): Use current configuration
    Position (>4h): Use larger timeframes for macro view
    """
    tf_minutes = {
        "1": 1, "5": 5, "15": 15, "30": 30,
        "60": 60, "240": 240, "D": 1440, "1D": 1440, "W": 10080, "1W": 10080,
    }

    normalized_tf = str(signal_timeframe).upper().replace("M", "").replace("MIN", "")
    signal_minutes = tf_minutes.get(normalized_tf, 60)

    if signal_minutes <= 15:
        # Scalping: Use 1h as HTF, 5m as LTF
        return {"htf": "1h", "mtf": "30m", "stf": "15m", "ltf": "5m"}
    elif signal_minutes <= 240:
        # Swing trading: Current configuration
        return {"htf": "4h", "mtf": "1h", "stf": "30m", "ltf": "15m"}
    else:
        # Position trading: Use daily as HTF
        return {"htf": "1d", "mtf": "4h", "stf": "1h", "ltf": "30m"}


def _ohlcv_value(candle, index: int, key: str) -> float:
    """Read OHLCV values from either list-based or dict-based candle input."""
    if isinstance(candle, dict):
        return float(candle.get(key, 0.0) or 0.0)
    return float(candle[index])


# ─────────────────────────────────────────────
# Aging and effectiveness scoring (P1-5)
# ─────────────────────────────────────────────

def calculate_fvg_effectiveness_score(fvg_age: int, max_age: int = 100) -> float:
    """Calculate FVG effectiveness score based on age.

    Fresh FVG (< 20 candles) = High effectiveness (0.9-1.0)
    Medium age (20-50 candles) = Medium effectiveness (0.6-0.9)
    Old FVG (> 50 candles) = Low effectiveness (0.3-0.6)

    Args:
        fvg_age: Number of candles since FVG formation
        max_age: Maximum age before FVG considered stale

    Returns:
        Effectiveness score (0.3-1.0)
    """
    if fvg_age < 0:
        return 1.0

    # Age decay: exponential decay
    if fvg_age < 20:
        return 1.0 - (fvg_age * 0.005)  # 0.995-1.0
    elif fvg_age < 50:
        return 0.9 - ((fvg_age - 20) * 0.01)  # 0.6-0.9
    else:
        return max(0.3, 0.6 - ((fvg_age - 50) * 0.01))  # 0.3-0.6


def calculate_ob_effectiveness_score(ob_age: int, ob_strength: float, max_age: int = 80) -> float:
    """Calculate Order Block effectiveness score based on age and strength.

    Args:
        ob_age: Number of candles since OB formation
        ob_strength: Initial OB strength (0-1)
        max_age: Maximum age before OB considered stale

    Returns:
        Effectiveness score (0.2-1.0)
    """
    if ob_age < 0:
        return ob_strength

    # Age decay similar to FVG but slightly more aggressive
    age_factor = calculate_fvg_effectiveness_score(ob_age, max_age)

    # Combine age and strength
    return round(ob_strength * age_factor, 2)


# ─────────────────────────────────────────────
# Structure break strength analysis (P1-6)
# ─────────────────────────────────────────────

def calculate_break_strength(
    ohlcv: list[list],
    swing_point_index: int,
    swing_type: str,
    is_bos: bool,
    timeframe: str = "1h"
) -> float:
    """Calculate structure break strength (0-1).

    Strong break (>0.7): Large momentum, high confidence
    Moderate break (0.4-0.7): Average momentum
    Weak break (<0.4): Low momentum, questionable

    Args:
        ohlcv: OHLCV data
        swing_point_index: Index of swing point
        swing_type: "high" or "low"
        is_bos: True for BOS, False for CHoCH
        timeframe: Timeframe string

    Returns:
        Break strength score (0-1)
    """
    if swing_point_index + 1 >= len(ohlcv):
        return 0.5

    # Get swing point price
    swing_candle = ohlcv[swing_point_index]
    swing_price = _ohlcv_value(swing_candle, 2, "high") if swing_type == "high" else _ohlcv_value(swing_candle, 3, "low")

    # Get break candle
    break_candle = ohlcv[swing_point_index + 1]
    break_high = _ohlcv_value(break_candle, 2, "high")
    break_low = _ohlcv_value(break_candle, 3, "low")
    break_close = _ohlcv_value(break_candle, 4, "close")  # BUG-2 FIX: Use close to determine if break sustained
    break_volume = _ohlcv_value(break_candle, 5, "volume")

    # Calculate swing candle range
    swing_range = _ohlcv_value(swing_candle, 2, "high") - _ohlcv_value(swing_candle, 3, "low")
    if swing_range <= 0:
        swing_range = abs(swing_price) * 0.01

    # Calculate break magnitude
    if is_bos:
        # BOS: How far price broke beyond swing point
        if swing_type == "high":
            # Bullish BOS: broke above swing high
            break_magnitude = break_high - swing_price
        else:
            # Bearish BOS: broke below swing low
            break_magnitude = swing_price - break_low
    else:
        # CHoCH: Strength of reversal
        # Measure how strong the opposite move is
        if swing_type == "high":
            # Bearish CHoCH: broke below swing high, measure downside momentum
            break_magnitude = swing_price - break_low
        else:
            # Bullish CHoCH: broke above swing low, measure upside momentum
            break_magnitude = break_high - swing_price

    # Normalize to 0-1 based on swing range
    magnitude_score = min(1.0, abs(break_magnitude) / swing_range)

    # BUG-2 FIX: Add sustainability score - did close sustain beyond swing point?
    if swing_type == "high":
        # For high swing, close should be above swing for bullish, below for bearish
        if (is_bos and break_close > swing_price) or (not is_bos and break_close < swing_price):
            sustainability_score = 1.0  # Close sustained the break
        else:
            sustainability_score = 0.5  # Break but close didn't sustain (potentially false break)
    else:
        # For low swing, close should be below swing for bullish BOS (up), above for bearish CHoCH
        if (is_bos and break_close < swing_price) or (not is_bos and break_close > swing_price):
            sustainability_score = 1.0
        else:
            sustainability_score = 0.5

    # Volume confirmation (higher volume = stronger break)
    avg_volume = sum(_ohlcv_value(c, 5, "volume") for c in ohlcv[-10:]) / 10 if len(ohlcv) >= 10 else break_volume
    if avg_volume > 0:
        volume_factor = min(1.5, break_volume / avg_volume) / 1.5  # Normalize to 0-1
    else:
        volume_factor = 0.5

    # BUG-2 FIX: Combine magnitude, sustainability, and volume (40%, 30%, 30%)
    strength = round((magnitude_score * 0.4 + sustainability_score * 0.3 + volume_factor * 0.3), 2)

    return max(0.2, min(1.0, strength))


def _structure_dict(structure: MarketStructure) -> dict:
    event_type = "none"
    if structure.last_choch:
        event_type = "choch"
    elif structure.last_bos:
        event_type = "bos"
    return {
        "type": event_type,
        "trend": structure.trend,
        "last_bos": structure.last_bos,
        "last_choch": structure.last_choch,
        "swing_highs": structure.swing_highs,
        "swing_lows": structure.swing_lows,
    }


class CompatDictMixin:
    """Allow old tests and adapters to access dataclasses like dicts."""

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        if key == "high":
            top = getattr(self, "top", None)
            bottom = getattr(self, "bottom", None)
            if top is not None and bottom is not None:
                return max(float(top), float(bottom))
        if key == "low":
            top = getattr(self, "top", None)
            bottom = getattr(self, "bottom", None)
            if top is not None and bottom is not None:
                return min(float(top), float(bottom))
        return getattr(self, key)


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class FVG(CompatDictMixin):
    """A Fair Value Gap (imbalance zone)."""
    type: str           # "bullish" or "bearish"
    top: float          # upper boundary
    bottom: float       # lower boundary
    midpoint: float     # (top + bottom) / 2
    timeframe: str      # e.g. "1h", "4h", "15m"
    candle_index: int   # index in the OHLCV array
    filled: bool = False
    fill_percentage: float = 0.0  # ENH-1: Partial fill tracking (0-100%)
    effectiveness: float = 1.0  # Age-based effectiveness score (0.3-1.0)


@dataclass
class OrderBlock(CompatDictMixin):
    """An Order Block (last opposing candle before impulse)."""
    type: str           # "bullish" or "bearish"
    high: float
    low: float
    midpoint: float
    timeframe: str
    candle_index: int
    strength: float = 0.0  # 0-1 based on impulse magnitude
    effectiveness: float = 1.0  # Age+strength effectiveness score (0.2-1.0), start at 1.0 for fresh OB
    # ENH-2: Mitigation phase tracking
    mitigation_count: int = 0  # Number of times price has revisited the OB
    mitigation_status: str = "untested"  # States: "untested", "partial", "mitigated", "broken"
    last_mitigation_price: float | None = None  # Price at last mitigation touch


@dataclass
class StructurePoint(CompatDictMixin):  # ENH-5: Add CompatDictMixin for API consistency
    """A swing high or swing low."""
    type: str           # "high" or "low"
    price: float
    index: int
    timeframe: str
    break_strength: float = 0.5  # Strength of the break at this point (0-1)


@dataclass
class MarketStructure:
    """Break of Structure / Change of Character detection."""
    trend: str              # "bullish", "bearish", "ranging"
    last_bos: str | None = None   # "bullish_bos" or "bearish_bos"
    last_choch: str | None = None # "bullish_choch" or "bearish_choch"
    swing_highs: list[StructurePoint] = field(default_factory=list)
    swing_lows: list[StructurePoint] = field(default_factory=list)
    break_strength: float = 0.5  # NEW: Overall structure break strength
    # P3-13: Structural momentum tracking
    hh_count: int = 0       # Number of consecutive Higher Highs (bullish momentum)
    ll_count: int = 0       # Number of consecutive Lower Lows (bearish momentum)
    structure_age: int = 0  # Candles since last BOS/CHoCH (structure maturity)

    def get(self, key: str, default=None):
        if key == "type":
            if self.last_choch:
                return "choch"
            if self.last_bos:
                return "bos"
            return "none"
        return getattr(self, key, default)


@dataclass
class SMCContext:
    """Complete SMC analysis for a single timeframe."""
    timeframe: str
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    structure: MarketStructure | None = None
    premium_zone: float = 0.0   # price above which is "premium"
    discount_zone: float = 0.0  # price below which is "discount"
    equilibrium: float = 0.0    # midpoint
    risk_score: float = 0.5  # Structure-based risk score (0-1, higher = more risky)
    entry_timing_score: float = 0.7  # Entry timing quality (0-1, higher = better timing)
    timing_recommendation: str = ""  # Entry timing recommendation text
    # ENH-4: Liquidity sweep zones (populated if orderbook data available)
    liquidity_sweep_zones: list[dict] = field(default_factory=list)  # Simplified sweep zone data


@dataclass
class MultiTimeframeSMC:
    """SMC analysis across multiple timeframes."""
    htf: SMCContext | None = None   # Higher timeframe (4h/1d)
    mtf: SMCContext | None = None   # Medium timeframe (1h/4h)
    stf: SMCContext | None = None   # Sub-medium timeframe (30m/1h)
    ltf: SMCContext | None = None   # Lower timeframe (15m/30m)
    confluence_zones: list[dict] = field(default_factory=list)
    overall_risk_score: float = 0.5  # NEW: Combined risk score across all timeframes
    htf_conflict: bool = False  # NEW: HTF structure conflicts with signal direction
    htf_conflict_type: str = ""  # NEW: Type of HTF conflict if any


# ─────────────────────────────────────────────
# Core detection algorithms
# ─────────────────────────────────────────────

def detect_swing_points(
    ohlcv: list[list],
    lookback: int = 3,
    timeframe: str = "1h",
) -> tuple[list[StructurePoint], list[StructurePoint]]:
    """Detect swing highs and swing lows using a simple N-bar pivot method.

    P1-6: Dynamic lookback based on timeframe for better swing detection.
    """
    highs: list[StructurePoint] = []
    lows: list[StructurePoint] = []

    # P1-6: Dynamic lookback based on timeframe
    tf_minutes = {
        "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440
    }
    tf_mins = tf_minutes.get(timeframe, 60)

    # Larger TFs need larger lookback for meaningful swing points
    if tf_mins >= 240:  # 4h or larger
        lookback = 5  # 5 candles = 20h on 4h, 5d on daily
    elif tf_mins >= 60:  # 1h or 30m
        lookback = 4  # 4 candles = 4h on 1h, 2h on 30m
    else:
        lookback = 3  # Scalping TFs (5m, 15m), keep default

    if len(ohlcv) < lookback * 2 + 1:
        return highs, lows

    for i in range(lookback, len(ohlcv) - lookback):
        high_i = _ohlcv_value(ohlcv[i], 2, "high")
        low_i = _ohlcv_value(ohlcv[i], 3, "low")

        is_swing_high = all(high_i >= _ohlcv_value(ohlcv[i - j], 2, "high") for j in range(1, lookback + 1)) and \
                         all(high_i >= _ohlcv_value(ohlcv[i + j], 2, "high") for j in range(1, lookback + 1))
        is_swing_low = all(low_i <= _ohlcv_value(ohlcv[i - j], 3, "low") for j in range(1, lookback + 1)) and \
                        all(low_i <= _ohlcv_value(ohlcv[i + j], 3, "low") for j in range(1, lookback + 1))

        if is_swing_high:
            highs.append(StructurePoint(type="high", price=high_i, index=i, timeframe=timeframe))
        if is_swing_low:
            lows.append(StructurePoint(type="low", price=low_i, index=i, timeframe=timeframe))

    return highs, lows


def detect_market_structure(
    ohlcv: list,
    timeframe: str | float = "1h",
):
    """Detect BOS (Break of Structure) and CHoCH (Change of Character).

    P2-9: Explicit boundary handling for edge cases.
    """
    # P2-9: Explicit boundary checks
    if not ohlcv:
        return MarketStructure(trend="ranging")

    # P2-9: Need at least 7 candles for swing detection (lookback*2+1 with dynamic lookback)
    min_candles = 7 if str(timeframe) in ["1h", "30m", "15m", "5m"] else 11  # 4h/daily need larger lookback
    if len(ohlcv) < min_candles:
        return MarketStructure(trend="ranging")

    # Compat mode: convert dict format to StructurePoint list (P0-3 FIX: always return MarketStructure object)
    if ohlcv and isinstance(ohlcv[0], dict) and "price" in ohlcv[0]:
        highs = [StructurePoint(type="high", price=float(p["price"]), index=int(p.get("index", 0)), timeframe="compat") for p in ohlcv if p.get("type") == "high"]
        lows = [StructurePoint(type="low", price=float(p["price"]), index=int(p.get("index", 0)), timeframe="compat") for p in ohlcv if p.get("type") == "low"]
        structure = MarketStructure(trend="ranging", swing_highs=highs[-5:], swing_lows=lows[-5:])
        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1].price > highs[-2].price
            hl = lows[-1].price > lows[-2].price
            lh = highs[-1].price < highs[-2].price
            ll = lows[-1].price < lows[-2].price
            if hh and hl:
                structure.trend = "bullish"
                structure.last_bos = "bullish_bos"
            elif lh and ll:
                structure.trend = "bearish"
                structure.last_bos = "bearish_bos"
        return structure  # FIX: Return MarketStructure object instead of dict

    swing_highs, swing_lows = detect_swing_points(ohlcv, lookback=3, timeframe=str(timeframe))

    structure = MarketStructure(
        trend="ranging",
        swing_highs=swing_highs[-5:],
        swing_lows=swing_lows[-5:],
    )

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return structure

    # P3-13: Calculate structural momentum (consecutive HH/LL counts)
    hh_count = 0
    ll_count = 0
    for i in range(len(swing_highs) - 1):
        if swing_highs[i + 1].price > swing_highs[i].price:
            hh_count += 1
        else:
            break
    for i in range(len(swing_lows) - 1):
        if swing_lows[i + 1].price < swing_lows[i].price:
            ll_count += 1
        else:
            break

    # P3-13: Calculate structure age (candles since last swing point)
    last_swing_index = max(swing_highs[-1].index, swing_lows[-1].index)
    structure_age = len(ohlcv) - last_swing_index - 1

    structure.hh_count = hh_count
    structure.ll_count = ll_count
    structure.structure_age = structure_age

    # Check for Higher Highs + Higher Lows (bullish) or Lower Highs + Lower Lows (bearish)
    last_2_highs = swing_highs[-2:]
    last_2_lows = swing_lows[-2:]

    hh = last_2_highs[1].price > last_2_highs[0].price  # Higher High
    hl = last_2_lows[1].price > last_2_lows[0].price     # Higher Low
    lh = last_2_highs[1].price < last_2_highs[0].price   # Lower High
    ll = last_2_lows[1].price < last_2_lows[0].price     # Lower Low

    if hh and hl:
        structure.trend = "bullish"
        structure.last_bos = "bullish_bos"
        # NEW: Calculate break strength (P0-1 FIX)
        if len(swing_lows) >= 2:
            structure.break_strength = calculate_break_strength(
                ohlcv, swing_lows[-2].index, "low", True, timeframe
            )
    elif lh and ll:
        structure.trend = "bearish"
        structure.last_bos = "bearish_bos"
        # NEW: Calculate break strength (P0-1 FIX)
        if len(swing_highs) >= 2:
            structure.break_strength = calculate_break_strength(
                ohlcv, swing_highs[-2].index, "high", True, timeframe
            )
    else:
        structure.trend = "ranging"

    # CHoCH detection: trend was bullish but made a Lower Low, or bearish but made a Higher High
    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        prev_highs = swing_highs[-3:-1]
        prev_lows = swing_lows[-3:-1]

        was_bullish = prev_highs[1].price > prev_highs[0].price and prev_lows[1].price > prev_lows[0].price
        was_bearish = prev_highs[1].price < prev_highs[0].price and prev_lows[1].price < prev_lows[0].price

        if was_bullish and ll:
            structure.last_choch = "bearish_choch"
            # NEW: Calculate CHoCH break strength (P0-1 FIX)
            if len(swing_lows) >= 2:
                structure.break_strength = calculate_break_strength(
                    ohlcv, swing_lows[-2].index, "low", False, timeframe
                )
        elif was_bearish and hh:
            structure.last_choch = "bullish_choch"
            # NEW: Calculate CHoCH break strength (P0-1 FIX)
            if len(swing_highs) >= 2:
                structure.break_strength = calculate_break_strength(
                    ohlcv, swing_highs[-2].index, "high", False, timeframe
                )

    return structure


def detect_fvgs(
    ohlcv: list[list],
    timeframe: str = "1h",
    current_price: float = 0.0,
    max_results: int = 5,
    min_volume_ratio: float = 1.2,  # P1-7: Volume confirmation threshold
) -> list[FVG]:
    """Detect Fair Value Gaps (imbalance zones) in OHLCV data.

    A Bullish FVG: candle[i-1].high < candle[i+1].low  (gap up)
    A Bearish FVG: candle[i-1].low > candle[i+1].high  (gap down)

    NEW: Includes effectiveness scoring based on age.
    P1-7: Volume confirmation to filter weak FVGs.
    """
    fvgs: list[FVG] = []

    if len(ohlcv) < 3:
        return fvgs

    total_candles = len(ohlcv)

    # P1-7: Calculate average volume for confirmation
    volumes = [_ohlcv_value(c, 5, "volume") for c in ohlcv[-20:] if len(c) >= 6]
    # BUG-3 FIX: Filter out zero-volume candles and warn if data quality is poor
    non_zero_volumes = [v for v in volumes if v > 0]
    zero_volume_count = len(volumes) - len(non_zero_volumes)
    if zero_volume_count > len(volumes) * 0.3 and len(volumes) > 0:  # >30% zero volume
        logger.warning(f"[SMC/FVG] Poor data quality: {zero_volume_count}/{len(volumes)} zero-volume candles in {timeframe}")
    avg_volume = sum(non_zero_volumes) / len(non_zero_volumes) if non_zero_volumes else 1.0  # BUG-3 FIX: Use 1.0 minimum

    # ENH-1/BUG-3: If no volume data available (compat mode or missing data), skip volume filter
    skip_volume_filter = len(non_zero_volumes) == 0

    for i in range(1, len(ohlcv) - 1):
        prev_high = _ohlcv_value(ohlcv[i - 1], 2, "high")
        prev_low = _ohlcv_value(ohlcv[i - 1], 3, "low")
        next_high = _ohlcv_value(ohlcv[i + 1], 2, "high")
        next_low = _ohlcv_value(ohlcv[i + 1], 3, "low")

        # P1-7: Get impulse candle volume
        impulse_volume = _ohlcv_value(ohlcv[i], 5, "volume")
        volume_ratio = impulse_volume / avg_volume if avg_volume > 0 else 1.0

        # Calculate age (distance from current candle)
        fvg_age = total_candles - i - 1
        effectiveness = calculate_fvg_effectiveness_score(fvg_age)

        # P1-7: Boost effectiveness if volume confirmed
        if volume_ratio >= min_volume_ratio:
            effectiveness = min(1.0, effectiveness * 1.2)  # Boost by 20%

        # Bullish FVG: gap between prev candle high and next candle low
        if prev_high < next_low:
            top = next_low
            bottom = prev_high
            fvg_size = top - bottom

            # ENH-1: Calculate fill percentage
            fill_percentage = 0.0
            filled = False
            if current_price > 0 and fvg_size > 0:
                if current_price <= bottom:
                    filled = True
                    fill_percentage = 100.0
                elif current_price < top:
                    # Partially filled - calculate percentage
                    filled = False
                    fill_percentage = round((current_price - bottom) / fvg_size * 100, 2)

            # P1-7: Only add FVG with sufficient volume (skip filter if no volume data available)
            if skip_volume_filter or volume_ratio >= min_volume_ratio:
                fvgs.append(FVG(
                    type="bullish",
                    top=top,
                    bottom=bottom,
                    midpoint=round((top + bottom) / 2, 8),
                    timeframe=timeframe,
                    candle_index=i,
                    filled=filled,
                    fill_percentage=fill_percentage,  # ENH-1: Add fill percentage
                    effectiveness=effectiveness,
                ))

        # Bearish FVG: gap between prev candle low and next candle high
        if prev_low > next_high:
            top = prev_low
            bottom = next_high
            fvg_size = top - bottom

            # ENH-1: Calculate fill percentage
            fill_percentage = 0.0
            filled = False
            if current_price > 0 and fvg_size > 0:
                if current_price >= top:
                    filled = True
                    fill_percentage = 100.0
                elif current_price > bottom:
                    # Partially filled - calculate percentage
                    filled = False
                    fill_percentage = round((top - current_price) / fvg_size * 100, 2)

            # P1-7: Only add FVG with sufficient volume (skip filter if no volume data available)
            if skip_volume_filter or volume_ratio >= min_volume_ratio:
                fvgs.append(FVG(
                    type="bearish",
                    top=top,
                    bottom=bottom,
                    midpoint=round((top + bottom) / 2, 8),
                    timeframe=timeframe,
                    candle_index=i,
                    filled=filled,
                    fill_percentage=fill_percentage,  # ENH-1: Add fill percentage
                    effectiveness=effectiveness,
                ))

    # ENH-1: Return unfilled OR partially filled (<70%) FVGs, most recent first
    unfilled_or_partial = [f for f in fvgs if not f.filled or f.fill_percentage < 70.0]
    return unfilled_or_partial[-max_results:]


def detect_order_blocks(
    ohlcv: list[list],
    timeframe: str = "1h",
    min_impulse_pct: float = 0.5,
    max_results: int = 3,
    min_volume_ratio: float = 1.5,  # P1-7: Volume confirmation threshold (higher for OBs)
) -> list[OrderBlock]:
    """Detect Order Blocks — the last opposing candle before a strong impulse move.

    Bullish OB: last bearish candle before a strong bullish move
    Bearish OB: last bullish candle before a strong bearish move

    NEW: Includes effectiveness scoring based on age and strength.
    P1-7: Volume confirmation to filter weak OBs.
    """
    obs: list[OrderBlock] = []

    if len(ohlcv) < 4:
        return obs

    total_candles = len(ohlcv)

    # P1-7: Calculate average volume for confirmation
    volumes = [_ohlcv_value(c, 5, "volume") for c in ohlcv[-20:] if len(c) >= 6]
    # BUG-3 FIX: Filter out zero-volume candles and warn if data quality is poor
    non_zero_volumes = [v for v in volumes if v > 0]
    zero_volume_count = len(volumes) - len(non_zero_volumes)
    if zero_volume_count > len(volumes) * 0.3 and len(volumes) > 0:  # >30% zero volume
        logger.warning(f"[SMC/OB] Poor data quality: {zero_volume_count}/{len(volumes)} zero-volume candles in {timeframe}")
    avg_volume = sum(non_zero_volumes) / len(non_zero_volumes) if non_zero_volumes else 1.0  # BUG-3 FIX: Use 1.0 minimum

    # ENH-1/BUG-3: If no volume data available (compat mode or missing data), skip volume filter
    skip_volume_filter = len(non_zero_volumes) == 0

    for i in range(1, len(ohlcv) - 2):
        open_i = _ohlcv_value(ohlcv[i], 1, "open")
        close_i = _ohlcv_value(ohlcv[i], 4, "close")
        high_i = _ohlcv_value(ohlcv[i], 2, "high")
        low_i = _ohlcv_value(ohlcv[i], 3, "low")

        # P1-7: Get impulse candles volume (next 2 candles)
        impulse_volume = (_ohlcv_value(ohlcv[i + 1], 5, "volume") + _ohlcv_value(ohlcv[i + 2], 5, "volume")) / 2.0
        volume_ratio = impulse_volume / avg_volume if avg_volume > 0 else 1.0

        is_bearish_candle = close_i < open_i
        is_bullish_candle = close_i > open_i

        # Check the next 2 candles for impulse
        next_high = max(_ohlcv_value(ohlcv[i + 1], 2, "high"), _ohlcv_value(ohlcv[i + 2], 2, "high"))
        next_low = min(_ohlcv_value(ohlcv[i + 1], 3, "low"), _ohlcv_value(ohlcv[i + 2], 3, "low"))

        mid_price = (high_i + low_i) / 2 if (high_i + low_i) > 0 else 1

        # Calculate age
        ob_age = total_candles - i - 1

        # Bullish OB: bearish candle followed by strong bullish impulse
        if is_bearish_candle:
            impulse_pct = (next_high - high_i) / mid_price * 100
            if impulse_pct >= min_impulse_pct:
                strength = min(1.0, impulse_pct / 3.0)
                effectiveness = calculate_ob_effectiveness_score(ob_age, strength)

                # P1-7: Boost effectiveness if volume confirmed
                if volume_ratio >= min_volume_ratio:
                    effectiveness = min(1.0, effectiveness * 1.3)  # Boost by 30%

                # P1-7: Only add OB with sufficient volume (skip filter if no volume data available)
                if skip_volume_filter or volume_ratio >= min_volume_ratio:
                    obs.append(OrderBlock(
                        type="bullish",
                        high=high_i,
                        low=low_i,
                        midpoint=round(mid_price, 8),
                        timeframe=timeframe,
                        candle_index=i,
                        strength=round(strength, 3),
                        effectiveness=effectiveness,
                    ))

        # Bearish OB: bullish candle followed by strong bearish impulse
        if is_bullish_candle:
            impulse_pct = (low_i - next_low) / mid_price * 100
            if impulse_pct >= min_impulse_pct:
                strength = min(1.0, impulse_pct / 3.0)
                effectiveness = calculate_ob_effectiveness_score(ob_age, strength)

                # P1-7: Boost effectiveness if volume confirmed
                if volume_ratio >= min_volume_ratio:
                    effectiveness = min(1.0, effectiveness * 1.3)  # Boost by 30%

                # P1-7: Only add OB with sufficient volume (skip filter if no volume data available)
                if skip_volume_filter or volume_ratio >= min_volume_ratio:
                    obs.append(OrderBlock(
                        type="bearish",
                        high=high_i,
                        low=low_i,
                        midpoint=round(mid_price, 8),
                        timeframe=timeframe,
                        candle_index=i,
                        strength=round(strength, 3),
                        effectiveness=effectiveness,
                    ))

    return obs[-max_results:]


def calculate_premium_discount(
    swing_highs,
    swing_lows,
    atr_pct: float = 0.0,  # ENH-3: ATR percentage for dynamic zones
):
    """Calculate Premium/Discount/Equilibrium zones from recent swing range.

    Returns (premium_zone, discount_zone, equilibrium).
    Premium = above 61.8% of range (expensive, good for shorts)
    Discount = below 38.2% of range (cheap, good for longs)

    ENH-3: ATR-based dynamic zones:
    - High ATR (>3%): Expand zones to 0.70/0.30 (account for volatility)
    - Low ATR (<1%): Contract zones to 0.60/0.40 (tighter ranges)
    - Normal ATR: Use standard Fibonacci 0.618/0.382
    """
    # ENH-3: Determine Fibonacci levels based on ATR
    if atr_pct > 3.0:  # High volatility
        premium_fib = 0.70
        discount_fib = 0.30
    elif atr_pct < 1.0:  # Low volatility
        premium_fib = 0.60
        discount_fib = 0.40
    else:  # Normal volatility
        premium_fib = 0.618
        discount_fib = 0.382

    if isinstance(swing_highs, (int, float)) and isinstance(swing_lows, (int, float)):
        range_high = float(swing_highs or 0.0)
        range_low = float(swing_lows or 0.0)
        range_size = range_high - range_low
        if range_size <= 0:
            return {"premium": 0.0, "discount": 0.0, "equilibrium": 0.0}
        equilibrium = range_low + range_size * 0.5
        premium_zone = range_low + range_size * premium_fib  # ENH-3: Dynamic Fibonacci
        discount_zone = range_low + range_size * discount_fib  # ENH-3: Dynamic Fibonacci
        return {
            "premium": round(premium_zone, 8),
            "discount": round(discount_zone, 8),
            "equilibrium": round(equilibrium, 8),
        }

    if not swing_highs or not swing_lows:
        return 0.0, 0.0, 0.0

    range_high = max(sh.price for sh in swing_highs[-3:])
    range_low = min(sl.price for sl in swing_lows[-3:])
    range_size = range_high - range_low

    if range_size <= 0:
        return 0.0, 0.0, 0.0

    equilibrium = range_low + range_size * 0.5
    premium_zone = range_low + range_size * premium_fib  # ENH-3: Dynamic Fibonacci
    discount_zone = range_low + range_size * discount_fib  # ENH-3: Dynamic Fibonacci

    return round(premium_zone, 8), round(discount_zone, 8), round(equilibrium, 8)


def find_confluence_zones(
    htf_ctx: SMCContext | None,
    mtf_ctx: SMCContext | None,
    stf_ctx: SMCContext | None = None,
    ltf_ctx: SMCContext | None = None,
    direction: str = "long",
    current_price: float = 0.0,
) -> list[dict]:
    """Find zones where multiple timeframe levels overlap (highest probability entries).

    Supports both new API (SMCContext objects) and legacy API (list of dicts).
    Legacy API: find_confluence_zones(fvg_zones: list, ob_zones: list) -> list[dict]
    """
    # Legacy API support: if first two args are lists and rest are missing
    if isinstance(htf_ctx, list) and isinstance(mtf_ctx, list) and stf_ctx is None and ltf_ctx is None and direction == "long" and current_price == 0.0:
        compat_zones: list[dict] = []
        for a in htf_ctx:
            for b in mtf_ctx:
                if a.get("type") != b.get("type"):
                    continue
                overlap_top = min(float(a.get("high", 0.0)), float(b.get("high", 0.0)))
                overlap_bottom = max(float(a.get("low", 0.0)), float(b.get("low", 0.0)))
                if overlap_top > overlap_bottom:
                    compat_zones.append({
                        "confluence_top": round(overlap_top, 8),
                        "confluence_bottom": round(overlap_bottom, 8),
                        "confluence_midpoint": round((overlap_top + overlap_bottom) / 2, 8),
                        "strength": "medium",
                    })
        return compat_zones

    # New API: SMCContext objects
    zones: list[dict] = []
    all_levels: list[dict] = []

    for ctx, tf_label in [(htf_ctx, "HTF"), (mtf_ctx, "MTF"), (stf_ctx, "STF"), (ltf_ctx, "LTF")]:
        if not ctx:
            continue

        for fvg in ctx.fvgs:
            relevant = (direction == "long" and fvg.type == "bullish") or \
                       (direction == "short" and fvg.type == "bearish")
            if relevant and not fvg.filled:
                all_levels.append({
                    "type": "fvg",
                    "subtype": fvg.type,
                    "tf": tf_label,
                    "timeframe": fvg.timeframe,
                    "top": fvg.top,
                    "bottom": fvg.bottom,
                    "midpoint": fvg.midpoint,
                })

        for ob in ctx.order_blocks:
            relevant = (direction == "long" and ob.type == "bullish") or \
                       (direction == "short" and ob.type == "bearish")
            if relevant:
                all_levels.append({
                    "type": "order_block",
                    "subtype": ob.type,
                    "tf": tf_label,
                    "timeframe": ob.timeframe,
                    "top": ob.high,
                    "bottom": ob.low,
                    "midpoint": ob.midpoint,
                    "strength": ob.strength,
                })

        # Premium/Discount zone
        if ctx.discount_zone > 0 and direction == "long" and current_price > ctx.discount_zone:
            all_levels.append({
                "type": "discount_zone",
                "subtype": "discount",
                "tf": tf_label,
                "timeframe": ctx.timeframe,
                "top": ctx.equilibrium,
                "bottom": ctx.discount_zone,
                "midpoint": round((ctx.equilibrium + ctx.discount_zone) / 2, 8),
            })
        if ctx.premium_zone > 0 and direction == "short" and current_price < ctx.premium_zone:
            all_levels.append({
                "type": "premium_zone",
                "subtype": "premium",
                "tf": tf_label,
                "timeframe": ctx.timeframe,
                "top": ctx.premium_zone,
                "bottom": ctx.equilibrium,
                "midpoint": round((ctx.premium_zone + ctx.equilibrium) / 2, 8),
            })

    # P3-12: Check same-TF FVG+OB overlaps (high probability zones)
    for ctx, tf_label in [(htf_ctx, "HTF"), (mtf_ctx, "MTF"), (stf_ctx, "STF"), (ltf_ctx, "LTF")]:
        if not ctx:
            continue

        # Check FVG+OB overlap within same timeframe
        for fvg in ctx.fvgs:
            for ob in ctx.order_blocks:
                # Must be same direction
                if fvg.type != ob.type:
                    continue

                # Check overlap
                overlap_top = min(fvg.top, ob.high)
                overlap_bottom = max(fvg.bottom, ob.low)
                if overlap_top > overlap_bottom:
                    tf_weight = get_timeframe_weight(fvg.timeframe)
                    avg_effectiveness = (fvg.effectiveness + ob.effectiveness) / 2.0

                    zones.append({
                        "confluence_top": round(overlap_top, 8),
                        "confluence_bottom": round(overlap_bottom, 8),
                        "confluence_midpoint": round((overlap_top + overlap_bottom) / 2, 8),
                        "sources": [
                            {"type": "fvg", "tf": tf_label, "timeframe": fvg.timeframe, "weight": tf_weight, "effectiveness": fvg.effectiveness},
                            {"type": "order_block", "tf": tf_label, "timeframe": ob.timeframe, "weight": tf_weight, "effectiveness": ob.effectiveness},
                        ],
                        "strength": "high" if tf_label == "HTF" else "medium",  # Same-TF overlap is strong
                        "weighted_strength": round(tf_weight / 2.0, 2),
                        "effectiveness": round(avg_effectiveness, 2),
                    })

    # Find overlapping zones (confluence) across different timeframes
    for i, a in enumerate(all_levels):
        for b in all_levels[i + 1:]:
            if a["tf"] == b["tf"]:
                continue  # P3-12: Skip same-TF (already handled above)
            # Check if zones overlap
            overlap_top = min(a["top"], b["top"])
            overlap_bottom = max(a["bottom"], b["bottom"])
            if overlap_top > overlap_bottom:
                # NEW: Calculate confluence strength using timeframe weights
                tf_weight_a = get_timeframe_weight(a["timeframe"])
                tf_weight_b = get_timeframe_weight(b["timeframe"])
                base_strength = "high" if any(x["tf"] == "HTF" for x in [a, b]) else "medium"

                # NEW: Add effectiveness scores to sources
                sources = [
                    {
                        "type": a["type"],
                        "tf": a["tf"],
                        "timeframe": a["timeframe"],
                        "weight": tf_weight_a,
                        "effectiveness": a.get("effectiveness", 1.0),
                    },
                    {
                        "type": b["type"],
                        "tf": b["tf"],
                        "timeframe": b["timeframe"],
                        "weight": tf_weight_b,
                        "effectiveness": b.get("effectiveness", 1.0),
                    },
                ]

                # NEW: Calculate weighted effectiveness
                total_weight = tf_weight_a + tf_weight_b
                avg_effectiveness = (a.get("effectiveness", 1.0) * tf_weight_a + b.get("effectiveness", 1.0) * tf_weight_b) / total_weight

                zones.append({
                    "confluence_top": round(overlap_top, 8),
                    "confluence_bottom": round(overlap_bottom, 8),
                    "confluence_midpoint": round((overlap_top + overlap_bottom) / 2, 8),
                    "sources": sources,
                    "strength": base_strength,
                    "weighted_strength": round(total_weight / 4.0, 2),  # NEW: normalized weight score
                    "effectiveness": round(avg_effectiveness, 2),  # NEW: weighted effectiveness
                })

    # Also include standalone HTF levels as they're significant on their own
    for level in all_levels:
        if level["tf"] == "HTF":
            already_in_confluence = any(
                z["confluence_bottom"] <= level["midpoint"] <= z["confluence_top"]
                for z in zones
            )
            if not already_in_confluence:
                zones.append({
                    "confluence_top": level["top"],
                    "confluence_bottom": level["bottom"],
                    "confluence_midpoint": level["midpoint"],
                    "sources": [{"type": level["type"], "tf": level["tf"], "timeframe": level["timeframe"]}],
                    "strength": "medium",
                })

    # Sort by proximity to current price
    zones.sort(key=lambda z: abs(float(z["confluence_midpoint"]) - current_price))
    return zones[:5]


def analyze_smc_single_tf(
    ohlcv: list[list],
    timeframe: str,
    current_price: float = 0.0,
    signal_direction: str = "long",
) -> SMCContext:
    """Run full SMC analysis on a single timeframe's OHLCV data.

    NEW: Includes risk score, entry timing score, and timing recommendation.
    """
    structure = detect_market_structure(ohlcv, timeframe)
    fvgs = detect_fvgs(ohlcv, timeframe, current_price)
    obs = detect_order_blocks(ohlcv, timeframe)

    premium_data = calculate_premium_discount(structure.swing_highs, structure.swing_lows)
    if isinstance(premium_data, dict):
        premium = premium_data.get("premium", 0.0)
        discount = premium_data.get("discount", 0.0)
        equilibrium = premium_data.get("equilibrium", 0.0)
    else:
        premium, discount, equilibrium = premium_data

    # NEW: Calculate structure risk score
    risk_score = calculate_structure_risk_score(structure, fvgs, obs, signal_direction, current_price, premium, discount)

    # NEW: Calculate entry timing score
    timing_score, timing_recommendation = calculate_entry_timing_score(current_price, fvgs, obs, premium, discount)

    return SMCContext(
        timeframe=timeframe,
        fvgs=fvgs,
        order_blocks=obs,
        structure=structure,
        premium_zone=premium,
        discount_zone=discount,
        equilibrium=equilibrium,
        risk_score=risk_score,
        entry_timing_score=timing_score,
        timing_recommendation=timing_recommendation,
    )


# ─────────────────────────────────────────────
# Structure risk scoring (P2-7)
# ─────────────────────────────────────────────

def calculate_structure_risk_score(
    structure: MarketStructure | None,
    fvgs: list[FVG],
    obs: list[OrderBlock],
    signal_direction: str,
    current_price: float,
    premium_zone: float,
    discount_zone: float,
) -> float:
    """Calculate structure-based risk score (0-1, higher = more risky).

    Risk factors:
    - Structure trend conflict (+0.4)
    - No FVG/OB support (+0.2)
    - Trading in premium/discount zone against optimal (+0.15)
    - Weak structure break (+0.1)
    """
    risk = 0.0

    # Structure trend conflict (highest risk)
    if structure and structure.trend != "ranging":
        if signal_direction == "long" and structure.trend == "bearish":
            risk += 0.4 if not structure.last_choch else 0.2  # CHoCH reduces conflict risk
        elif signal_direction == "short" and structure.trend == "bullish":
            risk += 0.4 if not structure.last_choch else 0.2

    # No SMC support zones
    has_support = False
    for fvg in fvgs:
        if (fvg.type == "bullish" and signal_direction == "long") or \
           (fvg.type == "bearish" and signal_direction == "short"):
            has_support = True
            break
    if not has_support:
        for ob in obs:
            if (ob.type == "bullish" and signal_direction == "long") or \
               (ob.type == "bearish" and signal_direction == "short"):
                has_support = True
                break

    if not has_support:
        risk += 0.2

    # Premium/Discount zone risk
    if current_price > 0 and premium_zone > 0 and discount_zone > 0:
        if signal_direction == "long" and current_price > premium_zone:
            risk += 0.15  # Buying in premium zone (expensive)
        elif signal_direction == "short" and current_price < discount_zone:
            risk += 0.15  # Selling in discount zone (cheap)

    # Weak structure break (if BOS/CHoCH detected)
    if structure and structure.break_strength < 0.4:
        risk += 0.1

    return round(min(1.0, risk), 2)


# ─────────────────────────────────────────────
# Entry timing scoring (P2-8)
# ─────────────────────────────────────────────

def calculate_entry_timing_score(
    current_price: float,
    fvgs: list[FVG],
    obs: list[OrderBlock],
    premium_zone: float,
    discount_zone: float,
    atr_pct: float = 1.0,
) -> tuple[float, str]:
    """Calculate entry timing quality score (0-1, higher = better timing).

    Returns:
        (timing_score, recommendation_text)

    Excellent (0.9-1.0): Price at FVG/OB zone or discount/premium zone
    Good (0.7-0.9): Price within 1 ATR of optimal zone
    Fair (0.5-0.7): Price within 2 ATR of optimal zone
    Poor (<0.5): Price far from optimal zone, recommend reject or tight stops
    """
    if current_price <= 0:
        return 0.7, "No price data available, default medium timing score"

    # Find nearest relevant FVG/OB
    nearest_zone_distance_pct = 100.0  # Initialize with large value
    zone_type = ""

    for fvg in fvgs:
        distance_to_mid = abs(current_price - fvg.midpoint) / current_price * 100
        if distance_to_mid < nearest_zone_distance_pct:
            nearest_zone_distance_pct = distance_to_mid
            zone_type = fvg.type + " FVG"

    for ob in obs:
        distance_to_mid = abs(current_price - ob.midpoint) / current_price * 100
        if distance_to_mid < nearest_zone_distance_pct:
            nearest_zone_distance_pct = distance_to_mid
            zone_type = ob.type + " OB"

    # Check discount/premium zone alignment
    if discount_zone > 0 and premium_zone > 0:
        # Long signal: check if in discount zone
        if current_price < discount_zone:
            distance_to_discount = abs(current_price - discount_zone) / current_price * 100
            if distance_to_discount < nearest_zone_distance_pct:
                nearest_zone_distance_pct = distance_to_discount
                zone_type = "Discount zone"
        # Short signal: check if in premium zone
        elif current_price > premium_zone:
            distance_to_premium = abs(current_price - premium_zone) / current_price * 100
            if distance_to_premium < nearest_zone_distance_pct:
                nearest_zone_distance_pct = distance_to_premium
                zone_type = "Premium zone"

    # Calculate score based on distance (normalized by ATR)
    distance_atr = nearest_zone_distance_pct / atr_pct if atr_pct > 0 else nearest_zone_distance_pct

    if distance_atr < 0.5:
        return 0.95, f"Excellent entry - price at {zone_type} (within 0.5 ATR), optimal timing for limit or market order"
    elif distance_atr < 1.0:
        return 0.8, f"Good entry - price near {zone_type} (within 1 ATR), good timing for immediate or limit order"
    elif distance_atr < 2.0:
        return 0.6, f"Fair entry - price within 2 ATR of {zone_type}, recommend limit order at target zone"
    elif distance_atr < 3.0:
        return 0.4, f"Poor timing - price {distance_atr:.1f} ATR from {zone_type}, tight stops required or wait for better price"
    else:
        return 0.2, f"Very poor timing - price far from any SMC zone ({distance_atr:.1f} ATR), strong reject recommended"


# ─────────────────────────────────────────────
# HTF structure validation (P0-2)
# ─────────────────────────────────────────────

def check_htf_structure_conflict(
    htf_ctx: SMCContext | None,
    signal_direction: str,
) -> tuple[bool, str, float]:
    """Check if HTF structure conflicts with signal direction.

    Returns:
        (has_conflict, conflict_type, risk_penalty)

    Critical conflicts:
    - HTF bearish + LONG signal without CHoCH = high risk
    - HTF bullish + SHORT signal without CHoCH = high risk

    Args:
        htf_ctx: Higher timeframe SMC context
        signal_direction: Signal direction ("long" or "short")

    Returns:
        Tuple of (conflict_exists, conflict_description, risk_penalty_0-1)
    """
    if not htf_ctx or not htf_ctx.structure:
        return False, "", 0.0

    structure = htf_ctx.structure
    htf_trend = structure.trend
    has_choch = structure.last_choch is not None and structure.last_choch != ""

    # No conflict if ranging or aligned
    if htf_trend == "ranging":
        return False, "HTF ranging - no trend direction conflict", 0.0

    if signal_direction == "long" and htf_trend == "bullish":
        return False, "HTF bullish trend aligned with LONG signal", 0.0

    if signal_direction == "short" and htf_trend == "bearish":
        return False, "HTF bearish trend aligned with SHORT signal", 0.0

    # Check for conflicts
    if signal_direction == "long" and htf_trend == "bearish":
        if has_choch:
            return True, "HTF bearish but CHoCH detected - reversal possibility, moderate risk", 0.25
        else:
            return True, "HTF BEARISH TREND WITHOUT CHoCH - LONG signal trades against trend, HIGH RISK", 0.45

    if signal_direction == "short" and htf_trend == "bullish":
        if has_choch:
            return True, "HTF bullish but CHoCH detected - reversal possibility, moderate risk", 0.25
        else:
            return True, "HTF BULLISH TREND WITHOUT CHoCH - SHORT signal trades against trend, HIGH RISK", 0.45

    return False, "", 0.0


def format_smc_for_ai(mtf_smc: MultiTimeframeSMC, direction: str, current_price: float) -> str:
    """Format SMC analysis into a text block for the AI system prompt.

    NEW: Includes risk scores, timing scores, HTF conflict warnings, and effectiveness.
    """
    lines = ["## Smart Money Concepts (Multi-Timeframe Analysis)"]

    # NEW: HTF structure conflict warning (P0-2)
    if mtf_smc.htf_conflict:
        lines.append("\n### ⚠️ HTF STRUCTURE WARNING")
        lines.append(f"- **{mtf_smc.htf_conflict_type}**")
        lines.append(f"- Overall Risk Score: {mtf_smc.overall_risk_score:.2f} (higher = more risky)")
        if mtf_smc.overall_risk_score >= 0.4:
            lines.append("- **HIGH RISK TRADE** - Consider rejecting or using very tight stops")

    for label, ctx in [("4H (HTF)", mtf_smc.htf), ("1H (MTF)", mtf_smc.mtf), ("30M (STF)", mtf_smc.stf), ("15M (LTF)", mtf_smc.ltf)]:
        if not ctx:
            continue
        lines.append(f"\n### {label}")

        # NEW: Display risk and timing scores
        lines.append(f"- Risk Score: {ctx.risk_score:.2f} (0-1, higher = risky)")
        lines.append(f"- Entry Timing: {ctx.entry_timing_score:.2f} ({ctx.timing_recommendation})")

        if ctx.structure:
            s = ctx.structure
            lines.append(f"- Market Structure: {s.trend.upper()}")
            if s.last_bos:
                lines.append(f"- Last BOS: {s.last_bos} (strength: {s.break_strength:.2f})")
            if s.last_choch:
                lines.append(f"- CHoCH detected: {s.last_choch} (strength: {s.break_strength:.2f})")

        if ctx.equilibrium > 0:
            lines.append(f"- Premium Zone: above {ctx.premium_zone:.2f}")
            lines.append(f"- Equilibrium: {ctx.equilibrium:.2f}")
            lines.append(f"- Discount Zone: below {ctx.discount_zone:.2f}")
            if current_price > ctx.premium_zone:
                lines.append("- Current price is in PREMIUM (expensive for longs)")
            elif current_price < ctx.discount_zone:
                lines.append("- Current price is in DISCOUNT (cheap for longs)")
            else:
                lines.append("- Current price is near EQUILIBRIUM")

        if ctx.fvgs:
            lines.append(f"- Unfilled FVGs ({len(ctx.fvgs)}):")
            for fvg in ctx.fvgs[-3:]:
                eff_text = f"effectiveness: {fvg.effectiveness:.2f}" if fvg.effectiveness < 1.0 else "fresh"
                lines.append(f"  • {fvg.type.upper()} FVG: {fvg.bottom:.2f} - {fvg.top:.2f} (mid: {fvg.midpoint:.2f}, {eff_text})")

        if ctx.order_blocks:
            lines.append(f"- Order Blocks ({len(ctx.order_blocks)}):")
            for ob in ctx.order_blocks[-2:]:
                eff_text = f"effectiveness: {ob.effectiveness:.2f}" if ob.effectiveness < ob.strength else f"strength: {ob.strength:.2f}"
                lines.append(f"  • {ob.type.upper()} OB: {ob.low:.2f} - {ob.high:.2f} ({eff_text})")

    if mtf_smc.confluence_zones:
        lines.append("\n### Confluence Zones (Multi-TF Overlap)")
        for i, zone in enumerate(mtf_smc.confluence_zones[:3], 1):
            sources = " + ".join(f"{s['type']}({s['tf']})" for s in zone["sources"])

            # NEW: Enhanced quality classification (P2-9)
            weighted_strength = zone.get("weighted_strength", 1.0)
            effectiveness = zone.get("effectiveness", 1.0)

            if weighted_strength >= 1.5 and effectiveness >= 0.7:
                quality_text = "⭐ HIGH PROBABILITY (HTF+MTF overlap, fresh zones)"
            elif weighted_strength >= 1.0 and effectiveness >= 0.6:
                quality_text = "✓ GOOD ENTRY (solid confluence)"
            elif zone["strength"] == "high":
                quality_text = "○ FAIR ENTRY (HTF zone, moderate effectiveness)"
            else:
                quality_text = "• ACCEPTABLE ENTRY (LTF confluence, use tight stops)"

            lines.append(
                f"  {i}. {zone['confluence_bottom']:.2f} - {zone['confluence_top']:.2f} "
                f"(mid: {zone['confluence_midpoint']:.2f}, eff: {effectiveness:.2f}) "
                f"— {quality_text} — {sources}"
            )

    return "\n".join(lines)
