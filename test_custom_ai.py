#!/usr/bin/env python3
"""
Test script for custom AI provider functionality.
This script tests the custom AI provider integration.
"""

import asyncio
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from ai_analyzer import analyze_signal
from models import TradingViewSignal, MarketContext, SignalDirection

async def test_custom_ai():
    """Test custom AI provider functionality."""
    print("=== Testing Custom AI Provider ===\n")
    
    # Check current AI configuration
    print(f"Current AI Provider: {settings.ai.provider}")
    print(f"Custom Provider Enabled: {settings.ai.custom_provider_enabled}")
    print(f"Custom Provider Name: {settings.ai.custom_provider_name}")
    print(f"Custom Provider Model: {settings.ai.custom_provider_model}")
    print(f"Custom Provider API URL: {settings.ai.custom_provider_api_url}")
    print(f"Custom Provider API Key Set: {'Yes' if settings.ai.custom_provider_api_key else 'No'}")
    
    # Create a test signal
    test_signal = TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        entry=50000.0,
        stop_loss=49000.0,
        take_profit=51000.0,
        strategy="Test Strategy",
        timeframe="1h",
        timestamp="2024-01-01T00:00:00Z"
    )
    
    # Create test market context
    test_market = MarketContext(
        current_price=50100.0,
        funding_rate=0.0001,
        volume_24h=1000000000.0,
        change_1h=0.5,
        change_24h=2.0,
        rsi=65.0,
        orderbook_bid_ask_ratio=1.2,
        atr=200.0,
        volatility=1.5
    )
    
    print(f"\nTest Signal: {test_signal.ticker} {test_signal.direction.value}")
    print(f"Market Price: ${test_market.current_price:,.2f}")
    print(f"RSI: {test_market.rsi}")
    
    # Test if custom provider is properly configured
    if settings.ai.provider == settings.ai.custom_provider_name and settings.ai.custom_provider_enabled:
        if not settings.ai.custom_provider_api_url:
            print("\n❌ ERROR: Custom AI provider API URL is not configured")
            return False
        if not settings.ai.custom_provider_api_key:
            print("\n⚠️  WARNING: Custom AI provider API key is not configured")
            print("  The test will fail if the API requires authentication")
    
    print("\n=== Running AI Analysis ===")
    try:
        # Run analysis
        analysis = await analyze_signal(test_signal, test_market)
        
        print(f"\n✅ Analysis Successful!")
        print(f"Recommendation: {analysis.recommendation}")
        print(f"Confidence: {analysis.confidence:.2f}")
        print(f"Risk Score: {analysis.risk_score:.2f}")
        print(f"Position Size: {analysis.position_size_pct:.1f}%")
        
        if analysis.recommendation == "reject":
            print(f"Reason: {analysis.reasoning[:100]}...")
        
        return True
        
    except ValueError as e:
        if "Unknown AI provider" in str(e):
            print(f"\n❌ ERROR: {e}")
            print("  Make sure the custom provider is properly configured in config.py")
        else:
            print(f"\n❌ ERROR: {e}")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return False

if __name__ == "__main__":
    result = asyncio.run(test_custom_ai())
    sys.exit(0 if result else 1)