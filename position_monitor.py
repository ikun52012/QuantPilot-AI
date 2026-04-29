"""
Signal Server - Position Monitor
Tracks open positions, settles paper TP/SL, reconciles exchange closes,
and keeps realised PnL in the database.
"""
import asyncio
import json
from datetime import timezone

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
from core.utils.common import loads_dict, loads_list, price_pnl_pct, safe_bool, safe_float, symbol_key
from core.utils.datetime import utcnow

# Backward-compatible aliases used by older tests and imports.
_safe_float = safe_float
_loads_list = loads_list
_loads_dict = loads_dict


def _has_partial_position_fills(position: PositionModel) -> bool:
    return any(
        str(level.get("status") or "").lower() in {"hit", "filled", "closed"}
        for level in loads_list(position.take_profit_json)
        if isinstance(level, dict)
    )


def _effective_remaining_quantity(position: PositionModel, opened_qty: float) -> float:
    remaining_qty = safe_float(position.remaining_quantity, opened_qty)
    if remaining_qty > 0:
        return remaining_qty
    if (
        position.status in {"open", "pending"}
        and safe_float(position.realized_pnl_pct) == 0
        and not _has_partial_position_fills(position)
    ):
        return opened_qty
    return 0.0


def _symbol_key(symbol: str) -> str:
    return symbol_key(symbol)


def _price_pnl_pct(direction: str, entry_price: float, exit_price: float, leverage: float = 1.0) -> float:
    return price_pnl_pct(direction, entry_price, exit_price, leverage)


def _get_exchange_config_for_position(position: PositionModel) -> dict | None:
    if not position.user_id:
        return None
    exchange_name = str(position.exchange or "").lower()
    if not exchange_name:
        return None
    return {
        "exchange": exchange_name,
        "user_id": position.user_id,
        "sandbox_mode": position.sandbox_mode,
        "live_trading": position.live_trading,
    }


async def get_monitor_state() -> dict:
    """Get position monitor state."""
    return {
        "enabled": True,
        "position_tracking_enabled": True,
        "trailing_stop_enabled": settings.trailing_stop.mode != "none",
        "interval_secs": settings.position_monitor_interval_secs,
        "mode": settings.trailing_stop.mode,
    }


