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
import hashlib
import json
import os
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import DATA_DIR
from core.redis_coordination import (
    distributed_lock,
    make_key,
    redis_hdel,
    redis_hget_json,
    redis_hset_json,
)
from core.utils.datetime import utcnow
from core.utils.decimal_utils import MoneyAmount, safe_decimal

_ACCOUNT_DAILY_TRACKER: dict[str, dict[str, Any]] = {}
_ACCOUNT_TRACKER_GUARD = asyncio.Lock()
_ACCOUNT_TRACKER_FILE = DATA_DIR / "account_risk_tracker.json"
_RISK_FILE_LOCK = threading.RLock()

_GLOBAL_ACCOUNT_KEY = "__global__"

_DRAWDOWN_CIRCUIT_BREAKERS: dict[str, dict[str, Any]] = {}
_DRAWDOWN_CB_LOCK = asyncio.Lock()
_ACCOUNT_RISK_REDIS_HASH = make_key("risk", "account_tracker")
_DRAWDOWN_REDIS_HASH = make_key("risk", "drawdown")


def _atomic_write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    """Durably replace a JSON state file without exposing partial contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug(f"[AccountRisk] Could not remove temporary state file {temp_path}")


async def _check_drawdown_circuit_breaker_local(
    user_id: str | None,
    account_equity_usdt: float,
    rolling_1h_max_drawdown_pct: float = 5.0,
    rolling_4h_max_drawdown_pct: float = 10.0,
) -> tuple[bool, str]:
    if account_equity_usdt <= 0:
        return True, ""

    key = user_id or _GLOBAL_ACCOUNT_KEY
    now = utcnow()
    now_ts = now.timestamp()
    cutoff_1h = now_ts - 3600
    cutoff_4h = now_ts - 4 * 3600

    async with _DRAWDOWN_CB_LOCK:
        cb = _DRAWDOWN_CIRCUIT_BREAKERS.get(key)
        if cb is None:
            # Try to restore from disk first (Round-4 audit fix: state should
            # survive restarts, otherwise a restart wipes drawdown memory).
            cb = _load_drawdown_state_from_disk(key)
            if cb is None:
                cb = {"peaks": [], "drawdown_events": []}
            _DRAWDOWN_CIRCUIT_BREAKERS[key] = cb

        cb["peaks"].append({"time": now.isoformat(), "ts": now_ts, "equity": account_equity_usdt})
        # FIX (Round-4 audit P0): the previous filter was
        #   [p for p in cb["peaks"] if now.timestamp() - cutoff_4h < 4 * 3600]
        # which evaluates to (4*3600) < (4*3600) = False, so the condition was
        # ALWAYS False → the list was never trimmed → memory leak + the 1h
        # filter (same bug) included the entire history → "1h drawdown" was
        # effectively "all-time drawdown". Fix: properly filter by ts.
        cb["peaks"] = [p for p in cb["peaks"] if p.get("ts", 0) >= cutoff_4h]
        if len(cb["peaks"]) > 200:
            cb["peaks"] = cb["peaks"][-200:]

        recent_1h = [p for p in cb["peaks"] if p.get("ts", 0) >= cutoff_1h]
        if recent_1h:
            peak_1h = max(p["equity"] for p in recent_1h)
            if peak_1h > 0:
                drawdown_1h = (peak_1h - account_equity_usdt) / peak_1h * 100
                if drawdown_1h >= rolling_1h_max_drawdown_pct:
                    _persist_drawdown_state(key, cb)
                    return False, f"1h drawdown circuit breaker: {drawdown_1h:.2f}% >= {rolling_1h_max_drawdown_pct}%"

        recent_4h = cb["peaks"]
        if recent_4h:
            peak_4h = max(p["equity"] for p in recent_4h)
            if peak_4h > 0:
                drawdown_4h = (peak_4h - account_equity_usdt) / peak_4h * 100
                if drawdown_4h >= rolling_4h_max_drawdown_pct:
                    _persist_drawdown_state(key, cb)
                    return False, f"4h drawdown circuit breaker: {drawdown_4h:.2f}% >= {rolling_4h_max_drawdown_pct}%"

        # Persist every sample. A crash between sparse checkpoints must not
        # erase the most recent peak and weaken the drawdown breaker.
        _persist_drawdown_state(key, cb)

    return True, ""


async def check_drawdown_circuit_breaker(
    user_id: str | None,
    account_equity_usdt: float,
    rolling_1h_max_drawdown_pct: float = 5.0,
    rolling_4h_max_drawdown_pct: float = 10.0,
) -> tuple[bool, str]:
    """Distributed wrapper around the rolling drawdown state machine."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    async with distributed_lock(
        f"account-drawdown:{key}",
        ttl_seconds=30,
        blocking_timeout_seconds=10,
        allow_local_fallback=True,
    ):
        remote = await redis_hget_json(_DRAWDOWN_REDIS_HASH, key)
        if isinstance(remote, dict):
            _DRAWDOWN_CIRCUIT_BREAKERS[key] = remote
        result = await _check_drawdown_circuit_breaker_local(
            user_id,
            account_equity_usdt,
            rolling_1h_max_drawdown_pct,
            rolling_4h_max_drawdown_pct,
        )
        state = _DRAWDOWN_CIRCUIT_BREAKERS.get(key)
        if isinstance(state, dict):
            await redis_hset_json(_DRAWDOWN_REDIS_HASH, key, state)
        return result


