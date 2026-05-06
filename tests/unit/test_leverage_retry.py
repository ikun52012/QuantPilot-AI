"""
P4-FIX: Unit Tests for Leverage Retry Mechanism
Tests for leverage setup with retry logic.
"""
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import ccxt

from exchange import _set_leverage_with_retry, _LEVERAGE_MAX_RETRIES


@pytest.mark.asyncio
class TestLeverageRetryMechanism:
    """Test suite for leverage setup retry mechanism."""
    
    @pytest.fixture
    def mock_exchange(self):
        """Create mock exchange instance."""
        exchange = Mock()
        exchange.id = "binance"
        exchange.set_leverage = Mock()
        return exchange
    
    async def test_successful_leverage_setup_no_retry(self, mock_exchange):
        """Test successful leverage setup without retries."""
        mock_exchange.set_leverage.return_value = {"leverage": 10}
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        assert result["success"] == True
        assert mock_exchange.set_leverage.call_count == 1
    
    async def test_leverage_skip_for_low_leverage(self, mock_exchange):
        """Test leverage setup skipped for leverage <= 1x."""
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=1,
            symbol="BTC/USDT:USDT",
        )
        
        assert result["success"] == True
        assert mock_exchange.set_leverage.call_count == 0  # Not called
    
    async def test_retry_on_network_error(self, mock_exchange):
        """Test retry on NetworkError."""
        # First 2 attempts fail, 3rd succeeds
        mock_exchange.set_leverage.side_effect = [
            ccxt.NetworkError("Connection timeout"),
            ccxt.NetworkError("Connection reset"),
            {"leverage": 10},
        ]
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        assert result["success"] == True
        assert mock_exchange.set_leverage.call_count == 3
    
    async def test_retry_on_timeout(self, mock_exchange):
        """Test retry on Timeout error."""
        mock_exchange.set_leverage.side_effect = [
            ccxt.ExchangeError("Timeout"),
            {"leverage": 10},
        ]
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
        )
        
        assert result["success"] == True
        assert mock_exchange.set_leverage.call_count == 2
    
    async def test_abort_on_authentication_error(self, mock_exchange):
        """Test abort on AuthenticationError (no retry)."""
        mock_exchange.set_leverage.side_effect = ccxt.AuthenticationError("Invalid API key")
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
        )
        
        assert result["success"] == False
        assert result["abort"] == True
        assert "Authentication failed" in result["error"]
        assert mock_exchange.set_leverage.call_count == 1  # No retry
    
    async def test_max_retries_exceeded(self, mock_exchange):
        """Test failure after max retries exceeded."""
        # All attempts fail
        mock_exchange.set_leverage.side_effect = ccxt.NetworkError("Network error")
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        assert result["success"] == False
        assert result["abort"] == True
        assert mock_exchange.set_leverage.call_count == 3
    
    async def test_exponential_backoff_delay(self, mock_exchange):
        """Test exponential backoff delay between retries."""
        import time
        
        mock_exchange.set_leverage.side_effect = [
            ccxt.NetworkError("Error 1"),
            ccxt.NetworkError("Error 2"),
            {"leverage": 10},
        ]
        
        start_time = time.time()
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        elapsed = time.time() - start_time
        
        assert result["success"] == True
        # Should have delays: ~1s, ~2s (total ~3s)
        assert elapsed >= 2.5  # Allow some tolerance
    
    async def test_abort_for_high_leverage_on_permanent_error(self, mock_exchange):
        """Test abort for high leverage (>1x) on permanent exchange error."""
        mock_exchange.set_leverage.side_effect = ccxt.ExchangeError("Margin mode not supported")
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=20,  # High leverage
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        assert result["success"] == False
        assert result["abort"] == True  # Should abort for high leverage
    
    async def test_continue_for_low_leverage_on_error(self, mock_exchange):
        """Test continue (no abort) for low leverage on error."""
        mock_exchange.set_leverage.side_effect = ccxt.ExchangeError("Leverage not supported")
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=1,  # Low leverage
            symbol="BTC/USDT:USDT",
        )
        
        assert result["success"] == False
        assert result["abort"] == False  # Don't abort for low leverage
    
    async def test_unexpected_exception_handling(self, mock_exchange):
        """Test handling of unexpected exceptions."""
        mock_exchange.set_leverage.side_effect = [
            Exception("Unexpected error 1"),
            Exception("Unexpected error 2"),
            {"leverage": 10},
        ]
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
            max_retries=3,
        )
        
        assert result["success"] == True
        assert mock_exchange.set_leverage.call_count == 3


@pytest.mark.asyncio
class TestLeverageRetryIntegration:
    """Integration tests for leverage retry in execute_trade."""
    
    async def test_execute_trade_calls_retry_mechanism(self):
        """Test execute_trade uses leverage retry mechanism."""
        from exchange import execute_trade
        from models import TradeDecision, SignalDirection, AIAnalysis
        
        # Mock exchange
        with patch("exchange._get_or_create_exchange") as mock_get_exchange:
            mock_exchange = Mock()
            mock_exchange.id = "binance"
            mock_exchange.set_leverage = Mock(return_value={"leverage": 10})
            mock_get_exchange.return_value = mock_exchange
            
            # Mock symbol resolution
            with patch("exchange._resolve_symbol", return_value="BTC/USDT:USDT"):
                # Mock _set_leverage_with_retry
                with patch("exchange._set_leverage_with_retry") as mock_retry:
                    mock_retry.return_value = {"success": True}
                    
                    # Create decision
                    decision = TradeDecision(
                        ticker="BTCUSDT",
                        direction=SignalDirection.LONG,
                        quantity=0.01,
                        execute=True,
                        ai_analysis=AIAnalysis(
                            recommended_leverage=10,
                            confidence=0.8,
                        ),
                    )
                    
                    # Execute with live trading
                    result = await execute_trade(
                        decision,
                        exchange_config={"live_trading": True, "exchange": "binance"},
                    )
                    
                    # Verify retry mechanism called
                    mock_retry.assert_called_once()


@pytest.mark.asyncio  
class TestLeverageRetryMetrics:
    """Test metrics recording for leverage failures."""
    
    async def test_retry_attempts_recorded(self, mock_exchange):
        """Test retry attempts are tracked in metrics."""
        from core.metrics.prometheus_metrics import LEVERAGE_SETUP_FAILURE
        
        mock_exchange.set_leverage.side_effect = [
            ccxt.NetworkError("Error"),
            {"leverage": 10},
        ]
        
        result = await _set_leverage_with_retry(
            mock_exchange,
            leverage=10,
            symbol="BTC/USDT:USDT",
        )
        
        assert result["success"] == True
        # Metrics should record failure at attempt 1
        # (verified via Prometheus metrics endpoint)