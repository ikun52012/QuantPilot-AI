"""Portfolio Risk — Round-4 audit P0 fix.

Implements:
  * Correlation matrix between open positions (90-day rolling returns)
  * Cluster-based correlation exposure limits (BTC-cluster / L1 / Meme / DeFi)
  * Historical-simulation Value-at-Risk (VaR) and Conditional VaR (CVaR / ES)
  * Pre-trade marginal VaR check: reject new trades that would breach the
    portfolio VaR limit.

All calculations are intentionally lightweight (no numpy / pandas dependency)
so they can run inside the existing codebase. For larger universes the caller
should swap in a vectorised implementation.
"""
from __future__ import annotations

import json
import math
from typing import Any

from loguru import logger

from core.config import DATA_DIR

# ─────────────────────────────────────────────
# Ticker classification (clusters of correlated assets)
# ─────────────────────────────────────────────
_CLUSTERS: dict[str, list[str]] = {
    "btc_cluster": ["BTC", "WBTC", "BCH", "BSV"],
    "eth_cluster": ["ETH", "WETH", "STETH", "ETC"],
    "l1_cluster": ["SOL", "AVAX", "MATIC", "ARB", "OP", "NEAR", "APT", "INJ", "FTM", "ADA", "DOT", "ATOM"],
    "defi_cluster": ["UNI", "AAVE", "COMP", "MKR", "CRV", "SNX", "LDO", "RPL", "PENDLE"],
    "meme_cluster": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME"],
    "ai_cluster": ["FET", "RNDR", "OCEAN", "AGIX", "TAO", "GRT"],
    "stable_cluster": ["USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD"],
}


def _classify_ticker(ticker: str) -> str:
    """Return the cluster name for a ticker (or ``"other"``)."""
    base = (ticker or "").upper().replace("USDT", "").replace("USDC", "").replace("PERP", "").replace("/", "")
    for cluster, members in _CLUSTERS.items():
        if any(base.startswith(m) for m in members):
            return cluster
    return "other"


