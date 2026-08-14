"""Quantitative indicators for the scanner / AI pipeline.

Round-4 audit additions:
  * Hurst exponent (R/S analysis) — distinguishes trending vs mean-reverting markets
  * Anchored VWAP (swing / session anchor)
  * Candlestick pattern recognition (Engulfing / Pin Bar / Inside Bar / Hammer / Star)
  * Equal Highs / Lows proximity (liquidity stacking detection)
  * ICT Killzone detection
  * Relative strength vs BTC

All functions are pure-Python (no TA-Lib / pandas-ta dependency) and operate
on OHLCV row lists ``[ts, o, h, l, c, v]`` to stay compatible with the rest
of the codebase.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


# ─────────────────────────────────────────────
# Hurst Exponent (Rescaled-Range analysis)
# ─────────────────────────────────────────────
def compute_hurst_exponent(prices: list[float], max_lag: int = 50) -> float | None:
    """Estimate the Hurst exponent via R/S analysis.

    Interpretation:
        H < 0.45  -> mean-reverting (anti-persistent)
        H ~ 0.50  -> random walk
        H > 0.55  -> trending (persistent)

    Returns ``None`` if insufficient data.
    """
    n = len(prices)
    if n < max_lag or max_lag < 10:
        return None

    # Use log returns
    returns: list[float] = []
    for i in range(1, n):
        if prices[i] > 0 and prices[i - 1] > 0:
            returns.append(math.log(prices[i] / prices[i - 1]))
    if len(returns) < max_lag:
        return None

    lags: list[int] = []
    rs_values: list[float] = []

    for lag in (10, 20, 30, 40, 50):
        if lag > len(returns):
            continue
        # Build R/S statistic for this lag, averaged over non-overlapping windows
        num_windows = len(returns) // lag
        if num_windows < 1:
            continue
        rs_list: list[float] = []
        for w in range(num_windows):
            chunk = returns[w * lag : (w + 1) * lag]
            if not chunk:
                continue
            mean = sum(chunk) / len(chunk)
            deviations = [c - mean for c in chunk]
            cumdev = 0.0
            max_dev = -1e18
            min_dev = 1e18
            std = 0.0
            for d in deviations:
                cumdev += d
                if cumdev > max_dev:
                    max_dev = cumdev
                if cumdev < min_dev:
                    min_dev = cumdev
                std += d * d
            std = math.sqrt(std / len(chunk)) if std > 0 else 1e-12
            r = max_dev - min_dev
            if std > 0:
                rs_list.append(r / std)
        if rs_list:
            lags.append(lag)
            rs_values.append(sum(rs_list) / len(rs_list))

    if len(lags) < 2:
        return None

    # Fit log(R/S) = H * log(lag) + c via least squares
    sum_x = sum(math.log(lag) for lag in lags)
    sum_y = sum(math.log(r) for r in rs_values)
    sum_xy = sum(math.log(lags[i]) * math.log(rs_values[i]) for i in range(len(lags)))
    sum_xx = sum(math.log(lag) ** 2 for lag in lags)
    n_pts = len(lags)
    denom = n_pts * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    hurst = (n_pts * sum_xy - sum_x * sum_y) / denom
    # Clamp to sensible range
    return round(max(0.0, min(1.0, hurst)), 4)


# ─────────────────────────────────────────────
# Anchored VWAP
# ─────────────────────────────────────────────
def compute_anchored_vwap(
    ohlcv_rows: list[list[float]],
    anchor_idx: int | None = None,
) -> dict[str, float] | None:
    """Compute VWAP anchored to a swing point.

    If ``anchor_idx`` is None, uses the most recent swing low (for uptrend)
    or the start of the current UTC session as the anchor.

    Returns ``{"vwap": float, "anchor_price": float, "bars_since_anchor": int}``
    or ``None``.
    """
    n = len(ohlcv_rows)
    if n < 5:
        return None

    if anchor_idx is None:
        # Find the most recent swing low in the last 50 bars as the anchor
        lookback = min(50, n - 2)
        anchor_idx = n - lookback
        lowest_low = float("inf")
        for i in range(n - lookback, n - 1):
            if i < 2 or i >= n - 1:
                continue
            low = float(ohlcv_rows[i][3])
            prev_low = float(ohlcv_rows[i - 1][3])
            next_low = float(ohlcv_rows[i + 1][3])
            if low < prev_low and low < next_low and low < lowest_low:
                lowest_low = low
                anchor_idx = i

    if anchor_idx < 0 or anchor_idx >= n - 1:
        return None

    cum_pv = 0.0
    cum_v = 0.0
    for i in range(anchor_idx, n):
        row = ohlcv_rows[i]
        typical = (float(row[1]) + float(row[2]) + float(row[3])) / 3.0
        vol = float(row[5]) if len(row) > 5 else 0.0
        if vol <= 0:
            continue
        cum_pv += typical * vol
        cum_v += vol

    if cum_v <= 0:
        return None

    vwap = cum_pv / cum_v
    anchor_price = float(ohlcv_rows[anchor_idx][3])
    return {
        "vwap": round(vwap, 8),
        "anchor_price": anchor_price,
        "anchor_idx": anchor_idx,
        "bars_since_anchor": n - 1 - anchor_idx,
    }


def compute_anchored_vwap_distance(
    ohlcv_rows: list[list[float]],
    current_price: float,
) -> float | None:
    """Return distance in %% between current price and anchored VWAP."""
    v = compute_anchored_vwap(ohlcv_rows)
    if not v or v["vwap"] <= 0 or current_price <= 0:
        return None
    return round((current_price - v["vwap"]) / v["vwap"] * 100.0, 3)


# ─────────────────────────────────────────────
# Candlestick Pattern Recognition
# ─────────────────────────────────────────────
def detect_candlestick_pattern(recent_rows: list[list[float]]) -> dict[str, Any]:
    """Detect a single most-recent-bar candlestick pattern.

    Input: last 3 OHLCV rows ``[ts, o, h, l, c, v]``.
    Output: ``{"pattern": str|None, "strength": float, "direction": str}``.
    """
    if len(recent_rows) < 2:
        return {"pattern": None, "strength": 0.0, "direction": "neutral"}

    cur = recent_rows[-1]
    prev = recent_rows[-2]
    o, h, low, c = float(cur[1]), float(cur[2]), float(cur[3]), float(cur[4])
    po, ph, pl, pc = float(prev[1]), float(prev[2]), float(prev[3]), float(prev[4])

    body = abs(c - o)
    body_pct = body / max(o, 1e-9) * 100.0
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - low
    total_range = h - low
    if total_range <= 0:
        return {"pattern": None, "strength": 0.0, "direction": "neutral"}

    is_bull = c > o
    is_bear = c < o
    body_ratio = body / total_range

    # Bullish Engulfing
    if is_bull and c > po and o < pc and body > abs(pc - po):
        return {"pattern": "bullish_engulfing", "strength": round(min(1.0, body_pct / 3.0), 2), "direction": "bullish"}
    # Bearish Engulfing
    if is_bear and c < po and o > pc and body > abs(pc - po):
        return {"pattern": "bearish_engulfing", "strength": round(min(1.0, body_pct / 3.0), 2), "direction": "bearish"}

    # Hammer (bullish reversal at lows)
    if lower_wick > body * 2.0 and upper_wick < body * 0.5 and body_ratio < 0.4:
        return {"pattern": "hammer", "strength": round(min(1.0, lower_wick / total_range), 2), "direction": "bullish"}
    # Shooting Star (bearish reversal at highs)
    if upper_wick > body * 2.0 and lower_wick < body * 0.5 and body_ratio < 0.4:
        return {"pattern": "shooting_star", "strength": round(min(1.0, upper_wick / total_range), 2), "direction": "bearish"}

    # Pin Bar (large rejection wick, body small at one extreme)
    if lower_wick > total_range * 0.6 and body_ratio < 0.35:
        return {"pattern": "bullish_pin_bar", "strength": round(lower_wick / total_range, 2), "direction": "bullish"}
    if upper_wick > total_range * 0.6 and body_ratio < 0.35:
        return {"pattern": "bearish_pin_bar", "strength": round(upper_wick / total_range, 2), "direction": "bearish"}

    # Inside Bar (current bar entirely within previous range — consolidation)
    if h <= ph and low >= pl:
        return {"pattern": "inside_bar", "strength": 0.3, "direction": "neutral"}

    # Doji (indecision)
    if body_ratio < 0.1:
        return {"pattern": "doji", "strength": 0.2, "direction": "neutral"}

    # Morning Star (3-bar bullish reversal)
    if len(recent_rows) >= 3:
        prev2 = recent_rows[-3]
        p2c = float(prev2[4])
        p2o = float(prev2[1])
        if p2c < p2o and abs(pc - po) < abs(p2c - p2o) * 0.5 and c > o and c > (p2o + p2c) / 2:
            return {"pattern": "morning_star", "strength": 0.8, "direction": "bullish"}
        # Evening Star
        if p2c > p2o and abs(pc - po) < abs(p2c - p2o) * 0.5 and c < o and c < (p2o + p2c) / 2:
            return {"pattern": "evening_star", "strength": 0.8, "direction": "bearish"}

    return {"pattern": None, "strength": 0.0, "direction": "neutral"}


# ─────────────────────────────────────────────
# Equal Highs / Lows proximity
# ─────────────────────────────────────────────
def detect_equal_highs_lows(
    ohlcv_rows: list[list[float]],
    lookback: int = 60,
    tolerance_pct: float = 0.12,
) -> dict[str, list[dict[str, Any]]]:
    """Detect Equal Highs / Equal Lows — liquidity stacking zones.

    Returns ``{"equal_highs": [...], "equal_lows": [...]}`` where each entry is
    ``{"price": float, "idx": int, "count": int}``.
    """
    n = len(ohlcv_rows)
    if n < 5:
        return {"equal_highs": [], "equal_lows": []}

    start = max(0, n - lookback)
    # Find swing highs / lows
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(start + 2, n - 2):
        h = float(ohlcv_rows[i][2])
        low = float(ohlcv_rows[i][3])
        if h > float(ohlcv_rows[i - 1][2]) and h > float(ohlcv_rows[i + 1][2]) and h > float(ohlcv_rows[i - 2][2]) and h > float(ohlcv_rows[i + 2][2]):
            swing_highs.append((i, h))
        if low < float(ohlcv_rows[i - 1][3]) and low < float(ohlcv_rows[i + 1][3]) and low < float(ohlcv_rows[i - 2][3]) and low < float(ohlcv_rows[i + 2][3]):
            swing_lows.append((i, low))

    # Cluster swings within tolerance
    def _cluster(swings: list[tuple[int, float]]) -> list[dict[str, Any]]:
        clusters: list[dict[str, Any]] = []
        for idx, price in swings:
            placed = False
            for c in clusters:
                if abs(price - c["price"]) / c["price"] * 100.0 < tolerance_pct:
                    c["count"] += 1
                    c["price"] = (c["price"] * (c["count"] - 1) + price) / c["count"]
                    c["idx"] = max(c["idx"], idx)
                    placed = True
                    break
            if not placed:
                clusters.append({"price": price, "idx": idx, "count": 1})
        # Only return clusters with >= 2 hits (equal levels)
        return [c for c in clusters if c["count"] >= 2]

    return {"equal_highs": _cluster(swing_highs), "equal_lows": _cluster(swing_lows)}


def detect_equal_highs_lows_proximity(
    ohlcv_rows: list[list[float]],
    current_price: float,
    lookback: int = 60,
    tolerance_pct:  float = 0.12,
) -> float | None:
    """Return %% distance from current price to the nearest EQH/EQL cluster.

    A small distance (e.g. <0.3%) indicates price is resting at a liquidity
    stack — high stop-hunt probability.
    """
    eq = detect_equal_highs_lows(ohlcv_rows, lookback, tolerance_pct)
    nearest: float | None = None
    for c in eq["equal_highs"] + eq["equal_lows"]:
        if c["price"] <= 0 or current_price <= 0:
            continue
        dist = abs(current_price - c["price"]) / current_price * 100.0
        if nearest is None or dist < nearest:
            nearest = dist
    return round(nearest, 3) if nearest is not None else None


# ─────────────────────────────────────────────
# ICT Killzone detection
# ─────────────────────────────────────────────
def get_killzone(now: datetime | None = None) -> dict[str, Any]:
    """Return the active ICT killzone (if any).

    ICT killzones (UTC):
      * Asian  : 23:00 - 02:00
      * London : 07:00 - 10:00
      * New York: 12:00 - 15:00
      * London Close: 15:00 - 17:00

    These are higher-probability windows for SMC setups.
    """
    if now is None:
        now = datetime.now(UTC)
    hour = now.hour

    zones = [
        (23, 2, "asian_killzone"),
        (7, 10, "london_killzone"),
        (12, 15, "new_york_killzone"),
        (15, 17, "london_close_killzone"),
    ]
    for start, end, name in zones:
        if start < end:
            if start <= hour < end:
                return {"active": True, "name": name, "start_utc": start, "end_utc": end}
        else:
            # wraps midnight
            if hour >= start or hour < end:
                return {"active": True, "name": name, "start_utc": start, "end_utc": end}
    return {"active": False, "name": None, "start_utc": None, "end_utc": None}


# ─────────────────────────────────────────────
# Relative Strength vs BTC
# ─────────────────────────────────────────────
async def compute_relative_strength_btc(
    symbol: str,
    ohlcv_rows: list[list[float]] | None,
    window_bars: int = 24,
) -> float | None:
    """Compute relative strength of ``symbol`` vs BTC over the last ``window_bars``.

    RS > 1.05: outperforming BTC
    RS < 0.95: underperforming BTC
    RS ~ 1.0:  correlated

    Returns ``None`` if BTC data is unavailable.
    """
    if not ohlcv_rows or len(ohlcv_rows) < window_bars + 1:
        return None
    try:
        # Pull BTC OHLCV from the same exchange
        from services.market_scanner import _ohlcv_rows
        from services.unified_ohlcv import fetch_ohlcv_bundle
        bundle = await fetch_ohlcv_bundle("BTCUSDT", timeframes=["1h"])
        btc_rows = _ohlcv_rows(bundle.candles.get("1h", []))
        if not btc_rows or len(btc_rows) < window_bars + 1:
            return None

        sym_start = float(ohlcv_rows[-window_bars][1])
        sym_end = float(ohlcv_rows[-1][4])
        btc_start = float(btc_rows[-window_bars][1])
        btc_end = float(btc_rows[-1][4])
        if sym_start <= 0 or btc_start <= 0:
            return None
        sym_ret = (sym_end - sym_start) / sym_start
        btc_ret = (btc_end - btc_start) / btc_start
        if abs(btc_ret) < 1e-9:
            return None
        return round(sym_ret / btc_ret, 4)
    except Exception:
        return None
