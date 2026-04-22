"""
Signal Server - Position Monitor
Tracks open positions, settles paper TP/SL, reconciles exchange closes,
and keeps realised PnL in the database.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select

from core.config import settings
from core.database import (
    PositionModel,
    UserModel,
    db_manager,
    record_position_close_trade_async,
)
from core.security import decrypt_settings_payload
from core.utils.datetime import utcnow, utcnow_iso


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _loads_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _has_partial_position_fills(position: PositionModel) -> bool:
    return any(
        str(level.get("status") or "").lower() in {"hit", "filled", "closed"}
        for level in _loads_list(position.take_profit_json)
        if isinstance(level, dict)
    )


def _effective_remaining_quantity(position: PositionModel, opened_qty: float) -> float:
    remaining_qty = _safe_float(position.remaining_quantity, opened_qty)
    if remaining_qty > 0:
        return remaining_qty
    if (
        position.status == "open"
        and _safe_float(position.realized_pnl_pct) == 0
        and not _has_partial_position_fills(position)
    ):
        return opened_qty
    return 0.0


def _symbol_key(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "").replace(":", "").replace("-", "").replace("_", "")


def _price_pnl_pct(direction: str, entry_price: float, exit_price: float, leverage: float = 1.0) -> float:
    entry_price = _safe_float(entry_price)
    exit_price = _safe_float(exit_price)
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    if str(direction).lower() == "short":
        raw = ((entry_price - exit_price) / entry_price) * 100.0
    else:
        raw = ((exit_price - entry_price) / entry_price) * 100.0
    return raw * max(1.0, _safe_float(leverage, 1.0))


async def get_monitor_state() -> dict:
    """Get position monitor state."""
    return {
        "enabled": True,
        "position_tracking_enabled": True,
        "trailing_stop_enabled": settings.trailing_stop.mode != "none",
        "interval_secs": settings.position_monitor_interval_secs,
        "mode": settings.trailing_stop.mode,
    }


async def run_position_monitor_once(user_configs: Optional[dict] = None) -> dict:
    """Run one full tracking cycle and persist TP/SL/PnL updates."""
    stats = {
        "tracked": 0,
        "updated": 0,
        "partials": 0,
        "closed": 0,
        "adjusted": 0,
        "errors": 0,
        "timestamp": utcnow().isoformat(),
    }

    try:
        async with db_manager.async_session_factory() as session:
            result = await session.execute(
                select(PositionModel)
                .where(PositionModel.status == "open")
                .order_by(PositionModel.opened_at.asc())
            )
            positions = list(result.scalars().all())
            stats["tracked"] = len(positions)

            for position in positions:
                try:
                    changed = await _reconcile_position(session, position, user_configs or {})
                    for key, value in changed.items():
                        stats[key] = stats.get(key, 0) + value
                except Exception as exc:
                    stats["errors"] += 1
                    logger.error(f"[PositionMonitor] Failed to reconcile {position.id}: {exc}")

            await session.commit()
    except Exception as exc:
        stats["errors"] += 1
        logger.error(f"[PositionMonitor] Cycle failed: {exc}")

    return stats


async def _reconcile_position(session, position: PositionModel, user_configs: dict) -> dict:
    stats = {"updated": 0, "partials": 0, "closed": 0, "adjusted": 0}
    exchange_config = await _exchange_config_for_position(session, position, user_configs)

    if not bool(position.live_trading):
        return await _reconcile_paper_position(session, position, exchange_config)

    exchange_config["live_trading"] = True
    return await _reconcile_exchange_position(session, position, exchange_config)


async def _exchange_config_for_position(session, position: PositionModel, user_configs: dict) -> dict:
    config = {
        "exchange": position.exchange or settings.exchange.name,
        "api_key": settings.exchange.api_key,
        "api_secret": settings.exchange.api_secret,
        "password": settings.exchange.password,
        "live_trading": bool(position.live_trading),
        "sandbox_mode": bool(position.sandbox_mode),
    }
    if position.user_id and position.user_id in user_configs:
        config.update(user_configs[position.user_id])
        return config

    if position.user_id:
        user = await session.get(UserModel, position.user_id)
        if user:
            try:
                raw = json.loads(user.settings_json or "{}")
                user_settings = decrypt_settings_payload(raw)
                exchange = (user_settings or {}).get("exchange") or {}
                config.update({
                    "exchange": exchange.get("name") or exchange.get("exchange") or config["exchange"],
                    "api_key": exchange.get("api_key") or "",
                    "api_secret": exchange.get("api_secret") or "",
                    "password": exchange.get("password") or "",
                    "live_trading": _safe_bool(exchange.get("live_trading"), config["live_trading"]),
                    "sandbox_mode": _safe_bool(exchange.get("sandbox_mode"), config["sandbox_mode"]),
                })
            except Exception as exc:
                logger.warning(f"[PositionMonitor] Could not decrypt user exchange config: {exc}")
    return config


async def _reconcile_paper_position(session, position: PositionModel, exchange_config: dict) -> dict:
    from exchange import get_latest_candle, get_ticker

    stats = {"updated": 0, "partials": 0, "closed": 0, "adjusted": 0}
    candle = await get_latest_candle(position.ticker, "1m", {**exchange_config, "live_trading": False})
    if not candle:
        ticker = await get_ticker(position.ticker, {**exchange_config, "live_trading": False})
        last = _safe_float(ticker.get("last") or ticker.get("bid") or ticker.get("ask"))
        candle = {"high": last, "low": last, "close": last}

    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))
    close = _safe_float(candle.get("close"))
    if close <= 0:
        return stats

    _update_unrealized(position, close)
    stats["updated"] += 1

    direction = str(position.direction or "long").lower()
    stop_loss = _safe_float(position.stop_loss)
    stop_hit = bool(stop_loss > 0 and ((direction == "long" and low <= stop_loss) or (direction == "short" and high >= stop_loss)))

    # If a single candle contains both TP and SL, choose the conservative SL path.
    if stop_hit:
        await record_position_close_trade_async(
            session=session,
            position=position,
            exit_price=stop_loss,
            close_reason="stop_loss",
            order_status="paper_closed",
            order_details={"trigger": "stop_loss", "candle": candle},
        )
        stats["closed"] += 1
        return stats

    tp_levels = _loads_list(position.take_profit_json)
    hit_levels = _hit_take_profit_levels(direction, tp_levels, high, low)
    if hit_levels:
        opened_qty = max(_safe_float(position.quantity), 0.0)
        remaining_qty = _effective_remaining_quantity(position, opened_qty)
        for level in hit_levels:
            qty_pct = max(0.0, _safe_float(level.get("qty_pct"), 100.0))
            qty = min(remaining_qty, opened_qty * (qty_pct / 100.0)) if opened_qty > 0 else 0.0
            if qty <= 0:
                level["status"] = "hit"
                continue
            weight = qty / opened_qty if opened_qty > 0 else 1.0
            level_pnl = _price_pnl_pct(position.direction, position.entry_price, level.get("price"), position.leverage)
            position.realized_pnl_pct = round(_safe_float(position.realized_pnl_pct) + (level_pnl * weight), 6)
            remaining_qty = max(0.0, remaining_qty - qty)
            level["status"] = "hit"
            level["hit_at"] = utcnow().isoformat()
            stats["partials"] += 1

        position.remaining_quantity = remaining_qty
        position.take_profit_json = json.dumps(tp_levels, ensure_ascii=False, default=str)
        position.updated_at = utcnow()
        await session.flush()

        if remaining_qty <= max(0.00000001, opened_qty * 0.000001):
            final_price = _safe_float(hit_levels[-1].get("price"), close)
            await record_position_close_trade_async(
                session=session,
                position=position,
                exit_price=final_price,
                close_reason="take_profit",
                order_status="paper_closed",
                order_details={"trigger": "take_profit", "levels": hit_levels, "candle": candle},
            )
            stats["closed"] += 1

    return stats


def _hit_take_profit_levels(direction: str, levels: list[dict], high: float, low: float) -> list[dict]:
    pending = [level for level in levels if str(level.get("status") or "pending").lower() not in {"hit", "filled", "closed"}]
    if str(direction).lower() == "short":
        pending.sort(key=lambda item: _safe_float(item.get("price")), reverse=True)
        return [level for level in pending if _safe_float(level.get("price")) > 0 and low <= _safe_float(level.get("price"))]
    pending.sort(key=lambda item: _safe_float(item.get("price")))
    return [level for level in pending if _safe_float(level.get("price")) > 0 and high >= _safe_float(level.get("price"))]


async def _reconcile_exchange_position(session, position: PositionModel, exchange_config: dict) -> dict:
    from exchange import get_open_positions, get_recent_orders, get_ticker, place_protective_stop

    stats = {"updated": 0, "partials": 0, "closed": 0, "adjusted": 0}
    exchange_positions = await get_open_positions(exchange_config)
    match = _find_exchange_position(position, exchange_positions)

    if match:
        mark_price = _safe_float(match.get("mark_price") or match.get("markPrice") or match.get("entry_price"))
        if mark_price > 0:
            _update_unrealized(position, mark_price)
            stats["updated"] += 1
        if await _maybe_adjust_trailing_stop(position, exchange_config, match, place_protective_stop):
            stats["adjusted"] += 1
        return stats

    order = await _find_recent_close_order(position, exchange_config, get_recent_orders)
    if not order:
        ticker = await get_ticker(position.ticker, exchange_config)
        mark_price = _safe_float(ticker.get("last") or position.last_price)
        if mark_price > 0:
            _update_unrealized(position, mark_price)
            stats["updated"] += 1
        return stats

    exit_price = _safe_float((order or {}).get("average") or (order or {}).get("price"))
    close_reason = _close_reason_for_order(position, order)

    if exit_price <= 0:
        ticker = await get_ticker(position.ticker, exchange_config)
        exit_price = _safe_float(ticker.get("last") or position.last_price or position.entry_price)
        close_reason = "exchange_closed_unmatched"

    if exit_price > 0:
        await record_position_close_trade_async(
            session=session,
            position=position,
            exit_price=exit_price,
            close_reason=close_reason,
            order_status="exchange_closed",
            order_details=order or {"trigger": close_reason},
        )
        stats["closed"] += 1

    return stats


def _find_exchange_position(position: PositionModel, exchange_positions: list[dict]) -> Optional[dict]:
    target = _symbol_key(position.ticker)
    direction = str(position.direction or "").lower()
    for item in exchange_positions:
        symbol = _symbol_key(item.get("symbol"))
        side = str(item.get("side") or "").lower()
        if target and target not in symbol and symbol not in target:
            continue
        if direction and side and direction not in side:
            continue
        return item
    return None


async def _find_recent_close_order(position: PositionModel, exchange_config: dict, get_recent_orders) -> Optional[dict]:
    orders = await get_recent_orders(position.ticker, 50, exchange_config)
    order_ids = set(_loads_list(position.take_profit_order_ids_json))
    if position.stop_loss_order_id:
        order_ids.add(position.stop_loss_order_id)
    for order in orders:
        if str(order.get("id") or "") in order_ids and _order_has_close_status(order):
            return order
    if order_ids:
        return None

    for order in orders:
        if _order_matches_position_close(position, order):
            return order
    return None


def _order_matches_position_close(position: PositionModel, order: dict) -> bool:
    if not _order_has_close_status(order):
        return False

    if not _symbols_match(position.ticker, order.get("symbol")):
        return False

    order_side = str(order.get("side") or "").lower()
    expected_side = "sell" if str(position.direction).lower() == "long" else "buy"
    if not order_side or order_side != expected_side:
        return False

    order_ts = _safe_float(order.get("timestamp"))
    opened_at = position.opened_at
    if order_ts <= 0 or not opened_at:
        return False
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    opened_ms = opened_at.timestamp() * 1000
    if order_ts < opened_ms:
        return False

    return True


def _order_has_close_status(order: dict) -> bool:
    return str(order.get("status") or "").lower() in {"closed", "filled"}


def _symbols_match(left: str, right: str) -> bool:
    left_key = _symbol_key(left)
    right_key = _symbol_key(right)
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def _close_reason_for_order(position: PositionModel, order: Optional[dict]) -> str:
    if not order:
        return "exchange_closed_unmatched"
    order_id = str(order.get("id") or "")
    if position.stop_loss_order_id and order_id == position.stop_loss_order_id:
        return "stop_loss"
    if order_id in set(_loads_list(position.take_profit_order_ids_json)):
        return "take_profit"
    return "exchange_closed"


def _update_unrealized(position: PositionModel, mark_price: float) -> None:
    opened_qty = max(_safe_float(position.quantity), 0.0)
    remaining_qty = _effective_remaining_quantity(position, opened_qty)
    remaining_weight = min(1.0, max(0.0, remaining_qty / opened_qty)) if opened_qty > 0 else 1.0
    open_pnl = _price_pnl_pct(position.direction, position.entry_price, mark_price, position.leverage) * remaining_weight
    position.last_price = mark_price
    position.current_pnl_pct = round(_safe_float(position.realized_pnl_pct) + open_pnl, 6)
    position.updated_at = utcnow()


async def _maybe_adjust_trailing_stop(position: PositionModel, exchange_config: dict, exchange_position: dict, place_protective_stop) -> bool:
    if settings.trailing_stop.mode == "none":
        return False
    mark_price = _safe_float(exchange_position.get("mark_price") or exchange_position.get("markPrice"))
    if mark_price <= 0:
        return False

    profit_pct = _price_pnl_pct(position.direction, position.entry_price, mark_price, 1.0)
    if profit_pct < settings.trailing_stop.activation_profit_pct:
        return False

    if str(position.direction).lower() == "short":
        new_stop = mark_price * (1 + settings.trailing_stop.trail_pct / 100.0)
    else:
        new_stop = mark_price * (1 - settings.trailing_stop.trail_pct / 100.0)

    current_stop = _safe_float(position.stop_loss)
    if current_stop > 0:
        if str(position.direction).lower() == "short" and new_stop >= current_stop:
            return False
        if str(position.direction).lower() != "short" and new_stop <= current_stop:
            return False

    result = await place_protective_stop(
        ticker=position.ticker,
        direction=position.direction,
        quantity=_effective_remaining_quantity(position, _safe_float(position.quantity)),
        stop_price=new_stop,
        exchange_config=exchange_config,
    )
    if result.get("status") in {"placed", "simulated"}:
        position.stop_loss = new_stop
        position.stop_loss_order_id = str(result.get("order_id") or position.stop_loss_order_id or "")
        position.updated_at = utcnow()
        logger.info(f"[PositionMonitor] Adjusted stop for {position.ticker}: new_stop={new_stop:.8f}")
        return True
    return False


async def check_position_risk(position: dict, config: dict) -> dict:
    """Check basic risk metrics for a position dict."""
    entry_price = _safe_float(position.get("entryPrice") or position.get("entry_price"))
    mark_price = _safe_float(position.get("markPrice") or position.get("mark_price"))
    liquidation_price = _safe_float(position.get("liquidationPrice") or position.get("liquidation_price"))
    leverage = _safe_float(position.get("leverage"), 1.0)

    if not entry_price or not mark_price:
        return {"risk_level": "unknown"}

    side = str(position.get("side") or "long").lower()
    pnl_pct = _price_pnl_pct(side, entry_price, mark_price, 1.0)

    liq_distance = 0.0
    if liquidation_price > 0:
        if side == "long":
            liq_distance = ((mark_price - liquidation_price) / mark_price) * 100
        else:
            liq_distance = ((liquidation_price - mark_price) / mark_price) * 100

    risk_level = "low"
    warnings = []
    if liq_distance and liq_distance < 5:
        risk_level = "critical"
        warnings.append(f"Liquidation within {liq_distance:.1f}%")
    elif liq_distance and liq_distance < 10:
        risk_level = "high"
        warnings.append(f"Liquidation within {liq_distance:.1f}%")
    elif pnl_pct < -5:
        risk_level = "high"
        warnings.append(f"Position down {abs(pnl_pct):.1f}%")
    elif leverage > 20:
        risk_level = "medium"
        warnings.append(f"High leverage: {leverage}x")

    return {
        "risk_level": risk_level,
        "pnl_pct": round(pnl_pct, 2),
        "liquidation_distance_pct": round(liq_distance, 2),
        "leverage": leverage,
        "warnings": warnings,
    }
