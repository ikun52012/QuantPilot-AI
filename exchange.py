"""
OpenClaw Signal Server - Exchange Executor
Executes trades on the exchange via ccxt.
"""
import ccxt
from loguru import logger
from config import settings
from models import TradeDecision, SignalDirection


def _get_exchange() -> ccxt.Exchange:
    """Create a ccxt exchange instance."""
    exchange_id = settings.exchange.name.lower()
    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unsupported exchange: {exchange_id}")

    exchange = exchange_class({
        "apiKey": settings.exchange.api_key,
        "secret": settings.exchange.api_secret,
        "options": {"defaultType": "future"},
        "enableRateLimit": True,
    })

    if not settings.exchange.live_trading:
        exchange.set_sandbox_mode(True)

    return exchange


def _normalize_symbol(ticker: str) -> str:
    """Convert ticker to ccxt symbol format."""
    ticker = ticker.upper().replace(" ", "")
    for quote in ["USDT", "BUSD", "USDC", "USD"]:
        if ticker.endswith(quote):
            base = ticker[: -len(quote)]
            return f"{base}/{quote}"
    return ticker


async def execute_trade(decision: TradeDecision) -> dict:
    """
    Execute a trade on the exchange based on the AI-optimized decision.

    Returns dict with order details or error info.
    """
    if not decision.execute:
        return {"status": "skipped", "reason": decision.reason}

    if not settings.exchange.live_trading:
        logger.warning("[Exchange] 🔶 PAPER TRADING MODE - not sending real orders")
        return _simulate_order(decision)

    exchange = _get_exchange()
    symbol = _normalize_symbol(decision.ticker)

    try:
        # Determine side
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

        # Place market entry order
        logger.info(f"[Exchange] Placing {side} order: {symbol} qty={decision.quantity}")
        order = exchange.create_order(
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

        # Place stop-loss order
        if decision.stop_loss:
            try:
                sl_side = "sell" if side == "buy" else "buy"
                sl_order = exchange.create_order(
                    symbol=symbol,
                    type="stop_market",
                    side=sl_side,
                    amount=decision.quantity,
                    params={"stopPrice": decision.stop_loss, "closePosition": False},
                )
                result["stop_loss_order_id"] = sl_order.get("id")
                logger.info(f"[Exchange] ✅ Stop-loss set at {decision.stop_loss}")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set stop-loss: {e}")
                result["stop_loss_error"] = str(e)

        # Place take-profit order
        if decision.take_profit:
            try:
                tp_side = "sell" if side == "buy" else "buy"
                tp_order = exchange.create_order(
                    symbol=symbol,
                    type="take_profit_market",
                    side=tp_side,
                    amount=decision.quantity,
                    params={"stopPrice": decision.take_profit, "closePosition": False},
                )
                result["take_profit_order_id"] = tp_order.get("id")
                logger.info(f"[Exchange] ✅ Take-profit set at {decision.take_profit}")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set take-profit: {e}")
                result["take_profit_error"] = str(e)

        return result

    except ccxt.InsufficientFunds as e:
        logger.error(f"[Exchange] Insufficient funds: {e}")
        return {"status": "error", "reason": f"Insufficient funds: {e}"}
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Network error: {e}")
        return {"status": "error", "reason": f"Network error: {e}"}
    except Exception as e:
        logger.error(f"[Exchange] Order failed: {e}")
        return {"status": "error", "reason": str(e)}
    finally:
        exchange.close()


async def _close_position(exchange: ccxt.Exchange, symbol: str, side: str) -> dict:
    """Close an existing position."""
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if pos["symbol"] == symbol and float(pos.get("contracts", 0)) > 0:
                amount = float(pos["contracts"])
                order = exchange.create_order(
                    symbol=symbol, type="market", side=side, amount=amount,
                    params={"reduceOnly": True},
                )
                logger.info(f"[Exchange] ✅ Position closed: {order.get('id')}")
                return {"status": "closed", "order_id": order.get("id")}

        return {"status": "no_position", "reason": "No open position to close"}
    except Exception as e:
        logger.error(f"[Exchange] Failed to close position: {e}")
        return {"status": "error", "reason": str(e)}


def _simulate_order(decision: TradeDecision) -> dict:
    """Simulate order execution for paper trading."""
    logger.info(
        f"[Exchange] 📝 SIMULATED: {decision.direction} {decision.ticker} "
        f"qty={decision.quantity} entry={decision.entry_price} "
        f"SL={decision.stop_loss} TP={decision.take_profit}"
    )
    return {
        "status": "simulated",
        "symbol": decision.ticker,
        "direction": decision.direction.value if decision.direction else "unknown",
        "quantity": decision.quantity,
        "entry_price": decision.entry_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
    }


async def get_account_balance() -> dict:
    """Fetch account balance from exchange."""
    exchange = _get_exchange()
    try:
        balance = exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return {
            "total": usdt.get("total", 0),
            "free": usdt.get("free", 0),
            "used": usdt.get("used", 0),
        }
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch balance: {e}")
        return {"total": 0, "free": 0, "used": 0, "error": str(e)}
    finally:
        exchange.close()
