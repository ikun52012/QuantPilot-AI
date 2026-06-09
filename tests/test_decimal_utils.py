"""
Test Decimal utilities for monetary calculations.

Tests verify that Decimal type prevents floating-point precision errors.
"""

from decimal import Decimal

import pytest

from core.utils.decimal_utils import (
    MoneyAmount,
    decimal_add,
    decimal_div,
    decimal_mul,
    decimal_sub,
    format_decimal,
    is_greater_than,
    is_less_than,
    safe_decimal,
)


class TestMoneyAmount:
    """Test MoneyAmount class for precise calculations."""

    def test_creation_from_string(self):
        amount = MoneyAmount("100.123456")
        assert amount.value == Decimal("100.123456")

    def test_creation_from_float(self):
        amount = MoneyAmount(100.123456)
        assert str(amount.value) == "100.123456"

    def test_creation_from_decimal(self):
        amount = MoneyAmount(Decimal("100.123456"))
        assert amount.value == Decimal("100.123456")

    def test_addition(self):
        a = MoneyAmount("100.123456")
        b = MoneyAmount("50.654321")
        result = a + b
        assert result.value == Decimal("150.777777")

    def test_subtraction(self):
        a = MoneyAmount("100.123456")
        b = MoneyAmount("50.654321")
        result = a - b
        assert result.value == Decimal("49.469135")

    def test_multiplication(self):
        a = MoneyAmount("100.123456")
        b = MoneyAmount("2.5")
        result = a * b
        assert result.value == Decimal("250.30864")

    def test_division(self):
        a = MoneyAmount("100.123456")
        b = MoneyAmount("3")
        result = a / b
        assert abs(result.value - Decimal("33.37448533")) < Decimal("0.00000001")

    def test_comparison(self):
        a = MoneyAmount("100.123456")
        b = MoneyAmount("50.654321")
        c = MoneyAmount("100.123456")

        assert a > b
        assert b < a
        assert a == c
        assert a >= c
        assert a <= c

    def test_round_to(self):
        amount = MoneyAmount("100.123456789")
        rounded = amount.round_to(6)
        assert rounded == Decimal("100.123457")

    def test_abs(self):
        amount = MoneyAmount("-100.123456")
        assert amount.abs().value == Decimal("100.123456")


class TestSafeDecimal:
    """Test safe_decimal conversion."""

    def test_from_float(self):
        result = safe_decimal(100.123456)
        assert result == Decimal("100.123456")

    def test_from_string(self):
        result = safe_decimal("100.123456")
        assert result == Decimal("100.123456")

    def test_from_none(self):
        result = safe_decimal(None, Decimal("0"))
        assert result == Decimal("0")

    def test_from_invalid(self):
        result = safe_decimal("invalid", Decimal("0"))
        assert result == Decimal("0")

    def test_max_decimals(self):
        result = safe_decimal("100.123456789", max_decimals=6)
        assert result == Decimal("100.123457")


class TestDecimalPrecision:
    """Test precision in financial calculations."""

    def test_no_precision_loss(self):
        """Verify Decimal prevents precision loss."""
        a = Decimal("0.1")
        b = Decimal("0.2")
        result = a + b
        assert result == Decimal("0.3")

    def test_float_precision_problem(self):
        """Demonstrate float precision problem."""
        a = 0.1
        b = 0.2
        result = a + b
        assert result != 0.3

    def test_cumulative_precision(self):
        """Test cumulative calculations with Decimal."""
        total = MoneyAmount("0")
        for _ in range(1000):
            total += MoneyAmount("0.1")

        expected = Decimal("100.0")
        assert total.value == expected

    def test_float_cumulative_error(self):
        """Demonstrate cumulative error with float."""
        total = 0.0
        for _ in range(1000):
            total += 0.1

        assert total != 100.0

    def test_large_amount_precision(self):
        """Test large amount calculations."""
        amount = MoneyAmount("1000000.123456")
        result = amount * MoneyAmount("2")
        assert result.value == Decimal("2000000.246912")


class TestDecimalComparisons:
    """Test decimal comparison functions."""

    def test_is_greater_than_true(self):
        assert is_greater_than("100.5", "100.4")

    def test_is_greater_than_false(self):
        assert not is_greater_than("100.4", "100.5")

    def test_is_greater_than_with_epsilon(self):
        assert not is_greater_than("100.000001", "100.0", epsilon=Decimal("0.01"))

    def test_is_less_than_true(self):
        assert is_less_than("100.4", "100.5")

    def test_is_less_than_false(self):
        assert not is_less_than("100.5", "100.4")


class TestDecimalFormatting:
    """Test decimal formatting."""

    def test_format_6_decimals(self):
        result = format_decimal("100.123456", decimals=6)
        assert result == "100.123456"

    def test_format_rounding(self):
        result = format_decimal("100.123456789", decimals=6)
        assert result == "100.123457"

    def test_format_zero_decimals(self):
        result = format_decimal("100.9", decimals=0)
        assert result == "101"


class TestDecimalOperations:
    """Test decimal operation functions."""

    def test_decimal_add(self):
        result = decimal_add("100.123456", "50.654321", decimals=6)
        assert result == Decimal("150.777777")

    def test_decimal_sub(self):
        result = decimal_sub("100.123456", "50.654321", decimals=6)
        assert result == Decimal("49.469135")

    def test_decimal_mul(self):
        result = decimal_mul("100.123456", "2.5", decimals=6)
        assert result == Decimal("250.308640")

    def test_decimal_div(self):
        result = decimal_div("100.123456", "3", decimals=6)
        expected = Decimal("33.37448533")
        assert abs(result - expected) < Decimal("0.000001")

    def test_decimal_div_by_zero(self):
        result = decimal_div("100.123456", "0", decimals=6)
        assert result == Decimal("0")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
