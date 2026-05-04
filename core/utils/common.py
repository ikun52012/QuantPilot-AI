"""
QuantPilot AI - Common Utility Functions
Typed utility functions for safer operations.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar('T')


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert value to float with fallback.

    Args:
        value: Any value to convert
        default: Default value if conversion fails

    Returns:
        Float value or default
    """
    if value is None:
        return default
    try:
        result = float(value)
        if not isinstance(result, float) or not math.isfinite(result):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """
    Safely convert value to int with fallback.

    Args:
        value: Any value to convert
        default: Default value if conversion fails

    Returns:
        Integer value or default
    """
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    """
    Safely convert value to bool with fallback.

    Args:
        value: Any value to convert
        default: Default value if conversion fails

    Returns:
        Boolean value or default
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    str_val = str(value).strip().lower()
    if str_val in {'1', 'true', 'yes', 'on', 'enabled'}:
        return True
    if str_val in {'0', 'false', 'no', 'off', 'disabled', ''}:
        return False
    return default


def safe_str(value: Any, default: str = '', max_length: int | None = None) -> str:
    """
    Safely convert value to string with fallback and optional truncation.

    Args:
        value: Any value to convert
        default: Default value if conversion fails
        max_length: Optional maximum length (truncates if exceeded)

    Returns:
        String value or default
    """
    if value is None:
        return default
    try:
        result = str(value).strip()
        if max_length and len(result) > max_length:
            result = result[:max_length]
        return result
    except (TypeError, ValueError):
        return default
    except Exception:
        return default


def safe_dict(value: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Safely convert value to dict with fallback.

    Args:
        value: Any value to convert
        default: Default dict if conversion fails

    Returns:
        Dictionary value or default empty dict
    """
    if default is None:
        default = {}
    if isinstance(value, dict):
        return value
    if value is None or value == '':
        return default
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else default
    except (TypeError, json.JSONDecodeError):
        return default


def safe_list(value: Any, default: list[Any] | None = None) -> list[Any]:
    """
    Safely convert value to list with fallback.

    Args:
        value: Any value to convert
        default: Default list if conversion fails

    Returns:
        List value or default empty list
    """
    if default is None:
        default = []
    if isinstance(value, list):
        return value
    if value is None or value == '':
        return default
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else default
    except (TypeError, json.JSONDecodeError):
        return default


def loads_list(value: Any) -> list[Any]:
    """Backward-compatible JSON list loader used across older modules."""
    return safe_list(value, [])


def loads_dict(value: Any) -> dict[str, Any]:
    """Backward-compatible JSON dict loader used across older modules."""
    return safe_dict(value, {})


def first_valid(*values: Any) -> Any:
    """
    Return first non-None value from arguments.

    Args:
        *values: Values to check

    Returns:
        First valid value or None
    """
    for v in values:
        if v is not None:
            return v
    return None


def clamp(value: float, min_val: float, max_val: float) -> float:
    """
    Clamp a value to a range.

    Args:
        value: Value to clamp
        min_val: Minimum allowed value
        max_val: Maximum allowed value

    Returns:
        Clamped value
    """
    return max(min_val, min(max_val, value))


def normalize_symbol(symbol: str) -> str:
    """
    Normalize trading symbol to standard format.

    Args:
        symbol: Raw symbol string

    Returns:
        Normalized symbol (uppercase, no special chars)
    """
    if not symbol:
        return ''
    normalized = symbol.upper().strip()
    for suffix in ('.P', 'PERP'):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return (
        normalized
        .replace(' ', '')
        .replace('-', '')
        .replace('_', '')
        .replace('/', '')
        .replace(':', '')
        .replace('.', '')
    )


def symbol_key(symbol: str) -> str:
    """
    Get canonical key for symbol matching.

    Args:
        symbol: Symbol string

    Returns:
        Lowercase compact symbol key
    """
    return normalize_symbol(symbol).lower()


def position_symbol_key(symbol: str) -> str:
    """Canonicalize equivalent display/exchange position symbols for matching."""
    normalized = str(symbol or "").upper().strip()
    if not normalized:
        return ""
    for suffix in (".P", "PERP"):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    if ":" in normalized and "/" in normalized:
        left, _, contract = normalized.partition(":")
        base, _, quote = left.partition("/")
        if base and quote and contract == quote:
            return symbol_key(f"{base}{quote}")
    return symbol_key(normalized)


def price_pnl_pct(
    direction: str,
    entry_price: float,
    exit_price: float,
    leverage: float = 1.0
) -> float:
    """
    Calculate leveraged PnL percentage.

    Args:
        direction: Trade direction ('long' or 'short')
        entry_price: Entry price
        exit_price: Exit price
        leverage: Position leverage

    Returns:
        PnL percentage
    """
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    leverage = max(1.0, safe_float(leverage, 1.0))
    if direction.lower() == 'short':
        raw = ((entry_price - exit_price) / entry_price) * 100.0
    else:
        raw = ((exit_price - entry_price) / entry_price) * 100.0
    return raw * leverage


