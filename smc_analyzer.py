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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class FVG:
    """A Fair Value Gap (imbalance zone)."""
    type: str           # "bullish" or "bearish"
    top: float          # upper boundary
    bottom: float       # lower boundary
    midpoint: float     # (top + bottom) / 2
    timeframe: str      # e.g. "1h", "4h", "15m"
    candle_index: int   # index in the OHLCV array
    filled: bool = False


@dataclass
class OrderBlock:
    """An Order Block (last opposing candle before impulse)."""
    type: str           # "bullish" or "bearish"
    high: float
    low: float
    midpoint: float
    timeframe: str
    candle_index: int
    strength: float = 0.0  # 0-1 based on impulse magnitude


@dataclass
class StructurePoint:
    """A swing high or swing low."""
    type: str           # "high" or "low"
    price: float
    index: int
    timeframe: str


@dataclass
class MarketStructure:
    """Break of Structure / Change of Character detection."""
    trend: str              # "bullish", "bearish", "ranging"
    last_bos: Optional[str] = None   # "bullish_bos" or "bearish_bos"
    last_choch: Optional[str] = None # "bullish_choch" or "bearish_choch"
    swing_highs: list[StructurePoint] = field(default_factory=list)
    swing_lows: list[StructurePoint] = field(default_factory=list)


@dataclass
class SMCContext:
    """Complete SMC analysis for a single timeframe."""
    timeframe: str
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    structure: Optional[MarketStructure] = None
    premium_zone: float = 0.0   # price above which is "premium"
    discount_zone: float = 0.0  # price below which is "discount"
    equilibrium: float = 0.0    # midpoint


@dataclass
class MultiTimeframeSMC:
    """SMC analysis across multiple timeframes."""
    htf: Optional[SMCContext] = None   # Higher timeframe (4h)
    mtf: Optional[SMCContext] = None   # Medium timeframe (1h)
    ltf: Optional[SMCContext] = None   # Lower timeframe (15m)
    confluence_zones: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────
# Core detection algorithms
# ─────────────────────────────────────────────

def detect_swing_points(
    ohlcv: list[list],
    lookback: int = 3,
    timeframe: str = "1h",
) -> tuple[list[StructurePoint], list[StructurePoint]]:
    """Detect swing highs and swing lows using a simple N-bar pivot method."""
    highs: list[StructurePoint] = []
    lows: list[StructurePoint] = []

    if len(ohlcv) < lookback * 2 + 1:
        return highs, lows

    for i in range(lookback, len(ohlcv) - lookback):
        high_i = ohlcv[i][2]  # High
        low_i = ohlcv[i][3]   # Low

        is_swing_high = all(high_i >= ohlcv[i - j][2] for j in range(1, lookback + 1)) and \
                         all(high_i >= ohlcv[i + j][2] for j in range(1, lookback + 1))
        is_swing_low = all(low_i <= ohlcv[i - j][3] for j in range(1, lookback + 1)) and \
                        all(low_i <= ohlcv[i + j][3] for j in range(1, lookback + 1))

        if is_swing_high:
            highs.append(StructurePoint(type="high", price=high_i, index=i, timeframe=timeframe))
        if is_swing_low:
            lows.append(StructurePoint(type="low", price=low_i, index=i, timeframe=timeframe))

    return highs, lows


def detect_market_structure(
    ohlcv: list[list],
    timeframe: str = "1h",
) -> MarketStructure:
    """Detect BOS (Break of Structure) and CHoCH (Change of Character)."""
    swing_highs, swing_lows = detect_swing_points(ohlcv, lookback=3, timeframe=timeframe)

    structure = MarketStructure(
        trend="ranging",
        swing_highs=swing_highs[-5:],
        swing_lows=swing_lows[-5:],
    )

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return structure

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
    elif lh and ll:
        structure.trend = "bearish"
        structure.last_bos = "bearish_bos"
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
        elif was_bearish and hh:
            structure.last_choch = "bullish_choch"

    return structure


