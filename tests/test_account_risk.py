import pytest

import core.account_risk as account_risk
from core.account_risk import (
    check_account_loss_limits,
    get_account_risk_status,
    record_position_pnl,
    reset_account_tracker,
)


@pytest.fixture(autouse=True)
async def clean_tracker():
    await reset_account_tracker("test_user")
    await reset_account_tracker(None)
    yield
    await reset_account_tracker("test_user")
    await reset_account_tracker(None)


@pytest.mark.asyncio
async def test_record_position_pnl_accumulates_decimals():
    # Record a pnl of 10.123456 USDT
    res = await record_position_pnl(
        user_id="test_user",
        pnl_pct=2.0,
        pnl_usdt=10.123456,
        equity_usdt=1000.0
    )

    assert res["positions_closed"] == 1
    assert res["daily_pnl_usdt"] == 10.123456

    # Record another pnl of -5.000001 USDT
    res2 = await record_position_pnl(
        user_id="test_user",
        pnl_pct=-1.0,
        pnl_usdt=-5.000001,
        equity_usdt=1000.0
    )

    assert res2["positions_closed"] == 2
    # 10.123456 - 5.000001 = 5.123455
    assert abs(res2["daily_pnl_usdt"] - 5.123455) < 1e-9

    # Verify status retrieval
    status = get_account_risk_status("test_user")
    assert status["positions_closed"] == 2
    assert abs(status["daily_pnl_usdt"] - 5.123455) < 1e-9


@pytest.mark.asyncio
async def test_check_account_loss_limits():
    # Under limit (daily pnl is 0)
    allowed, reason = await check_account_loss_limits(
        user_id="test_user",
        account_equity_usdt=1000.0,
        max_daily_loss_pct=5.0
    )
    assert allowed is True
    assert reason == ""

    # Record a loss of -51.0 USDT (5.1% of 1000.0 equity)
    await record_position_pnl(
        user_id="test_user",
        pnl_pct=-10.0,
        pnl_usdt=-51.0,
        equity_usdt=1000.0
    )

    # Check limit - should be blocked
    allowed, reason = await check_account_loss_limits(
        user_id="test_user",
        account_equity_usdt=1000.0,
        max_daily_loss_pct=5.0
    )
    assert allowed is False
    assert "daily loss limit exceeded" in reason.lower()

    # Check with higher limit (e.g. 6.0%) - should be allowed
    allowed_higher, reason_higher = await check_account_loss_limits(
        user_id="test_user",
        account_equity_usdt=1000.0,
        max_daily_loss_pct=6.0
    )
    assert allowed_higher is True
    assert reason_higher == ""


@pytest.mark.asyncio
async def test_cumulative_loss_limit_applies_after_new_day():
    account_risk._ACCOUNT_DAILY_TRACKER["test_user"] = {
        "date": "2000-01-01",
        "daily_pnl_usdt": -1.0,
        "cumulative_pnl_usdt": -60.0,
        "positions_closed": 1,
        "limit_triggered": False,
        "account_equity_usdt": 1000.0,
    }

    allowed, reason = await check_account_loss_limits(
        user_id="test_user",
        account_equity_usdt=1000.0,
        max_daily_loss_pct=5.0,
        max_total_loss_pct=5.0,
    )

    assert allowed is False
    assert "cumulative loss limit exceeded" in reason.lower()


@pytest.mark.asyncio
async def test_reset_account_tracker_persists_to_disk(monkeypatch, tmp_path):
    tracker_path = tmp_path / "account_daily_tracker.json"
    monkeypatch.setattr(account_risk, "_ACCOUNT_TRACKER_FILE", tracker_path)
    account_risk._ACCOUNT_DAILY_TRACKER["test_user"] = {
        "date": "2000-01-01",
        "daily_pnl_usdt": -10.0,
        "cumulative_pnl_usdt": -10.0,
        "positions_closed": 1,
        "limit_triggered": False,
        "account_equity_usdt": 1000.0,
    }
    account_risk._save_tracker_to_disk()

    await reset_account_tracker("test_user")

    assert "test_user" not in account_risk._ACCOUNT_DAILY_TRACKER
    assert "test_user" not in tracker_path.read_text(encoding="utf-8")
