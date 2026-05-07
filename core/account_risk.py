"""
QuantPilot AI - Account-Level Risk Management

Tracks daily and cumulative account PnL to enforce account-level stop-loss limits.
When limits are breached, new trades are blocked until the next trading day.

FIX: Daily loss % should be calculated relative to account equity, NOT by summing
individual position PnL percentages (which are relative to position margin).

C5-FIX: Persist daily tracker to disk to survive server restarts.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from core.utils.common import safe_float
from core.utils.datetime import utcnow

_ACCOUNT_DAILY_TRACKER: dict[str, dict[str, Any]] = {}
_ACCOUNT_TRACKER_GUARD = asyncio.Lock()
_ACCOUNT_TRACKER_FILE = Path(__file__).parent.parent / "data" / "account_risk_tracker.json"

_GLOBAL_ACCOUNT_KEY = "__global__"


def _load_tracker_from_disk() -> dict[str, dict[str, Any]]:
    """C5-FIX: Load tracker state from disk on startup."""
    if not _ACCOUNT_TRACKER_FILE.exists():
        return {}
    try:
        data = json.loads(_ACCOUNT_TRACKER_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            logger.info(f"[AccountRisk] Loaded tracker from disk: {len(data)} entries")
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[AccountRisk] Failed to load tracker from disk: {e}")
    return {}


def _save_tracker_to_disk() -> None:
    """C5-FIX: Persist tracker state to disk."""
    try:
        _ACCOUNT_TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ACCOUNT_TRACKER_FILE.write_text(
            json.dumps(_ACCOUNT_DAILY_TRACKER, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"[AccountRisk] Failed to save tracker to disk: {e}")


_ACCOUNT_DAILY_TRACKER.update(_load_tracker_from_disk())


async def record_position_pnl(
    user_id: str | None,
    pnl_pct: float,
    pnl_usdt: float,
    equity_usdt: float = 0.0,
) -> dict[str, Any]:
    """Record realized PnL from a closed position into the daily tracker.

    FIX: Only accumulate USDT amounts. Daily PnL % is calculated relative to account
    equity when checking limits, NOT by summing position-level percentages.

    Args:
        pnl_pct: Position-level PnL % (relative to position margin, NOT account equity)
        pnl_usdt: Actual USDT profit/loss amount
        equity_usdt: Account equity at time of position close (for tracking)

    Returns the updated tracker state for the user.
    """
    key = user_id or _GLOBAL_ACCOUNT_KEY
    today = utcnow().strftime("%Y-%m-%d")

    async with _ACCOUNT_TRACKER_GUARD:
        tracker = _ACCOUNT_DAILY_TRACKER.get(key)
        if tracker is None or tracker.get("date") != today:
            tracker = {
                "date": today,
                "daily_pnl_usdt": 0.0,
                "cumulative_pnl_usdt": 0.0,
                "positions_closed": 0,
                "limit_triggered": False,
                "account_equity_usdt": 0.0,
            }
            _ACCOUNT_DAILY_TRACKER[key] = tracker

        tracker["daily_pnl_usdt"] = round(tracker["daily_pnl_usdt"] + safe_float(pnl_usdt, 0.0), 6)
        tracker["cumulative_pnl_usdt"] = round(tracker["cumulative_pnl_usdt"] + safe_float(pnl_usdt, 0.0), 6)
        tracker["positions_closed"] += 1

        if equity_usdt > 0:
            tracker["account_equity_usdt"] = max(equity_usdt, tracker.get("account_equity_usdt", 0.0))

        daily_pnl_usdt = tracker["daily_pnl_usdt"]
        logger.info(
            f"[AccountRisk] {key} daily PnL: {daily_pnl_usdt:+.2f} USDT "
            f"after position close (position PnL: {pnl_pct:+.2f}%, {pnl_usdt:+.2f} USDT)"
        )

        _save_tracker_to_disk()
        return tracker.copy()


async def check_account_loss_limits(
    user_id: str | None,
    account_equity_usdt: float,
    max_daily_loss_pct: float,
    max_total_loss_pct: float | None = None,
) -> tuple[bool, str]:
    """Check if account loss limits are breached.

    FIX: Calculate loss % relative to account equity, NOT by summing position %.

    Returns (allowed, reason) where allowed=True means trading can proceed.
    """
    key = user_id or _GLOBAL_ACCOUNT_KEY
    today = utcnow().strftime("%Y-%m-%d")

    async with _ACCOUNT_TRACKER_GUARD:
        tracker = _ACCOUNT_DAILY_TRACKER.get(key)
        if tracker is None or tracker.get("date") != today:
            return (True, "")

        daily_pnl_usdt = tracker.get("daily_pnl_usdt", 0.0)
        cumulative_pnl_usdt = tracker.get("cumulative_pnl_usdt", 0.0)

    if account_equity_usdt <= 0:
        logger.warning(f"[AccountRisk] {key} account equity is 0, skipping loss limit check")
        return (True, "")

    daily_pnl_pct = daily_pnl_usdt / account_equity_usdt * 100.0
    cumulative_pnl_pct = cumulative_pnl_usdt / account_equity_usdt * 100.0

    if max_daily_loss_pct > 0 and daily_pnl_usdt < 0:
        daily_loss_pct = abs(daily_pnl_pct)
        if daily_loss_pct >= max_daily_loss_pct:
            logger.warning(
                f"[AccountRisk] BLOCKED: {key} daily loss {daily_loss_pct:.2f}% "
                f"({abs(daily_pnl_usdt):.2f} USDT / {account_equity_usdt:.2f} USDT equity) "
                f"exceeds limit {max_daily_loss_pct:.2f}%"
            )
            return (
                False,
                f"Account daily loss limit exceeded: {daily_loss_pct:.2f}% "
                f"({abs(daily_pnl_usdt):.2f} USDT loss / {account_equity_usdt:.2f} USDT equity) >= {max_daily_loss_pct:.2f}%. "
                f"Trading paused until next day.",
            )

    if max_total_loss_pct and max_total_loss_pct > 0 and cumulative_pnl_usdt < 0:
        total_loss_pct = abs(cumulative_pnl_pct)
        if total_loss_pct >= max_total_loss_pct:
            logger.warning(
                f"[AccountRisk] BLOCKED: {key} cumulative loss {total_loss_pct:.2f}% "
                f"({abs(cumulative_pnl_usdt):.2f} USDT / {account_equity_usdt:.2f} USDT equity) "
                f"exceeds limit {max_total_loss_pct:.2f}%"
            )
            return (
                False,
                f"Account cumulative loss limit exceeded: {total_loss_pct:.2f}% "
                f"({abs(cumulative_pnl_usdt):.2f} USDT loss / {account_equity_usdt:.2f} USDT equity) >= {max_total_loss_pct:.2f}%. "
                f"Trading paused. Reset required.",
            )

    return (True, "")


def get_account_risk_status(user_id: str | None = None) -> dict[str, Any]:
    """Get current account risk status for monitoring/dashboards."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    today = utcnow().strftime("%Y-%m-%d")
    tracker = _ACCOUNT_DAILY_TRACKER.get(key)
    if tracker is None or tracker.get("date") != today:
        return {
            "date": today,
            "daily_pnl_usdt": 0.0,
            "cumulative_pnl_usdt": 0.0,
            "positions_closed": 0,
            "limit_triggered": False,
            "account_equity_usdt": 0.0,
        }
    return tracker.copy()


async def reset_account_tracker(user_id: str | None = None) -> None:
    """Reset account tracker (e.g., after manual admin approval)."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    async with _ACCOUNT_TRACKER_GUARD:
        _ACCOUNT_DAILY_TRACKER.pop(key, None)
    logger.info(f"[AccountRisk] Tracker reset for {key}")