def detect_fvgs(
    ohlcv: list[list],
    timeframe: str = "1h",
    current_price: float = 0.0,
    max_results: int = 5,
) -> list[FVG]:
    """Detect Fair Value Gaps (imbalance zones) in OHLCV data.

    A Bullish FVG: candle[i-1].high < candle[i+1].low  (gap up)
    A Bearish FVG: candle[i-1].low > candle[i+1].high  (gap down)
    """
    fvgs: list[FVG] = []

    if len(ohlcv) < 3:
        return fvgs

    for i in range(1, len(ohlcv) - 1):
        prev_high = ohlcv[i - 1][2]
        prev_low = ohlcv[i - 1][3]
        next_high = ohlcv[i + 1][2]
        next_low = ohlcv[i + 1][3]

        # Bullish FVG: gap between prev candle high and next candle low
        if prev_high < next_low:
            top = next_low
            bottom = prev_high
            filled = current_price <= bottom if current_price > 0 else False
            fvgs.append(FVG(
                type="bullish",
                top=top,
                bottom=bottom,
                midpoint=round((top + bottom) / 2, 8),
                timeframe=timeframe,
                candle_index=i,
                filled=filled,
            ))

        # Bearish FVG: gap between prev candle low and next candle high
        if prev_low > next_high:
            top = prev_low
            bottom = next_high
            filled = current_price >= top if current_price > 0 else False
            fvgs.append(FVG(
                type="bearish",
                top=top,
                bottom=bottom,
                midpoint=round((top + bottom) / 2, 8),
                timeframe=timeframe,
                candle_index=i,
                filled=filled,
            ))

    # Return only unfilled FVGs, most recent first
    unfilled = [f for f in fvgs if not f.filled]
    return unfilled[-max_results:]


def detect_order_blocks(
    ohlcv: list[list],
    timeframe: str = "1h",
    min_impulse_pct: float = 0.5,
    max_results: int = 3,
) -> list[OrderBlock]:
    """Detect Order Blocks — the last opposing candle before a strong impulse move.

    Bullish OB: last bearish candle before a strong bullish move
    Bearish OB: last bullish candle before a strong bearish move
    """
    obs: list[OrderBlock] = []

    if len(ohlcv) < 4:
        return obs

    for i in range(1, len(ohlcv) - 2):
        open_i = ohlcv[i][1]
        close_i = ohlcv[i][4]
        high_i = ohlcv[i][2]
        low_i = ohlcv[i][3]

        is_bearish_candle = close_i < open_i
        is_bullish_candle = close_i > open_i

        # Check the next 2 candles for impulse
        next_close_1 = ohlcv[i + 1][4]
        next_close_2 = ohlcv[i + 2][4]
        next_high = max(ohlcv[i + 1][2], ohlcv[i + 2][2])
        next_low = min(ohlcv[i + 1][3], ohlcv[i + 2][3])

        mid_price = (high_i + low_i) / 2 if (high_i + low_i) > 0 else 1

        # Bullish OB: bearish candle followed by strong bullish impulse
        if is_bearish_candle:
            impulse_pct = (next_high - high_i) / mid_price * 100
            if impulse_pct >= min_impulse_pct:
                strength = min(1.0, impulse_pct / 3.0)
                obs.append(OrderBlock(
                    type="bullish",
                    high=high_i,
                    low=low_i,
                    midpoint=round(mid_price, 8),
                    timeframe=timeframe,
                    candle_index=i,
                    strength=round(strength, 3),
                ))

        # Bearish OB: bullish candle followed by strong bearish impulse
        if is_bullish_candle:
            impulse_pct = (low_i - next_low) / mid_price * 100
            if impulse_pct >= min_impulse_pct:
                strength = min(1.0, impulse_pct / 3.0)
                obs.append(OrderBlock(
                    type="bearish",
                    high=high_i,
                    low=low_i,
                    midpoint=round(mid_price, 8),
                    timeframe=timeframe,
                    candle_index=i,
                    strength=round(strength, 3),
                ))

    return obs[-max_results:]


def calculate_premium_discount(
    swing_highs: list[StructurePoint],
    swing_lows: list[StructurePoint],
) -> tuple[float, float, float]:
    """Calculate Premium/Discount/Equilibrium zones from recent swing range.

    Returns (premium_zone, discount_zone, equilibrium).
    Premium = above 61.8% of range (expensive, good for shorts)
    Discount = below 38.2% of range (cheap, good for longs)
    """
    if not swing_highs or not swing_lows:
        return 0.0, 0.0, 0.0

    range_high = max(sh.price for sh in swing_highs[-3:])
    range_low = min(sl.price for sl in swing_lows[-3:])
    range_size = range_high - range_low

    if range_size <= 0:
        return 0.0, 0.0, 0.0

    equilibrium = range_low + range_size * 0.5
    premium_zone = range_low + range_size * 0.618
    discount_zone = range_low + range_size * 0.382

    return round(premium_zone, 8), round(discount_zone, 8), round(equilibrium, 8)


