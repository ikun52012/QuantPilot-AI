"""Tests for signal processing pipeline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import pre_filter
from core.database import PositionModel
from core.utils.datetime import utcnow
from models import AIAnalysis, MarketContext, PreFilterResult, SignalDirection, TradeDecision, TradingViewSignal
from services.signal_processor import SignalProcessor, compute_webhook_fingerprint


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

    @pytest.mark.asyncio
    async def test_check_position_conflict_matches_symbol_aliases(self, processor):
        decision = TradeDecision(ticker="SPY/USDT:USDT", direction=SignalDirection.SHORT)
        processor.session.execute = AsyncMock(return_value=type(
            "_Result",
            (),
            {
                "scalars": lambda self: type(
                    "_Scalars",
                    (),
                    {"all": lambda self: [type("_Pos", (), {"ticker": "SPYUSDT.P", "direction": "long", "id": "abcd1234-0000"})()]},
                )()
            },
        )())

        conflict_reason, conflicting_position = await processor._check_position_conflict(decision, "user-1")

        assert conflict_reason is not None
        assert "conflicting position" in conflict_reason.lower()
        assert conflicting_position is not None

    def test_fallback_stop_loss_when_ai_omits_stop_loss(self, processor, sample_signal, sample_market):
        """Should use bounded fallback SL when AI omits one."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="No SL",
            suggested_stop_loss=None,
            suggested_tp1=51000,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.stop_loss == pytest.approx(49400.0)
        assert len(decision.take_profit_levels) > 0

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
            suggested_tp1=51500,
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.stop_loss == 49000
        assert len(decision.take_profit_levels) > 0

    def test_ai_stop_loss_survives_atr_timeframe_guidance_conflict(self, processor):
        """AI structural SL should be accepted when ATR guidance exceeds timeframe cap."""
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=100.0,
            timeframe="15",
            strategy="test",
            message="",
        )
        market = MarketContext(ticker="BTCUSDT", current_price=100.0, atr_pct=1.018)
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="15m structure invalidates below local swing",
            suggested_stop_loss=99.4,
            suggested_tp1=101.5,
            tp1_qty_pct=100.0,
        )

        decision = processor._build_trade_decision(signal, analysis, market, None, {})

        assert decision.execute is True
        assert decision.stop_loss == pytest.approx(99.4)
        assert any("below ATR/timeframe guidance" in warning for warning in analysis.warnings)

    @pytest.mark.parametrize(
        ("timeframe", "expected_timeout"),
        [
            ("15", 2 * 60 * 60),
            ("30", 4 * 60 * 60),
            ("60", 8 * 60 * 60),
            ("240", 48 * 60 * 60),
            ("1D", 7 * 24 * 60 * 60),
        ],
    )
    def test_build_trade_decision_sets_timeframe_aware_limit_timeout(
        self,
        processor,
        sample_market,
        timeframe,
        expected_timeout,
    ):
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe=timeframe,
            strategy="test",
            message="",
        )
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="Good setup",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )

        decision = processor._build_trade_decision(signal, analysis, sample_market, None, {})

        assert decision.limit_timeout_secs == expected_timeout

    def test_build_trade_decision_uses_limit_timeout_overrides(self, processor, sample_market):
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="Good setup",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )

        decision = processor._build_trade_decision(
            signal,
            analysis,
            sample_market,
            None,
            {"exchange": {"limit_timeout_overrides": {"1h": 6 * 60 * 60}}},
        )

        assert decision.limit_timeout_secs == 6 * 60 * 60

    def test_build_trade_decision_preserves_explicit_empty_limit_timeout_overrides(
        self,
        processor,
        sample_market,
        monkeypatch,
    ):
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="Good setup",
            suggested_stop_loss=49000,
            suggested_tp1=51000,
            tp1_qty_pct=100.0,
        )
        monkeypatch.setattr("services.signal_processor.settings.exchange.limit_timeout_overrides", {"1h": 2 * 60 * 60})

        decision = processor._build_trade_decision(
            signal,
            analysis,
            sample_market,
            None,
            {"exchange": {"limit_timeout_overrides": {}}},
        )

        assert decision.limit_timeout_secs == 8 * 60 * 60

    @patch("services.signal_processor.analyze_signal", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_run_ai_analysis_passes_user_settings(self, mock_analyze_signal, processor, sample_signal, sample_market):
        mock_analyze_signal.return_value = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="ok")
        user_settings = {
            "take_profit": {"num_levels": 3},
            "trailing_stop": {"mode": "step_trailing", "trail_pct": 1.2},
        }

        await processor._run_ai_analysis(sample_signal, sample_market, user_settings)

        mock_analyze_signal.assert_awaited_once_with(sample_signal, sample_market, user_settings)

    @patch("services.signal_processor.analyze_signal", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_run_ai_analysis_includes_prefilter_summary_context(self, mock_analyze_signal, processor, sample_signal, sample_market):
        mock_analyze_signal.return_value = AIAnalysis(confidence=0.8, recommendation="execute", reasoning="ok")
        prefilter_result = PreFilterResult(
            passed=True,
            reason="soft issues only",
            score=72.5,
            checks={
                "spread": {"passed": False, "soft_fail": True},
                "funding_rate": {"passed": False, "soft_fail": True},
                "daily_trade_limit": {"passed": True},
            },
        )

        await processor._run_ai_analysis(
            sample_signal,
            sample_market,
            {"risk": {"ai_risk_profile": "balanced"}},
            prefilter_result=prefilter_result,
        )

        passed_settings = mock_analyze_signal.await_args.args[2]
        assert passed_settings["_prefilter_summary"]["score"] == 72.5
        assert passed_settings["_prefilter_summary"]["soft_fail_count"] == 2
        assert passed_settings["_prefilter_summary"]["hard_fail_count"] == 0
        assert passed_settings["_prefilter_summary"]["notable_checks"] == ["spread", "funding_rate"]

    @patch("services.signal_processor.run_pre_filter_async", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_run_prefilter_enables_scoring_when_min_pass_score_positive(
        self,
        mock_run_pre_filter,
        processor,
        sample_signal,
        sample_market,
        monkeypatch,
    ):
        mock_run_pre_filter.return_value = PreFilterResult(passed=True, reason="ok", checks={}, score=88.0)
        monkeypatch.setattr("pre_filter.get_thresholds", lambda: type("_T", (), {"get": staticmethod(lambda key, ticker="": 70.0 if key == "min_pass_score" else None)})())

        await processor._run_prefilter(sample_signal, sample_market, None, {})

        kwargs = mock_run_pre_filter.await_args.kwargs
        assert kwargs["use_scoring"] is True
        assert kwargs["min_pass_score"] == 70.0

    @pytest.mark.asyncio
    async def test_run_prefilter_blocks_aliased_duplicate_signal_during_cooldown(self, processor, monkeypatch):
        with pre_filter._state_lock:
            pre_filter._recent_signals.clear()

        monkeypatch.setattr("pre_filter.count_today_executed_trades_async", AsyncMock(return_value=0))
        monkeypatch.setattr("pre_filter.get_today_pnl_async", AsyncMock(return_value=0.0))
        monkeypatch.setattr("pre_filter.get_recent_trade_results_async", AsyncMock(return_value=[]))

        signal_a = TradingViewSignal(
            secret="test",
            ticker="SPYUSDT.P",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=500.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        signal_b = TradingViewSignal(
            secret="test",
            ticker="SPY/USDT:USDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=500.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        market = MarketContext(ticker="SPYUSDT", current_price=500.0)

        first = await pre_filter.run_pre_filter_async(signal_a, market, user_id="user-1")
        second = await pre_filter.run_pre_filter_async(signal_b, market, user_id="user-1")

        assert first.passed is True
        assert second.passed is False
        assert "cooldown" in second.reason.lower()

    def test_signal_saturation_is_scoped_by_ticker(self):
        with pre_filter._state_lock:
            pre_filter._recent_signals.clear()
            pre_filter._recent_signals.append({
                "user_id": "user-1",
                "ticker": "BTCUSDT",
                "ticker_key": pre_filter.position_symbol_key("BTCUSDT"),
                "direction": SignalDirection.LONG,
                "timestamp": utcnow(),
            })

        intc_signal = TradingViewSignal(
            secret="test",
            ticker="INTCUSDT.P",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=30.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        btc_signal = TradingViewSignal(
            secret="test",
            ticker="BTC/USDT:USDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe="60",
            strategy="test",
            message="",
        )

        assert pre_filter._count_recent_same_direction(intc_signal, user_id="user-1") == 0
        assert pre_filter._count_recent_same_direction(btc_signal, user_id="user-1") == 1

    def test_modified_entry_within_range(self, processor, sample_signal, sample_market):
        """Should use AI modified entry when within 5% of signal price."""
        # Note: For 60min timeframe, SL max is 2.0%, TP min is 3.0%
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="modify",
            reasoning="Better entry",
            suggested_entry=49500,
            suggested_stop_loss=49200,  # 1.6% from 50000, within 2.0% max
            suggested_tp1=51500,  # 3% from 50000, meets 3.0% min
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
        assert decision.execute is True
        assert decision.entry_price == 49500

    def test_modified_entry_out_of_range(self, processor, sample_signal, sample_market):
        """Should fallback to original signal price when AI modified entry >5% away."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="modify",
            reasoning="Wild entry",
            suggested_entry=40000,
            suggested_stop_loss=39200,  # Adjusted for timeframe limits
            suggested_tp1=41200,  # 3% from 40000
            tp1_qty_pct=100.0,
        )
        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})
# NEW BEHAVIOR: fallback to original price instead of reject
        assert decision.entry_price == 50000.0
        # May be rejected for other reasons (SL too far), but entry should be original price

    def test_modified_entry_without_suggested_entry_fallback(self, processor, sample_signal, sample_market):
        """Should fallback to original price when modify without suggested_entry."""
        analysis = AIAnalysis(
            confidence=0.8,
            recommendation="modify",
            reasoning="Need a better entry",
            suggested_stop_loss=49200,  # Adjusted for timeframe limits
            suggested_tp1=51500,
            tp1_qty_pct=100.0,
        )

        decision = processor._build_trade_decision(sample_signal, analysis, sample_market, None, {})

        # NEW BEHAVIOR: fallback to original price instead of reject
        assert decision.entry_price == 50000.0  # Should use original signal price


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

    def test_position_size_uses_user_risk_settings(self, processor):
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.fixed_position_size_usdt = 100.0
            mock_settings.risk.risk_per_trade_pct = 1.0
            mock_settings.risk.position_sizing_mode = "percentage"
            qty = processor._calculate_position_size(
                price=100,
                size_pct=1.0,
                leverage=1,
                user_settings={"risk": {"account_equity_usdt": 20000, "max_position_pct": 20.0}},
            )
            assert qty == 40.0

    def test_position_size_caps_notional_by_accepted_stop_loss_risk(self, processor):
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.fixed_position_size_usdt = 100.0
            mock_settings.risk.risk_per_trade_pct = 1.0
            mock_settings.risk.position_sizing_mode = "percentage"
            decision = TradeDecision(direction=SignalDirection.LONG, entry_price=100.0, stop_loss=80.0)

            qty = processor._calculate_position_size(price=100, size_pct=1.0, leverage=1, decision=decision)

            assert qty == 5.0

    def test_apply_position_limits_uses_user_account_equity(self, processor):
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.fixed_position_size_usdt = 100.0
            mock_settings.risk.risk_per_trade_pct = 1.0
            mock_settings.risk.position_sizing_mode = "percentage"
            decision = TradeDecision(entry_price=100.0, quantity=50.0)
            processor._apply_position_limits(
                decision,
                {"max_position_pct": 50.0, "max_leverage": 20},
                user_settings={"risk": {"account_equity_usdt": 1000.0, "max_position_pct": 10.0}},
            )
            assert decision.quantity == 1.0

    def test_position_size_uses_contract_size_for_contract_markets(self, processor):
        """For contract markets with contractSize > 1, quantity should be contract count, not base currency amount."""
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.fixed_position_size_usdt = 100.0
            mock_settings.risk.position_sizing_mode = "fixed"
            mock_settings.exchange.name = "okx"
            mock_settings.exchange.market_type = "contract"
            
            decision = TradeDecision(
                ticker="ARB/USDT:USDT",
                direction=SignalDirection.LONG,
                entry_price=0.12,
            )
            
            with patch("exchange.get_market_limits") as mock_get_limits:
                mock_get_limits.return_value = {
                    "min_amount": 1.0,
                    "max_amount": 10000.0,
                    "min_cost": 5.0,
                    "max_cost": float("inf"),
                    "contract_size": 10.0,
                    "amount_precision": 6,
                }
                
                qty = processor._calculate_position_size(
                    price=0.12,
                    size_pct=1.0,
                    leverage=5.0,
                    decision=decision,
                )
                
                assert qty == pytest.approx(416.666667)
                mock_get_limits.assert_called()

    def test_position_size_uses_no_contract_size_for_spot_markets(self, processor):
        """For spot markets (contractSize = 1), quantity should be base currency amount."""
        with patch("services.signal_processor.settings") as mock_settings:
            mock_settings.risk.account_equity_usdt = 10000
            mock_settings.risk.max_position_pct = 10.0
            mock_settings.risk.fixed_position_size_usdt = 100.0
            mock_settings.risk.position_sizing_mode = "fixed"
            mock_settings.exchange.name = "okx"
            mock_settings.exchange.market_type = "spot"
            
            decision = TradeDecision(
                ticker="ARB/USDT",
                direction=SignalDirection.LONG,
                entry_price=0.12,
            )
            
            with patch("exchange.get_market_limits") as mock_get_limits:
                mock_get_limits.return_value = {
                    "min_amount": 1.0,
                    "max_amount": 10000.0,
                    "min_cost": 5.0,
                    "max_cost": float("inf"),
                    "contract_size": 1.0,
                    "amount_precision": 6,
                }
                
                qty = processor._calculate_position_size(
                    price=0.12,
                    size_pct=1.0,
                    leverage=5.0,
                    decision=decision,
                )
                
                assert qty == pytest.approx(4166.666667)


class TestValidStopLoss:
    """Tests for stop loss validation."""

    def test_valid_long_stop_loss(self):
        """Long SL must be below entry (within timeframe limits)."""
        # Use 1D timeframe which allows up to 10% SL distance
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 95, timeframe="1D")
        assert result == 95

    def test_invalid_long_stop_loss(self):
        """Long SL above entry is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 105, timeframe="1D")
        assert result is None

    def test_valid_short_stop_loss(self):
        """Short SL must be above entry (within timeframe limits)."""
        # Use 1D timeframe which allows up to 10% SL distance
        result = SignalProcessor._valid_stop_loss(SignalDirection.SHORT, 100, 105, timeframe="1D")
        assert result == 105

    def test_invalid_short_stop_loss(self):
        """Short SL below entry is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.SHORT, 100, 95, timeframe="1D")
        assert result is None

    def test_zero_stop_loss(self):
        """Zero stop loss is invalid."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 0, timeframe="1D")
        assert result is None

    def test_timeframe_sl_distance_is_advisory_for_ai_stop(self):
        """Timeframe SL ranges should not override AI structural stops."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 95, timeframe="15")
        assert result == 95
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 95, timeframe="1D")
        assert result == 95

    def test_high_atr_min_sl_does_not_override_ai_stop(self):
        """ATR-derived guidance above timeframe max should not move AI SL."""
        result = SignalProcessor._valid_stop_loss(SignalDirection.LONG, 100, 99, atr_pct=5.0, timeframe="60")
        assert result == pytest.approx(99.0)


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


class _FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class TestPositionConflictSafety:
    @pytest.mark.asyncio
    async def test_pending_cancel_error_blocks_conflict_check(self, monkeypatch):
        pending = PositionModel(
            id="pending-short",
            user_id="user-1",
            ticker="BTCUSDT",
            direction="short",
            status="pending",
            entry_price=100.0,
            quantity=1.0,
            opened_at=utcnow(),
            live_trading=True,
            entry_order_id="entry-short",
        )
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[_FakeScalarResult([pending]), _FakeScalarResult([])])
        processor = SignalProcessor(session=session)
        monkeypatch.setattr(
            processor,
            "_cancel_pending_position",
            AsyncMock(return_value={"status": "error", "reason": "Exchange cancellation failed"}),
        )

        reason, position = await processor._check_position_conflict(
            TradeDecision(execute=True, direction=SignalDirection.LONG, ticker="BTCUSDT"),
            "user-1",
            {"exchange": {"live_trading": False}},
        )

        assert position is None
        assert reason == "Exchange cancellation failed"

    @pytest.mark.asyncio
    async def test_conflict_reason_without_position_rejects_before_execution(self, monkeypatch):
        processor = SignalProcessor(session=AsyncMock())
        signal = TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=100.0,
            timeframe="60",
            strategy="test",
            message="",
        )
        execute_trade = AsyncMock(return_value={"status": "filled"})

        monkeypatch.setattr("services.signal_processor.record_signal_received", lambda *args, **kwargs: None)
        monkeypatch.setattr("services.signal_processor.notify_signal_received", AsyncMock())
        monkeypatch.setattr(processor, "_load_user_settings", AsyncMock(return_value={}))
        monkeypatch.setattr(processor, "_reserve_webhook_event", AsyncMock(return_value=SimpleNamespace()))
        monkeypatch.setattr(processor, "_run_prefilter", AsyncMock(return_value=PreFilterResult(passed=True)))
        monkeypatch.setattr(
            processor,
            "_run_ai_analysis",
            AsyncMock(return_value=AIAnalysis(
                confidence=0.8,
                recommendation="execute",
                reasoning="ok",
                suggested_stop_loss=95.0,
                suggested_tp1=110.0,
            )),
        )
        monkeypatch.setattr(
            processor,
            "_build_trade_decision",
            lambda *args, **kwargs: TradeDecision(
                execute=True,
                direction=SignalDirection.LONG,
                ticker="BTCUSDT",
                entry_price=100.0,
                quantity=1.0,
            ),
        )
        monkeypatch.setattr(
            processor,
            "_check_position_conflict",
            AsyncMock(return_value=("Position conflict check failed: database unavailable", None)),
        )
        monkeypatch.setattr(processor, "_execute_trade", execute_trade)

        result = await processor._process_signal_locked(
            signal,
            user_id="user-1",
            prefetched_market=MarketContext(ticker="BTCUSDT", current_price=100.0),
        )

        assert result["status"] == "rejected"
        assert "Position conflict check failed" in result["reason"]
        execute_trade.assert_not_awaited()
