"""
QuantPilot AI - Liquidity Analyzer

Identifies liquidity pools, sweep zones, and vacuum areas for better trade decisions:
  - Liquidity Pools: Areas where large orders cluster (support/resistance)
  - Liquidity Sweeps: Price movements that clear out trapped positions
  - Vacuum Zones: Low liquidity areas where price moves quickly
  - Depth Analysis: Order book strength at key levels

These concepts help identify:
  - Where price is likely to reverse (liquidity pools)
  - Where price is likely to sweep through (trapped positions)
  - False breakout targets (liquidity grabs)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LiquidityPool:
    """A cluster of liquidity at a price level."""
    price: float
    volume: float          # Total volume at this level
    type: str              # "bid" or "ask"
    strength: float        # 0-1, relative to average depth
    distance_pct: float    # Distance from current price (%)
    is_major: bool = False # True if strength > 0.8


@dataclass
class SweepZone:
    """A zone where price swept through liquidity."""
    start_price: float
    end_price: float
    direction: str         # "up" or "down"
    volume_swept: float    # Volume cleared in sweep
    timestamp: float       # Unix timestamp
    was_false_breakout: bool = False  # True if price reversed after sweep


@dataclass
class VacuumZone:
    """A low liquidity area where price moves quickly."""
    start_price: float
    end_price: float
    avg_volume: float      # Average volume in zone (low = vacuum)
    direction: str         # Expected fast movement direction
    probability: float     # 0-1, probability of fast move


@dataclass
class LiquidityAnalysis:
    """Complete liquidity analysis for a ticker."""
    ticker: str
    current_price: float
    pools: list[LiquidityPool] = field(default_factory=list)
    sweeps: list[SweepZone] = field(default_factory=list)
    vacuums: list[VacuumZone] = field(default_factory=list)
    bid_strength: float = 0.0   # 0-1, overall bid liquidity
    ask_strength: float = 0.0   # 0-1, overall ask liquidity
    imbalance_ratio: float = 0.0  # bid/ask ratio
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    sweep_probability: float = 0.0  # Probability of liquidity sweep


def analyze_liquidity(
    ticker: str,
    current_price: float,
    orderbook: dict | None = None,
    recent_trades: list[dict] | None = None,
    ohlcv: list[dict] | None = None,
) -> LiquidityAnalysis:
    """
    Analyze liquidity structure for a ticker.

    Args:
        ticker: The ticker symbol
        current_price: Current market price
        orderbook: Order book data with bids/asks
        recent_trades: Recent trade data for sweep detection
        ohlcv: OHLCV data for support/resistance

    Returns:
        LiquidityAnalysis with pools, sweeps, vacuums, and strength metrics
    """
    analysis = LiquidityAnalysis(
        ticker=ticker,
        current_price=current_price,
    )

    if not orderbook:
        return analysis

    # Analyze order book for liquidity pools
    analysis.pools = _detect_liquidity_pools(
        orderbook, current_price, ticker
    )

    # Calculate overall bid/ask strength
    analysis.bid_strength, analysis.ask_strength = _calculate_depth_strength(
        orderbook, current_price
    )

    # Calculate imbalance ratio
    total_bid = sum(float(b.get("amount", 0) or 0) for b in (orderbook.get("bids") or []))
    total_ask = sum(float(a.get("amount", 0) or 0) for a in (orderbook.get("asks") or []))
    if total_ask > 0:
        analysis.imbalance_ratio = total_bid / total_ask
    elif total_bid > 0:
        analysis.imbalance_ratio = 100.0  # Heavy bid dominance

    # Find nearest support/resistance
    analysis.nearest_support = _find_nearest_pool(
        analysis.pools, current_price, "bid"
    )
    analysis.nearest_resistance = _find_nearest_pool(
        analysis.pools, current_price, "ask"
    )

    # Detect vacuum zones (low liquidity areas)
    if orderbook.get("bids") and orderbook.get("asks"):
        analysis.vacuums = _detect_vacuum_zones(
            orderbook, current_price
        )

    # Detect recent sweeps if trade data available
    if recent_trades:
        analysis.sweeps = _detect_recent_sweeps(
            recent_trades, current_price, ticker
        )

    # Calculate sweep probability
    analysis.sweep_probability = _calculate_sweep_probability(
        analysis, current_price
    )

    return analysis


def _detect_liquidity_pools(
    orderbook: dict,
    current_price: float,
    ticker: str,
) -> list[LiquidityPool]:
    """Detect significant liquidity pools in order book."""
    pools = []

    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []

    if not bids and not asks:
        return pools

    # Calculate average volume for strength comparison
    all_volumes = [
        float(b.get("amount", 0) or 0) for b in bids
    ] + [
        float(a.get("amount", 0) or 0) for a in asks
    ]
    avg_volume = sum(all_volumes) / len(all_volumes) if all_volumes else 0

    # Detect bid pools (support)
    for bid in bids[:50]:  # Top 50 bids
        price = float(bid.get("price", 0) or 0)
        volume = float(bid.get("amount", 0) or 0)
        if price <= 0 or volume <= 0:
            continue

        strength = volume / avg_volume if avg_volume > 0 else 0
        if strength < 0.5:  # Only significant pools
            continue

        distance_pct = (current_price - price) / current_price * 100

        pools.append(LiquidityPool(
            price=price,
            volume=volume,
            type="bid",
            strength=min(1.0, strength),
            distance_pct=distance_pct,
            is_major=strength > 0.8,
        ))

    # Detect ask pools (resistance)
    for ask in asks[:50]:  # Top 50 asks
        price = float(ask.get("price", 0) or 0)
        volume = float(ask.get("amount", 0) or 0)
        if price <= 0 or volume <= 0:
            continue

        strength = volume / avg_volume if avg_volume > 0 else 0
        if strength < 0.5:
            continue

        distance_pct = (price - current_price) / current_price * 100

        pools.append(LiquidityPool(
            price=price,
            volume=volume,
            type="ask",
            strength=min(1.0, strength),
            distance_pct=distance_pct,
            is_major=strength > 0.8,
        ))

    # Sort by strength (strongest first)
    pools.sort(key=lambda p: p.strength, reverse=True)

    return pools


def _calculate_depth_strength(
    orderbook: dict,
    current_price: float,
) -> tuple[float, float]:
    """Calculate overall bid and ask depth strength."""
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []

    # Sum volume within 2% of current price
    bid_volume = 0.0
    for bid in bids[:20]:
        price = float(bid.get("price", 0) or 0)
        if price <= 0:
            continue
        distance_pct = (current_price - price) / current_price * 100
        if distance_pct <= 2.0:  # Within 2%
            bid_volume += float(bid.get("amount", 0) or 0)

    ask_volume = 0.0
    for ask in asks[:20]:
        price = float(ask.get("price", 0) or 0)
        if price <= 0:
            continue
        distance_pct = (price - current_price) / current_price * 100
        if distance_pct <= 2.0:
            ask_volume += float(ask.get("amount", 0) or 0)

    # Normalize to 0-1 (relative to typical depth)
    # Typical depth varies by ticker, use relative scale
    total_volume = bid_volume + ask_volume
    if total_volume <= 0:
        return (0.0, 0.0)

    bid_strength = bid_volume / total_volume
    ask_strength = ask_volume / total_volume

    return (bid_strength, ask_strength)


def _find_nearest_pool(
    pools: list[LiquidityPool],
    current_price: float,
    pool_type: str,
) -> float | None:
    """Find nearest significant liquidity pool of given type."""
    matching_pools = [p for p in pools if p.type == pool_type and p.strength >= 0.3]

    if not matching_pools:
        return None

    # Find closest by distance
    matching_pools.sort(key=lambda p: abs(p.distance_pct))

    return matching_pools[0].price


def _detect_vacuum_zones(
    orderbook: dict,
    current_price: float,
) -> list[VacuumZone]:
    """Detect low liquidity zones where price could move quickly."""
    vacuums = []

    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []

    if not bids or not asks:
        return vacuums

    # Check for gaps in order book
    # A vacuum zone is where there's a significant gap between price levels

    # Check bid side (downward vacuum)
    prev_price = current_price
    for bid in bids[:30]:
        price = float(bid.get("price", 0) or 0)
        if price <= 0:
            continue

        gap_pct = (prev_price - price) / prev_price * 100
        if gap_pct > 0.5:  # Gap > 0.5%
            volume = float(bid.get("amount", 0) or 0)
            avg_volume = volume / gap_pct if gap_pct > 0 else 0

            # Low average volume = vacuum
            if avg_volume < 10:  # Threshold varies by market
                vacuums.append(VacuumZone(
                    start_price=prev_price,
                    end_price=price,
                    avg_volume=avg_volume,
                    direction="down",
                    probability=min(1.0, gap_pct / 2.0),
                ))

        prev_price = price

    # Check ask side (upward vacuum)
    prev_price = current_price
    for ask in asks[:30]:
        price = float(ask.get("price", 0) or 0)
        if price <= 0:
            continue

        gap_pct = (price - prev_price) / prev_price * 100
        if gap_pct > 0.5:
            volume = float(ask.get("amount", 0) or 0)
            avg_volume = volume / gap_pct if gap_pct > 0 else 0

            if avg_volume < 10:
                vacuums.append(VacuumZone(
                    start_price=prev_price,
                    end_price=price,
                    avg_volume=avg_volume,
                    direction="up",
                    probability=min(1.0, gap_pct / 2.0),
                ))

        prev_price = price

    return vacuums


def _detect_recent_sweeps(
    recent_trades: list[dict],
    current_price: float,
    ticker: str,
) -> list[SweepZone]:
    """Detect recent liquidity sweeps from trade data."""
    sweeps = []

    if not recent_trades or len(recent_trades) < 10:
        return sweeps

    # Group trades by price direction
    # A sweep is a rapid movement through a price range with high volume

    trades_by_direction = {"up": [], "down": []}
    prev_price = current_price

    for trade in recent_trades[:50]:
        price = float(trade.get("price", 0) or 0)
        if price <= 0:
            continue

        if price > prev_price:
            trades_by_direction["up"].append(trade)
        elif price < prev_price:
            trades_by_direction["down"].append(trade)

        prev_price = price

    # Detect sweeps (large volume in short time in one direction)
    for direction, trades in trades_by_direction.items():
        if len(trades) < 5:
            continue

        total_volume = sum(float(t.get("amount", 0) or 0) for t in trades)
        if total_volume <= 0:
            continue

        prices = [float(t.get("price", 0) or 0) for t in trades]
        start_price = min(prices) if direction == "up" else max(prices)
        end_price = max(prices) if direction == "up" else min(prices)

        # Check if this was a false breakout (reversed after sweep)
        last_trade_time = max(float(t.get("timestamp", 0) or 0) for t in trades)
        was_false_breakout = False

        # Simple heuristic: if price reversed > 50% of sweep range, it's false breakout
        sweep_range = abs(end_price - start_price)
        if sweep_range > 0:
            reversal = abs(current_price - end_price)
            if reversal > sweep_range * 0.5:
                was_false_breakout = True

        sweeps.append(SweepZone(
            start_price=start_price,
            end_price=end_price,
            direction=direction,
            volume_swept=total_volume,
            timestamp=last_trade_time,
            was_false_breakout=was_false_breakout,
        ))

    return sweeps


def _calculate_sweep_probability(
    analysis: LiquidityAnalysis,
    current_price: float,
) -> float:
    """Calculate probability of upcoming liquidity sweep."""
    probability = 0.0

    # Factors that increase sweep probability:

    # 1. Major imbalance (one side much stronger)
    imbalance = abs(analysis.bid_strength - analysis.ask_strength)
    if imbalance > 0.3:
        probability += 0.2

    # 2. Vacuum zones nearby
    nearby_vacuums = [v for v in analysis.vacuums if abs(v.start_price - current_price) / current_price * 100 < 1.0]
    if nearby_vacuums:
        probability += 0.3

    # 3. Recent false breakouts (indicating trapped positions)
    false_breakouts = [s for s in analysis.sweeps if s.was_false_breakout]
    if false_breakouts:
        probability += 0.2

    # 4. Major liquidity pools nearby (targets for sweep)
    nearby_pools = [p for p in analysis.pools if abs(p.distance_pct) < 2.0 and p.is_major]
    if nearby_pools:
        probability += 0.2

    # 5. Extreme imbalance ratio
    if analysis.imbalance_ratio > 3.0 or analysis.imbalance_ratio < 0.33:
        probability += 0.1

    return min(1.0, probability)


def format_liquidity_for_ai(
    analysis: LiquidityAnalysis,
    direction: str,
    current_price: float,
) -> str:
    """Format liquidity analysis for AI prompt."""
    lines = [
        "## Liquidity Analysis",
        f"- Ticker: {analysis.ticker}",
        f"- Current Price: {current_price}",
        f"- Bid Strength: {analysis.bid_strength:.2f}",
        f"- Ask Strength: {analysis.ask_strength:.2f}",
        f"- Imbalance Ratio: {analysis.imbalance_ratio:.2f}",
        f"- Sweep Probability: {analysis.sweep_probability:.2f}",
    ]

    if analysis.nearest_support:
        lines.append(f"- Nearest Support (Bid Pool): {analysis.nearest_support}")

    if analysis.nearest_resistance:
        lines.append(f"- Nearest Resistance (Ask Pool): {analysis.nearest_resistance}")

    # Major pools
    major_pools = [p for p in analysis.pools if p.is_major]
    if major_pools:
        lines.append(f"- Major Liquidity Pools: {len(major_pools)}")
        for pool in major_pools[:5]:
            lines.append(
                f"  - {pool.type.upper()} @ {pool.price:.2f} "
                f"(strength={pool.strength:.2f}, dist={pool.distance_pct:.2f}%)"
            )

    # Vacuum zones
    if analysis.vacuums:
        lines.append(f"- Vacuum Zones (Fast Move Areas): {len(analysis.vacuums)}")
        for vacuum in analysis.vacuums[:3]:
            lines.append(
                f"  - {vacuum.direction.upper()} {vacuum.start_price:.2f}-{vacuum.end_price:.2f} "
                f"(prob={vacuum.probability:.2f})"
            )

    # Recent sweeps
    if analysis.sweeps:
        lines.append(f"- Recent Liquidity Sweeps: {len(analysis.sweeps)}")
        for sweep in analysis.sweeps[:3]:
            fb_marker = " [FALSE BREAKOUT]" if sweep.was_false_breakout else ""
            lines.append(
                f"  - {sweep.direction.upper()} {sweep.start_price:.2f}->{sweep.end_price:.2f}"
                f"{fb_marker}"
            )

    # Trading implications
    lines.append("")
    lines.append("### Trading Implications:")

    if direction.lower() == "long":
        if analysis.ask_strength > 0.7:
            lines.append("- HIGH ask resistance - consider waiting for sweep")
        if analysis.nearest_resistance:
            lines.append(f"- Target resistance at {analysis.nearest_resistance}")
        if analysis.sweep_probability > 0.5:
            lines.append("- High sweep probability - watch for false breakout")
    else:
        if analysis.bid_strength > 0.7:
            lines.append("- HIGH bid support - consider waiting for sweep")
        if analysis.nearest_support:
            lines.append(f"- Target support at {analysis.nearest_support}")
        if analysis.sweep_probability > 0.5:
            lines.append("- High sweep probability - watch for false breakout")

    return "\n".join(lines)
