"""
Datetime utilities for PostgreSQL compatibility.
PostgreSQL TIMESTAMP WITHOUT TIME ZONE requires naive datetime (no timezone info).
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    Get current UTC time as naive datetime (without timezone info).
    
    PostgreSQL TIMESTAMP WITHOUT TIME ZONE column requires naive datetime objects.
    Using datetime.now(timezone.utc) creates a timezone-aware datetime which
    causes compatibility issues with PostgreSQL.
    
    This function returns the UTC time but strips timezone info for DB compatibility.
    
    Returns:
        datetime: Current UTC time as naive datetime object
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso() -> str:
    """
    Get current UTC time as ISO format string.
    
    Returns:
        str: ISO formatted UTC datetime string
    """
    return datetime.now(timezone.utc).isoformat()


def utcnow_str(fmt: str = "%Y-%m-%d") -> str:
    """
    Get current UTC time as formatted string.
    
    Args:
        fmt: Format string (default: "%Y-%m-%d")
    
    Returns:
        str: Formatted UTC datetime string
    """
    return datetime.now(timezone.utc).strftime(fmt)


def make_naive(dt: datetime) -> datetime:
    """
    Convert datetime to UTC and strip timezone info.
    
    Args:
        dt: datetime object (may be timezone-aware or naive)
    
    Returns:
        datetime: Naive datetime object
    """
    return to_utc(dt)


def to_utc(dt: datetime) -> datetime:
    """
    Convert datetime to UTC and strip timezone info.
    
    Args:
        dt: datetime object
    
    Returns:
        datetime: UTC naive datetime object
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_datetime_utc_naive(value) -> datetime:
    """
    Parse a datetime or ISO timestamp and return a UTC naive datetime.

    TradingView and browsers often send ISO strings with Z or an explicit
    offset. Database DateTime columns are intentionally stored as naive UTC,
    so every parsed value is normalized before it touches SQLAlchemy.
    """
    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty datetime")
    return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
