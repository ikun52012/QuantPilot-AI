"""
Tests for signal processing pipeline.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from models import TradingViewSignal, SignalDirection, AIAnalysis, MarketContext, TradeDecision, TakeProfitLevel
from services.signal_processor import SignalProcessor, compute_webhook_fingerprint, verify_webhook_signature


class TestWebhookFingerprint:
    """Tests for webhook fingerprint computation."""

    def test_fingerprint_deterministic(self):
        """Same input should produce same fingerprint."""
        body = {
            "secret": "test-secret",
            "ticker": "BTCUSDT",
            "direction": "long",
            "price": 50000,
            "timeframe": "60",
            "strategy": "test",
            "message": "test message",
        }
        fp1 = compute_webhook_fingerprint(body, "user1")
        fp2 = compute_webhook_fingerprint(body, "user1")
        assert fp1 == fp2

    def test_fingerprint_different_users(self):
        """Different users should produce different fingerprints."""
        body = {
            "secret": "test-secret",
            "ticker": "BTCUSDT",
            "direction": "long",
            "price": 50000,
            "timeframe": "60",
            "strategy": "test",
            "message": "test message",
        }
        fp1 = compute_webhook_fingerprint(body, "user1")
        fp2 = compute_webhook_fingerprint(body, "user2")
        assert fp1 != fp2

    def test_fingerprint_different_tickers(self):
        """Different tickers should produce different fingerprints."""
        body1 = {"secret": "test", "ticker": "BTCUSDT", "direction": "long", "price": 50000, "timeframe": "60", "strategy": "test", "message": ""}
        body2 = {"secret": "test", "ticker": "ETHUSDT", "direction": "long", "price": 50000, "timeframe": "60", "strategy": "test", "message": ""}
        fp1 = compute_webhook_fingerprint(body1)
        fp2 = compute_webhook_fingerprint(body2)
        assert fp1 != fp2

    def test_fingerprint_with_alert_id(self):
        """Alert ID should dominate fingerprint calculation."""
        body1 = {"secret": "test", "ticker": "BTCUSDT", "direction": "long", "price": 50000, "alert_id": "alert-123", "timeframe": "60", "strategy": "test", "message": ""}
        body2 = {"secret": "test", "ticker": "ETHUSDT", "direction": "short", "price": 3000, "alert_id": "alert-123", "timeframe": "60", "strategy": "test", "message": ""}
        fp1 = compute_webhook_fingerprint(body1)
        fp2 = compute_webhook_fingerprint(body2)
        assert fp1 == fp2


class TestWebhookSignature:
    """Tests for webhook signature verification."""

    @patch("services.signal_processor.settings")
    def test_verify_signature_no_secret(self, mock_settings):
        """Should allow when no HMAC secret configured (dev mode)."""
        mock_settings.exchange.live_trading = False
        mock_settings.webhook_hmac_secret = ""
        with patch("os.getenv", return_value=""):
            assert verify_webhook_signature(b"test", "") is True

    @patch("services.signal_processor.settings")
    def test_verify_signature_live_trading_no_secret(self, mock_settings):
        """Should reject when live trading but no secret."""
        mock_settings.exchange.live_trading = True
        mock_settings.webhook_hmac_secret = ""
        with patch("os.getenv", return_value=""):
            assert verify_webhook_signature(b"test", "") is False


class TestSignalProcessorBuildDecision:
    """Tests for trade decision building."""

    @pytest.fixture
    def processor(self):
        """Create a mock SignalProcessor."""
        mock_session = AsyncMock()
        return SignalProcessor(session=mock_session)

    @pytest.fixture
    def sample_signal(self):
        return TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe="60",
            strategy="test",
            message="",
        )

    @pytest.fixture
    def sample_market(self):
        return MarketContext(
            ticker="BTCUSDT",
            current_price=50000.0,
            price_change_1h=1.0,
            volume_24h=1000000000,
        )

    def test_reject_when_ai_rejects(self, processor, sample_signal, sample_market):
        """Should reject when AI recommendation is 'reject'."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="reject",
            reasoning="AI rejected",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is False
        assert "AI rejected" in decision.reason

    def test_reject_low_confidence(self, processor, sample_signal, sample_market):
        """Should reject when confidence is below 0.4."""
        analysis = AIAnalysis(
            confidence=0.3,
            recommendation="execute",
            reasoning="Low confidence",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is False
        assert "Low confidence" in decision.reason

    def test_reject_direction_conflict(self, processor, sample_signal, sample_market):
        """Should reject when AI suggests opposite direction."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="Direction conflict",
            suggested_direction=SignalDirection.SHORT,
            suggested_stop_loss=49000,
            suggested_tp1=51000,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is False
        assert "direction conflict" in decision.reason.lower()

    def test_reject_no_stop_loss(self, processor, sample_signal, sample_market):
        """Should reject when no valid stop loss for opening trade."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="No SL",
            suggested_stop_loss=None,
            suggested_tp1=51000,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is False
        assert "stop loss" in decision.reason.lower()

    def test_reject_no_take_profit(self, processor, sample_signal, sample_market):
        """Should reject when no valid take profit for opening trade."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="No TP",
            suggested_stop_loss=49000,
            suggested_tp1=None,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is False
        assert "take-profit" in decision.reason.lower()

    def test_accept_valid_signal(self, processor, sample_signal, sample_market):
        """Should accept when all conditions are met."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="Good setup",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.stop_loss == 49000
        assert len(decision.take_profit_levels) > 0

    def test_modified_entry_within_range(self, processor, sample_signal, sample_market):
        """Should use AI modified entry when within 5% of signal price."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="modify",
            reasoning="Better entry",
            suggested_entry=49500,
            suggested_stop_loss=48500,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.entry_price == 49500

    def test_modified_entry_out_of_range(self, processor, sample_signal, sample_market):
        """Should reject AI modified entry when >5% from signal price."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="modify",
            reasoning="Wild entry",
            suggested_entry=40000,
            suggested_stop_loss=39000,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.entry_price == 50000  # Original price


class TestPositionSizeCalculation:
    """Tests for position size calculation."""

    @pytest.fixture
    def processor(self):
        mock_session = AsyncMock()
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            return SignalProcessor(session=mock_session)

    def test_position_size_basic(self, processor):
        """Basic position size calculation."""
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.position_sizing_mode = "percentage"
            qty = processor._calculate_position_size(price=100, size_pct=1.0, leverage=1)
            assert qty == 10.0  # 10000 * 10% * 1.0 / 100 / 10 = 10 units at $100

    def test_position_size_with_leverage(self, processor):
        """Position size should scale with leverage."""
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.position_sizing_mode = "percentage"
            qty = processor._calculate_position_size(price=100, size_pct=1.0, leverage=10)
            assert qty == 100.0  # 10000 * 10% * 1.0 * 10 / 100 = 100 units

    def test_position_size_zero_price(self, processor):
        """Should return 0 when price is zero."""
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.position_sizing_mode = "percentage"
            qty = processor._calculate_position_size(price=0, size_pct=1.0, leverage=1)
            assert qty == 0.0


class TestValidStopLoss:
    """Tests for stop loss validation."""

    def test_valid_long_stop_loss(self):
        """Long SL must be below entry."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 95)
        assert result == 95

    def test_invalid_long_stop_loss(self):
        """Long SL above entry is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 105)
        assert result is None

    def test_valid_short_stop_loss(self):
        """Short SL must be above entry."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.SHORT, 100, 105)
        assert result == 105

    def test_invalid_short_stop_loss(self):
        """Short SL below entry is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.SHORT, 100, 95)
        assert result is None

    def test_zero_stop_loss(self):
        """Zero stop loss is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 0)
        assert result is None


class TestValidTakeProfit:
    """Tests for take profit validation."""

    def test_valid_long_tp(self):
        """Long TP must be above entry."""
        result = SignalProcessor._valid_take_profit(SignalDirection.LONG, 100, 110)
        assert result == 110

    def test_invalid_long_tp(self):
        """Long TP below entry is invalid."""
        result = SignalProcessor._valid_take_profit(SignalDirection.LONG, 100, 90)
        assert result is None

    def test_valid_short_tp(self):
        """Short TP must be below entry."""
        result = SignalProcessor._valid_take_profit(SignalDirection.SHORT, 100, 90)
        assert result == 90

    def test_invalid_short_tp(self):
        """Short TP above entry is invalid."""
        result = SignalProcessor._valid_take_profit(SignalDirection.SHORT, 100, 110)
        assert result is None
