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
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger

from core.utils.datetime import utcnow
from core.utils.decimal_utils import MoneyAmount, safe_decimal

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
            # Parse numeric fields to Decimal internally
            parsed_data = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    item = v.copy()
                    item["daily_pnl_usdt"] = safe_decimal(item.get("daily_pnl_usdt", 0.0))
                    item["cumulative_pnl_usdt"] = safe_decimal(item.get("cumulative_pnl_usdt", 0.0))
                    item["account_equity_usdt"] = safe_decimal(item.get("account_equity_usdt", 0.0))
                    parsed_data[k] = item
                else:
                    parsed_data[k] = v
            logger.info(f"[AccountRisk] Loaded tracker from disk: {len(parsed_data)} entries")
            return parsed_data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[AccountRisk] Failed to load tracker from disk: {e}")
    return {}


def _save_tracker_to_disk() -> None:
    """C5-FIX: Persist tracker state to disk. Converts Decimal to float for JSON compatibility."""
    try:
        serializable_tracker = {}
        for k, v in _ACCOUNT_DAILY_TRACKER.items():
            if isinstance(v, dict):
                item = v.copy()
                item["daily_pnl_usdt"] = float(safe_decimal(item.get("daily_pnl_usdt", 0.0)))
                item["cumulative_pnl_usdt"] = float(safe_decimal(item.get("cumulative_pnl_usdt", 0.0)))
                item["account_equity_usdt"] = float(safe_decimal(item.get("account_equity_usdt", 0.0)))
                serializable_tracker[k] = item
            else:
                serializable_tracker[k] = v
        _ACCOUNT_TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ACCOUNT_TRACKER_FILE.write_text(
            json.dumps(serializable_tracker, indent=2),
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
            # P1-FIX: Carry over cumulative_pnl_usdt to new day to prevent daily reset bypass
            prev_cumulative = safe_decimal(tracker.get("cumulative_pnl_usdt", 0.0) if tracker else 0.0)
            tracker = {
                "date": today,
                "daily_pnl_usdt": Decimal("0.0"),
                "cumulative_pnl_usdt": prev_cumulative,
                "positions_closed": 0,
                "limit_triggered": False,
                "account_equity_usdt": Decimal("0.0"),
            }
            _ACCOUNT_DAILY_TRACKER[key] = tracker
        else:
            # Ensure existing values are Decimals
            tracker["daily_pnl_usdt"] = safe_decimal(tracker.get("daily_pnl_usdt", 0.0))
            tracker["cumulative_pnl_usdt"] = safe_decimal(tracker.get("cumulative_pnl_usdt", 0.0))
            tracker["account_equity_usdt"] = safe_decimal(tracker.get("account_equity_usdt", 0.0))

        pnl_dec = safe_decimal(pnl_usdt)
        daily_amount = MoneyAmount(tracker["daily_pnl_usdt"]) + pnl_dec
        cumulative_amount = MoneyAmount(tracker["cumulative_pnl_usdt"]) + pnl_dec

        tracker["daily_pnl_usdt"] = daily_amount.value
        tracker["cumulative_pnl_usdt"] = cumulative_amount.value
        tracker["positions_closed"] += 1

        if equity_usdt > 0:
            equity_dec = safe_decimal(equity_usdt)
            tracker["account_equity_usdt"] = max(equity_dec, tracker["account_equity_usdt"])

        daily_pnl_val = float(tracker["daily_pnl_usdt"])
        logger.info(
            f"[AccountRisk] {key} daily PnL: {daily_pnl_val:+.2f} USDT "
            f"after position close (position PnL: {pnl_pct:+.2f}%, {pnl_usdt:+.2f} USDT)"
        )

        _save_tracker_to_disk()

        # Return float-based dict copy for backward compatibility
        ret = tracker.copy()
        ret["daily_pnl_usdt"] = float(ret["daily_pnl_usdt"])
        ret["cumulative_pnl_usdt"] = float(ret["cumulative_pnl_usdt"])
        ret["account_equity_usdt"] = float(ret["account_equity_usdt"])
        return ret


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
        if tracker is None:
            return (True, "")

        daily_pnl_usdt = safe_decimal(tracker.get("daily_pnl_usdt", 0.0)) if tracker.get("date") == today else Decimal("0.0")
        cumulative_pnl_usdt = safe_decimal(tracker.get("cumulative_pnl_usdt", 0.0))

    equity_dec = safe_decimal(account_equity_usdt)
    if equity_dec <= 0:
        logger.warning(f"[AccountRisk] {key} account equity is 0, skipping loss limit check")
        return (True, "")

    stored_equity = safe_decimal(tracker.get("account_equity_usdt", 0.0) if tracker else 0.0)
    effective_equity = max(equity_dec, stored_equity) if stored_equity > 0 else equity_dec

    daily_pnl_pct = daily_pnl_usdt / effective_equity * Decimal("100.0")
    cumulative_pnl_pct = cumulative_pnl_usdt / effective_equity * Decimal("100.0")

    max_daily_loss_dec = safe_decimal(max_daily_loss_pct)

    if max_daily_loss_dec > 0 and daily_pnl_usdt < 0:
        daily_loss_pct = abs(daily_pnl_pct)
        if daily_loss_pct >= max_daily_loss_dec:
            logger.warning(
                f"[AccountRisk] BLOCKED: {key} daily loss {float(daily_loss_pct):.2f}% "
                f"({float(abs(daily_pnl_usdt)):.2f} USDT / {float(effective_equity):.2f} USDT equity) "
                f"exceeds limit {float(max_daily_loss_dec):.2f}%"
            )
            return (
                False,
                f"Account daily loss limit exceeded: {float(daily_loss_pct):.2f}% "
                f"({float(abs(daily_pnl_usdt)):.2f} USDT loss / {float(effective_equity):.2f} USDT equity) >= {float(max_daily_loss_dec):.2f}%. "
                f"Trading paused until next day.",
            )

    if max_total_loss_pct:
        max_total_loss_dec = safe_decimal(max_total_loss_pct)
        if max_total_loss_dec > 0 and cumulative_pnl_usdt < 0:
            total_loss_pct = abs(cumulative_pnl_pct)
            if total_loss_pct >= max_total_loss_dec:
                logger.warning(
                    f"[AccountRisk] BLOCKED: {key} cumulative loss {float(total_loss_pct):.2f}% "
                    f"({float(abs(cumulative_pnl_usdt)):.2f} USDT / {float(effective_equity):.2f} USDT equity) "
                    f"exceeds limit {float(max_total_loss_dec):.2f}%"
                )
                return (
                    False,
                    f"Account cumulative loss limit exceeded: {float(total_loss_pct):.2f}% "
                    f"({float(abs(cumulative_pnl_usdt)):.2f} USDT loss / {float(effective_equity):.2f} USDT equity) >= {float(max_total_loss_dec):.2f}%. "
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
    ret = tracker.copy()
    ret["daily_pnl_usdt"] = float(ret.get("daily_pnl_usdt", 0.0))
    ret["cumulative_pnl_usdt"] = float(ret.get("cumulative_pnl_usdt", 0.0))
    ret["account_equity_usdt"] = float(ret.get("account_equity_usdt", 0.0))
    return ret


async def reset_account_tracker(user_id: str | None = None) -> None:
    """Reset account tracker (e.g., after manual admin approval)."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    async with _ACCOUNT_TRACKER_GUARD:
        _ACCOUNT_DAILY_TRACKER.pop(key, None)
        _save_tracker_to_disk()
    logger.info(f"[AccountRisk] Tracker reset for {key}")
