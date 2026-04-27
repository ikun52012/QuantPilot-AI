"""
QuantPilot AI - Edge Cases and Boundary Condition Tests
Tests for edge cases, boundary conditions, and error handling.
"""
import pytest
import pytest_asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    TradingViewSignal, SignalDirection, MarketContext,
    AIAnalysis, TradeDecision, TrailingStopMode,
    TakeProfitLevel, TakeProfitConfig, TrailingStopConfig,
)
from pre_filter import run_pre_filter_async, FilterThresholds, calculate_filter_score
from services.signal_processor import SignalProcessor
from exchange import _normalize_symbol, _symbol_candidates, _valid_stop_loss, _valid_take_profit
from smc_analyzer import (
    detect_fvgs, detect_order_blocks, detect_market_structure,
    calculate_premium_discount, FVG, OrderBlock,
)
from ai_analyzer import _parse_response, _fallback_analysis, _price_to_bucket
from core.security import (
    hash_password, verify_password, validate_password_strength,
    encrypt_value, decrypt_value, generate_token, is_placeholder_webhook_secret,
)
from core.utils.common import safe_float, safe_bool, safe_int, safe_str
class TestTradingViewSignalValidation:
    """Test signal validation edge cases."""

    def test_empty_ticker_rejected(self):
        """Empty ticker should be rejected."""
        with pytest.raises(ValueError):
            TradingViewSignal(
                secret="test",
                ticker="",
                direction=SignalDirection.LONG,
                price=100.0,
            )

    def test_ticker_with_special_chars_rejected(self):
        """Ticker with unsupported characters should be rejected."""
        with pytest.raises(ValueError):
            TradingViewSignal(
                secret="test",
                ticker="BTC#USDT",
                direction=SignalDirection.LONG,
                price=100.0,
            )

    def test_ticker_normalization(self):
        """Ticker should be normalized to uppercase."""
        signal = TradingViewSignal(
            secret="test",
            ticker="btcusdt",
            direction=SignalDirection.LONG,
            price=100.0,
        )
        assert signal.ticker == "BTCUSDT"

    def test_negative_price_rejected(self):
        """Negative price should be rejected."""
        with pytest.raises(ValueError):
            TradingViewSignal(
                secret="test",
                ticker="BTCUSDT",
                direction=SignalDirection.LONG,
                price=-100.0,
            )

    def test_zero_price_rejected(self):
        """Zero price should be rejected."""
        with pytest.raises(ValueError):
            TradingViewSignal(
                secret="test",
                ticker="BTCUSDT",
                direction=SignalDirection.LONG,
                price=0.0,
            )

    def test_extreme_price_accepted(self):
        """Extreme but valid prices should be accepted."""
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            direction=SignalDirection.LONG,
            price=1e-8,
        )
        assert signal.price == 1e-8

        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            direction=SignalDirection.LONG,
            price=1e12,
        )
        assert signal.price == 1e12


class TestSymbolNormalization:
    """Test symbol normalization edge cases."""

    def test_empty_symbol(self):
        """Empty symbol handling."""
        result = _normalize_symbol("")
        assert result == ""

    def test_already_normalized(self):
        """Already normalized symbols."""
        assert _normalize_symbol("BTC/USDT") == "BTC/USDT"

    def test_with_dash(self):
        """Symbol with dash."""
        assert _normalize_symbol("BTC-USDT") == "BTCUSDT"

    def test_lowercase(self):
        """Lowercase conversion."""
        assert _normalize_symbol("btcusdt") == "BTCUSDT"

    def test_with_spaces(self):
        """Symbol with spaces."""
        assert _normalize_symbol("BTC USDT") == "BTCUSDT"

    def test_unknown_quote(self):
        """Unknown quote currency."""
        assert _normalize_symbol("BTCXYZ") == "BTCXYZUSDT"


