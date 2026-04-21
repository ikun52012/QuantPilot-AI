"""
Position monitor for advanced trailing-stop modes.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from exchange import get_ticker, place_protective_stop
from models import SignalDirection, TrailingStopMode
from trade_logger import get_trade_history


DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "position_monitor_state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _prune_state(state: dict, retention_days: int = 14) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    pruned = {}
    for key, value in state.items():
        if key == "last_run_at":
            pruned[key] = value
            continue
        if not isinstance(value, dict):
            continue
        updated_at = value.get("updated_at")
        if not updated_at:
            pruned[key] = value
            continue
        try:
            if datetime.fromisoformat(str(updated_at)) >= cutoff:
                pruned[key] = value
        except ValueError:
            pruned[key] = value
    return pruned


def get_monitor_state() -> dict:
    return _load_state()


async def run_position_monitor_once(user_configs: dict[str, dict] | None = None) -> dict:
    """Scan recent executed trades and adjust protective stops when rules trigger."""
    user_configs = user_configs or {}
    state = _prune_state(_load_state())
    stats = {"checked": 0, "adjusted": 0, "skipped": 0, "errors": 0, "events": []}
    trades = get_trade_history(days=7)
    for trade in trades:
        if not trade.get("execute") or trade.get("order_status") not in {"filled", "simulated"}:
            continue
        mode = str(trade.get("trailing_stop") or "none")
        if mode not in {
            TrailingStopMode.BREAKEVEN_ON_TP1.value,
            TrailingStopMode.STEP_TRAILING.value,
            TrailingStopMode.PROFIT_PCT_TRAILING.value,
        }:
            continue
        stats["checked"] += 1
        try:
            event = await _monitor_trade(trade, mode, user_configs, state)
            if event.get("adjusted"):
                stats["adjusted"] += 1
            else:
                stats["skipped"] += 1
            stats["events"].append(event)
        except Exception as exc:
            logger.warning(f"[PositionMonitor] Failed for trade {trade.get('id')}: {exc}")
            stats["errors"] += 1
            stats["events"].append({"trade_id": trade.get("id"), "error": str(exc)})
    state["last_run_at"] = datetime.utcnow().isoformat()
    _save_state(state)
    return stats


async def _monitor_trade(trade: dict, mode: str, user_configs: dict[str, dict], state: dict) -> dict:
    trade_id = trade.get("id")
    direction = str(trade.get("direction") or "").lower()
    is_long = direction == SignalDirection.LONG.value
    entry = float(trade.get("entry_price") or 0)
    qty = float(trade.get("quantity") or 0)
    ticker = trade.get("ticker") or ""
    if not trade_id or not entry or not qty or not ticker or direction not in {"long", "short"}:
        return {"trade_id": trade_id, "adjusted": False, "reason": "missing trade data"}

    user_id = trade.get("user_id") or ""
    exchange_config = user_configs.get(user_id, {}).get("exchange") or {}
    ticker_data = await get_ticker(ticker, exchange_config=exchange_config)
    current = float(ticker_data.get("last") or ticker_data.get("bid") or ticker_data.get("ask") or 0)
    if not current:
        return {"trade_id": trade_id, "adjusted": False, "reason": "ticker unavailable"}
    profit_pct = ((current - entry) / entry * 100) if is_long else ((entry - current) / entry * 100)
    tp_levels = trade.get("take_profit_levels") or []
    target_stop = None
    label = ""

    if mode == TrailingStopMode.BREAKEVEN_ON_TP1.value and tp_levels:
        tp1 = float(tp_levels[0].get("price") or 0)
        hit = current >= tp1 if is_long else current <= tp1
        if hit:
            target_stop = entry
            label = "breakeven"
    elif mode == TrailingStopMode.STEP_TRAILING.value and tp_levels:
        hit_levels = []
        for tp in tp_levels:
            price = float(tp.get("price") or 0)
            if price and (current >= price if is_long else current <= price):
                hit_levels.append(price)
        if len(hit_levels) >= 2:
            target_stop = hit_levels[-2]
            label = f"step_tp_{len(hit_levels)-1}"
        elif len(hit_levels) == 1:
            target_stop = entry
            label = "step_breakeven"
    elif mode == TrailingStopMode.PROFIT_PCT_TRAILING.value:
        activation = float((trade.get("order_details") or {}).get("trailing_activation_profit_pct") or 1.0)
        trail_pct = float((trade.get("order_details") or {}).get("trailing_pct") or 1.0)
        if profit_pct >= activation:
            target_stop = current * (1 - trail_pct / 100.0) if is_long else current * (1 + trail_pct / 100.0)
            label = "profit_pct_trailing"

    if not target_stop:
        return {"trade_id": trade_id, "adjusted": False, "reason": "no trailing trigger", "profit_pct": round(profit_pct, 4)}

    key = f"{trade_id}:{label}"
    previous = state.get(key, {})
    previous_stop = float(previous.get("stop_price") or 0)
    if previous_stop and ((is_long and target_stop <= previous_stop) or ((not is_long) and target_stop >= previous_stop)):
        return {"trade_id": trade_id, "adjusted": False, "reason": "existing stop is better", "stop_price": previous_stop}

    if not exchange_config.get("live_trading"):
        state[key] = {"stop_price": round(target_stop, 8), "paper": True, "updated_at": datetime.utcnow().isoformat()}
        return {"trade_id": trade_id, "adjusted": True, "paper": True, "stop_price": round(target_stop, 8), "label": label}

    result = await place_protective_stop(
        ticker=ticker,
        direction=direction,
        quantity=qty,
        stop_price=round(target_stop, 8),
        exchange_config=exchange_config,
    )
    state[key] = {"stop_price": round(target_stop, 8), "result": result, "updated_at": datetime.utcnow().isoformat()}
    return {"trade_id": trade_id, "adjusted": result.get("status") == "placed", "stop_price": round(target_stop, 8), "label": label, "result": result}
