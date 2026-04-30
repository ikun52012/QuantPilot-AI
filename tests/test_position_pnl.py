"""
Position PnL calculation tests.

Uses core.database async SQLAlchemy for position tracking and PnL calculation.
"""
import os
import sys
import unittest

try:
    import cryptography  # noqa: F401
    import loguru  # noqa: F401
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"runtime dependency not installed: {exc.name}") from exc

os.environ.setdefault("APP_ENCRYPTION_KEY", "test-only-fernet-key-do-not-use")

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import PositionModel, insert_trade_log_async


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


@pytest.mark.asyncio
async def test_close_long_matches_aliased_symbol_even_when_newer_unrelated_position_exists(db_session: AsyncSession):
    """Closing should scan recent positions until it finds the aliased match."""
    target_open = {
        "id": "open-target",
        "timestamp": "2026-04-21T00:00:00+00:00",
        "user_id": "user-1",
        "ticker": "ETH/USDT:USDT",
        "direction": "long",
        "execute": True,
        "entry_price": 100.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 100.0, "quantity": 1.0},
    }
    unrelated_open = {
        "id": "open-other",
        "timestamp": "2026-04-21T00:01:00+00:00",
        "user_id": "user-1",
        "ticker": "BTCUSDT",
        "direction": "long",
        "execute": True,
        "entry_price": 50000.0,
        "quantity": 0.1,
        "order_status": "simulated",
        "order_details": {"entry_price": 50000.0, "quantity": 0.1},
    }
    await insert_trade_log_async(db_session, target_open)
    await insert_trade_log_async(db_session, unrelated_open)
    await db_session.flush()

    close_entry = {
        "id": "close-target",
        "timestamp": "2026-04-21T00:05:00+00:00",
        "user_id": "user-1",
        "ticker": "ETHUSDT.P",
        "direction": "close_long",
        "execute": True,
        "entry_price": 110.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 110.0, "quantity": 1.0},
    }
    result = await insert_trade_log_async(db_session, close_entry)

    assert result["position_event"] == "closed"
    assert abs(result.get("pnl_pct", 0) - 10.0) < 0.01

    positions = {
        row.ticker: row
        for row in (await db_session.execute(select(PositionModel))).scalars().all()
    }
    assert positions["ETH/USDT:USDT"].status == "closed"
    assert positions["BTCUSDT"].status == "open"