async def run_position_monitor_once(user_configs: dict | None = None) -> dict:
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
                .where(PositionModel.status.in_(["open", "pending"]))
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
        "market_type": settings.exchange.market_type,
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
                    "live_trading": safe_bool(exchange.get("live_trading"), config["live_trading"]),
                    "sandbox_mode": safe_bool(exchange.get("sandbox_mode"), config["sandbox_mode"]),
                    "market_type": exchange.get("market_type") or config["market_type"],
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
        last = safe_float(ticker.get("last") or ticker.get("bid") or ticker.get("ask"))
        candle = {"high": last, "low": last, "close": last}

    high = safe_float(candle.get("high"))
    low = safe_float(candle.get("low"))
    close = safe_float(candle.get("close"))
    if close <= 0:
        return stats

    entry_price = safe_float(position.entry_price)
    direction = str(position.direction or "long").lower()
    order_type = str(position.order_type or "market").lower()
    limit_timeout = safe_float(position.limit_timeout_secs, 300)

    entry_filled = position.status != "pending"

    if not entry_filled:
        if order_type == "limit" and entry_price > 0:
            entry_hit = (direction == "long" and low <= entry_price) or (direction == "short" and high >= entry_price)
            if entry_hit:
                position.status = "open"
                position.last_price = entry_price
                entry_filled = True
                logger.info(
                    f"[PositionMonitor] 📍 Paper LIMIT order FILLED: {position.ticker} "
                    f"{direction} @ {entry_price} (low={low}, high={high})"
                )
                stats["updated"] += 1
            else:
                opened_at = position.opened_at
                if opened_at:
                    if opened_at.tzinfo is None:
                        opened_at = opened_at.replace(tzinfo=timezone.utc)
                    age_secs = (utcnow() - opened_at).total_seconds()
                    if age_secs > limit_timeout:
                        position.status = "closed"
                        position.close_reason = "limit_order_timeout"
                        position.closed_at = utcnow()
                        position.updated_at = utcnow()
                        logger.warning(
                            f"[PositionMonitor] Paper limit order TIMEOUT: {position.ticker} "
                            f"(age={age_secs:.0f}s > timeout={limit_timeout}s) - position closed"
                        )
                        stats["closed"] += 1
                        return stats
                logger.debug(
                    f"[PositionMonitor] Paper LIMIT order waiting: {position.ticker} "
                    f"entry={entry_price} current=[{low},{high}]"
                )
                return stats
        else:
            position.status = "open"
            position.last_price = close
            entry_filled = True
            stats["updated"] += 1

    if not entry_filled:
        return stats

    _update_unrealized(position, close)
    if entry_filled and not stats.get("updated"):
        stats["updated"] += 1

    stop_loss = safe_float(position.stop_loss)
    stop_hit = bool(stop_loss > 0 and ((direction == "long" and low <= stop_loss) or (direction == "short" and high >= stop_loss)))

    if stop_hit:
        await record_position_close_trade_async(
            session=session,
            position=position,
            exit_price=stop_loss,
            close_reason="stop_loss",
            order_status="paper_closed",
            order_details={"trigger": "stop_loss", "candle": candle, "entry_filled": entry_filled},
        )
        stats["closed"] += 1
        return stats

    tp_levels = loads_list(position.take_profit_json)
    hit_levels = _hit_take_profit_levels(direction, tp_levels, high, low)
    if hit_levels:
        opened_qty = max(safe_float(position.quantity), 0.0)
        remaining_qty = _effective_remaining_quantity(position, opened_qty)
        total_level_pnl_usdt = 0.0

        for level in hit_levels:
            qty_pct = max(0.0, safe_float(level.get("qty_pct"), 100.0))
            qty = min(remaining_qty, opened_qty * (qty_pct / 100.0)) if opened_qty > 0 else 0.0
            if qty <= 0:
                level["status"] = "hit"
                continue
            weight = qty / opened_qty if opened_qty > 0 else 1.0
            level_pnl = _price_pnl_pct(position.direction, position.entry_price, level.get("price"), position.leverage)
            position.realized_pnl_pct = round(safe_float(position.realized_pnl_pct) + (level_pnl * weight), 6)
            remaining_qty = max(0.0, remaining_qty - qty)
            level["status"] = "hit"
            level["hit_at"] = utcnow().isoformat()
            stats["partials"] += 1

            # Calculate USDT PnL for this partial close
            entry_price = safe_float(position.entry_price)
            leverage = safe_float(position.leverage, 1.0)
            if entry_price > 0 and qty > 0:
                margin_used = (entry_price * qty) / max(1.0, leverage)
                level_pnl_usdt = margin_used * (level_pnl / 100.0)
                total_level_pnl_usdt += level_pnl_usdt

        position.remaining_quantity = remaining_qty
        position.take_profit_json = json.dumps(tp_levels, ensure_ascii=False, default=str)
        position.updated_at = utcnow()
        await session.flush()

        # Update user balance for partial TP hits in paper trading
        if not position.live_trading and position.user_id and total_level_pnl_usdt != 0.0:
            from core.database import update_user_balance
            await update_user_balance(session, position.user_id, total_level_pnl_usdt)

        if remaining_qty > 0:
            from exchange import place_protective_stop
            exchange_config = _get_exchange_config_for_position(position)
            if exchange_config:
                await _adjust_trailing_stop_on_tp_hit(position, tp_levels, hit_levels, exchange_config, place_protective_stop)

        if remaining_qty <= max(0.00000001, opened_qty * 0.000001):
            final_price = safe_float(hit_levels[-1].get("price"), close)
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
        pending.sort(key=lambda item: safe_float(item.get("price")), reverse=True)
        return [level for level in pending if safe_float(level.get("price")) > 0 and low <= safe_float(level.get("price"))]
    pending.sort(key=lambda item: safe_float(item.get("price")))
    return [level for level in pending if safe_float(level.get("price")) > 0 and high >= safe_float(level.get("price"))]