class TestStopLossTakeProfitValidation:
    """Test stop-loss and take-profit validation edge cases."""

    def test_long_stop_loss_below_entry(self):
        """Long stop-loss must be below entry."""
        assert _valid_stop_loss(SignalDirection.LONG, 100.0, 90.0) == 90.0

    def test_long_stop_loss_above_entry_rejected(self):
        """Long stop-loss above entry is invalid."""
        assert _valid_stop_loss(SignalDirection.LONG, 100.0, 110.0) is None

    def test_short_stop_loss_above_entry(self):
        """Short stop-loss must be above entry."""
        assert _valid_stop_loss(SignalDirection.SHORT, 100.0, 110.0) == 110.0

    def test_short_stop_loss_below_entry_rejected(self):
        """Short stop-loss below entry is invalid."""
        assert _valid_stop_loss(SignalDirection.SHORT, 100.0, 90.0) is None

    def test_zero_stop_loss_rejected(self):
        """Zero stop-loss is invalid."""
        assert _valid_stop_loss(SignalDirection.LONG, 100.0, 0.0) is None

    def test_negative_entry_rejected(self):
        """Negative entry price is invalid."""
        assert _valid_stop_loss(SignalDirection.LONG, -100.0, 90.0) is None

    def test_long_take_profit_above_entry(self):
        """Long take-profit must be above entry."""
        assert _valid_take_profit(SignalDirection.LONG, 100.0, 110.0) == 110.0

    def test_long_take_profit_below_entry_rejected(self):
        """Long take-profit below entry is invalid."""
        assert _valid_take_profit(SignalDirection.LONG, 100.0, 90.0) is None

    def test_short_take_profit_below_entry(self):
        """Short take-profit must be below entry."""
        assert _valid_take_profit(SignalDirection.SHORT, 100.0, 90.0) == 90.0

    def test_short_take_profit_above_entry_rejected(self):
        """Short take-profit above entry is invalid."""
        assert _valid_take_profit(SignalDirection.SHORT, 100.0, 110.0) is None


class TestMarketContext:
    """Test market context edge cases."""

    def test_empty_market_context(self):
        """Empty market context should be valid."""
        ctx = MarketContext(ticker="BTCUSDT")
        assert ctx.ticker == "BTCUSDT"
        assert ctx.current_price == 0.0

    def test_extreme_price_change(self):
        """Extreme price changes."""
        ctx = MarketContext(
            ticker="BTCUSDT",
            current_price=100.0,
            price_change_1h=999.9,
            price_change_24h=-999.9,
        )
        assert ctx.price_change_1h == 999.9

    def test_nan_values(self):
        """NaN values handling."""
        ctx = MarketContext(
            ticker="BTCUSDT",
            current_price=100.0,
            rsi_1h=float('nan'),
        )
        assert ctx.rsi_1h is None or math.isnan(ctx.rsi_1h)


class TestAIAnalysisParsing:
    """Test AI response parsing edge cases."""

    def test_empty_response(self):
        """Empty response parsing."""
        result = _parse_response("")
        assert result.recommendation == "hold"

    def test_invalid_json(self):
        """Invalid JSON parsing."""
        result = _parse_response("not json")
        assert result.recommendation == "hold"

    def test_markdown_code_block(self):
        """Markdown code block parsing."""
        response = "```json\n{\"confidence\":0.8,\"recommendation\":\"execute\"}\n```"
        result = _parse_response(response)
        assert result.confidence == 0.8

    def test_missing_required_fields(self):
        """Missing required fields."""
        result = _parse_response("{\"confidence\":\"high\"}")
        assert result.confidence == 0.5

    def test_extreme_confidence(self):
        """Extreme confidence values."""
        result = _parse_response("{\"confidence\":2.0,\"recommendation\":\"execute\"}")
        assert result.confidence == 1.0

    def test_invalid_recommendation(self):
        """Invalid recommendation normalized."""
        result = _parse_response("{\"confidence\":0.8,\"recommendation\":\"invalid\"}")
        assert result.recommendation == "hold"


class TestPreFilterThresholds:
    """Test pre-filter threshold edge cases."""

    def test_default_thresholds(self):
        """Default thresholds exist."""
        thresholds = FilterThresholds.instance()
        assert thresholds.get("atr_pct_max") == 15.0

    def test_dynamic_thresholds(self):
        """Dynamic thresholds for specific tickers."""
        thresholds = FilterThresholds.instance()
        btc_atr = thresholds.get("atr_pct_max", "BTCUSDT")
        assert btc_atr == 10.0

    def test_custom_threshold_override(self):
        """Custom threshold override."""
        thresholds = FilterThresholds.instance()
        thresholds.set_custom("atr_pct_max", 25.0)
        assert thresholds.get("atr_pct_max") == 25.0
        thresholds.clear_custom("atr_pct_max")

    def test_unknown_threshold_key(self):
        """Unknown threshold returns None."""
        thresholds = FilterThresholds.instance()
        assert thresholds.get("unknown_key") is None


