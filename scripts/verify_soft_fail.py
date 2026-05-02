"""Verify soft fail handling after fixes."""
import sys

sys.path.insert(0, '.')

import asyncio

from models import MarketContext, SignalDirection, TradingViewSignal
from pre_filter import run_pre_filter_async


async def test_soft_fail_scenarios():
    print("=" * 60)
    print("Testing Pre-Filter Soft Fail Handling")
    print("=" * 60)

    # Scenario 1: Orderbook imbalance (now soft fail)
    signal = TradingViewSignal(
        secret="test",
        ticker="LINKUSDT",
        exchange="BINANCE",
        direction=SignalDirection.LONG,
        price=15.0,
        timeframe="60",
        strategy="test",
        message="",
    )

    market = MarketContext(
        ticker="LINKUSDT",
        current_price=15.0,
        volume_24h=932503,  # Low volume (soft fail)
        orderbook_imbalance=0.34,  # Imbalance against long (NOW SOFT FAIL)
        bid_ask_spread=0.08,  # Wide spread (soft fail)
        atr_pct=2.5,
        rsi_1h=72,  # RSI extreme but not extreme enough for hard fail
    )

    result = await run_pre_filter_async(signal, market)

    print("\nScenario: Orderbook imbalance 0.34 against LONG")
    print(f"  passed: {result.passed}")
    print(f"  score: {result.score:.1f}")
    print(f"  reason: {result.reason[:200]}...")
    print("\n  Checks:")

    for name, check in result.checks.items():
        status = "PASS" if check.get("passed", True) else "FAIL"
        soft = " [SOFT]" if check.get("soft_fail", False) else ""
        missing = " [MISSING]" if check.get("missing_data", False) else ""
        print(f"    {name}: {status}{soft}{missing}")

    # Count hard fails vs soft fails
    hard_fail_count = sum(1 for c in result.checks.values() if not c.get("passed", True) and not c.get("disabled", False) and not c.get("soft_fail", False))
    soft_fail_count = sum(1 for c in result.checks.values() if c.get("soft_fail", False))

    print(f"\n  Hard fails: {hard_fail_count}")
    print(f"  Soft fails: {soft_fail_count}")

    if result.passed:
        print("\n✓ CORRECT: Signal passed despite soft fails")
        print("  Orderbook imbalance is now soft fail, not blocking signal")
    else:
        print("\n✗ ERROR: Signal was blocked!")
        print("  Check if orderbook_imbalance still has soft_fail=False")

    # Scenario 2: Daily limit (should be hard fail)
    print("\n" + "=" * 60)
    print("Scenario 2: Daily trade limit (should be HARD FAIL)")
    print("=" * 60)

    # This should be blocked due to daily limit

    from pre_filter import _daily_trade_count, _state_lock

    # Simulate max daily trades reached
    with _state_lock:
        _daily_trade_count["test_user"] = 50  # Max trades

    signal2 = TradingViewSignal(
        secret="test",
        ticker="BTCUSDT",
        exchange="BINANCE",
        direction=SignalDirection.LONG,
        price=50000.0,
        timeframe="60",
        strategy="test",
        message="",
    )

    market2 = MarketContext(
        ticker="BTCUSDT",
        current_price=50000.0,
        volume_24h=1_000_000_000,
    )

    result2 = await run_pre_filter_async(signal2, market2, max_daily_trades=50, user_id="test_user")

    print(f"  passed: {result2.passed}")
    print(f"  reason: {result2.reason[:100]}...")

    if not result2.passed:
        print("\n✓ CORRECT: Daily limit is HARD FAIL (blocks signal)")
    else:
        print("\n✗ ERROR: Daily limit should block signal!")

    print("\n" + "=" * 60)
    print("Verification Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_soft_fail_scenarios())
