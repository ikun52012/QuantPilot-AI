"""
QuantPilot AI - Common Utility Functions
Shared utilities to reduce code duplication across modules.
"""
import json
from typing import Any

from loguru import logger


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    """Safely convert value to bool, returning default on failure."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def loads_list(value: Any) -> list:
    """Safely load value as list."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def loads_dict(value: Any) -> dict:
    """Safely load value as dict."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def normalize_symbol(symbol: str) -> str:
    """Normalize symbol to exchange format (BTCUSDT -> BTC/USDT)."""
    symbol = str(symbol or "").upper().replace(" ", "").replace("-", "").replace("_", "").replace("/", "")
    for quote in ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"]:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            base = symbol[:-len(quote)]
            return f"{base}/{quote}"
    return symbol


def symbol_key(symbol: str) -> str:
    """Convert symbol to compact key for matching (removes /, :, -, _)."""
    return str(symbol or "").upper().replace("/", "").replace(":", "").replace("-", "").replace("_", "")


def price_pnl_pct(direction: str, entry_price: float, exit_price: float, leverage: float = 1.0) -> float:
    """Calculate PnL percentage for a position."""
    entry_price = safe_float(entry_price)
    exit_price = safe_float(exit_price)
    leverage = max(1.0, safe_float(leverage, 1.0))

    if entry_price <= 0 or exit_price <= 0:
        return 0.0

    if str(direction).lower() == "short":
        raw = ((entry_price - exit_price) / entry_price) * 100.0
    else:
        raw = ((exit_price - entry_price) / entry_price) * 100.0

    return raw * leverage