async def _check_pending_limit_orders(session, position: PositionModel, exchange_config: dict) -> None:
    """Check status of pending limit orders and update position if filled or expired."""
    if not position.entry_order_id or position.entry_order_id == "":
        return

    try:
        import ccxt

        from exchange import _close_exchange, _get_or_create_exchange, _resolve_symbol

        exchange = _get_or_create_exchange(
            exchange_id=exchange_config.get("exchange", settings.exchange.name),
            api_key=exchange_config.get("api_key", settings.exchange.api_key),
            api_secret=exchange_config.get("api_secret", settings.exchange.api_secret),
            password=exchange_config.get("password", settings.exchange.password),
            live=bool(exchange_config.get("live_trading", False)),
            sandbox=bool(exchange_config.get("sandbox_mode", False)),
            market_type=exchange_config.get("market_type", settings.exchange.market_type),
        )

        try:
            symbol = await asyncio.to_thread(_resolve_symbol, exchange, position.ticker)
            order = await asyncio.to_thread(exchange.fetch_order, position.entry_order_id, symbol)

            order_status = str(order.get("status", "")).lower()

            if order_status in {"closed", "filled"}:
                # Limit order filled - update position entry price
                filled_price = safe_float(order.get("average") or order.get("price"))
                position.status = "open"
                position.updated_at = utcnow()
                if filled_price > 0:
                    position.entry_price = filled_price
                    position.last_price = filled_price
                    logger.info(f"[PositionMonitor] Limit order filled for {position.ticker} @ {filled_price}")

            elif order_status in {"canceled", "cancelled", "expired", "rejected"}:
                # Order expired/cancelled - close position as failed
                position.status = "closed"
                position.close_reason = "limit_order_expired"
                position.closed_at = utcnow()
                position.updated_at = utcnow()
                logger.warning(f"[PositionMonitor] Limit order {order_status} for {position.ticker}, position closed")

            elif order_status in {"open", "new"}:
                # Check if order has exceeded timeout
                created_at = order.get("timestamp")
                if created_at:
                    import time
                    order_age_secs = (time.time() * 1000 - created_at) / 1000
                    limit_timeout = getattr(position, "limit_timeout_secs", 300) or 300
                    if order_age_secs > limit_timeout:
                        # Cancel the order
                        try:
                            await asyncio.to_thread(exchange.cancel_order, position.entry_order_id, symbol)
                            position.status = "closed"
                            position.close_reason = "limit_order_timeout"
                            position.closed_at = utcnow()
                            position.updated_at = utcnow()
                            logger.info(f"[PositionMonitor] Cancelled expired limit order for {position.ticker}")
                        except Exception as e:
                            logger.warning(f"[PositionMonitor] Failed to cancel limit order: {e}")
        finally:
            await _close_exchange(exchange)
    except ccxt.OrderNotFound:
        # Order no longer exists on exchange
        position.status = "closed"
        position.close_reason = "limit_order_not_found"
        position.closed_at = utcnow()
        position.updated_at = utcnow()
        logger.warning(f"[PositionMonitor] Limit order not found on exchange for {position.ticker}")
    except Exception as e:
        logger.debug(f"[PositionMonitor] Error checking limit order for {position.ticker}: {e}")


async def _reconcile_exchange_position(session, position: PositionModel, exchange_config: dict) -> dict:
    from exchange import get_open_positions, get_recent_orders, get_ticker, place_protective_stop

    stats = {"updated": 0, "partials": 0, "closed": 0, "adjusted": 0}

    # Check pending limit orders first
    await _check_pending_limit_orders(session, position, exchange_config)

    exchange_positions = await get_open_positions(exchange_config)
    match = _find_exchange_position(position, exchange_positions)

    if match:
        mark_price = safe_float(match.get("mark_price") or match.get("markPrice") or match.get("entry_price"))
        if mark_price > 0:
            _update_unrealized(position, mark_price)
            stats["updated"] += 1
        if await _maybe_adjust_trailing_stop(position, exchange_config, match, place_protective_stop):
            stats["adjusted"] += 1
            await session.flush()
        tp_orders = await get_recent_orders(position.ticker, 20, exchange_config)
        tp_hit_levels = _detect_tp_hits_from_orders(position, tp_orders)
        if tp_hit_levels:
            tp_levels = loads_list(position.take_profit_json)
            if await _adjust_trailing_stop_on_tp_hit(position, tp_levels, tp_hit_levels, exchange_config, place_protective_stop):
                stats["adjusted"] += 1
                await session.flush()
        return stats

    order = await _find_recent_close_order(position, exchange_config, get_recent_orders)
    if not order:
        ticker = await get_ticker(position.ticker, exchange_config)
        mark_price = safe_float(ticker.get("last") or position.last_price)
        if mark_price > 0:
            _update_unrealized(position, mark_price)
            stats["updated"] += 1
        return stats

    exit_price = safe_float((order or {}).get("average") or (order or {}).get("price"))
    close_reason = _close_reason_for_order(position, order)

    if exit_price <= 0:
        ticker = await get_ticker(position.ticker, exchange_config)
        exit_price = safe_float(ticker.get("last") or position.last_price or position.entry_price)
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