class TestFilterScoreCalculation:
    """Test filter score calculation."""

    def test_all_passed(self):
        """All checks passed."""
        checks = {
            "daily_trade_limit": {"passed": True},
            "volatility_guard": {"passed": True},
        }
        score = calculate_filter_score(checks)
        assert score > 0

    def test_all_failed(self):
        """All checks failed."""
        checks = {
            "daily_trade_limit": {"passed": False},
            "volatility_guard": {"passed": False},
        }
        score = calculate_filter_score(checks)
        assert score < 100

    def test_soft_fail(self):
        """Soft fail scores."""
        checks = {
            "spread": {"passed": False, "soft_fail": True},
        }
        score = calculate_filter_score(checks)
        assert score > 0

    def test_disabled_check(self):
        """Disabled checks don't affect score."""
        checks = {
            "daily_trade_limit": {"passed": False, "disabled": True},
        }
        score = calculate_filter_score(checks)
        assert score == 100.0


class TestSMCAnalysis:
    """Test SMC analysis edge cases."""

    def test_empty_ohlcv_fvg(self):
        """Empty OHLCV for FVG detection."""
        fvgs = detect_fvgs([])
        assert fvgs == []

    def test_single_candle_fvg(self):
        """Single candle can't form FVG."""
        fvgs = detect_fvgs([[1000, 100, 110, 90, 105, 1000]])
        assert fvgs == []

    def test_empty_ohlcv_order_blocks(self):
        """Empty OHLCV for OB detection."""
        obs = detect_order_blocks([])
        assert obs == []

    def test_no_impulse_order_blocks(self):
        """No impulse means no order blocks."""
        ohlcv = [
            [1000, 100, 100, 100, 100, 1000],
            [1001, 100, 100, 100, 100, 1000],
            [1002, 100, 100, 100, 100, 1000],
        ]
        obs = detect_order_blocks(ohlcv)
        assert obs == []

    def test_premium_discount_empty(self):
        """Premium/discount with empty swings."""
        result = calculate_premium_discount([], [])
        assert result == (0.0, 0.0, 0.0)

    def test_premium_discount_negative_range(self):
        """Premium/discount with negative range."""
        result = calculate_premium_discount(100.0, 100.0)
        assert result["premium"] == 0.0


class TestPasswordSecurity:
    """Test password security edge cases."""

    def test_empty_password(self):
        """Empty password validation."""
        valid, msg = validate_password_strength("")
        assert not valid

    def test_short_password(self):
        """Short password rejected."""
        valid, msg = validate_password_strength("abc")
        assert not valid
        assert "8 characters" in msg

    def test_common_password(self):
        """Common password rejected."""
        valid, msg = validate_password_strength("password")
        assert not valid
        assert "common" in msg

    def test_username_in_password(self):
        """Password containing username rejected."""
        valid, msg = validate_password_strength("AdminPass123!", "admin")
        assert not valid

    def test_valid_password(self):
        """Valid password accepted."""
        valid, msg = validate_password_strength("SecurePass123!")
        assert valid

    def test_password_hash_format(self):
        """Password hash format."""
        hash = hash_password("testpass123!")
        parts = hash.split("$")
        assert len(parts) == 3
        assert int(parts[0]) >= 260000

    def test_wrong_password_verification(self):
        """Wrong password rejected."""
        hash = hash_password("correctpass")
        assert not verify_password("wrongpass", hash)


class TestEncryption:
    """Test encryption edge cases."""

    def test_empty_value(self):
        """Empty value encryption."""
        result = encrypt_value("")
        assert result == ""

    def test_already_encrypted(self):
        """Already encrypted value not re-encrypted."""
        encrypted = encrypt_value("test")
        result = encrypt_value(encrypted)
        assert result == encrypted

    def test_decrypt_plain_value(self):
        """Decrypting plain value returns unchanged."""
        result = decrypt_value("plain_value")
        assert result == "plain_value"


class TestWebhookSecretValidation:
    """Test webhook secret validation."""

    def test_placeholder_detected(self):
        """Placeholder secrets detected."""
        assert is_placeholder_webhook_secret("changeme")
        assert is_placeholder_webhook_secret("your-webhook-secret")
        assert is_placeholder_webhook_secret("replace-with-a-long-random-webhook-secret")

    def test_valid_secret_accepted(self):
        """Valid secrets not flagged."""
        assert not is_placeholder_webhook_secret("abc123xyz789")

    def test_empty_secret_rejected(self):
        """Empty secret is placeholder."""
        assert is_placeholder_webhook_secret("")