_DRAWDOWN_STATE_FILE = DATA_DIR / "drawdown_cb_state.json"


def _load_drawdown_state_from_disk(key: str) -> dict[str, Any] | None:
    """Restore drawdown circuit-breaker state from disk (Round-4 audit fix)."""
    try:
        with _RISK_FILE_LOCK:
            if not _DRAWDOWN_STATE_FILE.exists():
                return None
            data = json.loads(_DRAWDOWN_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get(key)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[AccountRisk] Failed to load drawdown state from disk: {e}")
    return None


def _persist_drawdown_state(key: str, cb: dict[str, Any]) -> None:
    """Persist drawdown circuit-breaker state to disk (fire-and-forget)."""
    try:
        with _RISK_FILE_LOCK:
            existing: dict[str, Any] = {}
            if _DRAWDOWN_STATE_FILE.exists():
                existing = json.loads(_DRAWDOWN_STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            # Strip any non-serializable values; keep only ts/equity per peak
            serializable_peaks = [
                {"time": p.get("time"), "ts": p.get("ts", 0), "equity": float(p.get("equity", 0))}
                for p in cb.get("peaks", [])
                if isinstance(p, dict)
            ]
            existing[key] = {"peaks": serializable_peaks[-200:], "drawdown_events": cb.get("drawdown_events", [])}
            _atomic_write_json(_DRAWDOWN_STATE_FILE, existing)
    except Exception as e:
        logger.debug(f"[AccountRisk] Failed to persist drawdown state: {e}")


def _load_tracker_from_disk() -> dict[str, dict[str, Any]]:
    """C5-FIX: Load tracker state from disk on startup."""
    if not _ACCOUNT_TRACKER_FILE.exists():
        return {}
    try:
        with _RISK_FILE_LOCK:
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
        with _RISK_FILE_LOCK:
            _atomic_write_json(_ACCOUNT_TRACKER_FILE, serializable_tracker, indent=2)
    except OSError as e:
        logger.warning(f"[AccountRisk] Failed to save tracker to disk: {e}")


_ACCOUNT_DAILY_TRACKER.update(_load_tracker_from_disk())


async def _record_position_pnl_local(
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
            # Keep only the latest observed equity for display.  A historical
            # high-water mark must never dilute current loss percentages.
            tracker["account_equity_usdt"] = equity_dec

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


async def record_position_pnl(
    user_id: str | None,
    pnl_pct: float,
    pnl_usdt: float,
    equity_usdt: float = 0.0,
) -> dict[str, Any]:
    """Atomically record account PnL across processes when Redis is enabled."""
    key = user_id or _GLOBAL_ACCOUNT_KEY
    async with distributed_lock(
        f"account-risk:{key}",
        ttl_seconds=30,
        blocking_timeout_seconds=10,
        allow_local_fallback=True,
    ):
        remote = await redis_hget_json(_ACCOUNT_RISK_REDIS_HASH, key)
        if isinstance(remote, dict):
            _ACCOUNT_DAILY_TRACKER[key] = remote
        result = await _record_position_pnl_local(
            user_id,
            pnl_pct,
            pnl_usdt,
            equity_usdt,
        )
        await redis_hset_json(_ACCOUNT_RISK_REDIS_HASH, key, result)
        return result


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

    remote = await redis_hget_json(_ACCOUNT_RISK_REDIS_HASH, key)
    if isinstance(remote, dict):
        async with _ACCOUNT_TRACKER_GUARD:
            _ACCOUNT_DAILY_TRACKER[key] = remote

    async with _ACCOUNT_TRACKER_GUARD:
        tracker = _ACCOUNT_DAILY_TRACKER.get(key)
        if tracker is None:
            return (True, "")

        daily_pnl_usdt = safe_decimal(tracker.get("daily_pnl_usdt", 0.0)) if tracker.get("date") == today else Decimal("0.0")
        cumulative_pnl_usdt = safe_decimal(tracker.get("cumulative_pnl_usdt", 0.0))

    equity_dec = safe_decimal(account_equity_usdt)
    if equity_dec <= 0:
        logger.error(f"[AccountRisk] {key} account equity is invalid; blocking risk-sensitive entry")
        return (False, "Account equity is unavailable or non-positive; trading blocked")

    # The caller must supply freshly verified equity.  Using a larger stored
    # balance (or a deployment default) lets losses on a small/current account
    # appear artificially harmless.
    effective_equity = equity_dec

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
    await redis_hdel(_ACCOUNT_RISK_REDIS_HASH, key)
    await redis_hdel(_DRAWDOWN_REDIS_HASH, key)
    _DRAWDOWN_CIRCUIT_BREAKERS.pop(key, None)
    logger.info(f"[AccountRisk] Tracker reset for {key}")


# ─────────────────────────────────────────────
# Live account equity (Round-4 audit fix)
# ─────────────────────────────────────────────
# Round-4 audit P0 fix: ``settings.risk.account_equity_usdt`` is a static
# config constant used for paper-trading. In live mode, calling code must
# use this function so that daily-loss limits scale with the actual account
# size (otherwise a $10k → $50k account still enforces a $500 daily cap).
# The result is cached for 60s per user to avoid hammering the exchange.
_LIVE_EQUITY_CACHE: dict[str, tuple[float, float]] = {}  # key -> (equity, cached_at_ts)
_LIVE_EQUITY_TTL = 60.0  # seconds


async def get_live_account_equity(
    user_id: str | None = None,
    exchange_config: dict | None = None,
    fallback: float | None = None,
    *,
    require_live_balance: bool = False,
) -> float:
    """Fetch real account equity from the exchange.

    Paper trading uses configured equity. In live mode, ``require_live_balance``
    makes missing credentials, failed balance reads, and non-positive equity
    hard failures; callers that do not require it may use ``fallback``.
    """
    import time

    from core.config import settings

    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    explicit_config = exchange_config is not None
    exchange_config = dict(exchange_config or {})
    exchange_name = str(exchange_config.get("exchange") or exchange_config.get("name") or "")
    sandbox_marker = "sandbox" if _as_bool(exchange_config.get("sandbox_mode"), False) else "production"
    market_marker = str(exchange_config.get("market_type") or "")
    credential_marker = hashlib.sha256(
        str(exchange_config.get("api_key") or "").encode("utf-8")
    ).hexdigest()[:12]
    key = f"{user_id or _GLOBAL_ACCOUNT_KEY}:{exchange_name}:{market_marker}:{sandbox_marker}:{credential_marker}"
    now_ts = time.time()
    cached = _LIVE_EQUITY_CACHE.get(key)
    # Pre-trade live checks request require_live_balance=True and must use a
    # fresh exchange snapshot. A minute-old higher balance can materially
    # weaken position sizing and daily-loss limits after a fast drawdown.
    if not require_live_balance and cached and (now_ts - cached[1]) < _LIVE_EQUITY_TTL:
        return cached[0]

    # If live trading is disabled, fall back to the configured paper equity.
    if explicit_config:
        live = _as_bool(exchange_config.get("live_trading"), False)
    else:
        live = _as_bool(getattr(settings.exchange, "live_trading", False), False)
    if not live:
        equity = float(getattr(settings.risk, "account_equity_usdt", 10000.0) or 10000.0)
        _LIVE_EQUITY_CACHE[key] = (equity, now_ts)
        return equity

    try:
        from exchange import get_account_balance

        if require_live_balance and user_id and (
            not exchange_config.get("api_key") or not exchange_config.get("api_secret")
        ):
            raise RuntimeError("User exchange API credentials are required to read live account equity")
        balance = await get_account_balance(exchange_config)
        total = float(balance.get("total_quote") or 0)
        if total > 0:
            _LIVE_EQUITY_CACHE[key] = (total, now_ts)
            return total
        logger.warning("[AccountRisk] fetch_balance returned no positive quote equity")
    except Exception as e:
        logger.warning(f"[AccountRisk] Failed to fetch live equity: {e}")
        if require_live_balance:
            raise RuntimeError("Live account equity could not be verified") from e

    if require_live_balance:
        raise RuntimeError("Live account equity could not be verified")

    # Fallback path
    if fallback is not None and fallback > 0:
        equity = float(fallback)
    else:
        equity = float(getattr(settings.risk, "account_equity_usdt", 10000.0) or 10000.0)
    # Cache failures for a shorter window to retry sooner
    _LIVE_EQUITY_CACHE[key] = (equity, now_ts - _LIVE_EQUITY_TTL + 15.0)
    return equity