def _detect_tp_hits_from_orders(position: PositionModel, orders: list[dict]) -> list[dict]:
    tp_order_ids = set(loads_list(position.take_profit_order_ids_json))
    tp_levels = loads_list(position.take_profit_json)
    hit_levels = []

    for order in orders:
        order_id = str(order.get("id") or "")
        if order_id not in tp_order_ids:
            continue
        if not _order_has_close_status(order):
            continue

        order_price = safe_float(order.get("average") or order.get("price"))
        for i, level in enumerate(tp_levels):
            level_price = safe_float(level.get("price"))
            level_status = str(level.get("status") or "pending").lower()
            if level_status in {"hit", "filled", "closed"}:
                continue
            if abs(order_price - level_price) / level_price < 0.001:
                hit_levels.append({
                    "level": i + 1,
                    "price": level_price,
                    "qty_pct": safe_float(level.get("qty_pct"), 100.0),
                    "status": "hit",
                    "order_id": order_id,
                })
                level["status"] = "hit"
                level["hit_at"] = utcnow().isoformat()
                break

    if hit_levels:
        position.take_profit_json = json.dumps(tp_levels, ensure_ascii=False, default=str)

    return hit_levels


def _find_exchange_position(position: PositionModel, exchange_positions: list[dict]) -> dict | None:
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


async def _find_recent_close_order(position: PositionModel, exchange_config: dict, get_recent_orders) -> dict | None:
    orders = await get_recent_orders(position.ticker, 50, exchange_config)
    order_ids = set(loads_list(position.take_profit_order_ids_json))
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

    order_ts = safe_float(order.get("timestamp"))
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


def _close_reason_for_order(position: PositionModel, order: dict | None) -> str:
    if not order:
        return "exchange_closed_unmatched"
    order_id = str(order.get("id") or "")
    if position.stop_loss_order_id and order_id == position.stop_loss_order_id:
        return "stop_loss"
    if order_id in set(loads_list(position.take_profit_order_ids_json)):
        return "take_profit"
    return "exchange_closed"


def _update_unrealized(position: PositionModel, mark_price: float) -> None:
    opened_qty = max(safe_float(position.quantity), 0.0)
    remaining_qty = _effective_remaining_quantity(position, opened_qty)
    remaining_weight = min(1.0, max(0.0, remaining_qty / opened_qty)) if opened_qty > 0 else 1.0
    open_pnl = _price_pnl_pct(position.direction, position.entry_price, mark_price, position.leverage) * remaining_weight
    position.last_price = mark_price
    position.current_pnl_pct = round(safe_float(position.realized_pnl_pct) + open_pnl, 6)
    position.updated_at = utcnow()


async def _maybe_adjust_trailing_stop(position: PositionModel, exchange_config: dict, exchange_position: dict, place_protective_stop) -> bool:
    trailing_config = loads_dict(position.trailing_stop_config_json)
    trailing_mode = str(trailing_config.get("mode") or settings.trailing_stop.mode or "none").lower()

    if trailing_mode == "none":
        return False

    mark_price = safe_float(exchange_position.get("mark_price") or exchange_position.get("markPrice"))
    if mark_price <= 0:
        return False

    direction = str(position.direction or "long").lower()
    entry_price = safe_float(position.entry_price)
    current_stop = safe_float(position.stop_loss)
    remaining_qty = _effective_remaining_quantity(position, safe_float(position.quantity))

    new_stop = None

    if trailing_mode == "moving":
        trail_pct = safe_float(trailing_config.get("trail_pct") or settings.trailing_stop.trail_pct, 1.5)
        activation_pct = safe_float(trailing_config.get("activation_profit_pct") or settings.trailing_stop.activation_profit_pct, 0.5)
        profit_pct = _price_pnl_pct(direction, entry_price, mark_price, 1.0)
        if profit_pct < activation_pct:
            return False
        if direction == "short":
            new_stop = mark_price * (1 + trail_pct / 100.0)
        else:
            new_stop = mark_price * (1 - trail_pct / 100.0)

    elif trailing_mode == "profit_pct_trailing":
        activation_pct = safe_float(trailing_config.get("activation_profit_pct") or settings.trailing_stop.activation_profit_pct, 1.0)
        trail_pct = safe_float(trailing_config.get("trail_pct") or settings.trailing_stop.trail_pct, 0.5)
        profit_pct = _price_pnl_pct(direction, entry_price, mark_price, 1.0)
        if profit_pct < activation_pct:
            return False
        if direction == "short":
            new_stop = mark_price * (1 + trail_pct / 100.0)
        else:
            new_stop = mark_price * (1 - trail_pct / 100.0)

    if new_stop is None or new_stop <= 0:
        return False

    if current_stop > 0:
        if direction == "short" and new_stop >= current_stop:
            return False
        if direction != "short" and new_stop <= current_stop:
            return False

    result = await place_protective_stop(
        ticker=position.ticker,
        direction=position.direction,
        quantity=remaining_qty,
        stop_price=new_stop,
        exchange_config=exchange_config,
    )
    if result.get("status") in {"placed", "simulated"}:
        position.stop_loss = new_stop
        position.stop_loss_order_id = str(result.get("order_id") or position.stop_loss_order_id or "")
        position.updated_at = utcnow()
        logger.info(f"[PositionMonitor] Adjusted trailing stop for {position.ticker}: mode={trailing_mode}, new_stop={new_stop:.8f}")
        return True
    return False


