#!/usr/bin/env python3
"""
Diagnostic script to check the pnl_pct issue for COINUSDT.P and LINKUSDT.P.

This script checks:
1. TradeModel records for these tickers
2. PositionModel records for these tickers
3. The actual pnl_pct values stored
4. The order_status of each record
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta

from sqlalchemy import select

from core.database import PositionModel, TradeModel, db_manager


def utcnow():
    return datetime.utcnow()


async def diagnose_pnl_issue():
    """Check COINUSDT.P and LINKUSDT.P records."""

    # Initialize database manager
    await db_manager.init()

    tickers_to_check = ["COINUSDT.P", "LINKUSDT.P"]
    cutoff = utcnow() - timedelta(days=7)  # Last 7 days

    async with db_manager.async_session_factory() as session:
        for ticker in tickers_to_check:
            print(f"\n{'='*60}")
            print(f"Checking {ticker}")
            print(f"{'='*60}\n")

            # Check TradeModel records
            print("TradeModel Records:")
            print("-" * 60)
            result = await session.execute(
                select(TradeModel)
                .where(TradeModel.ticker == ticker)
                .where(TradeModel.timestamp >= cutoff)
                .order_by(TradeModel.timestamp.desc())
                .limit(5)
            )
            trades = result.scalars().all()

            if not trades:
                print(f"No TradeModel records found for {ticker} in the last 7 days\n")
            else:
                for t in trades:
                    print(f"ID: {t.id}")
                    print(f"Timestamp: {t.timestamp}")
                    print(f"Direction: {t.direction}")
                    print(f"Order Status: {t.order_status}")
                    print(f"PnL Pct: {t.pnl_pct}%")  # ← THIS IS THE KEY FIELD
                    print(f"Execute: {t.execute}")

                    # Parse payload to see what's stored
                    import json
                    try:
                        payload = json.loads(t.payload_json) if t.payload_json else {}
                        signal = payload.get("signal", {})
                        analysis = payload.get("analysis", {})
                        result_data = payload.get("result", {})

                        print(f"Signal Price: {signal.get('price')}")
                        print(f"AI Confidence: {analysis.get('confidence')}")
                        print(f"AI Recommendation: {analysis.get('recommendation')}")
                        print(f"AI suggested TP1: {analysis.get('suggested_tp1')}")
                        print(f"AI suggested TP2: {analysis.get('suggested_tp2')}")
                        print(f"Result Entry Price: {result_data.get('entry_price')}")
                        print(f"Result Order ID: {result_data.get('order_id')}")
                    except Exception as e:
                        print(f"Payload parsing error: {e}")

                    print("-" * 60)

            # Check PositionModel records
            print("\nPositionModel Records:")
            print("-" * 60)
            result = await session.execute(
                select(PositionModel)
                .where(PositionModel.ticker == ticker)
                .where(PositionModel.status.in_(["open", "pending"]))
                .order_by(PositionModel.opened_at.desc())
                .limit(5)
            )
            positions = result.scalars().all()

            if not positions:
                print(f"No open PositionModel records for {ticker}\n")
            else:
                for p in positions:
                    print(f"ID: {p.id}")
                    print(f"Status: {p.status}")
                    print(f"Direction: {p.direction}")
                    print(f"Entry Price: {p.entry_price}")
                    print(f"Last Price: {p.last_price}")
                    print(f"Quantity: {p.quantity}")
                    print(f"Leverage: {p.leverage}")
                    print(f"Current PnL Pct: {p.current_pnl_pct}%")  # ← Unrealized PnL
                    print(f"Realized PnL Pct: {p.realized_pnl_pct}%")  # ← Partial TP hits
                    print(f"PnL Pct: {p.pnl_pct}%")  # ← Should be 0 for open positions
                    print(f"Stop Loss: {p.stop_loss}")

                    # Calculate expected unrealized PnL
                    if p.last_price and p.entry_price:
                        if p.direction == "long":
                            expected_pnl = ((p.last_price - p.entry_price) / p.entry_price) * 100 * p.leverage
                        else:
                            expected_pnl = ((p.entry_price - p.last_price) / p.entry_price) * 100 * p.leverage
                        print(f"Expected Current PnL: {expected_pnl:.2f}%")

                    print("-" * 60)

    print("\n" + "=" * 60)
    print("Diagnosis Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(diagnose_pnl_issue())