def find_confluence_zones(
    htf: Optional[SMCContext],
    mtf: Optional[SMCContext],
    ltf: Optional[SMCContext],
    direction: str,
    current_price: float,
) -> list[dict]:
    """Find zones where multiple timeframe SMC levels overlap (confluence).

    These are the highest-probability entry zones.
    """
    zones: list[dict] = []
    all_levels: list[dict] = []

    for ctx, tf_label in [(htf, "HTF"), (mtf, "MTF"), (ltf, "LTF")]:
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

    # Find overlapping zones (confluence)
    for i, a in enumerate(all_levels):
        for b in all_levels[i + 1:]:
            if a["tf"] == b["tf"]:
                continue
            # Check if zones overlap
            overlap_top = min(a["top"], b["top"])
            overlap_bottom = max(a["bottom"], b["bottom"])
            if overlap_top > overlap_bottom:
                zones.append({
                    "confluence_top": round(overlap_top, 8),
                    "confluence_bottom": round(overlap_bottom, 8),
                    "confluence_midpoint": round((overlap_top + overlap_bottom) / 2, 8),
                    "sources": [
                        {"type": a["type"], "tf": a["tf"], "timeframe": a["timeframe"]},
                        {"type": b["type"], "tf": b["tf"], "timeframe": b["timeframe"]},
                    ],
                    "strength": "high" if any(x["tf"] == "HTF" for x in [a, b]) else "medium",
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
    zones.sort(key=lambda z: abs(z["confluence_midpoint"] - current_price))
    return zones[:5]


def analyze_smc_single_tf(
    ohlcv: list[list],
    timeframe: str,
    current_price: float = 0.0,
) -> SMCContext:
    """Run full SMC analysis on a single timeframe's OHLCV data."""
    structure = detect_market_structure(ohlcv, timeframe)
    fvgs = detect_fvgs(ohlcv, timeframe, current_price)
    obs = detect_order_blocks(ohlcv, timeframe)

    premium, discount, equilibrium = calculate_premium_discount(
        structure.swing_highs, structure.swing_lows
    )

    return SMCContext(
        timeframe=timeframe,
        fvgs=fvgs,
        order_blocks=obs,
        structure=structure,
        premium_zone=premium,
        discount_zone=discount,
        equilibrium=equilibrium,
    )


def format_smc_for_ai(mtf_smc: MultiTimeframeSMC, direction: str, current_price: float) -> str:
    """Format SMC analysis into a text block for the AI system prompt."""
    lines = ["## Smart Money Concepts (Multi-Timeframe Analysis)"]

    for label, ctx in [("4H (HTF)", mtf_smc.htf), ("1H (MTF)", mtf_smc.mtf), ("15M (LTF)", mtf_smc.ltf)]:
        if not ctx:
            continue
        lines.append(f"\n### {label}")

        if ctx.structure:
            s = ctx.structure
            lines.append(f"- Market Structure: {s.trend.upper()}")
            if s.last_bos:
                lines.append(f"- Last BOS: {s.last_bos}")
            if s.last_choch:
                lines.append(f"- CHoCH detected: {s.last_choch}")

        if ctx.equilibrium > 0:
            lines.append(f"- Premium Zone: above {ctx.premium_zone:.2f}")
            lines.append(f"- Equilibrium: {ctx.equilibrium:.2f}")
            lines.append(f"- Discount Zone: below {ctx.discount_zone:.2f}")
            if current_price > ctx.premium_zone:
                lines.append(f"- Current price is in PREMIUM (expensive for longs)")
            elif current_price < ctx.discount_zone:
                lines.append(f"- Current price is in DISCOUNT (cheap for longs)")
            else:
                lines.append(f"- Current price is near EQUILIBRIUM")

        if ctx.fvgs:
            lines.append(f"- Unfilled FVGs ({len(ctx.fvgs)}):")
            for fvg in ctx.fvgs[-3:]:
                lines.append(f"  • {fvg.type.upper()} FVG: {fvg.bottom:.2f} - {fvg.top:.2f} (mid: {fvg.midpoint:.2f})")

        if ctx.order_blocks:
            lines.append(f"- Order Blocks ({len(ctx.order_blocks)}):")
            for ob in ctx.order_blocks[-2:]:
                lines.append(f"  • {ob.type.upper()} OB: {ob.low:.2f} - {ob.high:.2f} (strength: {ob.strength:.2f})")

    if mtf_smc.confluence_zones:
        lines.append(f"\n### Confluence Zones (Multi-TF Overlap)")
        for i, zone in enumerate(mtf_smc.confluence_zones[:3], 1):
            sources = " + ".join(f"{s['type']}({s['tf']})" for s in zone["sources"])
            lines.append(
                f"  {i}. {zone['confluence_bottom']:.2f} - {zone['confluence_top']:.2f} "
                f"(mid: {zone['confluence_midpoint']:.2f}) [{zone['strength']}] — {sources}"
            )

    return "\n".join(lines)
