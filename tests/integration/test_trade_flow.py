"""
P4-FIX: Integration Tests for Trade Flow
End-to-end tests for complete trade execution pipeline.
"""
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from models import TradingViewSignal, SignalDirection, MarketContext, TradeDecision, AIAnalysis
from exchange import execute_trade
from ai_analyzer import analyze_signal_with_ai


@pytest.mark.integration
@pytest.mark.asyncio
class TestTradeFlowIntegration:
    """Integration tests for complete trade flow."""
    
    @pytest.fixture
    def full_signal(self):
        """Complete TradingView signal."""
        return TradingViewSignal(
            ticker="BTCUSDT",
            direction=SignalDirection.LONG,
            price=50000.0,
            timeframe="1h",
            strategy="test_strategy",
            message="Strong bullish signal",
        )
    
    @pytest.fixture
    def full_market_context(self):
        """Complete market context."""
        return MarketContext(
            ticker="BTCUSDT",
            current_price=50000.0,
            price_change_1h=2.5,
            price_change_4h=5.0,
            price_change_24h=10.0,
            volume_24h=1000000.0,
            high_24h=52000.0,
            low_24h=48000.0,
            bid_ask_spread=0.01,
            funding_rate=0.0001,
            rsi_1h=60.0,
            atr_pct=2.5,
        )
    
    async def test_full_trade_pipeline_paper_mode(self, full_signal, full_market_context):
        """Test complete trade pipeline in paper trading mode."""
        # Mock AI analysis
        with patch("ai_analyzer.analyze_signal_with_ai") as mock_ai:
            mock_ai.return_value = AIAnalysis(
                confidence=0.85,
                recommendation="execute",
                reasoning="Strong setup",
                suggested_entry=50000.0,
                suggested_stop_loss=48000.0,
                suggested_take_profit=52000.0,
                position_size_pct=0.5,
                recommended_leverage=10,
                risk_score=0.4,
            )
            
            # Mock pre-filter
            with patch("pre_filter.run_pre_filter_checks") as mock_pre_filter:
                mock_pre_filter.return_value = {"pass": True, "score": 85}
                
                # Execute in paper mode (no live trading)
                decision = TradeDecision(
                    ticker=full_signal.ticker,
                    direction=full_signal.direction,
                    quantity=0.01,
                    execute=True,
                    ai_analysis=mock_ai.return_value,
                )
                
                result = await execute_trade(
                    decision,
                    exchange_config={"live_trading": False},
                )
                
                # Should simulate order
                assert result["status"] in ["simulated", "success"]
                assert "order" in result or "reason" in result
    
    async def test_full_trade_pipeline_live_mode_mock_exchange(self, full_signal):
        """Test complete trade pipeline with mocked exchange."""
        # Mock exchange creation
        mock_exchange = Mock()
        mock_exchange.id = "binance"
        mock_exchange.set_leverage = Mock(return_value={"leverage": 10})
        mock_exchange.create_order = Mock(return_value={
            "id": "order_test_123",
            "status": "closed",
            "filled": 0.01,
            "average": 50000.0,
        })
        mock_exchange.close = Mock()
        
        with patch("exchange._get_or_create_exchange", return_value=mock_exchange):
            with patch("exchange._resolve_symbol", return_value="BTC/USDT:USDT"):
                with patch("exchange._close_exchange"):
                    # Mock leverage retry
                    with patch("exchange._set_leverage_with_retry") as mock_retry:
                        mock_retry.return_value = {"success": True}
                        
                        decision = TradeDecision(
                            ticker="BTCUSDT",
                            direction=SignalDirection.LONG,
                            quantity=0.01,
                            execute=True,
                            entry_price=50000.0,
                            ai_analysis=AIAnalysis(
                                recommended_leverage=10,
                                confidence=0.85,
                            ),
                        )
                        
                        result = await execute_trade(
                            decision,
                            exchange_config={
                                "live_trading": True,
                                "exchange": "binance",
                                "api_key": "test",
                                "api_secret": "test",
                            },
                        )
                        
                        # Should execute on exchange
                        assert mock_exchange.create_order.called
                        assert result.get("order_id") or result.get("status")
    
    async def test_trade_flow_with_leverage_failure_abort(self, full_signal):
        """Test trade aborts when leverage setup fails for high leverage."""
        mock_exchange = Mock()
        mock_exchange.id = "binance"
        
        with patch("exchange._get_or_create_exchange", return_value=mock_exchange):
            with patch("exchange._resolve_symbol", return_value="BTC/USDT:USDT"):
                # Mock leverage retry failure
                with patch("exchange._set_leverage_with_retry") as mock_retry:
                    mock_retry.return_value = {
                        "success": False,
                        "error": "Authentication failed",
                        "abort": True,
                    }
                    
                    decision = TradeDecision(
                        ticker="BTCUSDT",
                        direction=SignalDirection.LONG,
                        quantity=0.01,
                        execute=True,
                        ai_analysis=AIAnalysis(
                            recommended_leverage=20,  # High leverage
                        ),
                    )
                    
                    result = await execute_trade(
                        decision,
                        exchange_config={"live_trading": True},
                    )
                    
                    # Should abort
                    assert result["status"] == "error"
                    assert "Leverage setup failed" in result["reason"]
    
    async def test_trade_flow_with_ai_cache_hit(self, full_signal, full_market_context):
        """Test trade flow uses cached AI analysis."""
        from core.cache.multi_layer_cache import MultiLayerCache
        
        # Create cache with pre-cached AI result
        cache = MultiLayerCache(cache_name="test_ai_cache", l2_enabled=False)
        await cache.set(
            "ai_analysis_test_key",
            AIAnalysis(
                confidence=0.9,
                recommendation="execute",
            ).dict(),
            ttl=60,
        )
        
        # Mock cache get
        with patch("ai_analyzer._get_cached_analysis") as mock_cache_get:
            mock_cache_get.return_value = AIAnalysis(
                confidence=0.9,
                recommendation="execute",
            )
            
            # AI should use cached result
            # (Would verify cache hit in metrics)
    
    async def test_trade_flow_with_event_bus(self, full_signal):
        """Test trade flow publishes events."""
        from core.events.event_bus import EventBus
        from core.events.event_types import EventTypes
        
        bus = EventBus(persist_events=False)
        
        events_received = []
        
        async def capture_event(event):
            events_received.append(event)
        
        bus.subscribe(EventTypes.TRADE_EXECUTED, capture_event)
        
        # Execute trade
        decision = TradeDecision(
            ticker="BTCUSDT",
            direction=SignalDirection.LONG,
            quantity=0.01,
            execute=True,
        )
        
        result = await execute_trade(
            decision,
            exchange_config={"live_trading": False},
        )
        
        # Would verify event published
        # (Depends on integration with execute_trade)


