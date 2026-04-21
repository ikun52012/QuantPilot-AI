"""
Signal Server - Multi-Exchange Executor
Supports: Binance, OKX, Bybit, Bitget, Gate.io, Coinbase
Enhanced with multi-TP and trailing-stop execution
"""
import asyncio
import inspect
import ccxt
from loguru import logger
from config import settings
from models import TradeDecision, SignalDirection, TrailingStopMode


async def _close_exchange(exchange):
    close = getattr(exchange, "close", None)
    if not close:
        return
    result = await asyncio.to_thread(close)
    if inspect.isawaitable(result):
        await result


# ─────────────────────────────────────────────
# Supported exchanges
# ─────────────────────────────────────────────
SUPPORTED_EXCHANGES = {
    "binance": {
        "class": ccxt.binance,
        "futures_option": {"defaultType": "future"},
        "has_sandbox": True,
    },
    "okx": {
        "class": ccxt.okx,
        "futures_option": {"defaultType": "swap"},
        "has_sandbox": True,
        "extra_keys": ["password"],     # OKX requires passphrase
    },
    "bybit": {
        "class": ccxt.bybit,
        "futures_option": {"defaultType": "linear"},
        "has_sandbox": True,
    },
    "bitget": {
        "class": ccxt.bitget,
        "futures_option": {"defaultType": "swap"},
        "has_sandbox": True,
        "extra_keys": ["password"],     # Bitget requires passphrase
    },
    "gate": {
        "class": ccxt.gate,
        "futures_option": {"defaultType": "swap"},
        "has_sandbox": False,
    },
    "coinbase": {
        "class": ccxt.coinbase,
        "futures_option": {},
        "has_sandbox": True,
    },
}


def get_supported_exchanges() -> list[str]:
    """Return list of supported exchange IDs."""
    return list(SUPPORTED_EXCHANGES.keys())