async def _adjust_trailing_stop_on_tp_hit(
    position: PositionModel,
    tp_levels: list[dict],
    hit_levels: list[dict],
    exchange_config: dict,
    place_protective_stop
) -> bool:
    trailing_config = loads_dict(position.trailing_stop_config_json)
    trailing_mode = str(trailing_config.get("mode") or "none").lower()

    if trailing_mode not in {"breakeven_on_tp1", "step_trailing"}:
        return False

    if not hit_levels:
        return False

    direction = str(position.direction or "long").lower()
    entry_price = safe_float(position.entry_price)
    current_stop = safe_float(position.stop_loss)
    remaining_qty = _effective_remaining_quantity(position, safe_float(position.quantity))

    new_stop = None
    tp_note = ""

    if trailing_mode == "breakeven_on_tp1":
        tp1_hit = any(
            level.get("level") == 1 or (not level.get("level") and i == 0)
            for i, level in enumerate(hit_levels)
        )
        if tp1_hit:
            new_stop = entry_price
            tp_note = "TP1 hit — SL moved to breakeven"

    elif trailing_mode == "step_trailing":
        # BUG FIX: Sort TP levels by distance from entry, not raw price.
        # For LONG: ascending price (TP1 < TP2 < TP3)
        # For SHORT: descending price (TP1 > TP2 > TP3)
        reverse_sort = direction == "short"
        all_levels = sorted(tp_levels, key=lambda x: safe_float(x.get("price")), reverse=reverse_sort)
        hit_level_numbers = []
        for i, level in enumerate(all_levels):
            if str(level.get("status") or "pending").lower() in {"hit", "filled", "closed"}:
                hit_level_numbers.append(i + 1)

        if hit_level_numbers:
            highest_hit = max(hit_level_numbers)
            if highest_hit > 1:
                prev_level_idx = highest_hit - 2
                if prev_level_idx >= 0 and prev_level_idx < len(all_levels):
                    prev_tp_price = safe_float(all_levels[prev_level_idx].get("price"))
                    if prev_tp_price > 0:
                        # BUG FIX: For LONG, SL should be at least at the previous TP
                        # level (use max, not min). For SHORT, SL should be at most
                        # at the previous TP level (use min, not max).
                        if direction == "long":
                            new_stop = max(prev_tp_price, entry_price * 1.002)
                        else:
                            new_stop = min(prev_tp_price, entry_price * 0.998)
                        tp_note = f"TP{highest_hit} hit — SL moved to TP{highest_hit - 1} level"
            elif highest_hit == 1:
                new_stop = entry_price
                tp_note = "TP1 hit — SL moved to breakeven (step trailing)"

    if new_stop is None or new_stop <= 0:
        return False

    if current_stop > 0:
        if direction == "short" and new_stop >= current_stop:
            return False
        if direction != "short" and new_stop <= current_stop:
            return False

    result = await place_protective_stop(
        ticker=position.ticker,
        direction=position.direction,
        quantity=remaining_qty,
        stop_price=new_stop,
        exchange_config=exchange_config,
    )
    if result.get("status") in {"placed", "simulated"}:
        position.stop_loss = new_stop
        position.stop_loss_order_id = str(result.get("order_id") or position.stop_loss_order_id or "")
        position.updated_at = utcnow()
        logger.info(f"[PositionMonitor] {tp_note} for {position.ticker}: new_stop={new_stop:.8f}")
        return True
    return False


async def check_position_risk(position: dict, config: dict) -> dict:
    """Check basic risk metrics for a position dict."""
    entry_price = safe_float(position.get("entryPrice") or position.get("entry_price"))
    mark_price = safe_float(position.get("markPrice") or position.get("mark_price"))
    liquidation_price = safe_float(position.get("liquidationPrice") or position.get("liquidation_price"))
    leverage = safe_float(position.get("leverage"), 1.0)

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