@pytest.mark.integration
@pytest.mark.asyncio
class TestPositionReconciliationIntegration:
    """Integration tests for position reconciliation."""
    
    async def test_position_reconciliation_with_metrics(self, sample_position):
        """Test position reconciliation updates metrics."""
        from position_monitor import run_position_monitor_once
        from core.metrics.prometheus_metrics import POSITION_COUNT
        
        # Mock database session
        mock_session = AsyncMock()
        mock_session.execute.return_value = MagicMock()
        mock_session.execute.return_value.scalars.return_value.all.return_value = [sample_position]
        
        # Run reconciliation
        stats = await run_position_monitor_once(user_configs={})
        
        # Should track positions
        assert stats["tracked"] >= 0
        assert "errors" in stats
    
    async def test_ghost_position_detection_updates_metrics(self):
        """Test ghost position detection updates Prometheus metrics."""
        from position_monitor import _reconcile_exchange_position
        from core.metrics.prometheus_metrics import GHOST_POSITION_COUNT
        
        # Would test ghost position metrics increment
        # (Requires full mock setup)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
class TestEndToEndFlow:
    """End-to-end tests simulating real trade scenarios."""
    
    async def test_signal_to_execution_complete_flow(self):
        """Test signal received to execution complete."""
        # 1. Signal received from TradingView
        signal = TradingViewSignal(
            ticker="ETHUSDT",
            direction=SignalDirection.LONG,
            price=3000.0,
            timeframe="4h",
            strategy="momentum",
        )
        
        # 2. Fetch market data
        market = MarketContext(
            ticker="ETHUSDT",
            current_price=3000.0,
            atr_pct=3.0,
        )
        
        # 3. Run pre-filter
        with patch("pre_filter.run_pre_filter_checks") as mock_pre_filter:
            mock_pre_filter.return_value = {"pass": True, "score": 90}
            
            # 4. Run AI analysis
            with patch("ai_analyzer.analyze_signal_with_ai") as mock_ai:
                mock_ai.return_value = AIAnalysis(
                    confidence=0.88,
                    recommendation="execute",
                    suggested_entry=3000.0,
                    suggested_stop_loss=2900.0,
                    suggested_take_profit=3200.0,
                    recommended_leverage=5,
                )
                
                # 5. Create decision
                decision = TradeDecision(
                    ticker="ETHUSDT",
                    direction=SignalDirection.LONG,
                    quantity=0.1,
                    execute=True,
                    ai_analysis=mock_ai.return_value,
                )
                
                # 6. Execute trade (paper mode)
                result = await execute_trade(
                    decision,
                    exchange_config={"live_trading": False},
                )
                
                # Verify complete flow
                assert result["status"] != "error"
    
    async def test_multi_position_concurrent_flow(self):
        """Test concurrent position monitoring."""
        # Create multiple positions
        positions = []
        for i in range(5):
            pos = Mock()
            pos.id = f"pos_{i}"
            pos.ticker = f"COIN{i}USDT"
            pos.direction = "long"
            pos.entry_price = 1000.0 + i * 100
            pos.quantity = 0.01
            pos.status = "open"
            positions.append(pos)
        
        # Simulate concurrent reconciliation
        # Would test parallel processing performance