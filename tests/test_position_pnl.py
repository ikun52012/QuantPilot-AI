"""
Position PnL calculation tests.

Uses core.database async SQLAlchemy for position tracking and PnL calculation.
"""
import json
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

from analytics import calculate_performance
from core.config import settings
from core.database import PositionModel, TradeModel, insert_trade_log_async, record_position_partial_close_trade_async
from core.utils.datetime import utcnow


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


@pytest.mark.asyncio
async def test_performance_profit_uses_principal_based_pnl_usdt(db_session: AsyncSession, monkeypatch):
    """Winning trades should be scaled by account equity, not summed position PnL %."""
    monkeypatch.setattr(settings.risk, "account_equity_usdt", 1000.0)

    open_entry = {
        "id": "open-principal-profit",
        "timestamp": utcnow().isoformat(),
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
        "id": "close-principal-profit",
        "timestamp": utcnow().isoformat(),
        "ticker": "ETHUSDT",
        "direction": "close_long",
        "execute": True,
        "entry_price": 110.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 110.0, "quantity": 1.0},
    }
    await insert_trade_log_async(db_session, close_entry)

    close_trade = await db_session.scalar(select(TradeModel).where(TradeModel.id == "close-principal-profit"))
    assert close_trade is not None
    assert close_trade.pnl_pct == pytest.approx(10.0)
    assert close_trade.pnl_usdt == pytest.approx(10.0)

    perf = await calculate_performance(db_session, days=365)

    assert perf["total_pnl_pct"] == pytest.approx(1.0)
    assert perf["avg_win_pct"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_performance_uses_payload_pnl_usdt_for_existing_logs(db_session: AsyncSession, monkeypatch):
    """Existing logs with pnl_usdt only in payload should still use principal-based PnL."""
    monkeypatch.setattr(settings.risk, "account_equity_usdt", 1000.0)
    db_session.add(TradeModel(
        id="legacy-close-profit",
        timestamp=utcnow(),
        ticker="ETHUSDT",
        direction="close_long",
        execute=True,
        order_status="closed",
        pnl_pct=10.0,
        pnl_usdt=0.0,
        payload_json=json.dumps({"position_event": "closed", "pnl_usdt": 10.0}),
    ))
    await db_session.flush()

    perf = await calculate_performance(db_session, days=365)

    assert perf["total_pnl_pct"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_partial_close_records_principal_based_pnl_event(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings.risk, "account_equity_usdt", 1000.0)
    open_entry = {
        "id": "open-partial-profit",
        "timestamp": utcnow().isoformat(),
        "ticker": "ETHUSDT",
        "direction": "long",
        "execute": True,
        "entry_price": 100.0,
        "quantity": 1.0,
        "order_status": "simulated",
        "order_details": {"entry_price": 100.0, "quantity": 1.0},
    }
    await insert_trade_log_async(db_session, open_entry)
    position = await db_session.scalar(select(PositionModel).where(PositionModel.open_trade_id == "open-partial-profit"))
    assert position is not None

    pnl_pct, pnl_usdt, remaining_qty = await record_position_partial_close_trade_async(
        db_session,
        position,
        exit_price=110.0,
        close_quantity=0.5,
        close_reason="manual_partial_close",
    )

    assert pnl_pct == pytest.approx(5.0)
    assert pnl_usdt == pytest.approx(5.0)
    assert remaining_qty == pytest.approx(0.5)

    perf = await calculate_performance(db_session, days=365)
    assert perf["total_pnl_pct"] == pytest.approx(0.5)