def timeframe_to_minutes(timeframe: Any, default: int = 60) -> int:
    """Normalize TradingView-style timeframe values into minutes."""
    if timeframe is None:
        return default

    text = str(timeframe).strip().lower()
    if not text:
        return default

    text = {
        "week": "1w",
        "weekly": "1w",
        "day": "1d",
        "daily": "1d",
        "hour": "1h",
    }.get(text, text)

    numeric = re.fullmatch(r"(\d+)", text)
    if numeric:
        value = int(numeric.group(1))
        return value if value > 0 else default

    match = re.fullmatch(r"(\d+)([mhdw])", text)
    if not match:
        return default

    value = int(match.group(1))
    unit = match.group(2)
    multiplier = {
        "m": 1,
        "h": 60,
        "d": 1440,
        "w": 10080,
    }[unit]
    minutes = value * multiplier
    return minutes if minutes > 0 else default


def suggested_limit_timeout_secs(timeframe: Any, default: int = 4 * 60 * 60) -> int:
    """Return a reasonable pending-limit timeout based on signal timeframe."""
    minutes = timeframe_to_minutes(timeframe, default=60)
    if minutes <= 15:
        return 2 * 60 * 60
    if minutes <= 30:
        return 4 * 60 * 60
    if minutes <= 60:
        return 8 * 60 * 60
    if minutes <= 4 * 60:
        return 48 * 60 * 60
    if minutes <= 24 * 60:
        return 7 * 24 * 60 * 60
    return max(default, 7 * 24 * 60 * 60)


def normalize_limit_timeout_overrides(data: Any) -> dict[str, int]:
    """Normalize custom pending-limit timeout overrides in seconds."""
    if not isinstance(data, dict):
        return {}

    normalized: dict[str, int] = {}
    for key in ("15m", "30m", "1h", "4h", "1d"):
        value = safe_int(data.get(key), 0)
        if value > 0:
            normalized[key] = value
    return normalized


def resolve_limit_timeout_secs(timeframe: Any, overrides: dict[str, Any] | None = None, default: int = 4 * 60 * 60) -> int:
    """Resolve pending-limit timeout using defaults plus optional overrides."""
    key_minutes = timeframe_to_minutes(timeframe, default=60)
    normalized = normalize_limit_timeout_overrides(overrides)
    if key_minutes <= 15:
        return normalized.get("15m", 2 * 60 * 60)
    if key_minutes <= 30:
        return normalized.get("30m", 4 * 60 * 60)
    if key_minutes <= 60:
        return normalized.get("1h", 8 * 60 * 60)
    if key_minutes <= 4 * 60:
        return normalized.get("4h", 48 * 60 * 60)
    if key_minutes <= 24 * 60:
        return normalized.get("1d", 7 * 24 * 60 * 60)
    return max(default, normalized.get("1d", 7 * 24 * 60 * 60))


def is_valid_email(email: str) -> bool:
    """
    Validate email format.

    Args:
        email: Email string

    Returns:
        True if valid email format
    """
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email.lower()))


def truncate_text(text: str, max_length: int = 100, suffix: str = '...') -> str:
    """
    Truncate text to max length with suffix.

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix to append when truncated

    Returns:
        Truncated text
    """
    if not text or len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Merge two dictionaries, override takes precedence.

    Args:
        base: Base dictionary
        override: Override dictionary

    Returns:
        Merged dictionary
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def chunks(lst: list[T], n: int) -> list[list[T]]:
    """
    Split list into chunks of size n.

    Args:
        lst: List to split
        n: Chunk size

    Returns:
        List of chunks
    """
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def debounce_async(func: Callable[..., Any], delay: float) -> Callable[..., Any]:
    """
    Create async debounced version of function.

    Args:
        func: Function to debounce
        delay: Delay in seconds

    Returns:
        Debounced function
    """
    import asyncio
    task: asyncio.Task | None = None

    async def debounced(*args: Any, **kwargs: Any) -> Any:
        nonlocal task
        if task:
            task.cancel()
        await asyncio.sleep(delay)
        return await func(*args, **kwargs)

    return debounced


def retry_async(
    func: Callable[..., Any],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,)
) -> Callable[..., Any]:
    """
    Create retry wrapper for async function.

    Args:
        func: Function to wrap
        max_attempts: Maximum retry attempts
        delay: Initial delay
        backoff: Backoff multiplier
        exceptions: Exceptions to retry on

    Returns:
        Wrapped function with retry logic
    """
    import asyncio

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        current_delay = delay
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return await func(*args, **kwargs)
            except exceptions as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        if last_exc:
            raise last_exc
        raise RuntimeError("Retry failed without exception")

    return wrapper
