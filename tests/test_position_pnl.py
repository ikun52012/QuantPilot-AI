"""
Position PnL calculation tests.

Uses core.database async SQLAlchemy for position tracking and PnL calculation.
"""
import os
import sys
import unittest

try:
    import loguru
    import cryptography
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"runtime dependency not installed: {exc.name}")

os.environ.setdefault("APP_ENCRYPTION_KEY", "test-only-fernet-key-do-not-use")

import asyncio
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import insert_trade_log_async, sync_position_from_trade_entry_async


@pytest.mark.asyncio
async def test_close_long_updates_realized_pnl(db_session: AsyncSession):
    """Opening a long then closing it should compute ~10% PnL."""
    open_entry = {
        "id": "open-1",
        "timestamp": "2026-04-21T00:00:00+00:00",
        "user_id": "user-1",
        "ticker": "ETHUSDT",
        "direction": "long",
        "execute": True,
        "entry_price": 100.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 100.0, "quantity": 1.0},
    }
    await insert_trade_log_async(db_session, open_entry)
    await db_session.flush()

    close_entry = {
        "id": "close-1",
        "timestamp": "2026-04-21T00:05:00+00:00",
        "user_id": "user-1",
        "ticker": "ETHUSDT",
        "direction": "close_long",
        "execute": True,
        "entry_price": 110.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 110.0, "quantity": 1.0},
    }
    result = await insert_trade_log_async(db_session, close_entry)
    assert abs(result.get("pnl_pct", 0) - 10.0) < 0.01