def _build_exchange(
    exchange_id: str = None,
    api_key: str = None,
    api_secret: str = None,
    password: str = "",
    live: bool = False,
) -> ccxt.Exchange:
    """Build CCXT exchange instance with proper configuration."""
    if exchange_id is None:
        exchange_id = settings.exchange.name
    exchange_id = exchange_id.lower().strip()

    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange_id}")

    config = SUPPORTED_EXCHANGES[exchange_id]
    exchange_class = config["class"]

    # Build exchange config
    exchange_config = {
        "apiKey": api_key if api_key else settings.exchange.api_key,
        "secret": api_secret if api_secret else settings.exchange.api_secret,
        "enableRateLimit": True,
        "options": config.get("futures_option", {}),
    }

    # Add password for exchanges that require it
    if password or "password" in (config.get("extra_keys") or []):
        exchange_config["password"] = password if password else settings.exchange.password

    # Create exchange instance
    exchange = exchange_class(exchange_config)

    # Use sandbox mode if not live trading
    if config.get("has_sandbox", False) and not (live or settings.exchange.live_trading):
        try:
            exchange.set_sandbox_mode(True)
        except Exception as e:
            logger.debug(f"[Exchange] Sandbox mode unavailable for {exchange_id}: {e}")

    # Set default market type
    if "defaultType" in exchange_config["options"]:
        exchange.options["defaultType"] = exchange_config["options"]["defaultType"]

    return exchange


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol to exchange format."""
    # Remove any spaces, slashes, or weird characters
    symbol = symbol.upper().replace(" ", "").replace("/", "").replace("-", "")
    # Add USDT suffix if missing and not already a pair
    if not symbol.endswith(("USDT", "USD", "BTC", "ETH", "BNB")):
        symbol = f"{symbol}USDT"
    return symbol


def _symbol_candidates(symbol: str) -> list[str]:
    """Return common CCXT symbol candidates for a TradingView-style ticker."""
    cleaned = symbol.upper().replace(" ", "").replace("-", "").replace("_", "").replace("/", "")
    quotes = ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"]
    candidates = [symbol.upper(), cleaned]
    for quote in quotes:
        if cleaned.endswith(quote) and len(cleaned) > len(quote):
            base = cleaned[:-len(quote)]
            candidates.extend([
                f"{base}/{quote}",
                f"{base}/{quote}:{quote}",
                f"{base}{quote}",
            ])
            break
    else:
        candidates.extend([f"{cleaned}/USDT", f"{cleaned}/USDT:USDT", f"{cleaned}USDT"])

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def _resolve_symbol(exchange: ccxt.Exchange, symbol: str) -> str:
    """Resolve a TradingView ticker into an exchange market symbol."""
    candidates = _symbol_candidates(symbol)
    try:
        markets = exchange.load_markets()
    except Exception as e:
        logger.debug(f"[Exchange] Could not load markets for symbol resolution: {e}")
        return candidates[0]

    for candidate in candidates:
        if candidate in markets:
            return candidate

    cleaned = symbol.upper().replace(" ", "").replace("-", "").replace("_", "").replace("/", "")
    for market_symbol, market in markets.items():
        market_id = str(market.get("id", "")).upper().replace("-", "").replace("_", "").replace("/", "")
        compact_symbol = market_symbol.upper().replace("/", "").replace(":", "").replace("-", "").replace("_", "")
        if cleaned in {market_id, compact_symbol}:
            return market_symbol

    logger.warning(f"[Exchange] Symbol {symbol} not found in loaded markets; using {candidates[0]}")
    return candidates[0]


async def execute_trade(decision: TradeDecision, exchange_config: dict | None = None) -> dict:
    """
    Execute a trade on the configured exchange.
    Enhanced with multi-TP and trailing-stop support.
    Returns dict with order details or error info.
    """
    if not decision.execute:
        return {"status": "skipped", "reason": decision.reason}

    exchange_config = exchange_config or {}
    live_trading = bool(exchange_config.get("live_trading", settings.exchange.live_trading))

    if not live_trading:
        logger.warning("[Exchange] 🔶 PAPER TRADING MODE - not sending real orders")
        return _simulate_order(decision)

    exchange = _build_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=exchange_config.get("api_key") or settings.exchange.api_key,
        api_secret=exchange_config.get("api_secret") or settings.exchange.api_secret,
        password=exchange_config.get("password") or settings.exchange.password,
        live=live_trading,
    )
    symbol = await asyncio.to_thread(_resolve_symbol, exchange, decision.ticker)

    try:
        leverage = None
        if decision.ai_analysis and decision.ai_analysis.recommended_leverage:
            max_leverage = max(1, min(int(exchange_config.get("max_leverage") or 125), 125))
            leverage = max(1, min(int(round(decision.ai_analysis.recommended_leverage)), max_leverage))
            try:
                await asyncio.to_thread(exchange.set_leverage, leverage, symbol)
                logger.info(f"[Exchange] Leverage set: {symbol} {leverage}x")
            except Exception as e:
                logger.warning(f"[Exchange] Could not set leverage for {symbol}: {e}")

        if decision.direction in [SignalDirection.LONG]:
            side = "buy"
        elif decision.direction in [SignalDirection.SHORT]:
            side = "sell"
        elif decision.direction == SignalDirection.CLOSE_LONG:
            return await _close_position(exchange, symbol, "sell")
        elif decision.direction == SignalDirection.CLOSE_SHORT:
            return await _close_position(exchange, symbol, "buy")
        else:
            return {"status": "error", "reason": f"Unknown direction: {decision.direction}"}

        if decision.quantity is None or decision.quantity <= 0:
            return {"status": "error", "reason": "Quantity must be greater than zero"}

        logger.info(f"[Exchange] Placing {side} order: {symbol} qty={decision.quantity}")
        order = await asyncio.to_thread(
            exchange.create_order,
            symbol=symbol,
            type="market",
            side=side,
            amount=decision.quantity,
        )
        order_id = order.get("id", "unknown")
        logger.info(f"[Exchange] ✅ Entry order placed: {order_id}")

        result = {
            "status": "filled",
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": decision.quantity,
            "entry_price": order.get("average", decision.entry_price),
        }
        if leverage:
            result["recommended_leverage"] = leverage

        # ── Multi Take-Profit Orders ──
        if decision.take_profit_levels:
            tp_orders = await _place_multi_tp_orders(
                exchange, symbol, side, decision.quantity, decision.take_profit_levels
            )
            result["take_profit_orders"] = tp_orders
        elif decision.take_profit:
            # Fallback: single TP order
            try:
                tp_side = "sell" if side == "buy" else "buy"
                tp_order = await _create_conditional_order(
                    exchange, symbol, "take_profit", tp_side, decision.quantity, decision.take_profit
                )
                result["take_profit_order_id"] = tp_order.get("id")
                logger.info(f"[Exchange] ✅ Take-profit set at {decision.take_profit}")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set take-profit: {e}")
                result["take_profit_error"] = str(e)

        # ── Stop-Loss / Trailing Stop ──
        trailing_mode = decision.trailing_stop.mode if decision.trailing_stop else TrailingStopMode.NONE

        if trailing_mode == TrailingStopMode.MOVING:
            # Place a trailing stop order
            try:
                sl_side = "sell" if side == "buy" else "buy"
                trail_pct = decision.trailing_stop.trail_pct
                callback_rate = trail_pct  # Binance uses callbackRate
                ts_order = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=symbol, type="trailing_stop_market", side=sl_side,
                    amount=decision.quantity,
                    params={
                        "callbackRate": callback_rate,
                        "closePosition": False,
                    },
                )
                result["trailing_stop_order_id"] = ts_order.get("id")
                result["trailing_stop_mode"] = "moving"
                result["trailing_pct"] = trail_pct
                logger.info(f"[Exchange] ✅ Moving trailing stop set: {trail_pct}%")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set trailing stop: {e}")
                result["trailing_stop_error"] = str(e)
                # Fallback to regular stop-loss
                if decision.stop_loss:
                    await _place_stop_loss(exchange, symbol, side, decision.quantity, decision.stop_loss, result)

        elif trailing_mode in (TrailingStopMode.BREAKEVEN_ON_TP1,
                                TrailingStopMode.STEP_TRAILING,
                                TrailingStopMode.PROFIT_PCT_TRAILING):
            # These modes require active monitoring; place initial SL now
            if decision.stop_loss:
                await _place_stop_loss(exchange, symbol, side, decision.quantity, decision.stop_loss, result)
            result["trailing_stop_mode"] = trailing_mode.value
            result["trailing_pct"] = decision.trailing_stop.trail_pct if decision.trailing_stop else 0
            result["trailing_activation_profit_pct"] = decision.trailing_stop.activation_profit_pct if decision.trailing_stop else 0
            result["trailing_stop_note"] = (
                "Initial SL placed. Trailing adjustments handled by position monitor."
            )
            logger.info(f"[Exchange] ⚡ Trailing mode '{trailing_mode.value}' active — initial SL placed")
        else:
            # No trailing: standard stop-loss
            if decision.stop_loss:
                await _place_stop_loss(exchange, symbol, side, decision.quantity, decision.stop_loss, result)

        return result

    except ccxt.InsufficientFunds as e:
        logger.error(f"[Exchange] Insufficient funds: {e}")
        return {"status": "error", "reason": "Insufficient funds"}
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Network error: {e}")
        return {"status": "error", "reason": "Network error"}
    except Exception as e:
        logger.error(f"[Exchange] Order failed: {e}")
        return {"status": "error", "reason": "Order execution failed"}
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def _place_stop_loss(exchange, symbol, side, quantity, stop_price, result):
    """Place a standard stop-loss order."""
    try:
        sl_side = "sell" if side == "buy" else "buy"
        sl_order = await _create_conditional_order(exchange, symbol, "stop_loss", sl_side, quantity, stop_price)
        result["stop_loss_order_id"] = sl_order.get("id")
        logger.info(f"[Exchange] ✅ Stop-loss set at {stop_price}")
    except Exception as e:
        logger.error(f"[Exchange] Failed to set stop-loss: {e}")
        result["stop_loss_error"] = "Failed to set stop-loss order"


async def _place_multi_tp_orders(exchange, symbol, side, total_qty, tp_levels):
    """Place multiple take-profit orders at different price levels."""
    tp_side = "sell" if side == "buy" else "buy"
    tp_results = []

    for i, tp in enumerate(tp_levels):
        tp_qty = total_qty * (tp.qty_pct / 100.0)
        if tp_qty <= 0:
            continue
        try:
            tp_order = await _create_conditional_order(
                exchange, symbol, "take_profit", tp_side, round(tp_qty, 6), tp.price
            )
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": tp.qty_pct,
                "order_id": tp_order.get("id"),
                "status": "placed",
            })
            logger.info(f"[Exchange] ✅ TP{i+1} set at {tp.price} ({tp.qty_pct}% = {tp_qty})")
        except Exception as e:
            logger.error(f"[Exchange] Failed to set TP{i+1}: {e}")
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": tp.qty_pct,
                "error": "Failed to place take-profit order",
                "status": "failed",
            })

    return tp_results


def _conditional_order_attempts(exchange_id: str, kind: str, trigger_price: float) -> list[tuple[str, dict]]:
    """Return exchange-aware conditional-order candidates."""
    reduce_params = {"reduceOnly": True, "closePosition": False}
    if kind == "take_profit":
        candidates = [
            ("take_profit_market", {**reduce_params, "stopPrice": trigger_price}),
            ("take_profit", {**reduce_params, "stopPrice": trigger_price}),
            ("market", {**reduce_params, "triggerPrice": trigger_price, "takeProfitPrice": trigger_price}),
        ]
    else:
        candidates = [
            ("stop_market", {**reduce_params, "stopPrice": trigger_price}),
            ("stop", {**reduce_params, "stopPrice": trigger_price}),
            ("market", {**reduce_params, "triggerPrice": trigger_price, "stopLossPrice": trigger_price}),
        ]
    if exchange_id == "okx":
        key = "tpTriggerPx" if kind == "take_profit" else "slTriggerPx"
        order_key = "tpOrdPx" if kind == "take_profit" else "slOrdPx"
        candidates.insert(0, ("market", {**reduce_params, key: trigger_price, order_key: "-1", "tdMode": "cross"}))
    if exchange_id == "bitget":
        candidates.insert(0, ("market", {**reduce_params, "triggerPrice": trigger_price, "planType": "profit_plan" if kind == "take_profit" else "loss_plan"}))
    if exchange_id == "bybit":
        candidates.insert(0, ("market", {**reduce_params, "triggerPrice": trigger_price, "triggerDirection": 1 if kind == "take_profit" else 2}))
    return candidates


async def _create_conditional_order(exchange, symbol: str, kind: str, side: str, amount: float, trigger_price: float) -> dict:
    """Try exchange-specific conditional order formats before failing."""
    exchange_id = getattr(exchange, "id", "").lower()
    errors = []
    for order_type, params in _conditional_order_attempts(exchange_id, kind, trigger_price):
        try:
            return await asyncio.to_thread(
                exchange.create_order,
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                params=params,
            )
        except Exception as exc:
            errors.append(f"{order_type}: {exc}")
            logger.debug(f"[Exchange] {exchange_id} {kind} candidate failed: {order_type} {exc}")
    raise RuntimeError("; ".join(errors[-3:]) or f"Failed to create {kind} order")


async def place_protective_stop(
    ticker: str,
    direction: str,
    quantity: float,
    stop_price: float,
    exchange_config: dict | None = None,
) -> dict:
    """Place a reduce-only protective stop for an already-open monitored position."""
    exchange_config = exchange_config or {}
    if not exchange_config.get("live_trading", settings.exchange.live_trading):
        return {"status": "simulated", "stop_price": stop_price}
    exchange = _build_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=exchange_config.get("api_key") or settings.exchange.api_key,
        api_secret=exchange_config.get("api_secret") or settings.exchange.api_secret,
        password=exchange_config.get("password") or settings.exchange.password,
        live=True,
    )
    try:
        symbol = await asyncio.to_thread(_resolve_symbol, exchange, ticker)
        side = "sell" if str(direction).lower() == SignalDirection.LONG.value else "buy"
        order = await _create_conditional_order(exchange, symbol, "stop_loss", side, quantity, stop_price)
        return {"status": "placed", "order_id": order.get("id"), "symbol": symbol, "stop_price": stop_price}
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass


async def _close_position(exchange: ccxt.Exchange, symbol: str, side: str) -> dict:
    """Close an existing position."""
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions, [symbol])
        for pos in positions:
            if pos["symbol"] == symbol and float(pos.get("contracts", 0)) > 0:
                amount = float(pos["contracts"])
                order = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=symbol, type="market", side=side, amount=amount,
                    params={"reduceOnly": True},
                )
                logger.info(f"[Exchange] ✅ Position closed: {order.get('id')}")
                return {"status": "closed", "order_id": order.get("id")}
        return {"status": "no_position", "reason": "No open position to close"}
    except Exception as e:
        logger.error(f"[Exchange] Failed to close position: {e}")
        return {"status": "error", "reason": "Failed to close position"}


def _simulate_order(decision: TradeDecision) -> dict:
    """Simulate order execution for paper trading."""
    tp_info = []
    for i, tp in enumerate(decision.take_profit_levels):
        tp_info.append({
            "level": i + 1,
            "price": tp.price,
            "qty_pct": tp.qty_pct,
            "status": "simulated",
        })

    trailing_mode = decision.trailing_stop.mode if decision.trailing_stop else "none"

    logger.info(
        f"[Exchange] 📝 SIMULATED: {decision.direction} {decision.ticker} "
        f"qty={decision.quantity} entry={decision.entry_price} "
        f"SL={decision.stop_loss} TPs={len(decision.take_profit_levels)} "
        f"trailing={trailing_mode}"
    )
    return {
        "status": "simulated",
        "symbol": decision.ticker,
        "direction": decision.direction.value if decision.direction else "unknown",
        "quantity": decision.quantity,
        "entry_price": decision.entry_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
        "take_profit_orders": tp_info,
        "trailing_stop_mode": trailing_mode if isinstance(trailing_mode, str) else trailing_mode.value,
        "trailing_pct": decision.trailing_stop.trail_pct if decision.trailing_stop else 0,
    }


async def get_account_balance() -> dict:
    """Fetch account balance from exchange."""
    exchange = _build_exchange()
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        quote = "USDT" if "USDT" in balance.get("total", {}) else "USD"
        # Extract relevant balance info
        result = {
            "total": balance.get("total", {}),
            "free": balance.get("free", {}),
            "used": balance.get("used", {}),
            "quote": quote,
            "total_quote": balance.get("total", {}).get(quote, 0.0) or 0.0,
            "free_quote": balance.get("free", {}).get(quote, 0.0) or 0.0,
            "used_quote": balance.get("used", {}).get(quote, 0.0) or 0.0,
            "timestamp": balance.get("timestamp"),
            "datetime": balance.get("datetime"),
        }
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch balance: {e}")
        return {}
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def get_balance() -> dict:
    """Fetch account balance from exchange."""
    exchange = _build_exchange()
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        # Extract relevant balance info
        result = {
            "total": balance.get("total", {}),
            "free": balance.get("free", {}),
            "used": balance.get("used", {}),
            "timestamp": balance.get("timestamp"),
            "datetime": balance.get("datetime"),
        }
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch balance: {e}")
        return {}
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def get_ticker(symbol: str, exchange_config: dict | None = None) -> dict:
    """Fetch ticker data for a symbol."""
    exchange_config = exchange_config or {}
    exchange = _build_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=exchange_config.get("api_key") or settings.exchange.api_key,
        api_secret=exchange_config.get("api_secret") or settings.exchange.api_secret,
        password=exchange_config.get("password") or settings.exchange.password,
        live=bool(exchange_config.get("live_trading", settings.exchange.live_trading)),
    )
    try:
        resolved_symbol = await asyncio.to_thread(_resolve_symbol, exchange, symbol)
        ticker = await asyncio.to_thread(exchange.fetch_ticker, resolved_symbol)
        return {
            "symbol": ticker.get("symbol"),
            "last": ticker.get("last"),
            "bid": ticker.get("bid"),
            "ask": ticker.get("ask"),
            "high": ticker.get("high"),
            "low": ticker.get("low"),
            "volume": ticker.get("volume"),
            "timestamp": ticker.get("timestamp"),
            "datetime": ticker.get("datetime"),
        }
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch ticker for {symbol}: {e}")
        return {}
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def get_open_positions() -> list[dict]:
    """Fetch open positions from exchange."""
    exchange = _build_exchange()
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        result = []
        for pos in positions:
            try:
                contracts = float(pos.get('contracts') or 0)
            except (TypeError, ValueError):
                contracts = 0.0
            if contracts != 0:
                unrealized_pnl = pos.get('unrealizedPnl')
                notional = pos.get('notional')
                percentage = pos.get('percentage')
                if percentage is None and unrealized_pnl is not None and notional:
                    try:
                        percentage = (float(unrealized_pnl) / abs(float(notional))) * 100
                    except (TypeError, ValueError, ZeroDivisionError):
                        percentage = None
                result.append({
                    "symbol": pos.get('symbol'),
                    "side": pos.get('side'),
                    "contracts": contracts,
                    "entryPrice": pos.get('entryPrice'),
                    "entry_price": pos.get('entryPrice'),
                    "markPrice": pos.get('markPrice'),
                    "mark_price": pos.get('markPrice'),
                    "notional": pos.get('notional'),
                    "unrealizedPnl": pos.get('unrealizedPnl'),
                    "unrealized_pnl": unrealized_pnl,
                    "liquidationPrice": pos.get('liquidationPrice'),
                    "liquidation_price": pos.get('liquidationPrice'),
                    "percentage": percentage,
                    "leverage": pos.get('leverage'),
                    "marginMode": pos.get('marginMode'),
                    "margin_mode": pos.get('marginMode'),
                })
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch positions: {e}")
        return []
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def get_recent_orders(symbol: str = None, limit: int = 50) -> list[dict]:
    """Fetch recent closed orders from exchange."""
    exchange = _build_exchange()
    try:
        if symbol:
            resolved_symbol = await asyncio.to_thread(_resolve_symbol, exchange, symbol)
            orders = await asyncio.to_thread(exchange.fetch_closed_orders, resolved_symbol, None, limit)
        else:
            orders = await asyncio.to_thread(exchange.fetch_closed_orders, None, None, limit)

        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "price": o.get("price"),
                "amount": o.get("amount"),
                "cost": o.get("cost"),
                "filled": o.get("filled"),
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
                "datetime": o.get("datetime"),
            }
            for o in orders
        ]
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch orders: {e}")
        return []
    finally:
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  


async def test_exchange_connection(
    exchange_id: str,
    api_key: str,
    api_secret: str,
    password: str = "",
) -> dict:
    """Test if exchange API keys are valid."""
    try:
        exchange = _build_exchange(
            exchange_id=exchange_id,
            api_key=api_key,
            api_secret=api_secret,
            password=password,
            live=True,
        )
        balance = await asyncio.to_thread(exchange.fetch_balance)
        try:
            await _close_exchange(exchange)
        except AttributeError:
            pass  
        return {"success": True, "message": f"Connected to {exchange_id} successfully"}
    except ccxt.AuthenticationError as e:
        return {"success": False, "message": f"Authentication failed: {e}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {e}"}
