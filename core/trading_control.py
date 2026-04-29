"""Global trading control state and kill-switch helpers."""
import json

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_admin_setting, set_admin_setting
from core.utils.datetime import utcnow

TRADING_CONTROL_MODE_KEY = "trading_control_mode"
TRADING_CONTROL_REASON_KEY = "trading_control_reason"
TRADING_CONTROL_UPDATED_BY_KEY = "trading_control_updated_by"
TRADING_CONTROL_UPDATED_AT_KEY = "trading_control_updated_at"

TRADING_MODES = {"enabled", "read_only", "paused", "emergency_stop"}
BLOCKING_MODES = {"read_only", "paused", "emergency_stop"}


def _normalize_mode(mode: str) -> str:
    mode = str(mode or "enabled").strip().lower()
    if mode not in TRADING_MODES:
        raise HTTPException(400, f"Invalid trading control mode: {mode}")
    return mode


async def get_trading_control_state(session: AsyncSession) -> dict:
    """Return current global trading control state."""
    mode = await get_admin_setting(session, TRADING_CONTROL_MODE_KEY, "enabled")
    if mode not in TRADING_MODES:
        mode = "enabled"

    reason = await get_admin_setting(session, TRADING_CONTROL_REASON_KEY, "")
    updated_by = await get_admin_setting(session, TRADING_CONTROL_UPDATED_BY_KEY, "")
    updated_at = await get_admin_setting(session, TRADING_CONTROL_UPDATED_AT_KEY, "")

    return {
        "mode": mode,
        "allowed": mode == "enabled",
        "live_allowed": mode == "enabled",
        "read_only": mode == "read_only",
        "paused": mode in {"paused", "emergency_stop"},
        "emergency_stop": mode == "emergency_stop",
        "reason": reason,
        "updated_by": updated_by,
        "updated_at": updated_at,
    }


async def set_trading_control_state(
    session: AsyncSession,
    mode: str,
    reason: str = "",
    updated_by: str = "",
) -> dict:
    """Persist global trading control mode. Caller owns commit."""
    normalized = _normalize_mode(mode)
    now = utcnow().isoformat()

    await set_admin_setting(session, TRADING_CONTROL_MODE_KEY, normalized)
    await set_admin_setting(session, TRADING_CONTROL_REASON_KEY, str(reason or ""))
    await set_admin_setting(session, TRADING_CONTROL_UPDATED_BY_KEY, str(updated_by or ""))
    await set_admin_setting(session, TRADING_CONTROL_UPDATED_AT_KEY, now)

    return {
        "mode": normalized,
        "allowed": normalized == "enabled",
        "live_allowed": normalized == "enabled",
        "read_only": normalized == "read_only",
        "paused": normalized in {"paused", "emergency_stop"},
        "emergency_stop": normalized == "emergency_stop",
        "reason": str(reason or ""),
        "updated_by": str(updated_by or ""),
        "updated_at": now,
    }


async def trading_allowed(
    session: AsyncSession,
    user_id: str | None = None,
    live_trading: bool = False,
) -> dict:
    """Return whether a new trade may be placed right now."""
    state = await get_trading_control_state(session)
    mode = state.get("mode", "enabled")
    if mode not in BLOCKING_MODES:
        return {**state, "allowed": True, "block_reason": ""}

    reason = state.get("reason") or {
        "read_only": "Trading is in read-only mode",
        "paused": "Trading is paused",
        "emergency_stop": "Emergency stop is active",
    }.get(mode, "Trading is disabled")

    return {
        **state,
        "allowed": False,
        "block_reason": reason,
        "user_id": user_id,
        "live_trading": bool(live_trading),
    }


async def assert_trading_allowed(
    session: AsyncSession,
    user_id: str | None = None,
    live_trading: bool = False,
) -> dict:
    """Raise 423 when trading is globally blocked."""
    state = await trading_allowed(session, user_id=user_id, live_trading=live_trading)
    if not state.get("allowed"):
        raise HTTPException(423, state.get("block_reason") or "Trading is currently disabled")
    return state


def serialize_control_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, default=str)
