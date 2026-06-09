"""
Decimal utilities for precise monetary calculations.

This module provides Decimal-based utilities to avoid floating-point
precision errors in financial calculations. All monetary values should
use Decimal type for accuracy.

Created: 2026-06-06
Priority: P0-CRITICAL (Funds safety)
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from typing import Any, Union

getcontext().prec = 28


class MoneyAmount:
    """
    Money amount type that enforces Decimal precision.

    Usage:
        amount = MoneyAmount("100.123456")
        amount += MoneyAmount("50.5")
        result = amount.round_to(6)  # Decimal('150.623456')
    """

    def __init__(self, value: str | float | Decimal | int):
        if isinstance(value, Decimal):
            self._value = value
        elif isinstance(value, (int, float, str)):
            self._value = Decimal(str(value))
        else:
            self._value = Decimal(0)

    def __add__(self, other: Union['MoneyAmount', Decimal, float, int]) -> 'MoneyAmount':
        if isinstance(other, MoneyAmount):
            return MoneyAmount(self._value + other._value)
        return MoneyAmount(self._value + Decimal(str(other)))

    def __sub__(self, other: Union['MoneyAmount', Decimal, float, int]) -> 'MoneyAmount':
        if isinstance(other, MoneyAmount):
            return MoneyAmount(self._value - other._value)
        return MoneyAmount(self._value - Decimal(str(other)))

    def __mul__(self, other: Union['MoneyAmount', Decimal, float, int]) -> 'MoneyAmount':
        if isinstance(other, MoneyAmount):
            return MoneyAmount(self._value * other._value)
        return MoneyAmount(self._value * Decimal(str(other)))

    def __truediv__(self, other: Union['MoneyAmount', Decimal, float, int]) -> 'MoneyAmount':
        if isinstance(other, MoneyAmount):
            return MoneyAmount(self._value / other._value)
        return MoneyAmount(self._value / Decimal(str(other)))

    def __lt__(self, other: Union['MoneyAmount', Decimal, float, int]) -> bool:
        if isinstance(other, MoneyAmount):
            return self._value < other._value
        return self._value < Decimal(str(other))

    def __le__(self, other: Union['MoneyAmount', Decimal, float, int]) -> bool:
        if isinstance(other, MoneyAmount):
            return self._value <= other._value
        return self._value <= Decimal(str(other))

    def __gt__(self, other: Union['MoneyAmount', Decimal, float, int]) -> bool:
        if isinstance(other, MoneyAmount):
            return self._value > other._value
        return self._value > Decimal(str(other))

    def __ge__(self, other: Union['MoneyAmount', Decimal, float, int]) -> bool:
        if isinstance(other, MoneyAmount):
            return self._value >= other._value
        return self._value >= Decimal(str(other))

    def __eq__(self, other: Union['MoneyAmount', Decimal, float, int]) -> bool:
        if isinstance(other, MoneyAmount):
            return self._value == other._value
        return self._value == Decimal(str(other))

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return f"MoneyAmount({self._value})"

    @property
    def value(self) -> Decimal:
        return self._value

    def round_to(self, decimals: int = 6) -> Decimal:
        """
        Round to specified decimal places using HALF_UP rounding.

        Args:
            decimals: Number of decimal places (default 6)

        Returns:
            Decimal rounded to specified places
        """
        if decimals < 0:
            raise ValueError("Decimals must be non-negative")
        quantize_str = '0.' + '0' * decimals if decimals > 0 else '0'
        return self._value.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)

    def to_float(self) -> float:
        """Convert to float (WARNING: may lose precision)"""
        return float(self._value)

    def abs(self) -> 'MoneyAmount':
        return MoneyAmount(abs(self._value))


def safe_decimal(
    value: Any,
    default: Decimal = Decimal('0'),
    max_decimals: int = 8
) -> Decimal:
    """
    Safely convert any value to Decimal with precision limit.

    Args:
        value: Value to convert
        default: Default value if conversion fails
        max_decimals: Maximum decimal places (default 8)

    Returns:
        Decimal value

    Examples:
        >>> safe_decimal(100.123456)
        Decimal('100.123456')
        >>> safe_decimal("invalid", Decimal('0'))
        Decimal('0')
    """
    if isinstance(value, Decimal):
        return value

    if value is None:
        return default

    try:
        dec_value = Decimal(str(value))
        if max_decimals >= 0:
            quantize_str = '0.' + '0' * max_decimals if max_decimals > 0 else '0'
            dec_value = dec_value.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)
        return dec_value
    except (InvalidOperation, ValueError, TypeError):
        return default


def decimal_add(a: Any, b: Any, decimals: int = 6) -> Decimal:
    """Add two values as Decimals"""
    return safe_decimal(safe_decimal(a) + safe_decimal(b), max_decimals=decimals)


def decimal_sub(a: Any, b: Any, decimals: int = 6) -> Decimal:
    """Subtract two values as Decimals"""
    return safe_decimal(safe_decimal(a) - safe_decimal(b), max_decimals=decimals)


def decimal_mul(a: Any, b: Any, decimals: int = 6) -> Decimal:
    """Multiply two values as Decimals"""
    return safe_decimal(safe_decimal(a) * safe_decimal(b), max_decimals=decimals)


def decimal_div(a: Any, b: Any, decimals: int = 6) -> Decimal:
    """Divide two values as Decimals"""
    divisor = safe_decimal(b)
    if divisor == Decimal('0'):
        return Decimal('0')
    return safe_decimal(safe_decimal(a) / divisor, max_decimals=decimals)


def is_greater_than(a: Any, b: Any, epsilon: Decimal = Decimal('0.000001')) -> bool:
    """
    Check if a > b with epsilon tolerance for floating-point comparison.

    Args:
        a: First value
        b: Second value
        epsilon: Tolerance for comparison (default 0.000001)

    Returns:
        True if a > b + epsilon
    """
    return safe_decimal(a) > (safe_decimal(b) + epsilon)


def is_less_than(a: Any, b: Any, epsilon: Decimal = Decimal('0.000001')) -> bool:
    """
    Check if a < b with epsilon tolerance.

    Args:
        a: First value
        b: Second value
        epsilon: Tolerance for comparison (default 0.000001)

    Returns:
        True if a < b - epsilon
    """
    return safe_decimal(a) < (safe_decimal(b) - epsilon)


def format_decimal(value: Decimal | float | str, decimals: int = 6) -> str:
    """
    Format Decimal to string with fixed decimal places.

    Args:
        value: Value to format
        decimals: Number of decimal places (default 6)

    Returns:
        Formatted string
    """
    dec_value = safe_decimal(value)
    rounded = dec_value.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP)
    if decimals > 0:
        return f"{rounded:.{decimals}f}"
    return str(int(rounded))
