"""
QuantPilot AI - Account-Level Risk Management

Tracks daily and cumulative account PnL to enforce account-level stop-loss limits.
When limits are breached, new trades are blocked until the next trading day.
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from core.utils.common import safe_float
from core.utils.datetime import utcnow

# ─────────────────────────────────────────────
# In-memory daily PnL trackers
# ─────────────────────────────────────────────

# user_id -> {"date": "YYYY-MM-DD", "daily_pnl_pct": float, "daily_pnl_usdt": float}
_ACCOUNT_DAILY_TRACKER: dict[str, dict[str, Any]] = {}
_ACCOUNT_TRACKER_GUARD = asyncio.Lock()

# Global (admin/no-user) tracker key
_GLOBAL_ACCOUNT_KEY = "__global__"


async def record_position_pnl(
    user_id: str | None,
    pnl_pct: float,
    pnl_usdt: float,
    equity_usdt: float = 0.0,
) -> dict[str, Any]:
    """Record realized PnL from a closed position into the daily tracker.

    Returns the updated tracker state for the user.
    """
    key = user_id or _GLOBAL_ACCOUNT_KEY
    today = utcnow().strftime("%Y-%m-%d")

    async with _ACCOUNT_TRACKER_GUARD:
        tracker = _ACCOUNT_DAILY_TRACKER.get(key)
        if tracker is None or tracker.get("date") != today:
            # Reset for new day
            tracker = {
                "date": today,
                "daily_pnl_pct": 0.0,
                "daily_pnl_usdt": 0.0,
                "cumulative_pnl_pct": 0.0,
                "positions_closed": 0,
                "limit_triggered": False,
            }
            _ACCOUNT_DAILY_TRACKER[key] = tracker

        tracker["daily_pnl_pct"] = round(tracker["daily_pnl_pct"] + safe_float(pnl_pct, 0.0), 6)
        tracker["daily_pnl_usdt"] = round(tracker["daily_pnl_usdt"] + safe_float(pnl_usdt, 0.0), 6)
        tracker["cumulative_pnl_pct"] = round(tracker["cumulative_pnl_pct"] + safe_float(pnl_pct, 0.0), 6)
        tracker["positions_closed"] += 1

        # Check if limit just breached
        if equity_usdt > 0:
            daily_loss_pct = abs(min(0, tracker["daily_pnl_pct"]))
            if daily_loss_pct > 0:
                logger.info(
                    f"[AccountRisk] {key} daily PnL: {tracker['daily_pnl_pct']:+.4f}% "
                    f"({tracker['daily_pnl_usdt']:+.2f} USDT) after position close"
                )

        return tracker.copy()


async def check_account_loss_limits(
    user_id: str | None,
    account_equity_usdt: float,
    max_daily_loss_pct: float,
    max_total_loss_pct: float | None = None,
) -> tuple[bool, str]:
    """Check if account loss limits are breached.

    Returns (allowed, reason) where allowed=True means trading can proceed.
    """
    key = user_id or _GLOBAL_ACCOUNT_KEY
    today = utcnow().strftime("%Y-%m-%d")

    async with _ACCOUNT_TRACKER_GUARD:
        tracker = _ACCOUNT_DAILY_TRACKER.get(key)
        if tracker is None or tracker.get("date") != today:
            # No trades today yet, or new day
            return (True, "")

        daily_pnl_pct = tracker.get("daily_pnl_pct", 0.0)
        cumulative_pnl_pct = tracker.get("cumulative_pnl_pct", 0.0)

    # Daily loss limit check
    if max_daily_loss_pct > 0 and daily_pnl_pct < 0:
        daily_loss_pct = abs(daily_pnl_pct)
        if daily_loss_pct >= max_daily_loss_pct:
            logger.warning(
                f"[AccountRisk] BLOCKED: {key} daily loss {daily_loss_pct:.2f}% "
                f"exceeds limit {max_daily_loss_pct:.2f}%"
            )
            return (
                False,
                f"Account daily loss limit exceeded: {daily_loss_pct:.2f}% >= {max_daily_loss_pct:.2f}%. "
                f"Trading paused until next day.",
            )

    # Total/cumulative loss limit check
    if max_total_loss_pct and max_total_loss_pct > 0 and cumulative_pnl_pct < 0:
        total_loss_pct = abs(cumulative_pnl_pct)
        if total_loss_pct >= max_total_loss_pct:
            logger.warning(
                f"[AccountRisk] BLOCKED: {key} cumulative loss {total_loss_pct:.2f}% "
                f"exceeds limit {max_total_loss_pct:.2f}%"
            )
            return (
                False,
                f"Account cumulative loss limit exceeded: {total_loss_pct:.2f}% >= {max_total_loss_pct:.2f}%. "
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
            "daily_pnl_pct": 0.0,
            "daily_pnl_usdt": 0.0,
            "cumulative_pnl_pct": 0.0,
            "positions_closed": 0,
            "limit_triggered": False,
        }
    return tracker.copy()


async def reset_account_tracker(user_id: str | None = None) -> None:
    """Reset account tracker (e.g., after manual admin approval)."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    async with _ACCOUNT_TRACKER_GUARD:
        _ACCOUNT_DAILY_TRACKER.pop(key, None)
    logger.info(f"[AccountRisk] Tracker reset for {key}")
