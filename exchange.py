"""
Signal Server - Multi-Exchange Executor
Supports: Binance, OKX, Bybit, Bitget, Gate.io, Coinbase
"""
import ccxt
from loguru import logger
from config import settings
from models import TradeDecision, SignalDirection


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
    """Return list of supported exchange names."""
    return list(SUPPORTED_EXCHANGES.keys())


def _build_exchange(
    exchange_id: str = None,
    api_key: str = None,
    api_secret: str = None,
    password: str = None,
    live: bool = None,
) -> ccxt.Exchange:
    """
    Create a ccxt exchange instance.
    Uses settings defaults if parameters not provided.
    """
    exchange_id = (exchange_id or settings.exchange.name).lower()
    api_key = api_key or settings.exchange.api_key
    api_secret = api_secret or settings.exchange.api_secret
    password = password or settings.exchange.password
    live = live if live is not None else settings.exchange.live_trading

    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Unsupported exchange: {exchange_id}. "
            f"Supported: {', '.join(SUPPORTED_EXCHANGES.keys())}"
        )

    ex_config = SUPPORTED_EXCHANGES[exchange_id]
    params = {
        "apiKey": api_key,
        "secret": api_secret,
        "options": ex_config["futures_option"].copy(),
        "enableRateLimit": True,
    }

    # Some exchanges need a passphrase/password
    if password and "password" in ex_config.get("extra_keys", []):
        params["password"] = password

    exchange = ex_config["class"](params)

    # Sandbox mode
    if not live and ex_config.get("has_sandbox", False):
        try:
            exchange.set_sandbox_mode(True)
            logger.info(f"[Exchange] {exchange_id} sandbox mode enabled")
        except Exception:
            logger.warning(f"[Exchange] {exchange_id} sandbox mode not available")

    return exchange


def _normalize_symbol(ticker: str) -> str:
    """Convert ticker to ccxt symbol format."""
    ticker = ticker.upper().replace(" ", "")
    for quote in ["USDT", "BUSD", "USDC", "USD"]:
        if ticker.endswith(quote):
            base = ticker[: -len(quote)]
            return f"{base}/{quote}"
    return ticker


# ─────────────────────────────────────────────
# Trade Execution
# ─────────────────────────────────────────────

async def execute_trade(decision: TradeDecision) -> dict:
    """
    Execute a trade on the configured exchange.
    Returns dict with order details or error info.
    """
    if not decision.execute:
        return {"status": "skipped", "reason": decision.reason}

    if not settings.exchange.live_trading:
        logger.warning("[Exchange] 🔶 PAPER TRADING MODE - not sending real orders")
        return _simulate_order(decision)

    exchange = _build_exchange()
    symbol = _normalize_symbol(decision.ticker)

    try:
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

        # Place stop-loss
        if decision.stop_loss:
            try:
                sl_side = "sell" if side == "buy" else "buy"
                sl_order = exchange.create_order(
                    symbol=symbol, type="stop_market", side=sl_side,
                    amount=decision.quantity,
                    params={"stopPrice": decision.stop_loss, "closePosition": False},
                )
                result["stop_loss_order_id"] = sl_order.get("id")
                logger.info(f"[Exchange] ✅ Stop-loss set at {decision.stop_loss}")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set stop-loss: {e}")
                result["stop_loss_error"] = str(e)

        # Place take-profit
        if decision.take_profit:
            try:
                tp_side = "sell" if side == "buy" else "buy"
                tp_order = exchange.create_order(
                    symbol=symbol, type="take_profit_market", side=tp_side,
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


# ─────────────────────────────────────────────
# Account & Position Queries
# ─────────────────────────────────────────────

async def get_account_balance() -> dict:
    """Fetch account balance from exchange."""
    exchange = _build_exchange()
    try:
        balance = exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return {
            "exchange": settings.exchange.name,
            "total": usdt.get("total", 0),
            "free": usdt.get("free", 0),
            "used": usdt.get("used", 0),
            "currencies": {
                k: {"total": v.get("total", 0), "free": v.get("free", 0)}
                for k, v in balance.items()
                if isinstance(v, dict) and v.get("total") and float(v["total"]) > 0
            },
        }
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch balance: {e}")
        return {"total": 0, "free": 0, "used": 0, "error": str(e)}
    finally:
        exchange.close()


async def get_open_positions() -> list[dict]:
    """Fetch all open positions from exchange."""
    exchange = _build_exchange()
    try:
        positions = exchange.fetch_positions()
        result = []
        for pos in positions:
            contracts = float(pos.get("contracts", 0))
            if contracts == 0:
                continue
            result.append({
                "symbol": pos.get("symbol", ""),
                "side": pos.get("side", ""),
                "contracts": contracts,
                "entry_price": float(pos.get("entryPrice", 0)),
                "mark_price": float(pos.get("markPrice", 0)),
                "liquidation_price": float(pos.get("liquidationPrice", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                "leverage": float(pos.get("leverage", 1)),
                "margin_type": pos.get("marginMode", "cross"),
                "notional": float(pos.get("notional", 0) or 0),
                "percentage": float(pos.get("percentage", 0) or 0),
            })
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch positions: {e}")
        return []
    finally:
        exchange.close()


async def get_recent_orders(symbol: str = None, limit: int = 50) -> list[dict]:
    """Fetch recent closed orders from exchange."""
    exchange = _build_exchange()
    try:
        if symbol:
            orders = exchange.fetch_closed_orders(_normalize_symbol(symbol), limit=limit)
        else:
            orders = exchange.fetch_closed_orders(limit=limit)

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
        exchange.close()


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
        balance = exchange.fetch_balance()
        exchange.close()
        return {"success": True, "message": f"Connected to {exchange_id} successfully"}
    except ccxt.AuthenticationError as e:
        return {"success": False, "message": f"Authentication failed: {e}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {e}"}