# ─────────────────────────────────────────────
# Correlation matrix
# ─────────────────────────────────────────────
def pearson_correlation(x: list[float], y: list[float]) -> float:
    """Compute Pearson correlation between two return series."""
    n = min(len(x), len(y))
    if n < 5:
        return 0.0
    x = x[-n:]
    y = y[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    sy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if sx <= 0 or sy <= 0:
        return 0.0
    return cov / (sx * sy)


def cluster_exposure_usdt(
    positions: list[dict[str, Any]],
) -> dict[str, float]:
    """Aggregate notional exposure per cluster.

    Each position dict should expose ``ticker`` and ``notional_usdt`` (or
    ``quantity`` * ``entry_price``).
    """
    exposure: dict[str, float] = {}
    for pos in positions:
        ticker = str(pos.get("ticker", ""))
        notional = float(pos.get("notional_usdt") or
                         (float(pos.get("quantity", 0)) * float(pos.get("entry_price", 0))))
        if notional <= 0:
            continue
        cluster = _classify_ticker(ticker)
        exposure[cluster] = exposure.get(cluster, 0.0) + notional
    return exposure


def check_cluster_concentration(
    positions: list[dict[str, Any]],
    new_ticker: str,
    new_notional_usdt: float,
    equity_usdt: float,
    max_cluster_exposure_pct: float = 30.0,
) -> tuple[bool, str]:
    """Return (allowed, reason). Rejects if the new trade would push a single
    cluster's exposure above ``max_cluster_exposure_pct`` of account equity.

    This prevents opening 5 longs on BTC/ETH/SOL/AVAX/MATIC which would
    effectively be one giant correlated BTC bet.
    """
    if equity_usdt <= 0:
        return False, "Cluster exposure cannot be checked with non-positive equity"
    cluster = _classify_ticker(new_ticker)
    current = cluster_exposure_usdt(positions)
    new_total = current.get(cluster, 0.0) + new_notional_usdt
    pct = new_total / equity_usdt * 100.0
    if pct > max_cluster_exposure_pct:
        return False, (
            f"Cluster '{cluster}' exposure ${new_total:,.0f} would be {pct:.1f}% of equity "
            f"(limit {max_cluster_exposure_pct}%). Rejecting to avoid correlated blowup."
        )
    return True, ""


# ─────────────────────────────────────────────
# Value at Risk (historical simulation)
# ─────────────────────────────────────────────
_VAR_CACHE_FILE = DATA_DIR / "portfolio_var_cache.json"
_VAR_CACHE: dict[str, Any] = {}


def _load_var_cache() -> dict[str, Any]:
    if _VAR_CACHE:
        return _VAR_CACHE
    try:
        if _VAR_CACHE_FILE.exists():
            _VAR_CACHE.update(json.loads(_VAR_CACHE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        pass
    return _VAR_CACHE


def historical_var(
    portfolio_returns: list[float],
    confidence: float = 0.95,
) -> float:
    """Historical-simulation VaR as a fraction (e.g. -0.04 = -4%).

    ``portfolio_returns`` is a list of daily portfolio returns (decimals),
    ideally ≥ 250 days.
    """
    if len(portfolio_returns) < 30:
        # Not enough history — return a conservative 4% default
        return -0.04
    sorted_returns = sorted(portfolio_returns)
    idx = int((1 - confidence) * len(sorted_returns))
    idx = max(0, min(idx, len(sorted_returns) - 1))
    return sorted_returns[idx]


def historical_cvar(
    portfolio_returns: list[float],
    confidence: float = 0.95,
) -> float:
    """Conditional VaR (Expected Shortfall): average of the tail beyond VaR."""
    if len(portfolio_returns) < 30:
        return -0.06
    sorted_returns = sorted(portfolio_returns)
    idx = int((1 - confidence) * len(sorted_returns))
    idx = max(0, min(idx, len(sorted_returns) - 1))
    tail = sorted_returns[: idx + 1]
    if not tail:
        return -0.06
    return sum(tail) / len(tail)


def portfolio_returns_from_positions(
    positions: list[dict[str, Any]],
    historical_prices: dict[str, list[float]],
    weights: list[float] | None = None,
    *,
    normalize_weights: bool = True,
) -> list[float]:
    """Build a portfolio daily-return series from per-asset price series.

    ``historical_prices`` maps ticker -> list of daily closes.
    ``weights`` (optional) maps to each position's notional share. If None,
    uses equal weighting.
    """
    if not positions or not historical_prices:
        return []
    tickers = [p.get("ticker", "") for p in positions]
    series = [historical_prices.get(t, []) for t in tickers]
    if not all(len(s) >= 30 for s in series):
        return []
    n = min(len(s) for s in series)
    if weights is None:
        w = [1.0 / len(positions)] * len(positions)
    elif not normalize_weights:
        # Used by the pre-trade VaR path: each signed weight is notional /
        # equity, so the resulting series is already a return on equity and
        # naturally scales with leverage and gross exposure.
        w = list(weights)
    else:
        total = sum(abs(value) for value in weights) or 1.0
        w = [x / total for x in weights]
    # Compute daily returns per asset
    asset_returns: list[list[float]] = []
    for s in series:
        s = s[-n:]
        r = [(s[i] / s[i - 1] - 1.0) for i in range(1, n) if s[i - 1] > 0]
        asset_returns.append(r)
    if not asset_returns:
        return []
    n_ret = min(len(r) for r in asset_returns)
    portfolio_rets: list[float] = []
    for i in range(n_ret):
        daily = sum(w[j] * asset_returns[j][i] for j in range(len(asset_returns)))
        portfolio_rets.append(daily)
    return portfolio_rets


def check_pre_trade_var(
    positions: list[dict[str, Any]],
    new_ticker: str,
    new_notional_usdt: float,
    equity_usdt: float,
    historical_prices: dict[str, list[float]] | None = None,
    max_var_pct: float = 5.0,
    confidence: float = 0.95,
    new_direction: str = "long",
    fail_closed_on_missing_data: bool = False,
) -> tuple[bool, str]:
    """Pre-trade marginal VaR check.

    Returns (allowed, reason). Rejects if adding the new position would push
    portfolio 95% VaR above ``max_var_pct`` of equity.

    If no historical price data is supplied, falls back to a conservative
    constant cluster-based check.
    """
    if equity_usdt <= 0 or new_notional_usdt <= 0:
        return False, "Portfolio VaR cannot run with non-positive equity or exposure"

    if not historical_prices or len(positions) < 1:
        # Fallback: use cluster concentration as proxy
        return check_cluster_concentration(
            positions, new_ticker, new_notional_usdt, equity_usdt,
            max_cluster_exposure_pct=max_var_pct * 2.0,
        )

    # Build position list including the new trade
    hypothetical = list(positions) + [{
        "ticker": new_ticker,
        "notional_usdt": new_notional_usdt,
        "direction": new_direction,
    }]
    weights = []
    for position in hypothetical:
        notional = abs(float(position.get("notional_usdt", 0) or 0))
        direction = str(position.get("direction") or "long").lower()
        sign = -1.0 if direction in {"short", "sell"} else 1.0
        weights.append(sign * notional / equity_usdt)
    port_rets = portfolio_returns_from_positions(
        hypothetical,
        historical_prices,
        weights,
        normalize_weights=False,
    )
    if not port_rets:
        if fail_closed_on_missing_data:
            return False, "Portfolio VaR historical data is incomplete; live entry blocked"
        return check_cluster_concentration(
            positions,
            new_ticker,
            new_notional_usdt,
            equity_usdt,
            max_cluster_exposure_pct=max_var_pct * 2.0,
        )
    var = historical_var(port_rets, confidence)
    var_usdt = abs(var) * equity_usdt
    var_pct = abs(var) * 100.0
    if var_pct > max_var_pct:
        return False, (
            f"Portfolio {int(confidence*100)}% VaR would be {var_pct:.2f}% "
            f"(${var_usdt:,.0f}) — exceeds limit {max_var_pct}%. Rejecting."
        )
    cvar = historical_cvar(port_rets, confidence)
    logger.info(
        f"[PortfolioRisk] Pre-trade VaR check OK: VaR={var_pct:.2f}%, "
        f"CVaR={abs(cvar)*100:.2f}%, limit={max_var_pct}%"
    )
    return True, ""


async def fetch_historical_prices_for_positions(
    positions: list[dict[str, Any]],
    days: int = 90,
) -> dict[str, list[float]]:
    """Fetch daily closes for each position's ticker (best-effort, cached).

    Uses the existing ``unified_ohlcv`` fetcher. Returns a dict
    ``{ticker: [close_day_1, close_day_2, ...]}``.
    """
    out: dict[str, list[float]] = {}
    for pos in positions:
        ticker = str(pos.get("ticker", ""))
        if not ticker:
            continue
        try:
            from services.unified_ohlcv import fetch_ohlcv_bundle
            bundle = await fetch_ohlcv_bundle(ticker, timeframes=["1d"], limit=days + 5)
            candles = bundle.candles.get("1d", [])
            if candles:
                out[ticker] = [float(candle.close) for candle in candles[-days:]]
        except Exception as e:
            logger.debug(f"[PortfolioRisk] Failed to fetch prices for {ticker}: {e}")
    return out