class TestUtilityFunctions:
    """Test utility function edge cases."""

    def test_safe_float_none(self):
        """None to float."""
        assert safe_float(None) == 0.0

    def test_safe_float_nan(self):
        """NaN to float."""
        assert safe_float(float('nan')) == 0.0

    def test_safe_float_inf(self):
        """Inf to float."""
        assert safe_float(float('inf')) == 0.0

    def test_safe_float_string(self):
        """String to float."""
        assert safe_float("123.45") == 123.45

    def test_safe_float_invalid_string(self):
        """Invalid string to float."""
        assert safe_float("abc") == 0.0

    def test_safe_bool_none(self):
        """None to bool."""
        assert safe_bool(None) == False

    def test_safe_bool_string(self):
        """String to bool."""
        assert safe_bool("true") == True
        assert safe_bool("1") == True
        assert safe_bool("false") == False

    def test_safe_int_none(self):
        """None to int."""
        assert safe_int(None) == 0

    def test_safe_str_none(self):
        """None to str."""
        assert safe_str(None) == ""


class TestPriceBucketing:
    """Test price bucketing for caching."""

    def test_zero_price(self):
        """Zero price returns empty bucket."""
        bucket = _price_to_bucket(0.0)
        assert bucket == ""

    def test_negative_price(self):
        """Negative price returns empty bucket."""
        bucket = _price_to_bucket(-100.0)
        assert bucket == ""

    def test_normal_price(self):
        """Normal price bucketing."""
        bucket = _price_to_bucket(100.0)
        assert bucket != ""

    def test_small_price(self):
        """Small price bucketing."""
        bucket = _price_to_bucket(0.001)
        assert bucket != ""


class TestTakeProfitLevels:
    """Test take-profit level configurations."""

    def test_empty_levels(self):
        """Empty levels list."""
        config = TakeProfitConfig(levels=[])
        assert config.levels == []

    def test_max_levels(self):
        """Maximum 4 levels."""
        levels = [
            TakeProfitLevel(price=110.0, qty_pct=25.0),
            TakeProfitLevel(price=120.0, qty_pct=25.0),
            TakeProfitLevel(price=130.0, qty_pct=25.0),
            TakeProfitLevel(price=140.0, qty_pct=25.0),
        ]
        config = TakeProfitConfig(levels=levels)
        assert len(config.levels) == 4

    def test_qty_pct_range(self):
        """Quantity percentage range."""
        level = TakeProfitLevel(price=110.0, qty_pct=100.0)
        assert level.qty_pct == 100.0

        level = TakeProfitLevel(price=110.0, qty_pct=1.0)
        assert level.qty_pct == 1.0

    def test_invalid_qty_pct(self):
        """Invalid quantity percentage."""
        with pytest.raises(ValueError):
            TakeProfitLevel(price=110.0, qty_pct=0.0)

        with pytest.raises(ValueError):
            TakeProfitLevel(price=110.0, qty_pct=101.0)


class TestTrailingStopConfig:
    """Test trailing stop configurations."""

    def test_none_mode(self):
        """None mode."""
        config = TrailingStopConfig(mode=TrailingStopMode.NONE)
        assert config.mode == TrailingStopMode.NONE

    def test_trail_pct_range(self):
        """Trail percentage range."""
        config = TrailingStopConfig(trail_pct=20.0)
        assert config.trail_pct == 20.0

    def test_invalid_trail_pct(self):
        """Invalid trail percentage."""
        with pytest.raises(ValueError):
            TrailingStopConfig(trail_pct=0.0)

        with pytest.raises(ValueError):
            TrailingStopConfig(trail_pct=25.0)


class TestTradeDecision:
    """Test trade decision model."""

    def test_empty_decision(self):
        """Empty decision."""
        decision = TradeDecision()
        assert not decision.execute

    def test_with_signal(self):
        """Decision with signal."""
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            direction=SignalDirection.LONG,
            price=100.0,
        )
        decision = TradeDecision(signal=signal)
        assert decision.signal.ticker == "BTCUSDT"

    def test_take_profit_levels(self):
        """Take-profit levels."""
        decision = TradeDecision(
            take_profit_levels=[
                TakeProfitLevel(price=110.0, qty_pct=50.0),
                TakeProfitLevel(price=120.0, qty_pct=50.0),
            ]
        )
        assert len(decision.take_profit_levels) == 2
